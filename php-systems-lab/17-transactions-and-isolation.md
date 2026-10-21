# Глава 17. Транзакции и изоляция

> Проект:
> [`php-mini-database`](https://github.com/Researcher86/php-mini-database)
> (релиз `v1.1.0`, коммит `9adde5d`). Нужно знать: страницы, кучу и адрес строки
> `RecordId` (→ глава 15); порядок «сначала мутация, потом запись в журнал» и
> ленивый конвейер операторов (→ глава 16). Пригодятся дальше: WAL, fsync и
> восстановление после сбоя (→ глава 18) и `kill -9` как проверка (→ глава 26).

Глава 16 закончилась на том, что каждое одиночное утверждение обёрнуто в
`withTransaction()`: `INSERT` без `BEGIN` — это уже транзакция, просто никем не
названная. Теперь пора назвать её явно — и выяснить, что означает «изоляция» в
движке, у которого нет ни версий строк, ни снимков.

## 1. Проблема

Автокоммит — хорошая точка отсчёта и плохой ответ на всё сразу: как только одну
операцию приложения начинают описывать несколько утверждений, появляются четыре
хрупкости.

**Хрупкость первая: между двумя утверждениями нет границы.** «Спиши 100 со счёта
A и зачисли на счёт B» — это два `UPDATE`. Если процесс умирает между ними,
деньги исчезли; если выполняется ещё раз целиком — удвоились. Ни то ни другое не
видно снаружи как ошибка: обе строки по отдельности абсолютно корректны. Границу
— «всё или ничего» — должен уметь назвать сам пользователь: это и есть
`BEGIN`/`COMMIT`.

**Хрупкость вторая: два читателя перезаписывают друг друга.** Пусть оба
прочитали `balance = 100` и оба записали `110` («плюс десять»). Одно из двух
начислений потеряно — не потому, что кто-то ошибся, а потому, что никто не
запретил второму писателю прикоснуться к уже читанной строке. Классическая
потерянная запись (lost update) лечится блокировкой, взятой на запись и
удерживаемой до конца транзакции, — на любом уровне изоляции.

**Хрупкость третья: чтение видит то, что ещё не подтверждено.** Без изоляции
между читателем и писателем нет ничего: читатель увидит незакоммиченную строку,
а после отката она исчезнет. Хуже того, повторное чтение может вернуть другой
набор строк, и запрос, который «должен» был увидеть одно и то же, ответит
по-разному на двух проходах.

**Хрупкость четвёртая: ожидание в однопоточном движке блокирует сам себя.** Если
на конфликт блокировок отвечать «подожди, пока другой отпустит», то ждать будет
единственный поток, который мог бы её отпустить. Ожидание здесь — это не
терпение, а вечная блокировка с заранее известным диагнозом.

И поверх всего — вопрос принадлежности: если открытая транзакция одна на всю
базу, кто вообще имеет право её закрыть? До отдельного исправления ответ был
«кто угодно», и в документации проекта это описано как найденный ревью баг:
соединение, ни разу не писавшее `BEGIN`, успешно делало `COMMIT` чужой
транзакции, а его собственный `INSERT` молча исполнялся внутри неё.

## 2. Устройство

### Кто чем владеет

```text
Session / client
     │ SQL
     ▼
Executor (owner = $this)
     │ BEGIN / COMMIT / ROLLBACK / SAVEPOINT        │ bare write statement
     ▼                                              ▼
TransactionManager { current, owner }         withTransaction()
     │                       │                  (autocommit wrapper)
     │ WAL records           │ locks
     ▼                       ▼
   Wal (журнал)         LockManager
                        grants: "table:t" => (txId => mode)
                                "row:t:0:0" => (txId => mode)
```

Транзакция и таблица блокировок принадлежат не `Executor`, а `Schema\Database`:
два `Executor` над одной базой видят одну и ту же открытую транзакцию — как два
утверждения на одном соединении с обычной СУБД. Владелец — непрозрачный токен,
который `Executor` передаёт как `$this`; сравнивается только по идентичности и
никогда не разбирается. Это не второй слот для транзакции: движок по-прежнему
строго однописательный, второй `BEGIN` отвергается кем угодно, пока один открыт.
Добавляется ровно одно правило: закрыть, откатить или управлять точками
сохранения может только тот, кто открыл.

### Блокировка: два режима, один поток, никакого ожидания

`LockManager` держит в памяти таблицу «ресурс → (id транзакции → режим)». Ресурс
— это строка `table:<имя>` или `row:<таблица>:<page:slot>`: блокировка таблицы и
блокировка строки в ней — независимые записи. Режимов два: `SHARED` сосуществует
с любым числом других `SHARED`, `EXCLUSIVE` не терпит никого, включая другого
общего читателя.

Главное решение — `acquire()` либо выдаёт блокировку немедленно, либо немедленно
бросает `TransactionException`. Ни очереди, ни таймаута, ни детектора
взаимоблокировок: `docs/DECISIONS.md` объясняет это прямо — «making the current
call block would freeze the only thread that could ever release the lock it is
waiting on». Конфликт здесь разрешается отказом, и решение о повторной попытке
остаётся вызывающему. Это принципиально отличается от `Infrastructure\FileLock`,
который именно ждёт с таймаутом: там владельца нельзя попросить, потому что он —
другой процесс.

`releaseAll()` — фаза сжатия двухфазной блокировки: всё, что транзакция держала,
освобождается разом, когда она заканчивается.

### Изоляция — это расписание блокировок, а не снимок

В движке нет MVCC: ни версий строк, ни буфера страниц, хранящего несколько живых
версий, ни снимка на транзакцию. Поэтому три уровня `IsolationLevel` отличаются
только тем, какие блокировки берёт `SELECT` и как долго их держит:

| Уровень                         | Блокировки чтения                                                                                                                                                                              |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `READ COMMITTED` (по умолчанию) | Никаких: утверждение читает то, что лежит в куче прямо сейчас.                                                                                                                                 |
| `REPEATABLE READ`               | Каждая прочитанная строка берётся под `SHARED` до конца транзакции — повторное чтение той же строки даёт тот же ответ.                                                                         |
| `SERIALIZABLE`                  | То же плюс: **полный** скан таблицы (а не поиск по индексу) берёт общую блокировку таблицы, чтобы другая транзакция не вставила туда новую строку, — грубая, но корректная защита от фантомов. |

`DECISIONS.md` называет то, что проект сознательно отдаёт вместе с MVCC: «MVCC
is what most production databases actually use, and it has a real advantage this
project is giving up: a reader never blocks a writer, or vice versa». Здесь
читатель и писатель под `REPEATABLE READ`/ `SERIALIZABLE` могут блокировать друг
друга — зато не нужны ни сборка мусора версий, ни упорядочение снимков.

Из этого же следует главное практическое следствие: **при уровне по умолчанию
чтение грязное**. Строка, вставленная или изменённая открытой транзакцией,
физически уже в куче (глава 16 — мутация идёт до записи в журнал), поэтому её
видит любой другой читатель; откат так же физически её убирает. Приложению,
которому нужна изоляция от чужих незакоммиченных записей, недостаточно умолчания
— нужен `REPEATABLE READ` и своя удержанная блокировка чтения.

Два пробела названы, а не умолчаны. Первый: блокировки чтения берутся только
внутри явной, всё ещё открытой транзакции **этого** соединения — у
автокоммитного `SELECT` нет более позднего утверждения, которое надо защищать, и
он не наследует уровень изоляции чужой открытой транзакции. Второй: они
покрывают только одно-табличные утверждения — у строки `JOIN` после слияния
теряется пер-табличный `RecordId`, и прицепить блокировку не к чему.

### Точки сохранения: позиции в логе

`Transaction` помнит две вещи: список записей журнала (для откатов) и карту «имя
точки сохранения → индекс в этом списке». Отсюда — обе операции:

- `ROLLBACK TO SAVEPOINT s` откатывает всё, что занесено в лог после `s`, в
  обратном порядке, и забывает точки, объявленные позже; сама `s` остаётся и
  пригодна к повторному откату;
- `RELEASE SAVEPOINT s` забывает `s` и всё объявленное после неё, **не откатывая
  ни строки**: изменения остаются, уходит только возможность вернуться в эту
  точку.

Обе — стандартные правила SQL, и обе опираются на одно: точка сохранения — это
индекс в истории одной транзакции, поэтому вне открытой транзакции `SAVEPOINT`
бессмысленен и отвергается.

### Порядок записи и барьер

`INSERT`/`UPDATE`/`DELETE` пишут в кучу и индексы **до** того, как
`TransactionManager` занесёт запись в журнал: `RecordId` новой строки (или
переехавшей) становится известен только после самой мутации. Что «write-ahead»
всё же обязана гарантировать — что полный журнал транзакции долговечен до её
маркера `COMMIT` — сохраняется: каждая запись попадает в журнал и `fsync()`-ится
до возврата утверждения, а значит всегда раньше `COMMIT`, который транзакцию
заканчивает. `commit()` и `rollback()` дополнительно синхронизируют страницы с
устройством **перед** записью своего маркера, потому что признак завершённости
транзакции для восстановления — только наличие этой записи. Детали журнала,
`fsync()` и восстановления после сбоя — глава 18; здесь важно лишь то, что откат
и завершение опираются на тот же журнал.

`DDL` в эту схему не входит: `CREATE`/`DROP TABLE` и `CREATE`/`DROP INDEX`
применяются немедленно и в журнал не пишутся, поэтому `ROLLBACK` после
`CREATE TABLE` оставляет таблицу как есть. И отдельно: сессия, которая
оборвалась, не оставит транзакцию открытой навсегда — при закрытии соединения
откатывается именно её транзакция, иначе все последующие `BEGIN` были бы
отвергнуты.

## 3. Код

### Уровни изоляции и режимы блокировок

Три уровня — три комментария, и в каждом сказано, какие блокировки он берёт, а
не какой снимок читает. Это и есть ответ главы на вопрос «что такое изоляция»:
набор правил блокирования.

```php
/**
 * How much of another transaction's concurrent activity a transaction is
 * shielded from. Implemented here through locking (2-phase locking), not
 * row versioning — there is no MVCC in this engine (Phase 2 chose no
 * buffer pool, and nothing since has added multi-version storage), so
 * "repeatable" and "serializable" are achieved by holding the right locks
 * for the whole transaction rather than by reading a frozen snapshot. See
 * DECISIONS.md.
 */
enum IsolationLevel: string
{
    /**
     * The default. A statement sees whatever is currently committed; locks
     * taken to read a row are not held past the statement, only writes are
     * held until commit (so a lost update is still prevented).
     */
    case READ_COMMITTED = 'READ COMMITTED';

    /**
     * Like READ COMMITTED, but every row a statement reads is also
     * shared-locked for the rest of the transaction — another transaction
     * cannot change a row this one has already read, so reading it again
     * gives the same answer.
     */
    case REPEATABLE_READ = 'REPEATABLE READ';

    /**
     * Like REPEATABLE READ, and additionally: scanning a whole table
     * (rather than a single indexed row) takes a shared lock on the table
     * itself for the rest of the transaction, so another transaction
     * cannot insert a row into it — a coarse but correct guard against the
     * new rows "phantom reads" are named for.
     */
    case SERIALIZABLE = 'SERIALIZABLE';
}
```

[`src/Transaction/IsolationLevel.php#L7-L41`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/IsolationLevel.php#L7-L41)

**Перевод комментариев:**
- `IsolationLevel` (PHPDoc): от сколькой части конкурентной активности другой
  транзакции транзакция защищена. Здесь это реализовано через блокировки
  (2-phase locking), а не версионирование строк — MVCC в этом движке нет (фаза 2
  выбрала отсутствие буферного пула, и с тех пор многоверсионное хранилище не
  добавлялось), поэтому «repeatable» и «serializable» достигаются удержанием
  нужных блокировок на всю транзакцию, а не чтением замороженного снимка. См.
  DECISIONS.md.
- `READ_COMMITTED` (PHPDoc): значение по умолчанию. Утверждение видит всё, что
  уже закоммичено; блокировки, взятые для чтения строки, не удерживаются после
  утверждения, только записи держатся до коммита (так потерянное обновление всё
  равно предотвращается).
- `REPEATABLE_READ` (PHPDoc): как `READ COMMITTED`, но каждая прочитанная
  утверждением строка дополнительно блокируется в режиме `SHARED` до конца
  транзакции — другая транзакция не может изменить строку, которую эта уже
  прочитала, поэтому повторное чтение даёт тот же ответ.
- `SERIALIZABLE` (PHPDoc): как `REPEATABLE READ`, и дополнительно: сканирование
  всей таблицы (а не одной строки по индексу) берёт `SHARED`-блокировку на саму
  таблицу до конца транзакции, поэтому другая транзакция не может вставить в неё
  строку — грубая, но корректная защита от новых строк, ради которых и названы
  «фантомные чтения».

Два режима — ровно те же, что у файловой блокировки, но живут в памяти и
применяются к таблице или строке:

```php
/**
 * SHARED locks coexist with any number of other SHARED locks on the same
 * resource; EXCLUSIVE excludes every other lock, shared or exclusive. The
 * standard two-mode lock, same semantics as `Infrastructure\FileLock`'s
 * shared/exclusive pair, just held in memory against a table or row rather
 * than a file.
 */
enum LockMode
{
    case SHARED;
    case EXCLUSIVE;
}
```

[`src/Transaction/LockMode.php#L7-L18`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/LockMode.php#L7-L18)

**Перевод комментариев:**
- `LockMode` (PHPDoc): блокировки `SHARED` сосуществуют с любым числом других
  `SHARED` на том же ресурсе; `EXCLUSIVE` исключает любую другую блокировку,
  общую или исключительную. Стандартная двухрежимная блокировка, та же
  семантика, что у пары shared/exclusive в `Infrastructure\FileLock`, только
  держится в памяти для таблицы или строки, а не файла.

### Кто на что имеет право: `LockManager`

Докблок класса объясняет, почему здесь нет ожидания, а есть отказ, — и чем это
отличается от `FileLock`:

```php
/**
 * Who holds what lock, in memory, for exactly as long as this `Database`
 * connection is open — nothing here is written to disk, because a lock
 * only ever needs to mean something to the other transactions of the same
 * process that granted it.
 *
 * Acquiring a lock either succeeds immediately or throws immediately —
 * there is no waiting. `Infrastructure\FileLock` polls with a timeout
 * because the process holding a file lock is a *different* process that
 * will eventually release it on its own schedule; a lock this class
 * refuses is held by another `Transaction` inside the *same* synchronous
 * call stack, which cannot release it while this call is still blocked
 * waiting — waiting would just be a hang with a fixed diagnosis. A
 * real wait, against a genuinely concurrent holder, is Phase 12's problem
 * once a server gives more than one connection a thread of control to
 * block in.
 */
```

[`src/Transaction/LockManager.php#L10-L26`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/LockManager.php#L10-L26)

**Перевод комментариев:**
- `LockManager` (PHPDoc): кто держит какую блокировку, в памяти, ровно столько,
  сколько открыто это соединение `Database` — ничего здесь не пишется на диск,
  потому что блокировка должна что-то значить только для других транзакций того
  же процесса, который её выдал. Захват блокировки либо сразу успешен, либо
  сразу бросает исключение — ожидания нет. `Infrastructure\FileLock` опрашивает
  с таймаутом, потому что процесс, держащий файловую блокировку, — *другой*
  процесс, который рано или поздно отпустит её по своему графику; блокировку, в
  которой этот класс отказывает, держит другая `Transaction` внутри *того же*
  синхронного стека вызовов, и она не может отпустить её, пока этот вызов всё
  ещё заблокирован в ожидании, — ожидание было бы просто зависанием с заранее
  известным диагнозом. Настоящее ожидание против действительно конкурентного
  держателя — задача фазы 12, когда сервер даст большему числу соединений поток
  управления, в котором можно блокироваться.

Ресурс собирается в строку: `table:<имя>` и `row:<таблица>:<page:slot>`.
Публичные методы — тонкая обёртка над одной приватной проверкой, а
`releaseAll()` освобождает всё сразу:

```php
/** @throws TransactionException when an incompatible lock is already held by another transaction */
public function acquireTableLock(string $table, int $txId, LockMode $mode): void
{
    $this->acquire('table:' . $table, $txId, $mode, sprintf('table "%s"', $table));
}

/** @throws TransactionException when an incompatible lock is already held by another transaction */
public function acquireRowLock(string $table, RecordId $id, int $txId, LockMode $mode): void
{
    $this->acquire('row:' . $table . ':' . $id, $txId, $mode, sprintf('row %s in "%s"', $id, $table));
}

/** Every lock this transaction holds, released together — 2-phase locking's "shrink phase". */
public function releaseAll(int $txId): void
{
    foreach ($this->grants as $resource => $holders) {
        unset($holders[$txId]);

        if ($holders === []) {
            unset($this->grants[$resource]);
        } else {
            $this->grants[$resource] = $holders;
        }
    }
}
```

[`src/Transaction/LockManager.php#L32-L56`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/LockManager.php#L32-L56)

**Перевод комментариев:**
- `acquireTableLock()` (`@throws`): бросает `TransactionException`, если
  несовместимую блокировку уже держит другая транзакция.
- `acquireRowLock()` (`@throws`): бросает `TransactionException`, если
  несовместимую блокировку уже держит другая транзакция.
- `releaseAll()` (PHPDoc): освобождает все блокировки этой транзакции вместе —
  «фаза сжатия» 2-phase locking.

Вся совместимость режимов — один `match`: `EXCLUSIVE` требует, чтобы чужих
владельцев не было вовсе, `SHARED` — чтобы среди них не было `EXCLUSIVE`.
Повторный захват более сильной блокировки тем же владельцем — не ошибка: строка
«уже держу `EXCLUSIVE`» выходит раньше проверки. Здесь же собирается то самое
сообщение об отказе, которое целиком увидят эксперименты 2 и 3:

```php
private function acquire(string $resource, int $txId, LockMode $mode, string $description): void
{
    $holders = $this->grants[$resource] ?? [];

    if (($holders[$txId] ?? null) === LockMode::EXCLUSIVE) {
        return; // already holds the strongest lock there is
    }

    $others = array_filter($holders, static fn (int $holder): bool => $holder !== $txId, ARRAY_FILTER_USE_KEY);

    $compatible = match ($mode) {
        // Exclusive tolerates no other holder at all, not even another
        // shared reader.
        LockMode::EXCLUSIVE => $others === [],
        // Shared tolerates other shared holders, but not an exclusive one.
        LockMode::SHARED => !in_array(LockMode::EXCLUSIVE, $others, true),
    };

    if (!$compatible) {
        throw new TransactionException(sprintf(
            'Cannot acquire a %s lock on %s: held by another transaction.',
            $mode === LockMode::EXCLUSIVE ? 'an exclusive' : 'a shared',
            $description,
        ));
    }

    $holders[$txId] = $mode;
    $this->grants[$resource] = $holders;
}
```

[`src/Transaction/LockManager.php#L58-L86`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/LockManager.php#L58-L86)

**Перевод комментариев:**
- `// already holds the strongest lock there is` — уже держит самую сильную
  блокировку из возможных.
- `// Exclusive tolerates no other holder at all, not even another` —
  `EXCLUSIVE` не терпит никакого другого держателя, даже другого `SHARED`
  -читателя.
- `// Shared tolerates other shared holders, but not an exclusive one.` —
  `SHARED` терпит других `SHARED`-держателей, но не `EXCLUSIVE`.

Одно исключение на все транзакционные отказы — от отсутствующей транзакции до
занятой блокировки:

```php
/**
 * Raised for anything transaction-shaped going wrong: a statement that
 * needs an active transaction finding none (`COMMIT` with nothing to
 * commit), a `SAVEPOINT` named that does not exist, or a lock a
 * transaction cannot get because another one already holds it.
 */
final class TransactionException extends DatabaseException
{
}
```

[`src/Exception/TransactionException.php#L7-L15`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Exception/TransactionException.php#L7-L15)

**Перевод комментариев:**
- `TransactionException` (PHPDoc): бросается при любом сбое транзакционного
  характера: утверждение, которому нужна активная транзакция, не находит её
  (`COMMIT`, когда нечего коммитить), `SAVEPOINT` с несуществующим именем или
  блокировка, которую транзакция не может получить, потому что её уже держит
  другая.

### Состояние транзакции: лог и точки сохранения

`Transaction` ничего не решает, а только помнит: список записей и карту точек
сохранения. Этого достаточно и для полного отката, и для отката к точке.

```php
/**
 * One open transaction's own state: which changes it has made so far (for
 * `ROLLBACK`/`ROLLBACK TO SAVEPOINT` to undo) and where each of its
 * `SAVEPOINT`s falls in that history. `TransactionManager` is what decides
 * *when* to consult this; this class only remembers.
 *
 * Both rolling back to a savepoint and releasing one drop every savepoint
 * declared after it — a point in history that no longer exists cannot be
 * rolled back to either. They differ on the named savepoint itself:
 * rolling back to it leaves it in place, usable again; releasing it
 * forgets it too. Both are standard SQL's own rules for the two.
 */
final class Transaction
{
    /** @var list<WalRecord> every change logged since BEGIN, oldest first */
    private array $log = [];

    /** @var array<string, int> savepoint name => index into $log when it was declared */
    private array $savepoints = [];

    public function __construct(
        public readonly int $id,
        public readonly IsolationLevel $isolationLevel,
    ) {
    }

    public function record(WalRecord $record): void
    {
        $this->log[] = $record;
    }

    public function declareSavepoint(string $name): void
    {
        $this->savepoints[$name] = count($this->log);
    }
```

[`src/Transaction/Transaction.php#L9-L43`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Transaction.php#L9-L43)

**Перевод комментариев:**
- `Transaction` (PHPDoc): состояние одной открытой транзакции: какие изменения
  она уже сделала (чтобы `ROLLBACK` /`ROLLBACK TO SAVEPOINT` могли их отменить)
  и где в этой истории находится каждая её `SAVEPOINT`. `TransactionManager`
  решает, *когда* к этому обращаться; этот класс только помнит. И откат к точке
  сохранения, и её освобождение отбрасывают все точки, объявленные после неё, —
  к точке истории, которой больше нет, тоже нельзя откатиться. Они различаются
  судьбой самой именованной точки: откат к ней оставляет её на месте, годной к
  повторному использованию; освобождение забывает и её. Оба правила —
  стандартные правила SQL для этих операций.
- `$log` (`@var`): все изменения, записанные в журнал с `BEGIN`, от старых к
  новым.
- `$savepoints` (`@var`): имя точки сохранения → индекс в `$log` на момент её
  объявления.

Откат «назад к `s`» — это срез лога по индексу `s`, перевёрнутый в порядок
«сначала последнее», плюс забывание точек, объявленных позже; `RELEASE`
отличается одним аргументом и тем, что лог не трогает вовсе:

```php
/**
 * Every record logged since $name was declared, most-recently-logged
 * first — the order `TransactionManager` needs to undo them in.
 *
 * @return list<WalRecord>
 */
public function recordsSince(string $name): array
{
    return array_reverse(array_slice($this->log, $this->savepointIndex($name)));
}

/**
 * Drops the log entries made since $name — $name itself survives and
 * can be rolled back to again, only the savepoints declared after it
 * are forgotten, matching `ROLLBACK TO SAVEPOINT`'s standard SQL
 * meaning.
 */
public function truncateToSavepoint(string $name): void
{
    $index = $this->savepointIndex($name);
    $this->log = array_slice($this->log, 0, $index);
    $this->forgetSavepointsAfter($index);
}

/** Forgets $name itself and every savepoint declared after it, keeping the log itself untouched. */
public function releaseSavepoint(string $name): void
{
    $this->forgetSavepointsAfter($this->savepointIndex($name) - 1);
}
```

[`src/Transaction/Transaction.php#L50-L78`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Transaction.php#L50-L78)

**Перевод комментариев:**
- `recordsSince()` (PHPDoc): все записи журнала с момента объявления `$name`,
  начиная с последней — в том порядке, в котором `TransactionManager` должен их
  отменять.
- `truncateToSavepoint()` (PHPDoc): отбрасывает записи журнала, сделанные после
  `$name`; сама `$name` выживает, и к ней можно откатиться снова, забываются
  только точки, объявленные после неё, что соответствует стандартному смыслу
  `ROLLBACK TO SAVEPOINT` в SQL.
- `releaseSavepoint()` (PHPDoc): забывает саму `$name` и все точки, объявленные
  после неё, не трогая сам журнал.

Отдельная деталь — почему лог вообще очищается до барьера долговечности: если
синхронизация упадёт после отката, транзакция остаётся открытой для повторной
попытки, и повтор обязан видеть пустой лог, иначе удалит уже удалённую строку и
получит `StorageException` вместо успеха.

```php
/**
 * Empties the log, once every change in it has actually been undone —
 * `TransactionManager::rollback()` calls this between applying the
 * undo and its own durability barrier, so that a barrier failure
 * leaves a transaction that is still open but has nothing left to
 * undo. Without it, the retry that failure invites would undo every
 * change a second time, and a second delete of an already-deleted row
 * is a `StorageException`, not a no-op — leaving the transaction
 * permanently unable to finish. The savepoints go with it: they index
 * into a log that no longer exists.
 */
public function forgetAllRecords(): void
{
    $this->log = [];
    $this->savepoints = [];
}
```

[`src/Transaction/Transaction.php#L86-L101`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Transaction.php#L86-L101)

**Перевод комментариев:**
- `forgetAllRecords()` (PHPDoc): очищает журнал, когда все изменения в нём
  действительно отменены; `TransactionManager::rollback()` вызывает это между
  применением undo и собственным барьером долговечности, чтобы сбой барьера
  оставил транзакцию открытой, но без работы для отмены. Без этого повторная
  попытка, к которой приглашает сбой, отменила бы каждое изменение второй раз, а
  второе удаление уже удалённой строки — это `StorageException`, а не no-op, и
  транзакция навсегда осталась бы неспособной завершиться. Точки сохранения
  уходят вместе с журналом: они индексируют журнал, которого больше нет.

### Менеджер: одна транзакция и её владелец

Докблок `TransactionManager` — место, где собраны оба решения сразу: одна
транзакция на базу и непрозрачный токен владельца. Там же — почему отмена
изменения вынесена в замыкание, которое подключает `Executor`, а не в
собственный код менеджера:

```php
/**
 * `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`, and the crash recovery that
 * shares their undo machinery. Owns the one `Transaction` that can be open
 * at a time — a second `begin()` while one is already active is refused
 * rather than implicitly committing the first, since a silent commit a
 * caller did not ask for is exactly the kind of surprise this project
 * avoids elsewhere.
 *
 * `$owner` is who opened the one open `Transaction` — an opaque token the
 * caller supplies (`Execution\Executor` passes `$this`), compared only by
 * identity, never inspected. This is not a second transaction slot: this
 * project stays single-writer (see `begin()`), and any caller can still
 * see whether *something* is open (`inTransaction()`). What owner tracking
 * adds is that only the caller `begin()` recorded may `commit()`/
 * `rollback()`/`savepoint()`/etc. that transaction, or have an autocommit
 * statement silently run inside it — a second `Network\Session` sharing
 * this same `Database` used to be able to do either, since nothing here
 * ever asked which caller was asking. See DECISIONS.md.
 *
 * Undoing a change is not this class's job: it holds the *log* of what
 * happened, but reversing a change means writing to the heap file and its
 * indexes, which only `Execution\Executor` knows how to do. `setUndoHandler()`
 * is where that capability is wired in — a `Closure(WalRecord): void` that
 * physically applies one record's reversal — resolving what would
 * otherwise be a circular dependency (`Executor` needs a
 * `TransactionManager` to dispatch `BEGIN`/`COMMIT`/…, and this class needs
 * `Executor`'s row-mutation logic to undo anything) without either class
 * depending on the other's concrete type. `setSyncHandler()` is the same
 * pattern for a second capability only `Schema\Database` has: pushing
 * every page a transaction touched all the way to the device before its
 * WAL record is checkpointed away. See DECISIONS.md.
 */
```

[`src/Transaction/TransactionManager.php#L11-L42`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L11-L42)

**Перевод комментариев:**
- `TransactionManager` (PHPDoc): `BEGIN` /`COMMIT`/`ROLLBACK`/`SAVEPOINT` и
  восстановление после сбоя, использующее ту же машинерию отмены. Владеет
  единственной `Transaction`, которая может быть открыта одновременно, — второй
  `begin()` при уже активной транзакции отвергается, а не коммитит первую
  неявно, поскольку молчаливый коммит, которого вызывающий не просил, — ровно
  тот сюрприз, которого проект избегает и в других местах. `$owner` — тот, кто
  открыл единственную открытую `Transaction`: непрозрачный токен, который
  предоставляет вызывающий (`Execution\Executor` передаёт `$this`), сравниваемый
  только по идентичности и никогда не инспектируемый. Это не второй слот
  транзакции: проект остаётся single-writer (см. `begin()`), и любой вызывающий
  по-прежнему может увидеть, открыто ли *что-нибудь* (`inTransaction()`).
  Отслеживание владельца добавляет то, что только вызывающий, записанный в
  `begin()`, может `commit()` /`rollback()`/`savepoint()`/и т. п. для этой
  транзакции или позволить автокоммитному утверждению молча выполниться внутри
  неё — второй `Network\Session`, использующий ту же `Database`, раньше мог и то
  и другое, поскольку здесь ничто не спрашивало, какой вызывающий спрашивает.
  См. DECISIONS.md. Отмена изменения — не работа этого класса: он хранит
  *журнал* произошедшего, но обратить изменение означает записать в файл кучи и
  его индексы, а это умеет только `Execution\Executor`. `setUndoHandler()` —
  место, где подключается эта возможность: `Closure(WalRecord): void`, физически
  применяющее откат одной записи, — разрешая то, что иначе было бы циклической
  зависимостью (`Executor` нужен `TransactionManager` для диспетчеризации
  `BEGIN` /`COMMIT`/…, а этому классу нужна логика мутации строк `Executor`,
  чтобы что-либо отменить), без зависимости ни одного класса от конкретного типа
  другого. `setSyncHandler()` — тот же приём для второй возможности, которая
  есть только у `Schema\Database`: протолкнуть каждую страницу, которую трогала
  транзакция, вплоть до устройства до того, как её запись WAL будет убрана
  контрольной точкой. См. DECISIONS.md.

`begin()` отвергает второй `BEGIN` и запоминает владельца:

```php
public function begin(IsolationLevel $level = IsolationLevel::READ_COMMITTED, mixed $owner = null): Transaction
{
    if ($this->current !== null) {
        throw new TransactionException('A transaction is already active; COMMIT or ROLLBACK it first.');
    }

    $id = $this->nextTransactionId++;
    $this->wal->append(WalRecord::begin($this->wal->nextLsn(), $id));

    $this->owner = $owner;

    return $this->current = new Transaction($id, $level);
}
```

[`src/Transaction/TransactionManager.php#L110-L122`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L110-L122)

`commit()` и `rollback()` начинаются с одной и той же проверки владельца;
`rollback()` дополнительно очищает лог между откатом и барьером — ровно так же,
как это делает откат к точке сохранения:

```php
/**
 * `$sync` runs *before* the `COMMIT` record is appended, not after —
 * recovery's only signal that a transaction is finished is that
 * record's presence, so a crash must never be able to show one without
 * the data it covers already being durable. See DECISIONS.md.
 */
public function commit(mixed $owner = null): void
{
    $tx = $this->requireOwnedCurrent($owner);

    $this->sync?->__invoke();
    $this->wal->append(WalRecord::commit($this->wal->nextLsn(), $tx->id));
    $this->finish($tx);
}

/**
 * @see commit() for why `$sync` runs before the `ROLLBACK` record, not after.
 *
 * The log is emptied between the undo and that barrier, because
 * everything after the undo can still fail and leave this transaction
 * open for the caller to retry: a retry that found the log still
 * populated would undo every change a second time, and the second
 * delete of an already-deleted row is a `StorageException`, which
 * would leave the transaction permanently unable to finish. This is
 * what `rollbackToSavepoint()` has always done with
 * `truncateToSavepoint()` — dropping what it just undid — applied to
 * the whole-transaction case.
 */
public function rollback(mixed $owner = null): void
{
    $tx = $this->requireOwnedCurrent($owner);

    $this->applyUndo($tx->allRecordsReversed());
    $tx->forgetAllRecords();
    $this->sync?->__invoke();
    $this->wal->append(WalRecord::rollback($this->wal->nextLsn(), $tx->id));
    $this->finish($tx);
}
```

[`src/Transaction/TransactionManager.php#L124-L161`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L124-L161)

**Перевод комментариев:**
- `commit()` (PHPDoc): `$sync` выполняется *до* добавления записи `COMMIT`, а не
  после — единственный сигнал восстановления о завершении транзакции — это
  наличие этой записи, поэтому сбой никогда не должен показать её без уже
  устойчивых данных, которые она покрывает. См. DECISIONS.md.
- `rollback()` (PHPDoc): о том, почему `$sync` выполняется до записи `ROLLBACK`,
  а не после, см. `commit()`. Журнал очищается между отменой и этим барьером,
  потому что всё после отмены ещё может упасть и оставить транзакцию открытой
  для повторной попытки вызывающего: повтор, заставший журнал всё ещё
  заполненным, отменил бы каждое изменение второй раз, а второе удаление уже
  удалённой строки — это `StorageException`, и транзакция навсегда осталась бы
  неспособной завершиться. Так всегда поступал `rollbackToSavepoint()` с
  `truncateToSavepoint()` — отбрасывая то, что только что отменил, — теперь
  применённое ко всему случаю транзакции.

Конец транзакции — это освобождение всех блокировок и обнуление владельца в
одном месте:

```php
private function finish(Transaction $tx): void
{
    $this->locks->releaseAll($tx->id);
    $this->current = null;
    $this->owner = null;

    // Safe only because exactly one transaction can ever be open at a
    // time in this process - once it ends, nothing depends on the log
    // that got it there.
    $this->wal->checkpoint();
}
```

[`src/Transaction/TransactionManager.php#L344-L354`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L344-L354)

**Перевод комментариев:**
- `// Safe only because exactly one transaction can ever be open at a` — это
  безопасно только потому, что в этом процессе одновременно может быть открыта
  ровно одна транзакция: когда она заканчивается, от журнала, который к этому
  привёл, ничего не зависит.

Проверка владельца — четыре строки; вся разница между «транзакция открыта» и
«эту транзакцию можно закрыть мной» здесь:

```php
/**
 * `logInsert()`/`logUpdate()`/`logDelete()`'s own check: no ownership
 * verification, since they only ever run from inside `Execution\Executor::withTransaction()`'s
 * closure, which has already established the caller either began this
 * transaction itself or already owned it before `$work()` ran.
 */
private function requireCurrent(): Transaction
{
    return $this->current ?? throw new TransactionException('No transaction is active.');
}

private function requireOwnedCurrent(mixed $owner): Transaction
{
    $tx = $this->requireCurrent();

    if ($this->owner !== $owner) {
        throw new TransactionException(
            'Another connection has an open transaction; only the connection that started it may COMMIT, ROLLBACK, or manage its savepoints.',
        );
    }

    return $tx;
}
```

[`src/Transaction/TransactionManager.php#L356-L378`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L356-L378)

**Перевод комментариев:**
- `requireCurrent()` (PHPDoc): собственная проверка `logInsert()`
  /`logUpdate()`/`logDelete()`: проверки владения нет, поскольку они выполняются
  только внутри замыкания `Execution\Executor::withTransaction()`, которое уже
  установило, что вызывающий либо начал эту транзакцию сам, либо уже владел ею
  до запуска `$work()`.

### Владелец состояния — база, а не исполнитель

Транзакция и таблица блокировок кэшируются в `Schema\Database`, поэтому любой
`Executor` над этой базой видит одну и ту же открытую транзакцию — и второй
`BEGIN` будет отвергнут независимо от того, кто его послал:

```php
/** The lock table every transaction against this database contends for. */
public function locks(): LockManager
{
    return $this->locks ??= new LockManager();
}

/**
 * The one `TransactionManager` for this database, shared by every
 * `Executor` built against it, so that `BEGIN`ning a transaction
 * through one is visible to (and blocks a second `BEGIN` from) another.
 */
public function transactions(): TransactionManager
{
    return $this->transactions ??= new TransactionManager($this->wal(), $this->locks());
}
```

[`src/Schema/Database.php#L389-L403`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Schema/Database.php#L389-L403)

**Перевод комментариев:**
- `locks()` (PHPDoc): таблица блокировок, за которую конкурирует каждая
  транзакция этой базы.
- `transactions()` (PHPDoc): единственный `TransactionManager` этой базы, общий
  для каждого построенного над ней `Executor`, так что `BEGIN` транзакции через
  один виден другому (и блокирует второй `BEGIN` из него).

### Чтение под изоляцией

`lockForRead()` — та самая точка, где уровень изоляции превращается в конкретные
блокировки. Обратите внимание на `currentOwnedBy($this)`: чужую открытую
транзакцию читатель не наследует, а под `READ COMMITTED` не берётся вообще
ничего, поэтому конвейер возвращается как есть:

```php
/**
 * `REPEATABLE_READ`/`SERIALIZABLE`'s read-locking. A no-op outside an
 * explicit transaction *of this `Executor`'s own*, or under
 * `READ_COMMITTED` inside one: an isolation level's guarantee is about
 * what a *later* statement in the same transaction sees, and an
 * autocommit `SELECT` has no later statement of its own to protect —
 * including one that happens to run while a *different* connection's
 * transaction is open, which must not borrow that connection's
 * isolation level for a read it never asked to be part of.
 * `SERIALIZABLE` additionally takes a shared table lock on a full scan,
 * to block another transaction's `INSERT` from creating a phantom row
 * this transaction would have matched on a re-scan; an `IndexScan`
 * never needs it, since a value outside its range could never have
 * matched anyway.
 */
private function lockForRead(Operator $pipeline, string $table, bool $isFullScan): Operator
{
    $tx = $this->transactions->currentOwnedBy($this);

    if ($tx === null || $tx->isolationLevel === IsolationLevel::READ_COMMITTED) {
        return $pipeline;
    }

    if ($tx->isolationLevel === IsolationLevel::SERIALIZABLE && $isFullScan) {
        $this->locks->acquireTableLock($table, $tx->id, LockMode::SHARED);
    }

    return new LockRows($pipeline, $this->locks, $table, $tx->id, LockMode::SHARED);
}
```

[`src/Execution/Executor.php#L391-L419`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L391-L419)

**Перевод комментариев:**
- `lockForRead()` (PHPDoc): блокировка чтения для `REPEATABLE_READ`
  /`SERIALIZABLE`. Ничего не делает вне явной транзакции *самого этого
  `Executor` * или под `READ_COMMITTED` внутри неё: гарантия уровня изоляции
  касается того, что увидит *следующее* утверждение в той же транзакции, а у
  автокоммитного `SELECT` нет собственного следующего утверждения, которое надо
  защищать, — включая тот, что случайно выполняется, пока открыта транзакция
  *другого* соединения, и не должен одалживать уровень изоляции этого соединения
  для чтения, частью которого его не просили быть. `SERIALIZABLE` дополнительно
  берёт `SHARED`-блокировку таблицы при полном сканировании, чтобы
  заблокировать `INSERT` другой транзакции, создающий фантомную строку, которую
  эта транзакция нашла бы при повторном сканировании; `IndexScan` в ней никогда
  не нуждается, поскольку значение вне его диапазона всё равно не могло
  совпасть.

### Автокоммит и владение

`withTransaction()` делает два дела сразу: отвергает одиночное утверждение, если
открыта **чужая** транзакция, и разворачивает собственную, если не открыто
ничего. Именно здесь жил найденный ревью баг: раньше проверялось только «открыто
ли что-нибудь», а не «кем»:

```php
private function withTransaction(Closure $work): mixed
{
    if ($this->transactions->inTransaction() && !$this->transactions->isOwnedBy($this)) {
        throw new TransactionException(
            'Another connection has an open transaction; try again once it finishes.',
        );
    }

    $autocommit = !$this->transactions->inTransaction();

    if ($autocommit) {
        $this->transactions->begin(owner: $this);
    }

    try {
        $result = $work();
    } catch (Throwable $e) {
        if ($autocommit) {
            $this->transactions->rollback(owner: $this);
        }

        throw $e;
    }

    if ($autocommit) {
        $this->transactions->commit(owner: $this);
    }

    return $result;
}
```

[`src/Execution/Executor.php#L862-L891`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L862-L891)

### Запись всегда берёт таблицу целиком

`INSERT` под любым уровнем изоляции берёт исключительную блокировку таблицы —
парную к общей блокировке, которую берёт полный скан под `SERIALIZABLE`. Это и
есть защита от фантома: пока существует скан, вставка не пройдёт, и наоборот:

```php
// An exclusive table lock, held until commit/rollback like every
// other lock here: the counterpart to the shared one a
// SERIALIZABLE full scan takes (see lockForRead()). Neither
// conflicts with a plain row lock, so this only ever actually
// blocks against another INSERT or a concurrent SERIALIZABLE
// scan - the phantom this exists to prevent.
$this->locks->acquireTableLock($table->name, $txId, LockMode::EXCLUSIVE);
```

[`src/Execution/Executor.php#L513-L519`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L513-L519)

**Перевод комментариев:**
- `// An exclusive table lock, held until commit/rollback like every` —
  исключительная блокировка таблицы, удерживаемая до commit/rollback, как и
  любая другая здесь: пара к общей блокировке, которую берёт полный скан под
  `SERIALIZABLE` (см. `lockForRead()`). Ни одна из них не конфликтует с обычной
  блокировкой строки, поэтому эта реально блокирует только другой `INSERT` или
  конкурентный `SERIALIZABLE`-скан — тот самый фантом, ради которого она
  существует.

### SQL-поверхность транзакций

`BEGIN ... ISOLATION LEVEL`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`,
`RELEASE SAVEPOINT`, `ROLLBACK TO SAVEPOINT` — шесть обработчиков, и каждый
передаёт владельца дальше:

```php
private function executeBegin(BeginStatement $statement): null
{
    $this->transactions->begin($statement->isolationLevel ?? IsolationLevel::READ_COMMITTED, owner: $this);

    return null;
}

private function executeCommit(): null
{
    $this->transactions->commit(owner: $this);

    return null;
}

private function executeRollback(): null
{
    $this->transactions->rollback(owner: $this);

    return null;
}

private function executeSavepoint(SavepointStatement $statement): null
{
    $this->transactions->savepoint($statement->name, owner: $this);

    return null;
}

private function executeReleaseSavepoint(ReleaseSavepointStatement $statement): null
{
    $this->transactions->releaseSavepoint($statement->name, owner: $this);

    return null;
}

private function executeRollbackToSavepoint(RollbackToSavepointStatement $statement): null
{
    $this->transactions->rollbackToSavepoint($statement->name, owner: $this);

    return null;
}
```

[`src/Execution/Executor.php#L1087-L1127`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L1087-L1127)

### Блокировка как стадия конвейера

Чтение блокируется не внутри сканов, а отдельным оператором над ними. `LockRows`
берёт блокировку на каждую строку **перед** её выдачей и стоит до `Filter` —
потому что обещание повторяемости относится и к строкам, которые предикат
отверг:

```php
/**
 * Acquires a lock on every row a scan visits, before yielding it —
 * REPEATABLE_READ/SERIALIZABLE's read-locking, wired in as a pipeline
 * stage so `SeqScan`/`IndexScan` need not know about transactions at all.
 * Sits directly on top of one of those two, before `Filter`: a repeatable
 * read promises that scanning again with the same predicate later in the
 * same transaction sees the same matching rows, which requires holding a
 * row the predicate rejected just as much as one it kept, in case a later
 * statement in this transaction would have matched it.
 */
final readonly class LockRows implements Operator
{
    public function __construct(
        private Operator $source,
        private LockManager $locks,
        private string $table,
        private int $txId,
        private LockMode $mode,
    ) {
    }

    public function getIterator(): Generator
    {
        /** @var RecordId $id */
        foreach ($this->source as $id => $row) {
            $this->locks->acquireRowLock($this->table, $id, $this->txId, $this->mode);

            yield $id => $row;
        }
    }
}
```

[`src/Execution/Operator/LockRows.php#L12-L42`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/LockRows.php#L12-L42)

**Перевод комментариев:**
- `LockRows` (PHPDoc): берёт блокировку на каждую строку, которую посещает скан,
  перед её выдачей — блокировка чтения `REPEATABLE_READ` /`SERIALIZABLE`,
  встроенная стадией конвейера, чтобы `SeqScan` /`IndexScan` вообще не знали о
  транзакциях. Стоит прямо над одним из этих двух, перед `Filter`: повторяемое
  чтение обещает, что повторное сканирование с тем же предикатом позже в той же
  транзакции увидит те же совпадающие строки, а для этого нужно удерживать и
  строку, отвергнутую предикатом, так же, как сохранённую, — на случай, если
  более позднее утверждение этой транзакции её бы совпало.
- `$id` (`@var`): `RecordId` текущей строки, ключ генератора `source`.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-database
make build install
```

Четыре скрипта ниже кладутся в `/tmp` на хосте и копируются в контейнер, где
проект смонтирован в `/app`. Вывод — реальный, снятый при написании главы. Все
четыре детерминированы: замеров времени нет ни в одном, и повторный прогон
каждого даёт те же байты.

### 1. Одна транзакция, её владелец и грязное чтение

Первый вопрос: что происходит, когда две «сессии» — два `Executor` над одной
базой — работают с одной транзакцией. Скрипт открывает `BEGIN` в соединении A,
пробует писать и завершать из B, а затем показывает чтение незакоммиченной
строки:

```bash
docker compose cp /tmp/ch17-ownership.php php:/tmp/ch17-ownership.php
docker compose exec php php /tmp/ch17-ownership.php
```

```php
<?php

declare(strict_types=1);

/**
 * One open transaction, owned by the connection that opened it — and a
 * read that sees another connection's uncommitted work.
 *
 * Two `Executor`s here stand in for two client connections sharing one
 * `Schema\Database`. The engine allows exactly one open transaction at a
 * time, so the interesting question is not how two writers interleave but
 * who may act on the one transaction that exists:
 *
 *   - a second connection's bare INSERT is refused outright rather than
 *     silently joined to the first connection's transaction;
 *   - a second connection cannot COMMIT a transaction it never began;
 *   - the owner itself cannot BEGIN twice;
 *   - and, once nobody holds a transaction open, every connection is back
 *     to plain autocommit.
 *
 * Then the gap the default isolation level leaves open: there is no MVCC
 * here, so a row A has inserted but not committed is already physically in
 * the heap and visible to B — and a ROLLBACK makes it vanish just as
 * immediately.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch17-ownership.php php:/tmp/ch17-ownership.php
 *   docker compose exec php php /tmp/ch17-ownership.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\TransactionException;
use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = sys_get_temp_dir() . '/ch17-ownership-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$setup = new Executor($database);
$setup->run('CREATE TABLE accounts (id INT PRIMARY KEY, balance INT)');

/** Two Executors over one Database — two connections to the same files. */
$a = new Executor($database);
$b = new Executor($database);

$attempt = static function (string $label, callable $work): void {
    try {
        $work();
        printf("%-43s -> ok\n", $label);
    } catch (TransactionException $e) {
        printf("%-43s -> %s\n", $label, $e->getMessage());
    }
};

/** What a given connection can read, asked the way any client would ask. */
$visible = static function (Executor $reader): string {
    $ids = array_map(
        static fn (Row $row): string => (string) $row->get('id'),
        iterator_to_array($reader->run('SELECT id FROM accounts ORDER BY id')->rows),
    );

    return $ids === [] ? '(no rows)' : implode(', ', $ids);
};

// ---------------------------------------------------------------------
// 1. Who may act on the one open transaction.
// ---------------------------------------------------------------------

printf("--- who may act on the open transaction ---\n");
printf("A: BEGIN\n");

$a->run('BEGIN');
$attempt('A: INSERT id=1 (inside BEGIN)', static fn () => $a->run('INSERT INTO accounts (id, balance) VALUES (1, 100)'));
$attempt('B: INSERT id=2 (no BEGIN of its own)', static fn () => $b->run('INSERT INTO accounts (id, balance) VALUES (2, 100)'));
$attempt('B: COMMIT', static fn () => $b->run('COMMIT'));
$attempt('A: BEGIN again', static fn () => $a->run('BEGIN'));
$attempt('A: COMMIT', static fn () => $a->run('COMMIT'));
$attempt('B: INSERT id=2 (autocommit now)', static fn () => $b->run('INSERT INTO accounts (id, balance) VALUES (2, 100)'));

printf("B reads: %s\n\n", $visible($b));

// ---------------------------------------------------------------------
// 2. The default isolation level sees uncommitted work: no MVCC.
// ---------------------------------------------------------------------

printf("--- a read that sees uncommitted work ---\n");

$a->run('BEGIN');
$a->run('INSERT INTO accounts (id, balance) VALUES (10, 999)');

printf("A: BEGIN; INSERT id=10 (nothing committed yet)\n");
printf("B reads ids: %s\n", $visible($b));

$a->run('ROLLBACK');

printf("A: ROLLBACK\n");
printf("B reads ids: %s\n", $visible($b));

$database->close();
```

**Перевод комментариев:**
- `ch17-ownership.php` (PHPDoc): одна открытая транзакция, принадлежащая
  открывшему её соединению, — и чтение, видящее незакоммиченную работу другого
  соединения. Два `Executor` здесь изображают два клиентских соединения,
  разделяющих одну `Schema\Database`. Движок допускает ровно одну открытую
  транзакцию одновременно, поэтому интересен не вопрос, как чередуются два
  писателя, а кто может действовать с единственной существующей транзакцией:
  голый `INSERT` второго соединения отвергается сразу, а не молча присоединяется
  к транзакции первого; второе соединение не может сделать `COMMIT` транзакции,
  которую не начинало; сам владелец не может сделать `BEGIN` дважды; и когда
  никто не держит транзакцию открытой, каждое соединение снова работает в
  обычном автокоммите. Затем пробел, который оставляет уровень изоляции по
  умолчанию: MVCC здесь нет, поэтому строка, вставленная A, но не закоммиченная,
  уже физически лежит в куче и видна B — а `ROLLBACK` заставляет её исчезнуть
  так же мгновенно.
- `$a`, `$b` (PHPDoc): два `Executor` над одной `Database` — два соединения к
  одним и тем же файлам.
- `$visible` (PHPDoc): что может прочитать данное соединение, спрошенное так,
  как спросил бы любой клиент.
- `// 1. Who may act on the one open transaction.` — кто может действовать с
  единственной открытой транзакцией.
- `// 2. The default isolation level sees uncommitted work: no MVCC.` — уровень
  изоляции по умолчанию видит незакоммиченную работу: MVCC нет.

```text
--- who may act on the open transaction ---
A: BEGIN
A: INSERT id=1 (inside BEGIN)               -> ok
B: INSERT id=2 (no BEGIN of its own)        -> Another connection has an open transaction; try again once it finishes.
B: COMMIT                                   -> Another connection has an open transaction; only the connection that started it may COMMIT, ROLLBACK, or manage its savepoints.
A: BEGIN again                              -> A transaction is already active; COMMIT or ROLLBACK it first.
A: COMMIT                                   -> ok
B: INSERT id=2 (autocommit now)             -> ok
B reads: 1, 2

--- a read that sees uncommitted work ---
A: BEGIN; INSERT id=10 (nothing committed yet)
B reads ids: 1, 2, 10
A: ROLLBACK
B reads ids: 1, 2
```

Пятнадцать строк — и три разных вида отказа, каждый со своим сообщением.
Одиночный `INSERT` из B отвергнут не потому, что мешает конкретная строка, а
потому, что открыта **чужая** транзакция: «Another connection has an open
transaction; try again once it finishes.» Это `withTransaction()`, и это вовсе
не про блокировки: до конкретной строки дело не доходит. `COMMIT` из B отвергнут
другой проверкой — владелец не тот: «…only the connection that started it may
COMMIT, ROLLBACK, or manage its savepoints.» А попытка A начать вторую
транзакцию — третьим сообщением: «A transaction is already active; COMMIT or
ROLLBACK it first.»

Обратите внимание на асимметрию: B **читает** без всяких ограничений —
`B reads: 1, 2`, и это не ошибка эксперимента. Чтение не обёрнуто в
`withTransaction()`, потому что ему нечего защищать: `SELECT` не меняет данные и
не открывает транзакции. Зато когда A вставляет строку `id=10` и не подтверждает
её, B её видит (`1, 2, 10`), а после `ROLLBACK` — снова `1, 2`. Это и есть «no
MVCC» из `docs/transactions.md`: незакоммиченная строка уже физически лежит в
куче, и умолчание `READ COMMITTED` её не прячет.

### 2. Блокировки напрямую: два режима и ни секунды ожидания

Второй вопрос — как выглядит сама таблица блокировок, без SQL. Скрипт работает с
`LockManager` напрямую и просит блокировки от имени трёх транзакций — ровно так,
как это делают модульные тесты:

```bash
docker compose cp /tmp/ch17-locks.php php:/tmp/ch17-locks.php
docker compose exec php php /tmp/ch17-locks.php
```

```php
<?php

declare(strict_types=1);

/**
 * The lock manager itself: two modes, one thread, and a refusal instead of
 * a wait.
 *
 * `Transaction\LockManager` is the whole of this engine's arbiter for
 * concurrent access. Nothing here involves SQL: the point is to see the
 * exact compatibility table the rest of the chapter rides on.
 *
 *   - SHARED coexists with SHARED; EXCLUSIVE excludes every other holder,
 *     shared or exclusive;
 *   - a transaction that already holds the strongest lock may re-acquire
 *     it freely — acquiring is idempotent, not additive;
 *   - a refused acquisition throws immediately: there is no queue, no
 *     timeout, no deadlock detector, because the only thread that could
 *     release the conflicting lock is the one that would otherwise wait;
 *   - a table lock and a row lock on that table are unrelated resources;
 *   - `releaseAll()` is 2-phase locking's shrink phase: everything a
 *     transaction holds, released together.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch17-locks.php php:/tmp/ch17-locks.php
 *   docker compose exec php php /tmp/ch17-locks.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\TransactionException;
use PhpMiniDatabase\Storage\RecordId;
use PhpMiniDatabase\Transaction\LockManager;
use PhpMiniDatabase\Transaction\LockMode;

$locks = new LockManager();
$row = new RecordId(0, 0);

$attempt = static function (string $label, callable $work): void {
    try {
        $work();
        printf("%-56s -> granted\n", $label);
    } catch (TransactionException $e) {
        printf("%-56s -> %s\n", $label, $e->getMessage());
    }
};

// ---------------------------------------------------------------------
// 1. Compatible and incompatible holders on one row.
// ---------------------------------------------------------------------

printf("--- one row, three transactions ---\n");

$attempt('tx1: SHARED on row 0:0', static fn () => $locks->acquireRowLock('t', $row, 1, LockMode::SHARED));
$attempt('tx2: SHARED on the same row', static fn () => $locks->acquireRowLock('t', $row, 2, LockMode::SHARED));
$attempt('tx2: upgrade to EXCLUSIVE while tx1 shares', static fn () => $locks->acquireRowLock('t', $row, 2, LockMode::EXCLUSIVE));

$locks->releaseAll(1);
printf("%-56s\n", 'tx1: releaseAll()');

$attempt('tx2: EXCLUSIVE after tx1 released', static fn () => $locks->acquireRowLock('t', $row, 2, LockMode::EXCLUSIVE));
$attempt('tx3: SHARED while tx2 holds EXCLUSIVE', static fn () => $locks->acquireRowLock('t', $row, 3, LockMode::SHARED));
$attempt('tx2: EXCLUSIVE again (already the strongest)', static fn () => $locks->acquireRowLock('t', $row, 2, LockMode::EXCLUSIVE));

$locks->releaseAll(2);

// ---------------------------------------------------------------------
// 2. Table and row locks are independent resources.
// ---------------------------------------------------------------------

printf("\n--- table \"t\" and row 0:0 in \"t\" ---\n");

$attempt('tx1: EXCLUSIVE on table "t"', static fn () => $locks->acquireTableLock('t', 1, LockMode::EXCLUSIVE));
$attempt('tx2: SHARED on row 0:0 (not the table)', static fn () => $locks->acquireRowLock('t', $row, 2, LockMode::SHARED));
$attempt('tx2: SHARED on table "t" as well', static fn () => $locks->acquireTableLock('t', 2, LockMode::SHARED));
$attempt('tx2: SHARED on table "u" (a different table)', static fn () => $locks->acquireTableLock('u', 2, LockMode::SHARED));
$attempt('tx1: SHARED on row 0:0 (its own exclusive table lock permits it)', static fn () => $locks->acquireRowLock('t', $row, 1, LockMode::SHARED));
$attempt('tx2: EXCLUSIVE on table "t" while tx1 holds it', static fn () => $locks->acquireTableLock('t', 2, LockMode::EXCLUSIVE));

$locks->releaseAll(1);
$attempt('tx2: EXCLUSIVE on table "t" after tx1 released', static fn () => $locks->acquireTableLock('t', 2, LockMode::EXCLUSIVE));
```

**Перевод комментариев:**
- `ch17-locks.php` (PHPDoc): сам менеджер блокировок: два режима, один поток и
  отказ вместо ожидания. `Transaction\LockManager` — весь арбитр конкурентного
  доступа этого движка. SQL здесь ни при чём: цель — увидеть точную таблицу
  совместимости, на которой держится остальная глава: `SHARED` сосуществует с
  `SHARED`; `EXCLUSIVE` исключает любого другого держателя, общего или
  исключительного; транзакция, уже держащая самую сильную блокировку, может
  свободно брать её снова — захват идемпотентен, а не аддитивен; в захвате
  отказано — исключение сразу: нет ни очереди, ни таймаута, ни детектора
  взаимоблокировок, потому что единственный поток, который мог бы отпустить
  конфликтующую блокировку, — тот, что иначе ждал бы; блокировка таблицы и
  блокировка строки в этой таблице — несвязанные ресурсы; `releaseAll()` — фаза
  сжатия 2-phase locking: всё, что держит транзакция, освобождается вместе.
- `// 1. Compatible and incompatible holders on one row.` — совместимые и
  несовместимые держатели одной строки.
- `// 2. Table and row locks are independent resources.` — блокировки таблицы и
  строки — независимые ресурсы.

```text
--- one row, three transactions ---
tx1: SHARED on row 0:0                                   -> granted
tx2: SHARED on the same row                              -> granted
tx2: upgrade to EXCLUSIVE while tx1 shares               -> Cannot acquire a an exclusive lock on row 0:0 in "t": held by another transaction.
tx1: releaseAll()                                       
tx2: EXCLUSIVE after tx1 released                        -> granted
tx3: SHARED while tx2 holds EXCLUSIVE                    -> Cannot acquire a a shared lock on row 0:0 in "t": held by another transaction.
tx2: EXCLUSIVE again (already the strongest)             -> granted

--- table "t" and row 0:0 in "t" ---
tx1: EXCLUSIVE on table "t"                              -> granted
tx2: SHARED on row 0:0 (not the table)                   -> granted
tx2: SHARED on table "t" as well                         -> Cannot acquire a a shared lock on table "t": held by another transaction.
tx2: SHARED on table "u" (a different table)             -> granted
tx1: SHARED on row 0:0 (its own exclusive table lock permits it) -> granted
tx2: EXCLUSIVE on table "t" while tx1 holds it           -> Cannot acquire a an exclusive lock on table "t": held by another transaction.
tx2: EXCLUSIVE on table "t" after tx1 released           -> granted
```

Здесь видно всю совместимость режимов. Два `SHARED` на одну строку уживаются;
попытка «усилиться» до `EXCLUSIVE`, пока второй держит `SHARED`, отвергается —
именно так звучит «потерянная запись», превращённая в исключение до того, как
она случилась. После `releaseAll(1)` та же строка выдаётся `EXCLUSIVE`, а третий
читатель уже не проходит. Повторный `EXCLUSIVE` тем же владельцем — успех:
захват идемпотентен.

Вторая половина — про независимость ресурсов: `EXCLUSIVE` на таблице не мешает
`SHARED` на строке в ней (это разные ключи), `SHARED` на таблице для того же
владельца — успех, а для второго — отказ; таблица `u` от блокировки `t` не
зависит вовсе.

И отдельная деталь, которую стоит заметить: в сообщениях об отказе читается
«Cannot acquire **a an** exclusive lock…» и «Cannot acquire **a a** shared
lock…». Формат собирается как `'Cannot acquire a %s lock'`, а подставляется
`'an exclusive'` или `'a shared'` — косметическая неточность в проекте, видная
только тому, кто действительно получил отказ. Она оставлена как есть: сообщение
однозначно, а правка текста не меняет поведения.

### 3. Уровни изоляции — это расписание блокировок

Третий вопрос — проверить таблицу из раздела 2 экспериментально. Скрипт
открывает настоящую транзакцию с каждым уровнем, выполняет один `SELECT`, а
затем спрашивает `LockManager` от имени «чужой» транзакции 999, свободна ли
прочитанная строка и таблица:

```bash
docker compose cp /tmp/ch17-isolation.php php:/tmp/ch17-isolation.php
docker compose exec php php /tmp/ch17-isolation.php
```

```php
<?php

declare(strict_types=1);

/**
 * Isolation levels are a lock schedule, not a snapshot.
 *
 * There is no MVCC in this engine — no row versions, no per-transaction
 * snapshot — so `READ COMMITTED` / `REPEATABLE READ` / `SERIALIZABLE`
 * differ only in which locks a SELECT takes and how long it holds them.
 * This script opens a real transaction with each level, runs one
 * statement, and then asks `LockManager` (as a foreign transaction id 999)
 * whether that statement's rows — or table — are now locked.
 *
 *   - READ COMMITTED: no read locks at all, so the foreign lock is granted
 *     immediately, for a single-row lookup and a full scan alike.
 *   - REPEATABLE READ: every row the statement read is SHARED-locked until
 *     the transaction ends, so a foreign EXCLUSIVE row lock is refused, but
 *     the table itself stays free.
 *   - SERIALIZABLE: like REPEATABLE READ, plus a SHARED table lock when the
 *     statement scanned the whole table (no WHERE, hence no index) — the
 *     coarse guard against phantom rows a concurrent INSERT could add.
 *     An indexed single-row lookup never takes that table lock.
 *
 * Then ROLLBACK, and the same foreign lock is granted again: every lock a
 * transaction held is released together when it ends.
 *
 * One more thing this script has to be careful about: `SELECT` results are
 * lazy generators, so a read nobody iterates visits no rows and takes no
 * row locks. The indexed lookup below is therefore pulled through
 * explicitly — and one REPEATABLE READ read is left uniterated on purpose,
 * right after, to show the difference.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch17-isolation.php php:/tmp/ch17-isolation.php
 *   docker compose exec php php /tmp/ch17-isolation.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\TransactionException;
use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Storage\RecordId;
use PhpMiniDatabase\Transaction\LockManager;
use PhpMiniDatabase\Transaction\LockMode;

$directory = sys_get_temp_dir() . '/ch17-isolation-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$executor = new Executor($database);
$executor->run('CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20))');
$executor->run("INSERT INTO t (id, name) VALUES (1, 'alice'), (2, 'bob')");

$locks = $database->locks();
$levels = ['READ COMMITTED', 'REPEATABLE READ', 'SERIALIZABLE'];

/** Runs a SELECT and pulls every row through it — operators here are lazy generators. */
$read = static function (Executor $executor, string $sql): int {
    return count(iterator_to_array($executor->run($sql)->rows));
};

/** The RecordId of the row with id = 1, straight from the heap. */
$row = null;

foreach ($database->heapFile('t')->scan() as $id => $record) {
    if ($database->table('t')->deserializeRow($record)->get('id') === 1) {
        $row = $id;
        break;
    }
}

if (!$row instanceof RecordId) {
    throw new RuntimeException('row id=1 not found');
}

/** Asks LockManager for a foreign transaction's lock; "granted" means the read left the resource free. */
$probe = static function (callable $work): string {
    try {
        $work();

        return 'granted';
    } catch (TransactionException) {
        return 'refused';
    }
};

// ---------------------------------------------------------------------
// 1. An indexed single-row lookup: WHERE id = 1 uses the primary key.
// ---------------------------------------------------------------------

printf("--- SELECT name FROM t WHERE id = 1 ---\n");

foreach ($levels as $level) {
    $executor->run('BEGIN ISOLATION LEVEL ' . $level);
    $read($executor, 'SELECT name FROM t WHERE id = 1');

    $outcome = $probe(static fn () => $locks->acquireRowLock('t', $row, 999, LockMode::EXCLUSIVE));

    printf("%-15s foreign EXCLUSIVE on the read row: %s\n", $level, $outcome);

    $executor->run('ROLLBACK');

    $after = $probe(static fn () => $locks->acquireRowLock('t', $row, 999, LockMode::EXCLUSIVE));

    printf("%-15s the same lock after ROLLBACK:      %s\n", $level, $after);

    $locks->releaseAll(999);
}

// ---------------------------------------------------------------------
// 1b. The same REPEATABLE READ, with the result never iterated: the
//     operators are lazy generators, so no row was ever visited and no
//     row lock was ever taken.
// ---------------------------------------------------------------------

printf("\n--- REPEATABLE READ, result not iterated ---\n");

$executor->run('BEGIN ISOLATION LEVEL REPEATABLE READ');
$executor->run('SELECT name FROM t WHERE id = 1');

$unread = $probe(static fn () => $locks->acquireRowLock('t', $row, 999, LockMode::EXCLUSIVE));

printf("%-15s foreign EXCLUSIVE on the read row: %s\n", 'REPEATABLE READ', $unread);

$executor->run('ROLLBACK');

// ---------------------------------------------------------------------
// 2. A full scan: no WHERE means SeqScan, which is what SERIALIZABLE's
//    table lock exists for.
// ---------------------------------------------------------------------

printf("\n--- SELECT name FROM t (no WHERE) ---\n");

foreach ($levels as $level) {
    $executor->run('BEGIN ISOLATION LEVEL ' . $level);
    $read($executor, 'SELECT name FROM t');

    $rowOutcome = $probe(static fn () => $locks->acquireRowLock('t', $row, 999, LockMode::EXCLUSIVE));
    $tableOutcome = $probe(static fn () => $locks->acquireTableLock('t', 999, LockMode::EXCLUSIVE));

    printf("%-15s foreign row lock: %-10s foreign table lock: %s\n", $level, $rowOutcome, $tableOutcome);

    $executor->run('ROLLBACK');
    $locks->releaseAll(999);
}

// ---------------------------------------------------------------------
// 3. The refusal itself, verbatim.
// ---------------------------------------------------------------------

printf("\n--- the message a refused lock raises ---\n");

$executor->run('BEGIN ISOLATION LEVEL REPEATABLE READ');
$read($executor, 'SELECT name FROM t WHERE id = 1');

try {
    $locks->acquireRowLock('t', $row, 999, LockMode::EXCLUSIVE);
} catch (TransactionException $e) {
    printf("%s\n", $e->getMessage());
}

$executor->run('ROLLBACK');

$database->close();
```

**Перевод комментариев:**
- `ch17-isolation.php` (PHPDoc): уровни изоляции — это расписание блокировок, а
  не снимок. MVCC в этом движке нет — ни версий строк, ни снимка на транзакцию,
  — поэтому `READ COMMITTED`/`REPEATABLE READ`/`SERIALIZABLE` различаются
  только тем, какие блокировки берёт `SELECT` и как долго их держит. Этот скрипт
  открывает настоящую транзакцию с каждым уровнем, выполняет одно утверждение и
  затем спрашивает `LockManager` (от имени чужой транзакции 999), заблокированы
  ли теперь строки этого утверждения — или таблица: `READ COMMITTED` не берёт
  блокировок чтения вовсе, поэтому чужая блокировка выдаётся сразу, и для
  точечного поиска по индексу, и для полного скана; `REPEATABLE READ` блокирует
  в `SHARED` каждую прочитанную утверждением строку до конца транзакции, поэтому
  чужой `EXCLUSIVE` на строку отвергается, но сама таблица остаётся свободной;
  `SERIALIZABLE` — как `REPEATABLE READ`, плюс `SHARED`-блокировка таблицы,
  когда утверждение сканировало всю таблицу (без `WHERE`, значит без индекса), —
  грубая защита от фантомных строк, которые мог бы добавить конкурентный
  `INSERT`; поиск одной строки по индексу эту блокировку таблицы никогда не
  берёт. Затем `ROLLBACK`, и та же чужая блокировка снова выдаётся: все
  блокировки, которые держала транзакция, освобождаются вместе при её
  завершении. Ещё одна вещь, о которой скрипт должен заботиться: результаты
  `SELECT` — ленивые генераторы, поэтому чтение, которое никто не итерирует, не
  посещает строк и не берёт блокировок строк. Поэтому поиск по индексу ниже явно
  вытягивается — а одно чтение `REPEATABLE READ` сразу после него намеренно
  оставлено неитерированным, чтобы показать разницу.
- `$read` (PHPDoc): выполняет `SELECT` и вытягивает через него все строки —
  операторы здесь ленивые генераторы.
- `$row` (PHPDoc): `RecordId` строки с `id = 1`, прямо из кучи.
- `$probe` (PHPDoc): спрашивает у `LockManager` блокировку от имени чужой
  транзакции; «granted» означает, что чтение оставило ресурс свободным.
- `// 1. An indexed single-row lookup: WHERE id = 1 uses the primary key.` —
  поиск одной строки по индексу: `WHERE id = 1` использует первичный ключ.
- `// 1b. The same REPEATABLE READ, with the result never iterated: the` — тот
  же `REPEATABLE READ`, но результат никогда не итерировался: операторы —
  ленивые генераторы, поэтому ни одна строка не была посещена и ни одна
  блокировка строки не была взята.
- `// 2. A full scan: no WHERE means SeqScan, which is what SERIALIZABLE's` —
  полный скан: отсутствие `WHERE` означает `SeqScan`, ради чего и существует
  блокировка таблицы у `SERIALIZABLE`.
- `// 3. The refusal itself, verbatim.` — сам отказ, дословно.

```text
--- SELECT name FROM t WHERE id = 1 ---
READ COMMITTED  foreign EXCLUSIVE on the read row: granted
READ COMMITTED  the same lock after ROLLBACK:      granted
REPEATABLE READ foreign EXCLUSIVE on the read row: refused
REPEATABLE READ the same lock after ROLLBACK:      granted
SERIALIZABLE    foreign EXCLUSIVE on the read row: refused
SERIALIZABLE    the same lock after ROLLBACK:      granted

--- REPEATABLE READ, result not iterated ---
REPEATABLE READ foreign EXCLUSIVE on the read row: granted

--- SELECT name FROM t (no WHERE) ---
READ COMMITTED  foreign row lock: granted    foreign table lock: granted
REPEATABLE READ foreign row lock: refused    foreign table lock: granted
SERIALIZABLE    foreign row lock: refused    foreign table lock: refused

--- the message a refused lock raises ---
Cannot acquire a an exclusive lock on row 0:0 in "t": held by another transaction.
```

Результат читается как таблица из раздела 2, только наоборот: «granted» —
блокировки нет, «refused» — она взята. Под `READ COMMITTED` поиск по первичному
ключу не оставляет за собой ничего, и чужой `EXCLUSIVE` проходит сразу. Под
`REPEATABLE READ` и `SERIALIZABLE` та же строка занята до конца транзакции — но
после `ROLLBACK` отпускается, и строка снова свободна.

Полный скан (`SELECT name FROM t` без `WHERE`) показывает разницу между двумя
последними уровнями: `REPEATABLE READ` держит строки, но таблицу не берёт, а
`SERIALIZABLE` занимает и её — и чужой `EXCLUSIVE` на таблицу отвергается. Это
та самая защита от фантомов: пока транзакция сканирует, вставка в таблицу не
пройдёт. Обратите внимание, что индексный поиск под `SERIALIZABLE` таблицу
**не** занимает: значение за границами диапазона индекса всё равно не могло
совпасть, фантома не будет.

Врезка про лень — самый полезный урок этого эксперимента. Отдельный прогон
`REPEATABLE READ`, чей результат **не вытягивали**, не оставляет блокировок:
`LockRows` — обычный генератор из главы 16, он берёт блокировку в момент выдачи
строки, а неначатый генератор не выдал ничего. Так работает честная ленивость:
если никто не читает результат, читать и блокировать нечего. Это же означает,
что форму строки «сколько блокировок взято» определяет не запрос, а способ его
потребления.

### 4. Точки сохранения: откатить часть, не завершая транзакцию

Последний вопрос — как откатывать не всё. Скрипт ведёт транзакцию через четыре
вставки, две точки сохранения и два вида отката, печатая содержимое таблицы
после каждого шага:

```bash
docker compose cp /tmp/ch17-savepoints.php php:/tmp/ch17-savepoints.php
docker compose exec php php /tmp/ch17-savepoints.php
```

```php
<?php

declare(strict_types=1);

/**
 * Savepoints: undoing part of a transaction without ending it.
 *
 * A transaction's in-memory log is what makes `ROLLBACK TO SAVEPOINT`
 * possible at all — each savepoint remembers a position in that log, so
 * "undo since s1" means replaying exactly the records appended after it,
 * newest first, through the same physical undo path a full ROLLBACK uses.
 *
 * The two operations differ on the named savepoint itself, and this script
 * shows both:
 *
 *   - ROLLBACK TO s1 undoes every change since s1 and keeps s1 usable, so
 *     the same savepoint can be rolled back to again;
 *   - RELEASE s2 forgets s2 (and any savepoint after it) without undoing a
 *     single row, so the changes stand and only the ability to return to
 *     that point is gone.
 *
 * It also fixes where savepoints live: `SAVEPOINT` outside an open
 * transaction is refused, and `COMMIT` with nothing open is refused too —
 * a savepoint is a position in one transaction's history, so there is no
 * meaningful version of either without that transaction.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch17-savepoints.php php:/tmp/ch17-savepoints.php
 *   docker compose exec php php /tmp/ch17-savepoints.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\TransactionException;
use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = sys_get_temp_dir() . '/ch17-savepoints-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$executor = new Executor($database);
$executor->run('CREATE TABLE t (id INT PRIMARY KEY, note VARCHAR(20))');

$contents = static function (Executor $executor): string {
    $rows = array_map(
        static fn (Row $row): string => sprintf('%d=%s', $row->get('id'), $row->get('note')),
        iterator_to_array($executor->run('SELECT id, note FROM t ORDER BY id')->rows),
    );

    return $rows === [] ? '(no rows)' : implode(', ', $rows);
};

$step = static function (string $label, callable $work) use ($executor, $contents): void {
    try {
        $work();
        printf("%-34s -> rows: %s\n", $label, $contents($executor));
    } catch (TransactionException $e) {
        printf("%-34s -> %s\n", $label, $e->getMessage());
    }
};

$step('SAVEPOINT s1 (before BEGIN)', static fn () => $executor->run('SAVEPOINT s1'));

$executor->run('BEGIN');

$executor->run("INSERT INTO t (id, note) VALUES (1, 'one')");
$step('BEGIN; INSERT id=1', static fn () => null);

$executor->run('SAVEPOINT s1');
$step('SAVEPOINT s1', static fn () => null);

$executor->run("INSERT INTO t (id, note) VALUES (2, 'two')");
$step('INSERT id=2', static fn () => null);

$step('ROLLBACK TO SAVEPOINT s1', static fn () => $executor->run('ROLLBACK TO SAVEPOINT s1'));

$executor->run("INSERT INTO t (id, note) VALUES (3, 'three')");
$executor->run('SAVEPOINT s2');
$executor->run("INSERT INTO t (id, note) VALUES (4, 'four')");
$step('INSERT 3, SAVEPOINT s2, INSERT 4', static fn () => null);

$step('RELEASE SAVEPOINT s2', static fn () => $executor->run('RELEASE SAVEPOINT s2'));

$step('ROLLBACK TO SAVEPOINT s1', static fn () => $executor->run('ROLLBACK TO SAVEPOINT s1'));

$step('ROLLBACK TO SAVEPOINT ghost', static fn () => $executor->run('ROLLBACK TO SAVEPOINT ghost'));

$executor->run("INSERT INTO t (id, note) VALUES (5, 'five')");
$step('INSERT id=5, then COMMIT', static function () use ($executor): void {
    $executor->run('COMMIT');
});

$step('COMMIT with nothing open', static fn () => $executor->run('COMMIT'));

$executor->run("INSERT INTO t (id, note) VALUES (6, 'six')");
$step('INSERT id=6 (autocommit)', static fn () => null);

$database->close();
```

**Перевод комментариев:**
- `ch17-savepoints.php` (PHPDoc): точки сохранения: отменить часть транзакции,
  не завершая её. Именно журнал транзакции в памяти делает
  `ROLLBACK TO SAVEPOINT` возможным: каждая точка сохранения запоминает позицию
  в этом журнале, поэтому «отменить всё после s1» означает воспроизвести ровно
  записи, добавленные после неё, от новых к старым, через тот же физический путь
  отмены, которым пользуется полный `ROLLBACK`. Две операции различаются судьбой
  самой именованной точки, и скрипт показывает обе: `ROLLBACK TO s1` отменяет
  каждое изменение после `s1` и оставляет `s1` годной к использованию, так что к
  той же точке можно откатиться снова; `RELEASE s2` забывает `s2` (и любую точку
  после неё), не отменяя ни одной строки, так что изменения остаются, а исчезает
  только возможность вернуться в эту точку. Скрипт также фиксирует, где живут
  точки сохранения: `SAVEPOINT` вне открытой транзакции отвергается, и `COMMIT`
  без открытой транзакции тоже отвергается — точка сохранения есть позиция в
  истории одной транзакции, поэтому без неё нет осмысленной версии ни того, ни
  другого.

```text
SAVEPOINT s1 (before BEGIN)        -> No transaction is active.
BEGIN; INSERT id=1                 -> rows: 1=one
SAVEPOINT s1                       -> rows: 1=one
INSERT id=2                        -> rows: 1=one, 2=two
ROLLBACK TO SAVEPOINT s1           -> rows: 1=one
INSERT 3, SAVEPOINT s2, INSERT 4   -> rows: 1=one, 3=three, 4=four
RELEASE SAVEPOINT s2               -> rows: 1=one, 3=three, 4=four
ROLLBACK TO SAVEPOINT s1           -> rows: 1=one
ROLLBACK TO SAVEPOINT ghost        -> No such savepoint "ghost".
INSERT id=5, then COMMIT           -> rows: 1=one, 5=five
COMMIT with nothing open           -> No transaction is active.
INSERT id=6 (autocommit)           -> rows: 1=one, 5=five, 6=six
```

Первая строка — отказ: `SAVEPOINT` вне транзакции не имеет позиции в истории,
которую мог бы запомнить. Дальше видна работа с индексом лога: после `BEGIN` и
вставки строки 1 объявлена `s1`; вставка строки 2; откат к `s1` физически
убирает вторую строку (`1=one`), при этом `s1` остаётся и работает во второй
раз.

Самая показательная строка — `RELEASE SAVEPOINT s2`: содержимое таблицы не
изменилось вовсе (`1=one, 3=three, 4=four`). Это отличает `RELEASE` от
`ROLLBACK TO`: первый забывает точку, второй к ней возвращает. И следующий откат
к `s1` убирает уже всё, что было занесено после неё, — и строку 3, и строку 4, —
оставляя только `1=one`. Откат к несуществующей точке даёт
`No such savepoint "ghost".`, `COMMIT` без открытой транзакции —
`No transaction is active.`, а вставка `id=6` после `COMMIT` снова работает как
автокоммит: одна строка — одна транзакция, никем не названная.

## 5. Отказы

| Что ломается                                         | Чем обнаруживается                                                                         | Что происходит                                                                                                            |
| ---------------------------------------------------- | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------- |
| Второй `BEGIN`, пока открыт первый                   | `TransactionException`: `A transaction is already active; COMMIT or ROLLBACK it first.`    | отказ вместо неявного `COMMIT` первой транзакции                                                                          |
| Одиночная запись при чужой открытой транзакции       | `Another connection has an open transaction; try again once it finishes.`                  | не «молча присоединиться» к чужой транзакции, а отказ (эксперимент 1)                                                     |
| `COMMIT`/`ROLLBACK` чужой транзакции                 | `…only the connection that started it may COMMIT, ROLLBACK, or manage its savepoints.`     | владелец — непрозрачный токен, проверяемый по идентичности                                                                |
| `COMMIT`/`ROLLBACK`/`SAVEPOINT` без транзакции       | `No transaction is active.`                                                                | отказ, а не молчаливый успех (эксперимент 4)                                                                              |
| Откат к несуществующей точке                         | `No such savepoint "ghost".`                                                               | весь `ROLLBACK TO` отвергается целиком                                                                                    |
| Конфликт блокировок                                  | `Cannot acquire a an exclusive lock on row 0:0 in "t": held by another transaction.`       | мгновенный отказ: ни очереди, ни таймаута (эксперименты 2–3)                                                              |
| Потерянная запись                                    | строка, занятая под `SHARED`, не даёт чужому `EXCLUSIVE`                                   | запись под любым уровнем берёт `EXCLUSIVE` на строку до конца транзакции                                                  |
| Фантом под `SERIALIZABLE`                            | полный скан держит `SHARED`-блокировку таблицы, `INSERT` берёт `EXCLUSIVE` (эксперимент 3) | другая транзакция не вставит строку, пока скан не закончится                                                              |
| Уровень изоляции при автокоммитном `SELECT`          | блокировок нет вовсе                                                                       | у утверждения нет «следующего», которое надо защищать; чужой уровень не наследуется                                       |
| `SELECT` с `JOIN` под `REPEATABLE READ`              | блокировки строк не берутся                                                                | после слияния у строки нет пер-табличного `RecordId` — названный пробел                                                   |
| Чтение незакоммиченной строки                        | она видна другому соединению (эксперимент 1)                                               | нет MVCC: умолчание `READ COMMITTED` не прячет чужие незакоммиченные данные                                               |
| `ROLLBACK` после `CREATE TABLE`                      | таблица остаётся                                                                           | `DDL` применяется немедленно и в журнал не пишется                                                                        |
| Сессия оборвалась с открытой транзакцией             | `Session::close()` откатывает именно её                                                    | иначе все последующие `BEGIN` были бы отвергнуты навсегда                                                                 |
| Сбой между мутацией и её записью в журнал            | узкое окно, названный компромисс                                                           | куча и индекс меняются до записи в журнал; `commit()`/`rollback()` синхронизируют страницы до своего маркера (→ глава 18) |
| Сбой между записью `COMMIT` и синхронизацией страниц | `testTheSyncHandlerRunsBeforeTheCommitRecordIsEvenWritten`                                 | порядок исправлен: синхронизация идёт **до** маркера, а не после (→ глава 18)                                             |
| Ошибка в тексте сообщения о блокировке               | `a an exclusive` вместо `an exclusive`                                                     | косметическая неточность: формат `'a %s lock'` плюс подстановка артикля                                                   |

Три строки таблицы описывают не механику, а историю дефектов. Баг с владельцем
воспроизведён в `docs/DECISIONS.md` дословно:

```text
A: BEGIN; INSERT id=1 (uncommitted)
B: INSERT id=2 (no BEGIN of its own)   -> SUCCEEDED, silently inside A's transaction
B: COMMIT (never opened one)            -> SUCCEEDED, committing both rows
A: COMMIT                               -> FAILED: "No transaction is active"
```

Причина ровно та, которую чинили: `withTransaction()` решал, открывать ли
собственную транзакцию, по вопросу «открыта ли хоть какая-нибудь», а не «моя ли
она». Второе утверждение B находило `inTransaction()` истинным, считало себя
не-автокоммитным и исполнялось внутри чужой транзакции, ни разу не вызвав
`begin()`. Добавленный токен владельца закрывает и это, и `COMMIT` из B, и
наследование чужого уровня изоляции при чтении.

## 6. Как это проверено

`tests/Unit/Transaction/` — 70 тестов, 103 утверждения, и они делятся почти
ровно по классам главы.

`LockManagerTest` — 10 тестов на 10 утверждений (по одному «не бросило» на
сценарий) и самая наглядная таблица совместимости в проекте:
`testASingleTransactionCanAcquireASharedThenAnExclusiveLockOnItsOwnRow`,
`testTwoTransactionsCanShareTheSameRow`,
`testAnExclusiveLockExcludesEvenAnotherSharedHolder`,
`testUpgradingFromSharedToExclusiveFailsWhileAnotherTransactionAlsoHoldsShared`,
`testASharedLockIsRefusedWhileAnotherTransactionHoldsExclusive`,
`testReleaseAllFreesEveryResourceForThatTransaction`,
`testTableAndRowLocksAreIndependentResources` и
`testRepeatedlyAcquiringTheSameLockIsIdempotent` — вся таблица совместимости
режимов, которую эксперимент 2 показывает разом.

`TransactionTest` — 10 тестов на 15 утверждений; здесь проверяется именно индекс
лога: `testRecordsSinceASavepointExcludesWhatCameBeforeIt`,
`testTruncateToSavepointKeepsTheSavepointItselfUsable`,
`testTruncateToSavepointForgetsLaterSavepointsToo`,
`testReleaseSavepointForgetsItButKeepsTheLog`,
`testReleaseSavepointAlsoForgetsLaterSavepoints` и
`testAnUnknownSavepointThrows` — эксперимент 4 без единой вставки строки.

`TransactionManagerTest` — 34 теста на 49 утверждений, и это глава целиком:
`testANestedBeginIsRejected`, вся серия
`testAnotherOwnerCannot{Commit,RollBack,Savepoint}TheOpenTransaction` и
`testTheOwnerThatBeganItCanStillCommitIt`,
`testIsOwnedByIsFalseWhenNothingIsOpen`,
`testOwnershipIsForgottenOnceTheTransactionEnds`,
`testCommitReleasesEveryLockTheTransactionHeld`, а также
`testTheSyncHandlerRunsBeforeTheCommitRecordIsEvenWritten` — тот самый порядок,
который упомянут в разделе 2 и разобран в главе 18.

`tests/Unit/Execution/ExecutorTransactionTest.php` — 21 тест на 41 утверждение,
уровень SQL: `testRollbackUndoesAn{Insert,Update,Delete}`,
`testASavepointCanBeReusedAfterRollingBackToIt`, `testAMultiRowInsertIsAtomic`,
`testDdlIsNotTransactional`,
`testInsertIsBlockedByAConcurrentlyHeldSharedTableLock`,
`testRepeatableReadHoldsASharedRowLockUntilTheTransactionEnds` (тот же факт, что
и в эксперименте 3, но внутри одного процесса и без прямого `LockManager`),
`testReadCommittedHoldsNoLockPastTheStatement` и
`testASecondExecutorSeesTheFirstsOpenTransaction` — эксперимент 1 одним тестом.
Остальные восемь — про идемпотентность отката и повторный проход восстановления:
`testARollbackRetriedAfterAFailedSyncDoesNotUndoTheSameChangeTwice`,
`testACrashBetweenARollbacksUndoAndItsWalRecordStillRecoversCleanly`,
`testRecoveryRepairsAnIndexLeftBehindByAHalfAppliedUndo`.

Живые отказы проверяются на реальных перезапусках процесса:
`tests/Integration/TransactionTest.php` — 4 теста на 8 утверждений
(`testAnUncommittedTransactionIsUndoneAfterACrashAndRestart`,
`testACommittedTransactionSurvivesACrashAndRestart`,
`testARollbackToASavepointBeforeACrashLeavesOnlyThePreSavepointWork`,
`testMultipleRestartsEachRecoverCleanlyFromTheLastCommittedState`). Это
территория главы 18, но именно эти тесты делают утверждение «откат опирается на
тот же журнал» проверяемым.

Два интеграционных теста бьют в самую сердцевину главы через настоящий сервер:
`ServerClientTest::testASecondConnectionCannotJoinOrCommitAnotherConnectionsOpenTransaction`
(баг с владельцем, доказанный против `bin/minidb-server`) и
`testAnUncommittedInsertIsAlreadyVisibleToAnotherConnectionAndDisappearsOnRollback`
(грязное чтение, доказанное против живого сервера). Там же —
`testOnlyOneConnectionAtATimeCanHaveAnOpenTransaction`. Всего `ServerClientTest`
— 7 тестов на 21 утверждение, плюс `ConcurrencyTest`/`EmbeddedConcurrencyTest` —
3 теста на 23 утверждения: несколько процессов на одном каталоге данных не
теряют подтверждённых вставок.

Прогоны при написании главы (Docker, PHP 8.5.11): `tests/Unit/Transaction` — OK,
70 тестов, 103 утверждения; `ExecutorTransactionTest` — OK, 21/41;
`tests/Integration/TransactionTest.php` — OK, 4/8; `ConcurrencyTest` +
`EmbeddedConcurrencyTest` — OK, 3/23; `ServerClientTest` — OK, 7/21; `make test`
— OK, **1082 теста, 3338 утверждений** (набор уже включает интеграционные;
полный `phpunit` добавляет бенчмарки — 1093 теста, 3354 утверждения).

## 7. Цифры

Размеры кода главы (все числа — из файлов релиза `v1.1.0`):

| Файл                                     | Строк | Что делает                                                          |
| ---------------------------------------- | ----- | ------------------------------------------------------------------- |
| `src/Transaction/IsolationLevel.php`     | 41    | три уровня, каждый — набор блокировок                               |
| `src/Transaction/LockMode.php`           | 18    | `SHARED`/`EXCLUSIVE`                                              |
| `src/Transaction/LockManager.php`        | 87    | таблица «ресурс → (txId → режим)», отказ вместо ожидания            |
| `src/Transaction/Transaction.php`        | 116   | лог изменений и позиции точек сохранения                            |
| `src/Transaction/TransactionManager.php` | 379   | `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT` и восстановление            |
| `src/Transaction/Wal.php`                | 257   | журнал записей на диске (→ глава 18)                                |
| `src/Exception/TransactionException.php` | 15    | один класс на все транзакционные отказы                             |
| `src/Execution/Operator/LockRows.php`    | 42    | блокировка строка-за-строкой в конвейере                            |
| `src/Infrastructure/FileLock.php`        | 108   | межпроцессная блокировка — ждёт таймаут, в отличие от `LockManager` |

Живые прогоны этой главы (Docker, PHP 8.5.11; все четыре скрипта
детерминированы, повторный прогон каждого даёт те же байты):

| Что                                 | Значение                                                                                                            |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `ch17-ownership`                    | 15 строк; три отказа с разными сообщениями; грязное чтение: `1, 2, 10` до `ROLLBACK`, `1, 2` после                  |
| `ch17-locks`                        | 17 строк; 9 раз «granted», 4 раза «refused»; таблица и строка — независимые ресурсы                                 |
| `ch17-isolation`                    | 18 строк; `READ COMMITTED` — 0 удержанных блокировок, `REPEATABLE READ` — строка, `SERIALIZABLE` — строка и таблица |
| `ch17-savepoints`                   | 12 строк; откат к `s1` убрал строку 2, `RELEASE s2` не убрал ничего, второй откат к `s1` убрал строки 3 и 4         |
| Тесты `tests/Unit/Transaction`      | 70 тестов, 103 утверждения                                                                                          |
| `ExecutorTransactionTest`           | 21 тест, 41 утверждение                                                                                             |
| `tests/Integration/TransactionTest` | 4 теста, 8 утверждений                                                                                              |
| `ServerClientTest`                  | 7 тестов, 21 утверждение                                                                                            |
| `make test` (unit + интеграционные) | 1082 теста, 3338 утверждений                                                                                        |
| Полный `phpunit` (+ benchmark)      | 1093 теста, 3354 утверждения                                                                                        |

## 8. Упражнения

1. **Посчитайте.** Сколько записей журнала и сколько блокировок остаётся у
   транзакции из эксперимента 4 в момент `RELEASE SAVEPOINT s2`? Какие из них
   снимет `ROLLBACK TO SAVEPOINT s1`? (Ответ: три записи вставок — строки 1, 3 и
   4: запись о строке 2 уже снята первым откатом; блокировки строк 1, 3, 4 и
   таблицы; откат снимет записи строк 3 и 4 вместе с их блокировками, блокировка
   строки 1 останется.)
2. **Объясните.** Почему `LockManager` отказывает мгновенно, а не ждёт таймаут,
   — и при каком изменении движка этот отказ придётся заменить ожиданием? Что
   для этого должно появиться внутри процесса?
3. **Найдите ошибку.** Воспроизведите транскрипт из раздела 5 на бумаге: A
   открыл транзакцию, B без `BEGIN` вставляет строку, B делает `COMMIT`. Какая
   проверка срабатывает в исправленной версии на каждом из трёх шагов и почему
   именно `isOwnedBy()`, а не `inTransaction()`?
4. **Спроектируйте.** Блокировки чтения не берутся для `JOIN`. Что нужно
   добавить в `Qualify`/`NestedLoopJoin`, чтобы `REPEATABLE READ` покрыл и
   соединения, — и почему сохранить пер-табличный `RecordId` проще, чем
   блокировать «квалифицированную строку»?
5. **Сравните.** `SERIALIZABLE` берёт таблицу только на **полный** скан, а
   индексный поиск — нет. Придумайте запрос, где индексный поиск под
   `SERIALIZABLE` всё-таки допускает фантом, и объясните, существует ли такой
   запрос вообще в этой модели данных.
6. **Разберите отказ.** Три утверждения: `CREATE TABLE` внутри `BEGIN`,
   `SAVEPOINT` без `BEGIN` и одиночный `INSERT` из второго соединения, пока
   открыта чужая транзакция. Для каждого решите, что именно его отвергает —
   владение, отсутствие транзакции или молчаливое применение. Проверьте ответ по
   экспериментам 1 и 4 и таблице отказов.
7. **Посчитайте.** `REPEATABLE READ`, читающий 1000 строк, берёт 1000
   блокировок. Сколько памяти займёт таблица блокировок, если запись — это
   строка `row:t:<page>:<slot>` и один байт режима, — и что произойдёт с этим
   числом, если тот же `SELECT` запустить без `BEGIN` при любом уровне изоляции?

## Что дальше

- **Глава 18** — WAL, fsync и восстановление после сбоя: вторая половина этой
  машины. Запись в журнал идёт после мутации, `commit()` синхронизирует страницы
  до своего маркера, а незавершённая транзакция откатывается при следующем
  открытии базы.
- **Глава 19** — анализ текста: Unicode, токены, стемминг — другой предмет, но
  тот же принцип «сначала разобрать в данные, потом решать».
- **Глава 20** — инвертированный индекс и BM25 в `php-mini-search`:
  версионирование и надгробия как замена блокировкам на уровне данных.
- **Глава 26** — как проверять тесты: почему зелёный тест на транзакцию ещё не
  доказывает, что он способен упасть.
