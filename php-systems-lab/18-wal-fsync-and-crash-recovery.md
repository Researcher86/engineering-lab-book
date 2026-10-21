# Глава 18. WAL, fsync и восстановление после сбоя

> Проекты:
> [`php-mini-database`](https://github.com/Researcher86/php-mini-database)
> (релиз `v1.1.0`, коммит `9adde5d`) и
> [`php-mini-search`](https://github.com/Researcher86/php-mini-search) (релиз
> `v1.0.0`, коммит `7ecddc0`). Нужно знать: страницы и кучу (→ глава 15),
> порядок «сначала мутация, потом журнал» (→ глава 16) и откат с блокировками (→
> глава 17). Пригодятся дальше: версии, надгробия и переиндексация (→ глава 21)
> и как проверять сами тесты `kill -9` (→ глава 26).

Глава 17 закончилась на обещании: `COMMIT` возвращается — значит, данные на
диске. Глава 16 — на другом: строка попадает в кучу **до** того, как о ней
появляется запись в журнале. Оба предложения что-то говорят о порядке записей, и
оба проверить, не разорвав процесс на середине, невозможно. Эта глава и есть
проверка: что именно обещано, чем это обещание обеспечено и что остаётся после
`kill -9`.

## 1. Проблема

Надёжность — это обещание о **порядке**, а не о количестве записанного.
`fwrite()` кладёт байты в буфер процесса, `fflush()` — в кэш страниц ядра, и
только `fsync()` просит устройство их записать. Пока не сделан последний шаг,
данные существуют ровно настолько, насколько ОС решит их сохранить. Из этого
следуют четыре разные опасности — и они не сводятся одна к другой.

**Опасность первая: сбой теряет то, что уже подтверждено.** Если сервис ответил
«записано», а страница к этому моменту прошла только `fwrite()`, то на пути от
подтверждения до устройства нет ни одного барьера. Это не теоретическая дыра: в
`php-mini-database` она существовала буквально — `PageManager::sync()` был
написан, но вызывался только из `VACUUM`, а журнал после `COMMIT` уже усекался.
Потеря при этом хуже, чем «пропала последняя запись»: журнал тоже пуст, и
восстанавливать её не из чего.

**Опасность вторая: сбой посреди записи.** Запись длиной в строку может быть
прервана на любом байте. Значит, у любого журнала есть состояние «последняя
запись оборвана» — и его нельзя путать с «в середине файла мусор». Первое — это
нормальный конец файла после сбоя, его надо просто отрезать; второе — потеря
данных, и обрезка по нему молча выбросит все подтверждённые записи, которые шли
после.

**Опасность третья: незавершённая работа осталась в файлах.** Строки уже лежат в
куче, индексы уже перестроены, а маркера `COMMIT` в журнале нет. Просто
«оставить как есть» нельзя: движок обязан вернуть базу к последнему
подтверждённому состоянию — и сделать это так, чтобы повторный проход по тем же
записям не сломался, если восстановление само будет прервано.

**Опасность четвёртая: восстановление — это тоже код, который может упасть.**
Оно пишет в кучу и индексы, то есть делает ровно то, что сбой только что
прервал. Если восстановление не идемпотентно, то сбой во время восстановления
оставляет базу, которую больше нельзя открыть: при следующем запуске
восстановление попытается откатить уже откаченное.

Два проекта этой главы решают эти четыре задачи по-разному, и разница полезна
именно потому, что она принципиальная. `php-mini-database` хранит данные в куче
и индексах, а журнал использует как описание **незавершённого**: восстановление
только откатывает (`UNDO`), потому что всё подтверждённое уже на месте.
`php-mini-search` не хранит индекс на диске вовсе: единственное надёжное
состояние — журнал, индекс перестраивается из него при каждом старте
(`REDO`/переигрывание). Отсюда и разные форматы: первому достаточно JSON-строк,
потому что читает их только он сам и только после своей же записи; второму нужен
самоописывающийся формат с длиной и контрольной суммой, потому что «индекс,
построенный из журнала» обязан уметь прочитать журнал, который оборвался на
середине.

## 2. Устройство

### Журнал как описание незавершённого (`php-mini-database`)

```text
statement
   │
   ├─ heap / index mutation            (chapter 16: change first)
   │        │
   │        └─ WalRecord: {lsn, tx, op, table, row_id, before, after}
   │                 │
   │                 └─ append() → fwrite + fflush + fsync   ← durable now
   │
   └─ COMMIT / ROLLBACK
            │
            ├─ syncStorage()   ← every open heap file and index, fsync'd
            └─ append(COMMIT)  ← the marker recovery looks for
                     │
                     └─ checkpoint() → truncate the log to 0 bytes
```

Порядок здесь не случайный. **Маркер завершения пишется последним, уже после
`syncStorage()`.** Это и есть инвариант, на котором держится восстановление: «в
журнале есть `COMMIT`» означает «данные, которые он покрывает, уже на
устройстве». Обратное направление никому не нужно — REDO нет, потому что
переделывать нечего: если `COMMIT` до устройства не дошёл, транзакция считается
незавершённой и откатывается, а если дошёл, то всё, что он покрывает, уже
записано.

Отсюда же — устройство записи. У каждой строки в журнале два набора значений,
`before` и `after`, хотя пишется только одно изменение: движок умеет
**отменять**, а не повторять, и для отмены нужны старые значения. Запись
логическая, а не физическая: «строка X в таблице Y была такой, стала такой», а
не «такие-то байты такой-то страницы». Поэтому откат идёт через те же
`Table`/`HeapFile`/`IndexMaintainer`, что и обычный `INSERT`/`UPDATE`/`DELETE`,
а не через восстановление сырых страниц.

Восстановление (`TransactionManager::recover()`) для каждой транзакции, у
которой есть `BEGIN` и нет `COMMIT`/`ROLLBACK`, собирает её живые изменения и
отменяет их от новых к старым, затем синхронизирует хранилище и усекает журнал.
Живые изменения — не «все записи этой транзакции»: процесс мог успеть выполнить
`ROLLBACK TO SAVEPOINT` перед тем, как умереть, и тогда его собственный откат
уже сделан физически. Поэтому восстановление прогоняет записи через **тот же
`Transaction`**, что и живой откат, — и получает ровно тот список отмен, который
был в памяти погибшего процесса.

Три честно названные границы:

- **Окно между мутацией и её записью в журнал.** Краш ровно в этот момент не
  восстановим: изменение на диске есть, описания нет. Узкое, но реальное; цена
  того, что `RecordId` новой строки известен только после самой вставки.
- **Восстановление не crash-safe посреди прохода.** Сбой между двумя отменами не
  отслеживается. Спасает идемпотентность: и `undoInsert`, и `undoDelete`, и
  `undoUpdate` считают «уже откачено» успехом, а не ошибкой, поэтому следующий
  запуск просто доводит работу до конца.
- **Полных образов страниц нет.** Предполагается, что запись страницы либо
  произошла, либо нет; страницу, порванную посередине, починить нечем.

### Журнал как единственное состояние (`php-mini-search`)

```text
PUT /documents
   │
   ├─ append(record)
   │       │
   │       └─ frame: uint32(length) uint32(crc32) body → fflush + fsync
   │
   └─ index in memory updated   ← derived; never on disk

startup
   │
   ├─ flock(<log>.lock)         ← service, or offline compact/check: never two
   ├─ LogScanner::scan()        ← front to back, every valid record replayed
   └─ the ending decides:
        last record short / bad checksum  → torn tail: truncate it, keep the rest
        a bad record before the last one  → damage: keep the file aside, start empty
        a prefix of the header            → missing: first start
        a length above 256 KB             → damage, wherever it is
```

Здесь индекс — производные данные, и это меняет всё. Восстановления «в обратную
сторону» не существует: переигрывание — это построение индекса заново из
журнала, поэтому обработка обрыва сводится к двум решениям. Первое: **оборванная
последняя запись — это нормальный конец файла.** Каждая запись была
`fsync()`-нута до того, как её подтвердили клиенту, значит незакрытой может быть
максимум одна — последняя. Её можно отрезать, ничего не потеряв. Второе:
**плохая запись не в конце — это не обрыв, а порча.** Обрезать по ней означало
бы выбросить все подтверждённые записи после неё, поэтому журнал целиком
откладывается в сторону (`<log>.damaged-<дата>` — никогда не удаляется) и сервис
стартует с пустым, требуя переиндексации.

Отдельная деталь — защита от подделки под обрыв: у записи с испорченным полем
длины `length` может указывать за конец файла, и такая запись выглядела бы
«обрезанной». Поэтому длина больше 256 КБ (максимум документа — 1 КБ заголовка
плюс 64 КБ тела) считается порчей **всегда**, где бы она ни лежала.

Доступ к журналу — ровно у одного процесса: `flock()` на отдельном файле
`<log>.lock`, потому что компактизация переименовывает новый файл поверх
журнала, и блокировка на самом журнале переименование бы не пережила.
Компактизация пишет во временный файл рядом, `fsync()`-ит его, переименовывает и
синхронизирует каталог — в любой момент на диске лежит один целый журнал: либо
старый, либо новый.

### Байты, которым можно верить

Оба проекта опираются на одни и те же свойства `fsync()`, и в `php-mini-search`
они записаны один раз, в `Durable`:

- `fflush()` **до** `fsync()`: после первого `fsync()` PHP буферизует поток в
  libc, и ошибка записи всплывёт только на следующей синхронизации;
- неудачный `fsync()` возвращает `false` — без `errno` и без предупреждения;
- каталог синхронизируется через `fopen($dir, 'r')` + `fsync()`;
- `rename()` атомарен только внутри одной файловой системы: между устройствами
  PHP молча копирует без синхронизации, поэтому временные файлы кладутся рядом с
  тем, что заменяют;
- на macOS PHP не использует `F_FULLFSYNC`, поэтому отключение питания там эти
  гарантии не переживает; на Linux — переживает.

И одна общая честная оговорка, которую оба проекта называют: `kill -9` — это не
отключение питания. После него кэш страниц ядра всё равно доедет до диска,
поэтому такой тест **не доказывает, что `fsync()` вообще вызывался**. Проверка
по-настоящему требует инжектора отказов уровня файловой системы (`dm-flakey`,
LazyFS) — это вне области обоих проектов.

## 3. Код

### Журнал: запись и чтение

Докблок `Wal` объясняет сразу и формат, и главное свойство: запись существует на
диске **до** того, как изменение доходит до кучи.

```php
/**
 * The write-ahead log: one growing file of JSON-lines records, in the
 * shape PLAN.md §6.5 shows — `{"lsn":1,"tx":1,"op":"BEGIN"}` and so on —
 * one record per line, each carrying its own sequence number.
 *
 * Every write goes through `append()` before the corresponding change
 * reaches the heap file, and every `append()` is flushed and `fsync()`'d
 * before it returns — the log line for a change exists on disk before the
 * change itself is made, which is the one property "write-ahead" promises
 * and the one thing recovery depends on.
 *
 * A row value can be a `DateTimeImmutable` or an arbitrary byte string
 * (`BLOB`), neither of which `json_encode()` handles on its own — a date
 * has no native JSON representation, and a byte string that is not valid
 * UTF-8 makes `json_encode()` fail outright. Both are wrapped in a small
 * tagged object (`{"__datetime__": "..."}`, `{"__base64__": "..."}`) on
 * the way out and unwrapped on the way in, so the log can hold exactly the
 * values `Schema\Row` does, not merely the ones JSON supports directly.
 *
 * There is no log rotation — `PLAN.md`'s own layout shows numbered
 * segments (`wal.0001.log`, `wal.0002.log`), but this implementation keeps
 * one file and reclaims space by truncating it outright once
 * `TransactionManager` confirms nothing is still relying on it (see
 * `checkpoint()` and DECISIONS.md), the same "space comes back only on an
 * explicit action" trade `HeapFile`'s `VACUUM` and `BTreeIndex`'s lack of
 * rebalancing already made elsewhere in this project.
 */
```

[`src/Transaction/Wal.php#L13-L39`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Wal.php#L13-L39)

**Перевод комментариев:**
- `Wal` (PHPDoc): журнал предзаписи — один растущий файл записей в формате
  JSON-lines, в форме из `PLAN.md` §6.5 (`{"lsn":1,"tx":1,"op":"BEGIN"}` и так
  далее), одна запись на строку, у каждой свой порядковый номер. Каждая запись
  проходит через `append()` до того, как соответствующее изменение доходит до
  файла кучи, и каждый `append()` делает flush и `fsync()` до возврата: строка
  журнала для изменения существует на диске раньше самого изменения — это
  единственное свойство, которое обещает «write-ahead», и единственное, на что
  опирается восстановление. Значением строки может быть `DateTimeImmutable` или
  произвольная байтовая строка (`BLOB`); `json_encode()` не справляется ни с
  тем, ни с другим сам: у даты нет нативного представления в JSON, а байтовая
  строка, не являющаяся валидным UTF-8, заставляет `json_encode()` падать. Оба
  заворачиваются в небольшой помеченный объект (`{"__datetime__": "..."}`,
  `{"__base64__": "..."}`) при записи и разворачиваются при чтении, чтобы журнал
  хранил ровно те значения, что и `Schema\Row`, а не только те, что JSON
  поддерживает напрямую. Ротации журнала нет: в самом `PLAN.md` есть
  нумерованные сегменты (`wal.0001.log`, `wal.0002.log`), но эта реализация
  держит один файл и возвращает место усечением, как только `TransactionManager`
  подтверждает, что от него ничего не зависит (см. `checkpoint()` и
  `DECISIONS.md`), — та же сделка «место возвращается только явным действием»,
  что уже заключена в `VACUUM` у `HeapFile` и в отсутствии ребалансировки у
  `BTreeIndex`.

Сама запись — четыре вызова подряд, и именно порядок делает её надёжной:
`fwrite()` (в буфер), `fflush()` (в ядро), `fsync()` (на устройство). Любой из
трёх шагов не удался — исключение, и утверждение не подтверждено.

```php
public function nextLsn(): int
{
    return $this->nextLsn++;
}

public function append(WalRecord $record): void
{
    $line = json_encode($this->encodeRecord($record), JSON_THROW_ON_ERROR) . "\n";

    if (fwrite($this->handle, $line) !== strlen($line) || !fflush($this->handle) || !fsync($this->handle)) {
        throw new StorageException(sprintf('Cannot append to the WAL at "%s".', $this->path));
    }
}
```

[`src/Transaction/Wal.php#L61-L73`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Wal.php#L61-L73)

Чтение устроено интереснее, чем кажется: оборванная **последняя** строка
завершает журнал, а не ломает его. Это не поблажка, а прямое следствие того, что
каждая целая запись уже синхронизирована: единственный способ получить
недописанную — сбой посреди записи, а такая запись никому не была подтверждена.
Некорректная строка где угодно ещё — реальная порча.

```php
/**
 * Every record currently in the log, in LSN order.
 *
 * A malformed *last* line ends the log rather than failing the read:
 * this file is append-only and every complete `append()` is `fsync()`'d,
 * so the only way to produce one is a write that a crash cut in half —
 * and a record that never finished being written is, correctly, not
 * part of the log. Anywhere else a malformed line is real corruption
 * and still throws. Without this, a torn final write would leave a WAL
 * that `Wal::open()` cannot read, and therefore a database that can
 * never be opened again.
 *
 * @return list<WalRecord>
 */
public function readAll(): array
{
    $lines = array_values(array_filter(
        explode("\n", $this->files->read($this->path)),
        static fn (string $line): bool => trim($line) !== '',
    ));

    $records = [];

    foreach ($lines as $index => $line) {
        try {
            /** @var array<string, mixed> $decoded */
            $decoded = json_decode($line, true, flags: JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            if ($index === count($lines) - 1) {
                break;
            }

            throw new StorageException(
                sprintf('The WAL at "%s" is corrupt at record %d.', $this->path, $index + 1),
                previous: $e,
            );
        }

        $records[] = $this->decodeRecord($decoded);
    }

    return $records;
}
```

[`src/Transaction/Wal.php#L75-L117`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Wal.php#L75-L117)

**Перевод комментариев:**
- `readAll()` (PHPDoc): все записи, которые сейчас есть в журнале, в порядке
  `LSN`. Некорректная **последняя** строка завершает журнал, а не ломает чтение:
  файл только дописывается, и каждый целый `append()` уже `fsync()`-нут,
  поэтому единственный способ получить такую строку — это запись, которую сбой
  разрезал пополам; запись, которую не дописали, по праву не является частью
  журнала. В любом другом месте некорректная строка — это настоящая порча, и
  исключение по-прежнему бросается. Без этого оборванная последняя запись
  оставила бы WAL, который `Wal::open()` не может прочитать, а значит, базу,
  которую больше никогда нельзя открыть.

Усечение журнала — тоже запись, и тоже с `fsync()`: пока усечение не дошло до
устройства, удалённые записи всё ещё там, а следующая запись пишется с нулевого
смещения — при сбое между ними получилась бы смесь новых строк с хвостом старых,
и каждая строка при этом разбирается.

```php
/**
 * Discards every record currently in the log. Only ever safe to call
 * when no transaction is open — a truncated log has nothing left to
 * recover an interrupted transaction from — which is
 * `TransactionManager`'s call to make, not this class's.
 */
public function checkpoint(): void
{
    // `fsync()` for the same reason `append()` does it: until the
    // truncation reaches the device, the records it dropped are still
    // there, and the next `append()` writes over them from offset 0 -
    // leaving, if a crash lands in between, new records followed by
    // the tail of older ones. Every line of that mixture parses, so
    // nothing would notice.
    if (!ftruncate($this->handle, 0) || fseek($this->handle, 0) !== 0 || !fflush($this->handle) || !fsync($this->handle)) {
        throw new StorageException(sprintf('Cannot checkpoint the WAL at "%s".', $this->path));
    }
}

public function close(): void
{
    fclose($this->handle);
}
```

[`src/Transaction/Wal.php#L119-L141`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Wal.php#L119-L141)

**Перевод комментариев:**
- `checkpoint()` (PHPDoc): выбрасывает все записи, которые сейчас есть в
  журнале. Вызывать безопасно, только когда нет открытой транзакции: у
  усечённого журнала не останется ничего, из чего восстанавливать прерванную
  транзакцию, — и это решение `TransactionManager`, а не этого класса.
- `// \` fsync()\` for the same reason \`append()\` does it: until the` —
  `fsync()` здесь по той же причине, что и в `append()`: пока усечение не дошло
  до устройства, удалённые записи всё ещё там, и следующий `append()` пишет
  поверх них с нулевого смещения. Если сбой случится между ними, получатся новые
  записи, за которыми идёт хвост старых. Каждая строка этой смеси разбирается,
  поэтому никто бы ничего не заметил.

Практическая часть этого — поставить курсор туда, где начинается следующая
запись. У `c+` открытие идёт с нулевого смещения, то есть записало бы поверх
первой строки; поэтому файл сначала обрезается до конца последней **целой**
строки, а недописанный хвост срезается.

```php
/**
 * Puts the cursor where the next `append()` belongs. "c+" opens at
 * offset 0, which would write over the first record. A final line with
 * no newline is finished first: one that still parses is a record
 * `readAll()` already counts, so it only gets its newline; a torn one
 * is cut off, since a record appended straight after it would be glued
 * onto the same line and turn a harmless torn tail into corruption in
 * the middle of the log.
 */
private function seekPastLastCompleteRecord(): void
{
    $contents = $this->files->read($this->path);
    $lastNewline = strrpos($contents, "\n");
    /** @var int<0, max> $end just past the last complete line */
    $end = $lastNewline === false ? 0 : $lastNewline + 1;
    $tail = substr($contents, $end);

    $ok = match (true) {
        $tail === '' => fseek($this->handle, $end) === 0,
        json_validate($tail) => fseek($this->handle, 0, SEEK_END) === 0 && fwrite($this->handle, "\n") === 1,
        default => ftruncate($this->handle, $end) && fseek($this->handle, $end) === 0,
    };

    if (!$ok) {
        throw new StorageException(sprintf('Cannot open the WAL at "%s" for appending.', $this->path));
    }
}
```

[`src/Transaction/Wal.php#L143-L169`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/Wal.php#L143-L169)

**Перевод комментариев:**
- `seekPastLastCompleteRecord()` (PHPDoc): ставит курсор туда, где должна быть
  следующая `append()`. `c+` открывает файл на смещении 0, и запись легла бы
  поверх первой. Сначала завершается последняя строка без перевода строки: та,
  что ещё разбирается, — это запись, которую `readAll()` уже считает, поэтому ей
  добавляется только перевод строки; оборванная отрезается, иначе запись,
  дописанная сразу после неё, приклеилась бы к той же строке и превратила
  безобидный оборванный хвост в порчу посреди журнала.
- `$end` (`@var`): позиция сразу за последней целой строкой.

### Что именно записывается

Словарь журнала — девять операций: три на транзакцию, четыре на строку, три на
точки сохранения.

```php
/** Every shape of entry the WAL records, per PLAN.md §6.5's own log format. */
enum WalOperation: string
{
    case BEGIN = 'BEGIN';
    case COMMIT = 'COMMIT';
    case ROLLBACK = 'ROLLBACK';
    case INSERT = 'INSERT';
    case UPDATE = 'UPDATE';
    case DELETE = 'DELETE';
    case SAVEPOINT = 'SAVEPOINT';
    case RELEASE_SAVEPOINT = 'RELEASE_SAVEPOINT';
    case ROLLBACK_TO_SAVEPOINT = 'ROLLBACK_TO_SAVEPOINT';
}
```

[`src/Transaction/WalOperation.php#L7-L19`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/WalOperation.php#L7-L19)

**Перевод комментариев:**
- `WalOperation` (PHPDoc): все виды записей, которые пишет WAL, по собственному
  формату журнала из `PLAN.md` §6.5.

Форма записи — логическая, и это решение, а не следствие формата файла: отменять
логическое изменение можно теми же средствами, что его сделали, а
восстанавливать сырые байты страницы — нет.

```php
/**
 * One entry in the write-ahead log: enough to redo *and* to undo one
 * change. `$before`/`$after` hold a row's values by column name, exactly
 * as `Schema\Row::toArray()` gives them — `Wal` is what makes those
 * JSON-safe on the way to and from disk (see its own docblock for why a
 * plain `json_encode()` is not enough).
 *
 * This is a logical record, not a physical one: it says "row X changed
 * from this to that," not which bytes of which page moved. `Undo` (Phase
 * 8's rollback and recovery) replays that logical change in reverse
 * against the live heap and indexes — through the same `Table`/`HeapFile`
 * machinery a fresh `INSERT`/`UPDATE`/`DELETE` would use — rather than
 * restoring raw page bytes the way a physical WAL would.
 */
```

[`src/Transaction/WalRecord.php#L9-L22`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/WalRecord.php#L9-L22)

**Перевод комментариев:**
- `WalRecord` (PHPDoc): одна запись журнала предзаписи — достаточно, чтобы
  повторить **и** отменить одно изменение. `$before` /`$after` хранят значения
  строки по имени колонки ровно так, как их отдаёт `Schema\Row::toArray()`;
  `Wal` делает их безопасными для JSON при записи на диск и чтении с него
  (почему обычного `json_encode()` недостаточно — в его собственном докблоке).
  Это логическая запись, а не физическая: она говорит «строка X изменилась из
  такого в такое», а не какие байты какой страницы сдвинулись. `Undo` (откат и
  восстановление фазы 8) проигрывает это логическое изменение в обратном порядке
  по живой куче и индексам — через ту же машинерию `Table` /`HeapFile`, которой
  пользуется свежий `INSERT` /`UPDATE`/`DELETE`, — а не восстанавливает сырые
  байты страниц, как это делал бы физический WAL.

### Два замыкания: отменять и записывать на устройство

`TransactionManager` знает **что** случилось, но не знает, **как** отменить
изменение в куче. Отмена приходит снаружи, замыканием — ровно так же, как
приходит синхронизация страниц. Оба хендлера — точки стыка между подсистемами.

```php
/** @param Closure(WalRecord): void $handler */
public function setUndoHandler(Closure $handler): void
{
    $this->undo = $handler;
}

/**
 * @param Closure(): void $handler called once, at the end of every
 *        `commit()`/`rollback()`, right before the WAL is checkpointed
 *        — see `Schema\Database::syncStorage()`, the real handler
 *        `Execution\Executor` wires in. Optional: a `TransactionManager`
 *        built without a real `Database` behind it (most of this
 *        class's own tests) has nothing to sync and no handler to
 *        configure.
 */
public function setSyncHandler(Closure $handler): void
{
    $this->sync = $handler;
}
```

[`src/Transaction/TransactionManager.php#L63-L81`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L63-L81)

**Перевод комментариев:**
- `setUndoHandler()` (`@param`): обработчик отмены, `Closure(WalRecord): void`.
- `setSyncHandler()` (PHPDoc): `$handler` вызывается один раз, в конце каждого
  `commit()` /`rollback()`, прямо перед тем, как WAL будет усечён, — см.
  `Schema\Database::syncStorage()`, настоящий обработчик, который подключает
  `Execution\Executor`. Необязателен: у `TransactionManager`, построенного без
  настоящей `Database` за спиной (в большинстве его собственных тестов),
  синхронизировать нечего и обработчик настраивать не нужно.

Три метода ниже вызываются из `Executor` **после** того, как изменение уже
применено к куче и индексам. Каждый дописывает запись и, что важнее, кладёт её
ещё и в память транзакции — из этого списка берётся откат:

```php
/**
 * `Executor` calls one of these three once per row mutation, *after*
 * applying it to the heap and its indexes — so the log always reflects
 * a change that has already actually happened, and always carries the
 * mutation's real final `RecordId` (which, for `INSERT` and a moving
 * `UPDATE`, is not knowable before the mutation itself runs). See
 * DECISIONS.md for what that ordering costs.
 *
 * @param array<string, mixed> $after
 */
public function logInsert(string $table, RecordId $id, array $after): void
{
    $this->append(fn (int $lsn, int $txId): WalRecord => WalRecord::insert($lsn, $txId, $table, $id, $after));
}

/**
 * @param array<string, mixed> $before
 * @param array<string, mixed> $after
 */
public function logUpdate(string $table, RecordId $id, array $before, array $after): void
{
    $this->append(fn (int $lsn, int $txId): WalRecord => WalRecord::update($lsn, $txId, $table, $id, $before, $after));
}

/** @param array<string, mixed> $before */
public function logDelete(string $table, RecordId $id, array $before): void
{
    $this->append(fn (int $lsn, int $txId): WalRecord => WalRecord::delete($lsn, $txId, $table, $id, $before));
}

/** @param Closure(int, int): WalRecord $build */
private function append(Closure $build): void
{
    $tx = $this->requireCurrent();
    $record = $build($this->wal->nextLsn(), $tx->id);

    $tx->record($record);
    $this->wal->append($record);
}
```

[`src/Transaction/TransactionManager.php#L188-L226`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L188-L226)

**Перевод комментариев:**
- `logInsert()` (PHPDoc): `Executor` вызывает один из этих трёх методов на
  каждое изменение строки, **после** того как применил его к куче и её индексам,
  — поэтому журнал всегда отражает изменение, которое уже действительно
  произошло, и всегда несёт настоящий итоговый `RecordId` изменения (для
  `INSERT` и смещающего `UPDATE` его нельзя узнать до самого изменения). Что
  стоит такой порядок — в `DECISIONS.md`.
- `$build` (`@param`): замыкание, которое собирает запись WAL по `LSN` и номеру
  транзакции.

### Восстановление

Восстановление начинается с группировки: сначала все записи по транзакциям,
потом поиск тех, у которых нет завершения. Обратите внимание, что синхронизация
хранилища и усечение журнала стоят **в конце** — так же, как в
`commit()`/`rollback()`, и по той же причине.

```php
/**
 * Undoes every transaction left open (a `BEGIN` with no matching
 * `COMMIT`) by a previous process that never got to finish it — meant
 * to run once, before any new statement runs. A no-op on every call
 * after the first: `Execution\Executor`'s constructor calls this
 * unconditionally, and a second `Executor` built against a `Database`
 * that already has a transaction open must not have this walk in and
 * undo it — that transaction is merely in progress, not abandoned.
 * A crash during this pass is survivable, which it did not used to
 * be: the checkpoint that discards the log comes last, so the WAL
 * still describes the whole transaction, and every undo step asks
 * what is left to do rather than assuming it runs once (see
 * `Execution\Executor::undo()`). The next pass finishes what this
 * one started. What is still assumed sits below this class: that a
 * page write either happened or did not — there are no full-page
 * images here to repair one torn in half. See DECISIONS.md.
 *
 * Syncs storage once, after every abandoned transaction's undo has
 * been applied and before the checkpoint below — the same "data
 * durable before the WAL record that could redo/undo it is discarded"
 * rule `commit()`/`rollback()` follow, applied here to the undo
 * writes this method itself just made.
 */
public function recover(): void
{
    if ($this->recovered) {
        return;
    }

    $this->recovered = true;

    /** @var array<int, list<WalRecord>> $byTransaction */
    $byTransaction = [];

    foreach ($this->wal->readAll() as $record) {
        $byTransaction[$record->txId][] = $record;
    }

    foreach ($byTransaction as $txId => $records) {
        $isComplete = array_any(
            $records,
            static fn (WalRecord $r): bool => $r->operation === WalOperation::COMMIT || $r->operation === WalOperation::ROLLBACK,
        );

        if ($isComplete) {
            continue;
        }

        $this->applyUndo($this->replayStillLiveRecords($txId, $records));
    }

    $this->sync?->__invoke();
    $this->wal->checkpoint();
}
```

[`src/Transaction/TransactionManager.php#L228-L281`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L228-L281)

**Перевод комментариев:**
- `recover()` (PHPDoc): отменяет каждую транзакцию, оставленную открытой
  (`BEGIN` без парного `COMMIT`) предыдущим процессом, который не успел её
  завершить; рассчитан на один запуск до выполнения любого нового оператора.
  Повторные вызовы ничего не делают: конструктор `Execution\Executor` вызывает
  его безусловно, а второй `Executor`, построенный над `Database` с уже открытой
  транзакцией, не должен откатывать её — она всего лишь выполняется, а не
  брошена. Сбой во время этого прохода переживаем, чего раньше не было:
  checkpoint, выбрасывающий журнал, идёт последним, поэтому WAL всё ещё
  описывает транзакцию целиком, а каждый шаг отмены спрашивает, что осталось
  сделать, вместо предположения, что выполняется один раз (см.
  `Execution\Executor::undo()`). Следующий проход завершает начатое. Что
  по-прежнему предполагается — уровнем ниже этого класса: запись страницы либо
  произошла, либо нет; полных образов страниц здесь нет, чтобы починить
  разорванную пополам. См. `DECISIONS.md`. Синхронизирует хранилище один раз,
  после применения отмен всех брошенных транзакций и до checkpoint ниже, — то же
  правило «данные на устройстве до того, как выброшена запись WAL, которая могла
  бы их повторить/отменить», которого держатся `commit()` /`rollback()`,
  применённое здесь к записям отмены, которые только что сделал сам метод.
- `$byTransaction` (`@var`): номер транзакции → её записи WAL.

Самое тонкое место — восстановить не «все изменения транзакции», а именно тот
список отмен, который был у неё в памяти на момент гибели. Решение — прогнать
записи через новый `Transaction` теми же методами, что вызывает живая сессия:

```php
/**
 * Rebuilds one crashed transaction's still-live change log by replaying
 * its WAL records through the same `Transaction` state machine
 * `savepoint()`/`rollbackToSavepoint()`/`releaseSavepoint()` drive
 * live, rather than naively undoing every mutation the transaction
 * ever logged: a process that managed to run `ROLLBACK TO SAVEPOINT`
 * before crashing has already undone everything after that savepoint,
 * both on disk and in that now-gone process's memory, so recovery
 * must not undo it a second time — that record is still sitting in
 * the WAL (nothing rewrites history there), but `Transaction::truncateToSavepoint()`
 * is exactly what already drops it from a live rollback's own undo
 * list, so replaying every record through the same calls reproduces
 * the identical, correct set here.
 *
 * @param list<WalRecord> $records
 *
 * @return list<WalRecord>
 */
private function replayStillLiveRecords(int $txId, array $records): array
{
    $tx = new Transaction($txId, IsolationLevel::READ_COMMITTED);
    $mutations = [WalOperation::INSERT, WalOperation::UPDATE, WalOperation::DELETE];

    foreach ($records as $record) {
        match (true) {
            in_array($record->operation, $mutations, true) => $tx->record($record),
            $record->operation === WalOperation::SAVEPOINT => $tx->declareSavepoint($this->requireSavepointName($record)),
            $record->operation === WalOperation::ROLLBACK_TO_SAVEPOINT => $tx->truncateToSavepoint($this->requireSavepointName($record)),
            $record->operation === WalOperation::RELEASE_SAVEPOINT => $tx->releaseSavepoint($this->requireSavepointName($record)),
            default => null,
        };
    }

    return $tx->allRecordsReversed();
}
```

[`src/Transaction/TransactionManager.php#L283-L317`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L283-L317)

**Перевод комментариев:**
- `replayStillLiveRecords()` (PHPDoc): перестраивает журнал изменений одной
  упавшей транзакции, которые ещё живы, прогоняя её записи WAL через ту же
  машину состояний `Transaction`, которой управляют живые `savepoint()` /
  `rollbackToSavepoint()` /`releaseSavepoint()`, вместо наивной отмены каждой
  мутации, которую транзакция когда-либо записала. Процесс, успевший выполнить
  `ROLLBACK TO SAVEPOINT` до падения, уже отменил всё после этой точки
  сохранения — и на диске, и в памяти исчезнувшего процесса, — поэтому
  восстановление не должно отменять это второй раз. Запись всё ещё лежит в WAL
  (историю там никто не переписывает), но `Transaction::truncateToSavepoint()` —
  ровно то, что уже выбрасывает её из списка отмен живого отката, поэтому прогон
  каждой записи через те же вызовы воспроизводит здесь тот же правильный набор.

Конец транзакции общий для `COMMIT` и `ROLLBACK`: снять блокировки, обнулить
владельца, усечь журнал. Синхронизация здесь уже сделана вызывающим — см.
следующий подраздел.

```php
/**
 * `commit()`/`rollback()` have already synced storage themselves (see
 * their own docblocks for why that has to happen before their own WAL
 * record, not here) — by the time either reaches this, the checkpoint
 * below is discarding a log nothing still depends on, not the only
 * remaining record of unsynced work.
 */
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

[`src/Transaction/TransactionManager.php#L337-L354`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Transaction/TransactionManager.php#L337-L354)

**Перевод комментариев:**
- `finish()` (PHPDoc): `commit()` /`rollback()` уже сами синхронизировали
  хранилище (почему это должно произойти до их собственной записи WAL, а не
  здесь, — в их докблоках); к моменту, когда любой из них доходит сюда,
  checkpoint ниже выбрасывает журнал, от которого ничего не зависит, а не
  единственную оставшуюся запись несинхронизированной работы.
- `// Safe only because exactly one transaction can ever be open at a` —
  безопасно только потому, что в этом процессе одновременно может быть открыта
  ровно одна транзакция: когда она заканчивается, от журнала, который к этому
  привёл, ничего не зависит.

### Барьер долговечности и его место в порядке

`Database::syncStorage()` — тот самый хендлер: пройтись по всем открытым кучам и
индексам и попросить `fsync()`. Синхронизируется всё открытое, а не только
тронутое транзакцией: учёт «какие страницы я изменил» потребовал бы от
`HeapFile`/`BTreeIndex` отчитываться о записях, чего `PageManager` не умеет.

```php
/**
 * Pushes every currently-open heap file and index all the way to the
 * device — `Executor` wires this in as `TransactionManager`'s sync
 * handler, called at the end of every `COMMIT`/`ROLLBACK`, right before
 * the WAL is checkpointed. Without it, `PageManager::write()`'s plain
 * `fwrite()` leaves a mutated page only as durable as the OS's own page
 * cache decides to make it — the WAL record for that change can already
 * be gone (checkpointed) by the time the page itself actually reaches
 * disk. See DECISIONS.md.
 *
 * Syncs every open table and index unconditionally, not only the ones
 * this particular transaction touched: tracking a per-transaction dirty
 * set would need `HeapFile`/`BTreeIndex` to report which pages they
 * wrote, machinery this project's `PageManager` does not have (see "No
 * buffer pool yet" in DECISIONS.md) — an `fsync()` on a handful of
 * already-open files this engine's scale keeps open anyway is the
 * simpler, obviously-correct trade.
 */
public function syncStorage(): void
{
    foreach ($this->heapFiles as $heap) {
        $heap->sync();
    }

    foreach ($this->indexes as $index) {
        $index->sync();
    }
}
```

[`src/Schema/Database.php#L405-L432`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Schema/Database.php#L405-L432)

**Перевод комментариев:**
- `syncStorage()` (PHPDoc): проталкивает каждый открытый файл кучи и индекс до
  самого устройства. `Executor` подключает его как обработчик синхронизации
  `TransactionManager`, вызываемый в конце каждого `COMMIT` /`ROLLBACK`, прямо
  перед усечением WAL. Без него обычный `fwrite()` в `PageManager::write()`
  оставляет изменённую страницу настолько долговечной, насколько решит кэш
  страниц ОС, — запись WAL об этом изменении может уже исчезнуть (быть
  усечённой) к тому моменту, когда страница действительно дойдёт до диска. См.
  `DECISIONS.md`. Синхронизирует каждую открытую таблицу и индекс безусловно, а
  не только тронутые этой транзакцией: учёт грязного набора на транзакцию
  потребовал бы от `HeapFile` /`BTreeIndex` сообщать, какие страницы они писали,
  — машинерии, которой у `PageManager` в этом проекте нет (см. «No buffer pool
  yet» в `DECISIONS.md`). `fsync()` на нескольких уже открытых файлах, которые
  этот движок всё равно держит открытыми, — более простая и очевидно правильная
  сделка.

Место в порядке видно прямо в конструкторе `Executor`: сначала подключаются
отмена и синхронизация, потом делается восстановление — до того, как будет
выполнен хоть один оператор. А `sync()` у `PageManager` — это три строки, вокруг
которых и построена вся глава.

```php
public function __construct(
    private Database $database,
    private Evaluator $evaluator = new Evaluator(),
    private TableBuilder $tableBuilder = new TableBuilder(),
) {
    $this->indexMaintainer = new IndexMaintainer($database);
    $this->constraints = new ConstraintEnforcer($database, $evaluator);
    $this->locks = $database->locks();
    $this->transactions = $database->transactions();
    $this->transactions->setUndoHandler($this->undo(...));
    $this->transactions->setSyncHandler($database->syncStorage(...));
    $this->transactions->recover();
    $this->planner = new Planner($database);
    $this->optimizer = new Optimizer();
}
```

[`src/Execution/Executor.php#L162-L176`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L162-L176)

### Отмена идемпотентна — и обязана быть

Докблок `undo()` перечисляет, почему повторная отмена того же самого — это
успех, а не ошибка. Ошибка здесь стоила бы слишком дорого: `recover()`
вызывается из конструктора, поэтому исключение на уже отменённой строке сделало
бы базу, которую нельзя открыть никогда.

```php
/**
 * `TransactionManager`'s undo handler: physically reverses one logged
 * change through the same `Table`/`HeapFile`/`IndexMaintainer`
 * machinery a fresh `INSERT`/`UPDATE`/`DELETE` uses, rather than
 * restoring raw page bytes. Only ever called by `TransactionManager`
 * (`rollback()`, `rollbackToSavepoint()`, `recover()`), and only ever
 * with an `INSERT`/`UPDATE`/`DELETE` record — see its own docblock for
 * why nothing else it might log ever reaches here.
 *
 * Each of the three treats "the change is already reversed" as done,
 * not as an error, because the same record genuinely can be undone
 * twice: `rollback()` applies its undo before a durability barrier
 * that may fail, and a crash in that window leaves the WAL still
 * showing an unfinished transaction for the next `recover()` to undo
 * again — from records whose rows are already gone. Throwing there
 * would not merely fail the rollback, it would fail every future
 * `Executor` constructed against that database, since `recover()`
 * runs in it: one full disk plus one crash would leave a database
 * that could never be opened again. See DECISIONS.md.
 */
private function undo(WalRecord $record): void
{
    match ($record->operation) {
        WalOperation::INSERT => $this->undoInsert($record),
        WalOperation::UPDATE => $this->undoUpdate($record),
        WalOperation::DELETE => $this->undoDelete($record),
        default => throw new ExecutionException(sprintf('Cannot undo a %s record.', $record->operation->value)),
    };
}
```

[`src/Execution/Executor.php#L1129-L1157`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L1129-L1157)

**Перевод комментариев:**
- `undo()` (PHPDoc): обработчик отмены `TransactionManager` — физически
  разворачивает одно записанное изменение через ту же машинерию `Table`
  /`HeapFile`/`IndexMaintainer`, что и свежий `INSERT` /`UPDATE`/`DELETE`, а не
  восстанавливает сырые байты страниц. Вызывается только `TransactionManager`
  (`rollback()`, `rollbackToSavepoint()`, `recover()`) и только с записью
  `INSERT` /`UPDATE`/`DELETE`; почему ничего другое, что он мог бы записать,
  сюда не доходит — в его собственном докблоке. Каждый из трёх считает
  «изменение уже отменено» выполненным, а не ошибкой, потому что одну и ту же
  запись действительно можно отменить дважды: `rollback()` применяет свою отмену
  до барьера долговечности, который может упасть, и сбой в этом окне оставляет в
  WAL незавершённую транзакцию, которую следующий `recover()` отменит снова — по
  записям, чьи строки уже исчезли. Исключение здесь сорвало бы не только откат:
  оно сорвало бы каждый будущий `Executor`, построенный над этой базой, потому
  что `recover()` выполняется в его конструкторе. Один полный диск плюс один
  сбой оставили бы базу, которую больше нельзя открыть. См. `DECISIONS.md`.

Отмена вставки — самый короткий пример: удалить строку, если она ещё есть
(проверка делает её идемпотентной), и снять с индекса то, что могло остаться, —
даже если строки уже нет.

```php
private function undoInsert(WalRecord $record): void
{
    $tableName = $this->requireTable($record);
    $table = $this->database->table($tableName);
    $heap = $this->database->heapFile($tableName);
    $row = new Row($this->requireAfter($record));
    $recordId = $this->requireRecordId($record);

    if ($heap->read($recordId) !== null) {
        $heap->delete($recordId);
    }

    $this->indexMaintainer->ensureNotIndexed($table, $row, $recordId);
}
```

[`src/Execution/Executor.php#L1159-L1172`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L1159-L1172)

### Второй проект: журнал — единственное состояние

Докблок `Log` формулирует контракт целиком: журнал — единственное надёжное
состояние, индекс строится из него при каждом старте, а `open()` чинит то, что
оставил сбой, **до** того, как примет первую запись.

```php
/**
 * The append-only log: the only durable state the service has. The index is
 * rebuilt from it on every start.
 *
 * open() replays the file and repairs what a crash can leave behind before
 * any write is accepted: a torn tail is cut off, and a damaged log is renamed
 * aside — kept for inspection, never deleted — and replaced by an empty one.
 * The caller learns which happened from recovery(), and decides what an
 * empty or lost log means for the corpus.
 *
 * append() returns only after the record is on disk: write, fflush, fsync.
 * Any failure is a StorageFailure, and the process is expected to stop.
 *
 * Only one process may hold the log: the service, or an offline `compact` or
 * `check`, never two at once. The exclusive flock() is on a separate
 * `<log>.lock` file, taken before anything is read or repaired, because
 * compaction renames a new file over the log and a lock on the old file would
 * not cover it.
 */
```

[`src/Storage/Log.php#L9-L27`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L9-L27)

**Перевод комментариев:**
- `Log` (PHPDoc): журнал только для дописывания — единственное устойчивое к
  сбоям состояние сервиса. Индекс перестраивается из него при каждом старте.
  `open()` переигрывает файл и чинит то, что может оставить сбой, до принятия
  любой записи: оборванный хвост отрезается, а испорченный журнал
  переименовывается в сторону — сохраняется для осмотра и никогда не удаляется —
  и заменяется пустым. Из `recovery()` вызывающий узнаёт, что именно произошло,
  и решает, что пустой или потерянный журнал значит для корпуса. `append()`
  возвращает управление только после того, как запись на диске: write, fflush,
  fsync. Любая ошибка — `StorageFailure`, и процесс должен остановиться. Держать
  журнал может только один процесс: сервис либо офлайновые `compact` или
  `check`, никогда два одновременно. Исключительный `flock()` берётся на
  отдельном файле `<log>.lock` до того, как что-либо прочитано или починено,
  потому что компактизация переименовывает новый файл поверх журнала, и
  блокировка на старом файле его бы не покрывала.

Само решение принимается по результату сканирования — и это три разных действия:
создать пустой, отрезать хвост, отложить файл целиком.

```php
/**
 * @param Closure(array<string, mixed>): void $onRecord replays each valid record, in order
 */
public static function open(string $path, Closure $onRecord): self
{
    $lock = self::lock($path);
    $scan = LogScanner::scan($path, $onRecord);
    $damagedCopy = null;

    switch ($scan->status) {
        case LogScan::MISSING:
            self::create($path);
            break;
        case LogScan::TORN_TAIL:
            self::truncate($path, $scan->validBytes);
            break;
        case LogScan::DAMAGED:
            $damagedCopy = $path . '.damaged-' . date('Ymd-His');
            if (!rename($path, $damagedCopy)) {
                throw new StorageFailure("Could not move the damaged log aside to {$damagedCopy}.");
            }
            self::create($path);
            break;
    }

    $handle = @fopen($path, 'ab');
    if ($handle === false) {
        throw new StorageFailure("Could not open the log {$path} for appending.");
    }

    return new self($handle, $lock, $path, $scan, $damagedCopy);
}
```

[`src/Storage/Log.php#L43-L74`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L43-L74)

**Перевод комментариев:**
- `$onRecord` (`@param`): переигрывает каждую валидную запись по порядку.

Компактизация — та же идея, что и `syncStorage()` в первом проекте, но на уровне
файла: записать новое, дождаться устройства, заменить одним неделимым
переименованием.

```php
/**
 * Replaces the log with exactly these records: written to a temporary
 * file next to it, synced, renamed over it, and the directory synced. A
 * crash at any step leaves one whole log, the old one or the new one.
 * Only for a stopped service — this is offline compaction.
 *
 * @param iterable<array<string, mixed>> $records
 */
public static function rewrite(string $path, iterable $records): void
{
    $temporary = $path . '.compact';
    $handle = @fopen($temporary, 'wb');
    if ($handle === false) {
        throw new StorageFailure("Could not create {$temporary}.");
    }

    try {
        Durable::write($handle, LogFormat::header(), 'the compacted log');
        foreach ($records as $record) {
            Durable::write($handle, LogFormat::frame($record), 'the compacted log');
        }
        Durable::sync($handle, 'the compacted log');
    } finally {
        fclose($handle);
    }

    if (!rename($temporary, $path)) {
        throw new StorageFailure("Could not replace {$path} with the compacted log.");
    }
    Durable::syncDirectory(dirname($path));
}
```

[`src/Storage/Log.php#L97-L127`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L97-L127)

**Перевод комментариев:**
- `rewrite()` (PHPDoc): заменяет журнал ровно этими записями: они пишутся во
  временный файл рядом, синхронизируются, переименовываются поверх журнала, и
  каталог синхронизируется. Сбой на любом шаге оставляет один целый журнал —
  старый или новый. Только для остановленного сервиса: это офлайновая
  компактизация.

А `append()` — это одна фраза, в которой смысл всей главы: не `fwrite()`, а
запись, после которой данные на устройстве.

```php
/**
 * @param array<string, mixed> $record
 */
public function append(array $record): void
{
    Durable::write($this->handle, LogFormat::frame($record), 'a log record');
    Durable::sync($this->handle, 'the log');
}
```

[`src/Storage/Log.php#L129-L136`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L129-L136)

### Байты, которым можно верить

Рецептура `fsync()` собрана в одном классе с комментарием о том, где она
проверялась: против php-src и `strace`.

```php
/**
 * The few file operations whose durability the log depends on, written once
 * with every check PHP allows. The recipe was verified against php-src and
 * strace on PHP 8.5 (docs/ARCHITECTURE.md, Storage):
 *
 *  - fflush() before fsync(): after the first fsync() PHP buffers the stream
 *    in libc, and a write error only surfaces at the next sync;
 *  - a failed fsync() returns a bare false, with no errno and no warning;
 *  - a directory is synced through fopen($dir, 'r');
 *  - rename() is atomic only within one filesystem — across devices PHP
 *    silently copies without syncing — so temporary files live next to the
 *    file they replace.
 *
 * On macOS PHP does not use F_FULLFSYNC, so none of this survives a power
 * cut there; it does on Linux.
 */
```

[`src/Storage/Durable.php#L7-L22`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Durable.php#L7-L22)

**Перевод комментариев:**
- `Durable` (PHPDoc): несколько файловых операций, от долговечности которых
  зависит журнал, написанные один раз со всеми проверками, которые позволяет
  PHP. Рецепт проверен по php-src и `strace` на PHP 8.5 (`docs/ARCHITECTURE.md`,
  Storage): `fflush()` до `fsync()`, потому что после первого `fsync()` PHP
  буферизует поток в libc и ошибка записи всплывает только на следующей
  синхронизации; неудачный `fsync()` возвращает голый `false`, без `errno` и
  предупреждения; каталог синхронизируется через `fopen($dir, 'r')`; `rename()`
  атомарен только внутри одной файловой системы — между устройствами PHP молча
  копирует без синхронизации, — поэтому временные файлы лежат рядом с тем, что
  заменяют. На macOS PHP не использует `F_FULLFSYNC`, поэтому отключение питания
  там ничего из этого не переживает; на Linux — переживает.

Отсюда — три операции: синхронизация файла (с обязательным `fflush()` до),
синхронизация каталога (через дескриптор каталога) и цикл записи, который
доводит `fwrite()` до конца, потому что короткая запись — это не ошибка, а
«записано меньше, чем просили».

```php
/**
 * @param resource $handle
 */
public static function sync($handle, string $what): void
{
    if (!@fflush($handle) || !@fsync($handle)) {
        throw new StorageFailure("Could not sync {$what}." . self::lastError());
    }
}

public static function syncDirectory(string $directory): void
{
    $handle = @fopen($directory, 'r');
    if ($handle === false) {
        throw new StorageFailure("Could not open directory {$directory} to sync it.");
    }

    try {
        if (!fsync($handle)) {
            throw new StorageFailure("Could not sync directory {$directory}.");
        }
    } finally {
        fclose($handle);
    }
}
```

[`src/Storage/Durable.php#L25-L49`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Durable.php#L25-L49)

### Формат записи

Грамматика журнала целиком: заголовок из magic и версии, дальше записи, каждая —
длина, контрольная сумма, тело. Границы записи определяются числами, а не
содержимым JSON, поэтому тело остаётся читаемым человеком (например, через
`strings`).

```php
/**
 * The bytes of the log, and nothing else.
 *
 *   file   = "MSLOG" uint32(format version) record*
 *   record = uint32(body length) uint32(crc32 of body) body
 *   body   = one JSON object
 *
 * Both integers are big-endian (pack 'N'). The length and the checksum frame
 * a record, so its boundary never depends on what the JSON contains; the
 * body stays readable with `strings`.
 */
```

[`src/Storage/LogFormat.php#L9-L19`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogFormat.php#L9-L19)

**Перевод комментариев:**
- `LogFormat` (PHPDoc): байты журнала и ничего больше.
  `file = "MSLOG" uint32(версия формата) record*`,
  `record = uint32(длина тела) uint32(crc32 тела) body`,
  `body = один JSON-объект`. Оба целых — big-endian (`pack 'N'`). Длина и
  контрольная сумма обрамляют запись, поэтому её граница никогда не зависит от
  содержимого JSON; тело остаётся читаемым через `strings`.

Максимум длины — не украшение: он и есть граница между «это обрыв» и «это
порча», о которой речь в следующем подразделе. Контрольная сумма считается по
телу и пишется в кадре.

```php
/**
 * No valid record comes near this: a document is at most 1 KB of title
 * and 64 KB of body. A larger length is a damaged length field, never a
 * torn write (docs/FAILURE-MODEL.md).
 */
public const int MAX_BODY = 262_144;

public const int FRAME_HEADER = 8;

public static function header(): string
{
    return self::MAGIC . pack('N', self::VERSION);
}

/**
 * @param array<string, mixed> $record
 */
public static function frame(array $record): string
{
    $body = json_encode($record, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);

    if (strlen($body) > self::MAX_BODY) {
        throw new StorageFailure('Record of ' . strlen($body) . ' bytes is over the ' . self::MAX_BODY . '-byte limit.');
    }

    return pack('NN', strlen($body), crc32($body)) . $body;
}
```

[`src/Storage/LogFormat.php#L26-L52`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogFormat.php#L26-L52)

**Перевод комментариев:**
- `MAX_BODY` (PHPDoc): ни одна валидная запись не подходит близко: документ — не
  больше 1 КБ заголовка и 64 КБ тела. Большая длина — это испорченное поле
  длины, а никогда не оборванная запись (`docs/FAILURE-MODEL.md`).

### Обрыв против порчи

Докблок сканера — лучшая формулировка правила: обрыв может быть только у
последней записи, потому что каждая подтверждённая запись до неё уже
синхронизирована. Отсюда три ветки: короткая последняя запись — хвост; длина
больше максимума — порча, где бы она ни была; неверная контрольная сумма не у
последней записи — порча.

```php
/**
 * Reads the log front to back, hands each valid record to a callback, and
 * decides what the end of the file means. It never writes.
 *
 * The one judgement that matters is torn tail versus damage. Every record is
 * fsynced before it is acknowledged, so a crash can leave at most one record
 * unfinished, and only the last one. Hence:
 *
 *  - the last record short, or failing its checksum → a torn tail: it was
 *    never acknowledged and is safe to cut;
 *  - a length above LogFormat::MAX_BODY → damage, wherever it is: a damaged
 *    length field in the middle could otherwise point past the end of the
 *    file and pass for a torn tail, and cutting there would silently drop
 *    every acknowledged record after it;
 *  - a checksum failure before the last record, or a valid checksum around a
 *    body that is not a JSON object → damage.
 *
 * A file shorter than the header that is a prefix of it was cut off while
 * being created, before anything was ever written to it: it counts as
 * missing.
 */
```

[`src/Storage/LogScanner.php#L9-L29`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogScanner.php#L9-L29)

**Перевод комментариев:**
- `LogScanner::scan()` (PHPDoc): читает журнал от начала к концу, отдаёт каждую
  валидную запись в колбэк и решает, что означает конец файла. Никогда не пишет.
  Единственное важное суждение — оборванный хвост против порчи. Каждая запись
  синхронизируется `fsync()` до подтверждения, поэтому сбой может оставить
  недописанной максимум одну запись, и только последнюю. Отсюда: последняя
  запись коротка или не проходит контрольную сумму → оборванный хвост, её
  никогда не подтверждали, и её безопасно отрезать; длина больше
  `LogFormat::MAX_BODY` → порча, где бы она ни была: испорченное поле длины в
  середине иначе могло бы указывать за конец файла и сойти за оборванный хвост,
  а обрезка там молча выбросила бы все подтверждённые записи после неё; ошибка
  контрольной суммы до последней записи или валидная сумма вокруг тела, не
  являющегося JSON-объектом → порча. Файл короче заголовка, являющийся его
  префиксом, был обрезан при создании, до того, как в него что-либо записали: он
  считается отсутствующим.

Реализация того же правила, ветка за веткой:

```php
    while ($offset < $size) {
        $remaining = $size - $offset;
        if ($remaining < LogFormat::FRAME_HEADER) {
            return new LogScan(LogScan::TORN_TAIL, $records, $offset, $size, "{$remaining} bytes of an unfinished record header");
        }

        /** @var array{1: int, 2: int} $frame */
        $frame = unpack('N2', self::bytes($handle, LogFormat::FRAME_HEADER));
        [1 => $length, 2 => $checksum] = $frame;

        if ($length > LogFormat::MAX_BODY) {
            return new LogScan(LogScan::DAMAGED, $records, $offset, $size, "record at byte {$offset} claims {$length} bytes");
        }

        $end = $offset + LogFormat::FRAME_HEADER + $length;
        if ($end > $size) {
            return new LogScan(LogScan::TORN_TAIL, $records, $offset, $size, "record at byte {$offset} is cut short");
        }

        $body = self::bytes($handle, $length);
        if (crc32($body) !== $checksum) {
            return $end === $size
                ? new LogScan(LogScan::TORN_TAIL, $records, $offset, $size, "last record at byte {$offset} fails its checksum")
                : new LogScan(LogScan::DAMAGED, $records, $offset, $size, "record at byte {$offset} fails its checksum");
        }

        $record = LogFormat::decode($body);
        if ($record === null) {
            return new LogScan(LogScan::DAMAGED, $records, $offset, $size, "record at byte {$offset} is not a JSON object");
        }

        $onRecord($record);
        $records++;
        $offset = $end;
    }

    return new LogScan(LogScan::CLEAN, $records, $offset, $size);
}
```

[`src/Storage/LogScanner.php#L75-L112`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogScanner.php#L75-L112)

**Перевод комментариев:**
- `$frame` (`@var`): распакованные из кадра длина тела и контрольная сумма.

Наружу сканер отдаёт не «да/нет», а четыре состояния с причиной: что именно
нашлось решает, что с файлом делать.

```php
/**
 * What a read of the log found. Reading never changes the file; acting on
 * the result is Log::open()'s job, and `bin/mini-search check` only prints it.
 */
final readonly class LogScan
{
    /** No log file: a first start. */
    public const string MISSING = 'missing';

    /** Every byte belongs to a valid record. */
    public const string CLEAN = 'clean';

    /** The last record is short or fails its checksum: a write that never finished, and so was never acknowledged. */
    public const string TORN_TAIL = 'torn_tail';

    /** A record that cannot be explained by an interrupted write. Truncating would drop acknowledged records. */
    public const string DAMAGED = 'damaged';

    /**
     * @param self::MISSING|self::CLEAN|self::TORN_TAIL|self::DAMAGED $status
     * @param int $validBytes where the last valid record ends; everything after it is the tail or the damage
     */
    public function __construct(
        public string $status,
        public int $records,
        public int $validBytes,
        public int $fileBytes,
        public ?string $reason = null,
    ) {
    }
}
```

[`src/Storage/LogScan.php#L7-L37`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogScan.php#L7-L37)

**Перевод комментариев:**
- `LogScan` (PHPDoc): что нашло чтение журнала. Чтение никогда не меняет файл;
  действовать по результату — работа `Log::open()`, а `bin/mini-search check`
  только печатает его.
- `MISSING`: файла журнала нет, первый старт.
- `CLEAN`: каждый байт принадлежит валидной записи.
- `TORN_TAIL`: последняя запись коротка или не проходит контрольную сумму —
  запись, которую не дописали и потому никогда не подтверждали.
- `DAMAGED`: запись, которую нельзя объяснить прерванной записью. Усечение
  выбросило бы подтверждённые записи.
- `$validBytes` (`@param`): где заканчивается последняя валидная запись; всё
  после неё — хвост или порча. Также `$status` (`@param`): одно из четырёх
  состояний — `MISSING`, `CLEAN`, `TORN_TAIL` или `DAMAGED`.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-database
make build install
cd ../php-mini-search
make build install
```

Первый и второй эксперименты выполняются в контейнере `php`
(`php-mini-database`), третий и четвёртый — в контейнере `php-mini-search`; в
обоих проект смонтирован в `/app`. Все четыре эксперимента детерминированы
(скриптов пять: у второго их два), замеров времени в них нет, а у эксперимента 2
детерминирован даже код возврата — `137`. Каждый прогон повторялся дважды, вывод
совпал байт-в-байт.

### 1. Журнал как файл: одна транзакция по шагам

Скрипт выполняет `BEGIN`, `INSERT`, `UPDATE`, `SAVEPOINT`, `INSERT`,
`ROLLBACK TO SAVEPOINT` и `COMMIT`, распечатывая **файл журнала** после каждого
шага:

```bash
docker compose cp /tmp/ch18-wal.php php:/tmp/ch18-wal.php
docker compose exec php php /tmp/ch18-wal.php
```

```php
<?php

declare(strict_types=1);

/**
 * The write-ahead log as a file: one JSON line per change, in the order the
 * changes happened.
 *
 * Chapter 16 showed that a mutation reaches the heap before its WAL record
 * is appended; this prints the log itself, step by step, through one
 * transaction that is never committed until the very end. What is on disk
 * after each statement is the whole recovery story: `BEGIN` first, one
 * record per row change (carrying both the old and the new values, because
 * the engine undoes changes rather than redoing them), a record for each
 * savepoint operation, and a `COMMIT` last — after which the log is
 * checkpointed back to zero bytes, since nothing still depends on it.
 *
 * Stop reading right before the COMMIT and you have exactly the state
 * experiment 2 kills the process in: a complete description of work that
 * was never finished.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch18-wal.php php:/tmp/ch18-wal.php
 *   docker compose exec php php /tmp/ch18-wal.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = sys_get_temp_dir() . '/ch18-wal-' . getmypid();

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
$executor->run('CREATE TABLE items (id INT PRIMARY KEY, name VARCHAR(20))');

$wal = $directory . '/wal/wal.log';

$dump = static function (string $label) use ($wal): void {
    printf("%s\n", $label);

    $contents = is_file($wal) ? file_get_contents($wal) : '';

    if ($contents === '' || $contents === false) {
        printf("  (empty — checkpointed)\n");

        return;
    }

    foreach (explode("\n", rtrim($contents, "\n")) as $line) {
        printf("  %s\n", $line);
    }
};

$items = static function (Executor $executor): string {
    $rows = array_map(
        static fn (Row $row): string => sprintf('%d=%s', $row->get('id'), $row->get('name')),
        iterator_to_array($executor->run('SELECT id, name FROM items ORDER BY id')->rows),
    );

    return $rows === [] ? '(no rows)' : implode(', ', $rows);
};

printf("data directory: %s/ch18-wal-<pid> (fresh on every run)\n", sys_get_temp_dir());

$dump('1. after a fresh OPEN — the WAL exists but is empty');

$executor->run('BEGIN');
$dump('2. after BEGIN');

$executor->run("INSERT INTO items (id, name) VALUES (1, 'one')");
$dump("3. after INSERT id=1 ('one')");

$executor->run("UPDATE items SET name = 'ONE' WHERE id = 1");
$dump('4. after UPDATE id=1 -> ONE');

$executor->run('SAVEPOINT s1');
$dump('5. after SAVEPOINT s1');

$executor->run("INSERT INTO items (id, name) VALUES (2, 'two')");
$dump('6. after INSERT id=2');

$executor->run('ROLLBACK TO SAVEPOINT s1');
$dump('7. after ROLLBACK TO SAVEPOINT s1 — the row is gone again');

printf("items now: %s\n", $items($executor));

$executor->run('COMMIT');
$dump('8. after COMMIT');

printf("items after COMMIT: %s\n", $items($executor));

$database->close();
```

**Перевод комментариев:**
- `ch18-wal.php` (скрипт, PHPDoc): журнал предзаписи как файл — одна строка JSON
  на изменение, в порядке, в котором изменения происходили. Глава 16 показала,
  что мутация доходит до кучи раньше, чем дописывается её запись WAL; здесь
  печатается сам журнал, шаг за шагом, через одну транзакцию, которая не
  подтверждается до самого конца. То, что лежит на диске после каждого
  оператора, — вся история восстановления: сначала `BEGIN`, по записи на
  изменение строки (со старым и новым значением, потому что движок отменяет
  изменения, а не повторяет их), запись на каждую операцию с точкой сохранения и
  `COMMIT` последним, после чего журнал усекается обратно до нуля байт, так как
  от него ничего не зависит. Остановитесь прямо перед `COMMIT` — и получите
  ровно то состояние, в котором процесс убивают в эксперименте 2: полное
  описание работы, которая так и не была завершена. Запускать внутри контейнера,
  где проект смонтирован в `/app`.

```text
data directory: /tmp/ch18-wal-<pid> (fresh on every run)
1. after a fresh OPEN — the WAL exists but is empty
  (empty — checkpointed)
2. after BEGIN
  {"lsn":1,"tx":1,"op":"BEGIN"}
3. after INSERT id=1 ('one')
  {"lsn":1,"tx":1,"op":"BEGIN"}
  {"lsn":2,"tx":1,"op":"INSERT","table":"items","row_id":"0:0","after":{"id":1,"name":"one"}}
4. after UPDATE id=1 -> ONE
  {"lsn":1,"tx":1,"op":"BEGIN"}
  {"lsn":2,"tx":1,"op":"INSERT","table":"items","row_id":"0:0","after":{"id":1,"name":"one"}}
  {"lsn":3,"tx":1,"op":"UPDATE","table":"items","row_id":"0:0","before":{"id":1,"name":"one"},"after":{"id":1,"name":"ONE"}}
5. after SAVEPOINT s1
  {"lsn":1,"tx":1,"op":"BEGIN"}
  {"lsn":2,"tx":1,"op":"INSERT","table":"items","row_id":"0:0","after":{"id":1,"name":"one"}}
  {"lsn":3,"tx":1,"op":"UPDATE","table":"items","row_id":"0:0","before":{"id":1,"name":"one"},"after":{"id":1,"name":"ONE"}}
  {"lsn":4,"tx":1,"op":"SAVEPOINT","savepoint":"s1"}
6. after INSERT id=2
  {"lsn":1,"tx":1,"op":"BEGIN"}
  {"lsn":2,"tx":1,"op":"INSERT","table":"items","row_id":"0:0","after":{"id":1,"name":"one"}}
  {"lsn":3,"tx":1,"op":"UPDATE","table":"items","row_id":"0:0","before":{"id":1,"name":"one"},"after":{"id":1,"name":"ONE"}}
  {"lsn":4,"tx":1,"op":"SAVEPOINT","savepoint":"s1"}
  {"lsn":5,"tx":1,"op":"INSERT","table":"items","row_id":"0:1","after":{"id":2,"name":"two"}}
7. after ROLLBACK TO SAVEPOINT s1 — the row is gone again
  {"lsn":1,"tx":1,"op":"BEGIN"}
  {"lsn":2,"tx":1,"op":"INSERT","table":"items","row_id":"0:0","after":{"id":1,"name":"one"}}
  {"lsn":3,"tx":1,"op":"UPDATE","table":"items","row_id":"0:0","before":{"id":1,"name":"one"},"after":{"id":1,"name":"ONE"}}
  {"lsn":4,"tx":1,"op":"SAVEPOINT","savepoint":"s1"}
  {"lsn":5,"tx":1,"op":"INSERT","table":"items","row_id":"0:1","after":{"id":2,"name":"two"}}
  {"lsn":6,"tx":1,"op":"ROLLBACK_TO_SAVEPOINT","savepoint":"s1"}
items now: 1=ONE
8. after COMMIT
  (empty — checkpointed)
items after COMMIT: 1=ONE
```

Тридцать четыре строки — и весь механизм на виду. Каждая запись несёт свой
`lsn`, номер транзакции и операцию; у `INSERT` есть `row_id` и `after`, у
`UPDATE` — и `before`, и `after`. `INSERT id=1` попал в слот `0:0`, `id=2` — в
`0:1`: адрес строки — это и есть то, что позволяет отменить изменение, не
сканируя таблицу.

Самое интересное — шаг 7. После `ROLLBACK TO SAVEPOINT s1` строка `id=2` исчезла
из таблицы, но запись `lsn=5` о её вставке **осталась в журнале**: журнал
описывает, что произошло, а не то, что осталось. Именно поэтому восстановление
не может просто взять «все `INSERT` транзакции»: ему пришлось бы отменять и ту
вставку, которую живой процесс уже отменил. Оно прогоняет записи через тот же
`Transaction`, и `ROLLBACK_TO_SAVEPOINT` в журнале снова вычёркивает `lsn=5` из
списка отмен — ровно как это сделал живой откат.

Шаг 8 объясняет, почему в обычной жизни журнала почти не видно: после `COMMIT`
он усечён до нуля. Всё, что в нём было, уже не нужно — данные синхронизированы
**до** того, как появился маркер `COMMIT`.

### 2. `kill -9`: незавершённое откатывается, подтверждённое остаётся

Здесь процесс убивают по-настоящему — `SIGKILL` самому себе, без завершающих
функций, деструкторов и сброса буферов. Перед этим успевают пройти три вещи:
подтверждённая вставка `id=3`, незавершённая вставка `id=10` и незавершённое
обновление `id=1`. Второй скрипт открывает ту же базу — и восстановление
запускается в конструкторе `Executor`, до первого оператора.

```bash
docker compose cp /tmp/ch18-crash.php php:/tmp/ch18-crash.php
docker compose cp /tmp/ch18-recover.php php:/tmp/ch18-recover.php
docker compose exec php php /tmp/ch18-crash.php; echo "crash exit=$?"
docker compose exec php php /tmp/ch18-recover.php
```

```php
<?php

declare(strict_types=1);

/**
 * Half of the crash experiment: do some work, leave a transaction open, and
 * die.
 *
 * Two writes go through before the crash and one does not, which is the
 * whole point:
 *
 *   - id=3 is committed inside its own explicit transaction, so its WAL
 *     records were checkpointed away and its pages were fsync()'d before
 *     the COMMIT record was even written;
 *   - id=10 and the update of id=1 sit in an open transaction with no
 *     COMMIT record — the pages are already written, the log says they are
 *     not finished, and that is precisely what recovery reads.
 *
 * `SIGKILL` rather than `exit()`: no shutdown function runs, no destructor
 * closes a file, no output buffer is flushed by PHP. This is the crash the
 * next script recovers from.
 *
 * Run inside the container:
 *
 *   docker compose cp /tmp/ch18-crash.php php:/tmp/ch18-crash.php
 *   docker compose exec php php /tmp/ch18-crash.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = '/tmp/ch18-crashdb';

// A fresh database on every run, so the same transcript comes out again.
$remove = static function (string $path) use (&$remove): void {
    if (!file_exists($path)) {
        return;
    }

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

$database = Database::open($directory);
$executor = new Executor($database);
$executor->run('CREATE TABLE notes (id INT PRIMARY KEY, body VARCHAR(20))');
$executor->run("INSERT INTO notes (id, body) VALUES (1, 'alpha'), (2, 'beta')");

$notes = static function (Executor $executor): string {
    return implode(', ', array_map(
        static fn (Row $row): string => sprintf('%d=%s', $row->get('id'), $row->get('body')),
        iterator_to_array($executor->run('SELECT id, body FROM notes ORDER BY id')->rows),
    ));
};

// A committed transaction: durable, and nothing left in the log to recover.
$executor->run('BEGIN');
$executor->run("INSERT INTO notes (id, body) VALUES (3, 'gamma')");
$executor->run('COMMIT');
printf("committed:  id=3, so its WAL records were checkpointed away\n");

// An abandoned transaction: written to the heap, never finished.
$executor->run('BEGIN');
$executor->run("INSERT INTO notes (id, body) VALUES (10, 'never')");
$executor->run("UPDATE notes SET body = 'CHANGED' WHERE id = 1");

printf("wal before the crash: %d bytes\n", (int) filesize($directory . '/wal/wal.log'));
printf("rows before the crash: %s\n", $notes($executor));
printf("killing this process with SIGKILL\n");

fflush(STDOUT);

posix_kill(posix_getpid(), SIGKILL);
```

**Перевод комментариев:**
- `ch18-crash.php` (скрипт, PHPDoc): половина эксперимента со сбоем: сделать
  работу, оставить транзакцию открытой и умереть. До сбоя проходят две записи, а
  одна нет — в этом весь смысл: `id=3` подтверждён внутри собственной явной
  транзакции, поэтому её записи WAL были усечены, а страницы `fsync()`-нуты ещё
  до того, как записалась даже запись `COMMIT`; `id=10` и обновление `id=1`
  сидят в открытой транзакции без записи `COMMIT` — страницы уже записаны,
  журнал говорит, что они не завершены, и именно это читает восстановление.
  `SIGKILL`, а не `exit()`: ни одна shutdown-функция не выполняется, ни один
  деструктор не закрывает файл, PHP не сбрасывает выходной буфер. Это тот сбой,
  из которого восстанавливается следующий скрипт. Запускать внутри контейнера.
- `// A fresh database on every run, so the same transcript comes out again.` —
  свежая база на каждом запуске, чтобы получился тот же вывод.
- `// A committed transaction: durable, and nothing left in the log to recover.`
  — подтверждённая транзакция: данные на устройстве, и в журнале не осталось
  ничего, что нужно восстанавливать.
- `// An abandoned transaction: written to the heap, never finished.` —
  брошенная транзакция: записана в кучу, но никогда не завершена.

```php
<?php

declare(strict_types=1);

/**
 * The other half of the crash experiment: open the database the previous
 * script killed, and let the constructor do the recovering.
 *
 * `Execution\Executor::__construct()` wires the undo and sync handlers and
 * then calls `TransactionManager::recover()` unconditionally, before a
 * single statement can run. Recovery finds the abandoned transaction: one with a
 * `BEGIN` and no `COMMIT`/`ROLLBACK`, replays its records through the same
 * `Transaction` state machine a live rollback uses, undoes them newest
 * first, syncs storage, and checkpoints the log.
 *
 * So id=10 must be gone and id=1 back to 'alpha' — while id=3, committed
 * before the crash, must still be exactly there.
 *
 * Run inside the container:
 *
 *   docker compose cp /tmp/ch18-recover.php php:/tmp/ch18-recover.php
 *   docker compose exec php php /tmp/ch18-recover.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = '/tmp/ch18-crashdb';
$wal = $directory . '/wal/wal.log';

printf("wal before recovery: %d bytes\n", is_file($wal) ? (int) filesize($wal) : 0);

$database = Database::open($directory);

// The constructor is the recovery: it runs before any statement can.
$executor = new Executor($database);

$rows = array_map(
    static fn (Row $row): string => sprintf('%d=%s', $row->get('id'), $row->get('body')),
    iterator_to_array($executor->run('SELECT id, body FROM notes ORDER BY id')->rows),
);

printf("rows after recovery: %s\n", implode(', ', $rows));
printf("wal after recovery: %d bytes\n", (int) filesize($wal));

$database->close();
```

**Перевод комментариев:**
- `ch18-recover.php` (скрипт, PHPDoc): другая половина эксперимента со сбоем:
  открыть базу, процесс которой убил предыдущий скрипт, и дать конструктору
  выполнить восстановление. `Execution\Executor::__construct()` подключает
  обработчики отмены и синхронизации, а затем безусловно вызывает
  `TransactionManager::recover()` до того, как может выполниться хоть один
  оператор. Восстановление находит брошенную транзакцию — ту, у которой есть
  `BEGIN` и нет `COMMIT` /`ROLLBACK`, — проигрывает её записи через ту же машину
  состояний `Transaction`, что и живой откат, отменяет их от новых к старым,
  синхронизирует хранилище и усекает журнал. Значит, `id=10` должен исчезнуть, а
  `id=1` — вернуться к `'alpha'`, тогда как `id=3`, подтверждённый до сбоя,
  должен остаться на месте. Запускать внутри контейнера.
- `// The constructor is the recovery: it runs before any statement can.` —
  конструктор и есть восстановление: оно выполняется до любого оператора.

```text
committed:  id=3, so its WAL records were checkpointed away
wal before the crash: 255 bytes
rows before the crash: 1=CHANGED, 2=beta, 3=gamma, 10=never
killing this process with SIGKILL
crash exit=137
wal before recovery: 255 bytes
rows after recovery: 1=alpha, 2=beta, 3=gamma
wal after recovery: 0 bytes
```

Восемь строк, и в них весь контракт. До сбоя:
`1=CHANGED, 2=beta, 3=gamma, 10=never` и 255 байт журнала. После:
`1=alpha, 2=beta, 3=gamma` и **ноль** байт журнала.

`id=3` остался, потому что его транзакция была подтверждена: страницы
синхронизированы, маркер `COMMIT` записан, журнал усечён ещё до сбоя. `id=10`
исчез, а `id=1` вернулся к `alpha`, потому что их транзакция осталась без
маркера: `recover()` нашёл `BEGIN` без `COMMIT`, отменил изменения от новых к
старым (сначала `UPDATE`, потом `INSERT`) и только потом усек журнал. Обратите
внимание на порядок в выводе: строка «`wal before recovery: 255 bytes`»
печатается **до** открытия базы — иначе конструктор успел бы всё починить и
журнал было бы уже не рассмотреть.

Код возврата `137` — это `128 + 9`, то есть `SIGKILL`. Он тоже воспроизводится:
сбой здесь не «примерно как при крахе», а буквально тот же сигнал, который
присылает `kill -9` и OOM-killer.

### 3. Байты журнала: заголовок, длина, crc32

У второго проекта журнал — единственное надёжное состояние, поэтому его формат
должен читаться без подсказок. Скрипт пишет две записи и разбирает получившийся
файл побайтно:

```bash
cd php-mini-search
docker compose cp /tmp/ch18-log-bytes.php php:/tmp/ch18-log-bytes.php
docker compose exec php php /tmp/ch18-log-bytes.php
```

```php
<?php

declare(strict_types=1);

/**
 * The other project's log, from the other end: not a log you undo from, but
 * the only durable state there is.
 *
 * `php-mini-search` keeps no index on disk at all — the index is rebuilt in
 * memory from the log on every start. So its log has to be readable without
 * any help: every record is framed as `uint32(length) uint32(crc32) body`,
 * behind a nine-byte header, so a reader can find record boundaries from the
 * bytes alone and tell a record that was never finished from one that is
 * simply not there.
 *
 * This prints that encoding for two records, and then the one property the
 * whole design rests on: `append()` returns only after `fflush()` and
 * `fsync()`, so the byte count below is what is on disk *before* the file is
 * even closed.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch18-log-bytes.php php:/tmp/ch18-log-bytes.php
 *   docker compose exec php php /tmp/ch18-log-bytes.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Storage\Log;
use PhpMiniSearch\Storage\LogFormat;

$directory = sys_get_temp_dir() . '/ch18-logbytes-' . getmypid() . '/';
$path = $directory . 'corpus.log';

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    foreach (scandir($directory) ?: [] as $entry) {
        if ($entry !== '.' && $entry !== '..') {
            unlink($directory . $entry);
        }
    }

    rmdir($directory);
});

$log = Log::open($path, static function (array $record): void {});

printf("header: magic=%s version=%d, %d bytes\n", LogFormat::MAGIC, LogFormat::VERSION, strlen(LogFormat::header()));

$log->append(['op' => 'put', 'id' => 'doc-1', 'title' => 'Alpha']);
printf("on disk immediately after append() #1: %d bytes\n", (int) filesize($path));

$log->append(['op' => 'put', 'id' => 'doc-2', 'title' => 'Beta']);
printf("on disk immediately after append() #2: %d bytes\n", (int) filesize($path));

$log->close();

$bytes = (string) file_get_contents($path);
$headerLength = strlen(LogFormat::header());

printf("whole file: %d bytes\n", strlen($bytes));
printf("header bytes: %s\n", bin2hex(substr($bytes, 0, $headerLength)));

$offset = $headerLength;
$number = 1;

while ($offset < strlen($bytes)) {
    $frame = unpack('N2', substr($bytes, $offset, LogFormat::FRAME_HEADER));
    $length = $frame[1];
    $checksum = $frame[2];
    $body = substr($bytes, $offset + LogFormat::FRAME_HEADER, $length);

    printf("record %d @%d: length=%d crc32=0x%08x\n", $number, $offset, $length, $checksum);
    printf("  body: %s\n", $body);
    printf(
        "  crc32 recomputed over that body: 0x%08x (%s)\n",
        crc32($body),
        crc32($body) === $checksum ? 'matches the frame' : 'MISMATCH',
    );

    $offset += LogFormat::FRAME_HEADER + $length;
    $number++;
}

$scan = Log::inspect($path);
printf(
    "inspect(): status=%s records=%d validBytes=%d fileBytes=%d\n",
    $scan->status,
    $scan->records,
    $scan->validBytes,
    $scan->fileBytes,
);
```

**Перевод комментариев:**
- `ch18-log-bytes.php` (скрипт, PHPDoc): журнал второго проекта с другой
  стороны: это не журнал, из которого откатывают, а единственное устойчивое к
  сбоям состояние, которое есть. `php-mini-search` вообще не хранит индекс на
  диске — индекс при каждом старте перестраивается в памяти из журнала. Поэтому
  его журнал должен читаться без подсказок: каждая запись обрамлена как
  `uint32(length) uint32(crc32) body` за девятибайтовым заголовком, так что
  читатель находит границы записей по самим байтам и отличает запись, которую не
  дописали, от записи, которой просто нет. Скрипт печатает эту кодировку для
  двух записей, а затем единственное свойство, на котором держится весь дизайн:
  `append()` возвращает управление только после `fflush()` и `fsync()`, поэтому
  число байт ниже — это то, что на диске, **до** того, как файл вообще закрыт.
  Запускать внутри контейнера, где проект смонтирован в `/app`.

```text
header: magic=MSLOG version=1, 9 bytes
on disk immediately after append() #1: 58 bytes
on disk immediately after append() #2: 106 bytes
whole file: 106 bytes
header bytes: 4d534c4f4700000001
record 1 @9: length=41 crc32=0x08896a9d
  body: {"op":"put","id":"doc-1","title":"Alpha"}
  crc32 recomputed over that body: 0x08896a9d (matches the frame)
record 2 @58: length=40 crc32=0x2a0cf1d0
  body: {"op":"put","id":"doc-2","title":"Beta"}
  crc32 recomputed over that body: 0x2a0cf1d0 (matches the frame)
inspect(): status=clean records=2 validBytes=106 fileBytes=106
```

Двенадцать строк. Первое, что стоит заметить: `58` и `106` байт — это размер
файла **сразу после `append()`**, до `close()`: запись уже на устройстве, а не в
буфере процесса. Второе — заголовок `4d534c4f4700000001`: `MSLOG` плюс `uint32`
версии, big-endian. Третье — кадр: `length=41`, `crc32=0x08896a9d`, затем ровно
41 байт тела. Сдвиг следующей записи получается сложением, а не поиском перевода
строки: `9 + 8 + 41 = 58` — и вторая запись действительно начинается на
байте 58. Контрольная сумма, пересчитанная по телу, совпадает с записанной —
именно эта проверка через несколько строк отличит обрыв от порчи.

### 4. Что сбой оставляет в журнале: четыре состояния

Настоящее отключение питания скриптом не устроить, поэтому четыре состояния
файла строятся вручную — ровно те, что может оставить сбой. Каждое сначала
**читается** (`inspect()` ничего не меняет), потом **открывается** (чинит),
потом читается снова — чтобы видно было, что починка сохранилась.

```bash
docker compose cp /tmp/ch18-log-recovery.php php:/tmp/ch18-log-recovery.php
docker compose exec php php /tmp/ch18-log-recovery.php
```

```php
<?php

declare(strict_types=1);

/**
 * What a crash can leave behind in a log, and what happens to each shape.
 *
 * Four states of the same file, built by hand because a real power cut is
 * not something a script can schedule:
 *
 *   - clean: every byte belongs to a valid record;
 *   - a torn tail: the last record cut short, which is the *only* thing an
 *     interrupted append can produce, since every record was fsynced before
 *     it was acknowledged — cut it off, keep the rest;
 *   - damage in the middle: a record that fails its checksum while more
 *     records follow it, which no interrupted write can explain — move the
 *     whole file aside and start over, because truncating there would drop
 *     acknowledged records silently;
 *   - a header cut off mid-creation: a prefix of the header, so nothing was
 *     ever written — treat it as no log at all.
 *
 * Every variant is inspected first (reading changes nothing), then opened
 * (which repairs), then inspected again to show the repair stuck.
 *
 * Run inside the container:
 *
 *   docker compose cp /tmp/ch18-log-recovery.php php:/tmp/ch18-log-recovery.php
 *   docker compose exec php php /tmp/ch18-log-recovery.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Storage\Log;
use PhpMiniSearch\Storage\LogScan;

$root = sys_get_temp_dir() . '/ch18-logrec-' . getmypid() . '/';
mkdir($root, 0o775, true);

register_shutdown_function(static function () use ($root): void {
    $remove = static function (string $path) use (&$remove): void {
        if (!file_exists($path)) {
            return;
        }

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

    $remove($root);
});

/** Writes a log holding these three records, then leaves it closed and clean. */
$build = static function (string $path, array $ids): void {
    $log = Log::open($path, static function (array $record): void {});

    foreach ($ids as $id) {
        $log->append(['op' => 'put', 'id' => $id, 'title' => strtoupper($id)]);
    }

    $log->close();
};

$report = static function (string $label, string $path): void {
    $scan = Log::inspect($path);
    printf("== %s ==\n", $label);
    printf(
        "inspect: status=%s records=%d validBytes=%d fileBytes=%d%s\n",
        $scan->status,
        $scan->records,
        $scan->validBytes,
        $scan->fileBytes,
        $scan->reason === null ? '' : sprintf(' (%s)', $scan->reason),
    );

    $replayed = [];
    $log = Log::open($path, static function (array $record) use (&$replayed): void {
        $replayed[] = $record['id'];
    });

    printf(
        "open: replayed %d record(s): %s\n",
        count($replayed),
        $replayed === [] ? '(none)' : implode(', ', $replayed),
    );
    printf(
        "open: recovery status=%s, damaged copy kept aside: %s\n",
        $log->recovery()->status,
        $log->damagedCopy() === null ? 'no' : 'yes',
    );
    $log->close();

    $after = Log::inspect($path);
    printf("inspect again after the repair: status=%s records=%d\n\n", $after->status, $after->records);
};

$ids = ['doc-1', 'doc-2', 'doc-3'];

// 1. Clean.
$clean = $root . 'clean.log';
$build($clean, $ids);
$report('clean', $clean);

// 2. A torn tail: cut the last record short, mid-body.
$torn = $root . 'torn.log';
$build($torn, $ids);
$handle = fopen($torn, 'r+b');
ftruncate($handle, (int) filesize($torn) - 6);
fclose($handle);
$report('torn tail (last record cut short)', $torn);

// 3. Damage in the middle: flip one byte inside the second record's body.
$damaged = $root . 'damaged.log';
$build($damaged, $ids);
$bytes = (string) file_get_contents($damaged);
$position = strpos($bytes, 'doc-2');
$bytes[$position] = 'X';
file_put_contents($damaged, $bytes);
$report('damaged middle (a flipped byte in record 2)', $damaged);

// 4. A header cut off while the file was being created.
$headless = $root . 'headless.log';
file_put_contents($headless, 'MSLOG');
$report('header cut off mid-creation', $headless);
```

**Перевод комментариев:**
- `ch18-log-recovery.php` (скрипт, PHPDoc): что сбой может оставить в журнале и
  что происходит с каждой формой. Четыре состояния одного файла, собранные
  вручную, потому что настоящее отключение питания скриптом не запланируешь:
  `clean` — каждый байт принадлежит валидной записи; оборванный хвост —
  последняя запись обрезана, и это **единственное**, что может дать прерванный
  `append()`, поскольку каждая запись была `fsync()`-нута до подтверждения, —
  отрезать и сохранить остальное; порча в середине — запись не проходит
  контрольную сумму, а после неё есть ещё записи, чего не объяснить прерванной
  записью, — отложить весь файл в сторону и начать заново, потому что усечение
  там молча выбросило бы подтверждённые записи; заголовок, обрезанный при
  создании, — префикс заголовка, значит, ничего никогда не писалось, — считать,
  что журнала нет вовсе. Каждый вариант сначала инспектируется (чтение ничего не
  меняет), затем открывается (что чинит), затем инспектируется снова, чтобы
  показать, что починка сохранилась. Запускать внутри контейнера.
- `$build` (PHPDoc): записывает журнал с этими тремя записями и оставляет его
  закрытым и чистым.
- `// 1. Clean.` — 1. Чистый.
- `// 2. A torn tail: cut the last record short, mid-body.` — 2. Оборванный
  хвост: обрезать последнюю запись посередине тела.
- `// 3. Damage in the middle: flip one byte inside the second record's body.` —
  3. Порча в середине: перевернуть один байт в теле второй записи.
- `// 4. A header cut off while the file was being created.` — 4. Заголовок,
  обрезанный при создании файла.

```text
== clean ==
inspect: status=clean records=3 validBytes=156 fileBytes=156
open: replayed 3 record(s): doc-1, doc-2, doc-3
open: recovery status=clean, damaged copy kept aside: no
inspect again after the repair: status=clean records=3

== torn tail (last record cut short) ==
inspect: status=torn_tail records=2 validBytes=107 fileBytes=150 (record at byte 107 is cut short)
open: replayed 2 record(s): doc-1, doc-2
open: recovery status=torn_tail, damaged copy kept aside: no
inspect again after the repair: status=clean records=2

== damaged middle (a flipped byte in record 2) ==
inspect: status=damaged records=1 validBytes=58 fileBytes=156 (record at byte 58 fails its checksum)
open: replayed 1 record(s): doc-1
open: recovery status=damaged, damaged copy kept aside: yes
inspect again after the repair: status=clean records=0

== header cut off mid-creation ==
inspect: status=missing records=0 validBytes=0 fileBytes=5
open: replayed 0 record(s): (none)
open: recovery status=missing, damaged copy kept aside: no
inspect again after the repair: status=clean records=0
```

Двадцать четыре строки — четыре разных исхода, и каждый по-своему важен.

`clean` — 3 записи, 156 байт, всё переиграно; после открытия по-прежнему чисто,
потому что чинить нечего.

`torn tail` — файл обрезан на 6 байт: 150 байт вместо 156, из них 107 валидных.
Сканер видит `torn_tail` и причину «record at byte 107 is cut short», открытие
отрезает хвост и переигрывает две целые записи; следующая проверка —
`clean records=2`. Это и есть штатный сценарий сбоя посреди `append()`:
подтверждённых записей не потеряно ни одной, потому что недописанная никому не
подтверждалась.

`damaged middle` — в теле **второй** записи заменён один байт. Сканер видит
`damaged`, `records=1` и причину «record at byte 58 fails its checksum».
Ключевое: это не последняя запись (после неё есть третья), поэтому обрезка здесь
выбросила бы подтверждённую запись 3. Открытие переигрывает то, что было до
порчи (`doc-1`), откладывает файл целиком (`damaged copy kept aside: yes`) и
создаёт пустой журнал — сервис поднимется в состоянии «нужна переиндексация».
Следующая проверка: `clean records=0`.

`header cut off mid-creation` — 5 байт `MSLOG`, префикс заголовка. Это
`missing`, а не порча: файл был создан и не успел получить ни одной записи.
Открытие создаёт нормальный журнал; после этого `clean records=0`.

## 5. Отказы

| Что ломается                                         | Чем обнаруживается                                                          | Что происходит                                                                                                                               |
| ---------------------------------------------------- | --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| Запись страницы без `fsync()`                        | `PageManager::sync()` существовал, но не вызывался ниоткуда, кроме `VACUUM` | подтверждённые данные могли остаться в кэше ядра, а журнал уже усечён — терять нечего восстанавливать                                        |
| Сбой между `sync()` и маркером `COMMIT`              | маркера нет                                                                 | транзакция считается незавершённой и откатывается; подтверждённой она не была                                                                |
| Сбой между маркером и `sync()` (историческая ошибка) | маркер есть, данные — нет                                                   | `recover()` пропускает транзакцию как завершённую; исправлено переносом `sync()` **до** маркера (эксперимент 2: 0 байт после восстановления) |
| Обрыв последней строки журнала (`db`)                | `Wal::readAll()` прекращает чтение на последней некорректной строке         | запись, которую не успели дописать, не считается частью журнала; `seekPastLastCompleteRecord()` срезает хвост                                |
| Порча в середине журнала (`db`)                      | `StorageException: The WAL at "…" is corrupt at record N.`                  | отказ, а не молчаливое усечение: строки после порчи — подтверждённые                                                                         |
| Сбой между мутацией и её записью в журнал            | ничем не обнаруживается                                                     | названное узкое окно: изменение на диске есть, описания нет                                                                                  |
| Повторная отмена того же изменения                   | `undoInsert`/`undoDelete`/`undoUpdate` проверяют «уже откачено»             | успех, а не исключение — иначе база, пережившая сбой при откате, не открылась бы                                                             |
| Сбой во время восстановления                         | следующий проход начинает заново                                            | восстановление идемпотентно, поэтому доводит начатое до конца (→ эксперимент 2)                                                              |
| Порванная страница                                   | ничем                                                                       | полных образов страниц нет: предполагается, что запись страницы произошла целиком                                                            |
| `ROLLBACK TO SAVEPOINT` на диске, затем крах         | `Page 0 slot 1 holds no record` (до исправления)                            | восстановление прогоняет записи через `Transaction`, а не отменяет все мутации подряд                                                        |
| Обрыв последней записи журнала (`search`)            | `torn_tail`, причина в `LogScan`                                            | хвост отрезается, подтверждённые записи переигрываются (эксперимент 4: 2 из 3)                                                               |
| Порча в середине журнала (`search`)                  | `damaged`, причина с байтовым смещением                                     | файл откладывается в `<log>.damaged-<дата>`, сервис стартует пустым и требует переиндексации (эксперимент 4: 1 из 3)                         |
| Длина записи больше 256 КБ                           | `damaged`, где бы запись ни лежала                                          | испорченное поле длины не может выдать себя за обрыв и унести подтверждённые записи                                                          |
| Заголовок, обрезанный при создании                   | `missing`, а не `damaged`                                                   | файл, который является префиксом заголовка, считается отсутствующим                                                                          |
| `fsync()` вернул `false`                             | `StorageFailure: Could not sync …`                                          | клиент получает `503`, процесс завершается; после перезапуска переигрывается то, что действительно на диске                                  |
| Два процесса на одном журнале (`search`)             | `flock(LOCK_EX\|LOCK_NB)` на `<log>.lock`                                   | `The log … is in use by another process (the service, compact or check).`                                                                    |
| Сбой посреди компактизации                           | временный файл рядом                                                        | на диске всегда один целый журнал: старый или новый                                                                                          |
| Отключение питания на macOS                          | —                                                                           | PHP не использует `F_FULLFSYNC`; гарантии `fsync()` там не действуют                                                                         |
| `kill -9` как доказательство `fsync()`               | —                                                                           | не доказывает: кэш страниц ядра всё равно доедет до диска (эксперимент 2 — про восстановление, не про питание)                               |

Формулировка из `docs/FAILURE-MODEL.md` для теста под нагрузкой стоит того,
чтобы привести её целиком: «Every acknowledged write must be present. An
unacknowledged write may or may not be: its fsync may have finished after the
response was lost. No document the client never sent may appear» —
подтверждённое обязано быть, неподтверждённое может быть или не быть, а того,
чего клиент не присылал, не должно быть вовсе. И там же — граница самого теста:
«`kill -9` is not power loss. The page cache still reaches the disk, so this
test cannot prove that `fsync()` is called at all».

## 6. Как это проверено

### `php-mini-database`

`tests/Unit/Transaction/WalTest.php` — 16 тестов на 29 утверждений, и почти
каждый — про одно из двух решений. Про формат и порядок:
`testLsnsStartAtOneAndIncrement`, `testAppendedRecordsReadBackInOrder`,
`testInsertRecordRoundTripsItsAfterValues`,
`testUpdateRecordRoundTripsBothBeforeAndAfter`,
`testDateTimeValuesRoundTripExactly`, `testBinaryBlobValuesRoundTripExactly` (те
самые теги `__datetime__`/`__base64__`, из-за которых журнал не может быть
«просто JSON»). Про сбой:
`testATornFinalRecordEndsTheLogRatherThanFailingTheRead`,
`testAMalformedRecordBeforeTheEndIsReportedAsCorruption`,
`testAppendingAfterATornFinalRecordStartsAFreshLine`,
`testAppendingAfterAFinalRecordWithoutItsNewlineKeepsIt`,
`testLsnContinuesAfterAReopenRatherThanRestarting`,
`testCheckpointDiscardsEveryRecord`.

`TransactionManagerTest` — 34 теста на 49 утверждений. Восстановление:
`testRecoveryUndoesATransactionThatNeverCommitted`,
`testRecoveryLeavesACommittedTransactionAlone`,
`testRecoveryCheckpointsTheLogAfterward`, `testRecoveryOnAnEmptyLogIsANoOp`.
Барьер и его место: `testTheSyncHandlerRunsBeforeTheCommitRecordIsEvenWritten`,
`testTheSyncHandlerRunsBeforeTheRollbackRecordIsEvenWritten`,
`testRecoverySyncsStorageBeforeCheckpointingToo`,
`testNoSyncHandlerConfiguredIsANoOpNotAnError`. Последние четыре проверяют не
«что-то осталось в журнале», а **отсутствие** маркера — именно это отличает
правильный порядок от первого, недостаточного варианта исправления.

`ExecutorTransactionTest` — 21 тест на 41 утверждение; здесь проверяется
идемпотентность на уровне SQL:
`testRecoveryUndoesATransactionThatNeverCommitted`,
`testARollbackRetriedAfterAFailedSyncDoesNotUndoTheSameChangeTwice`,
`testACrashBetweenARollbacksUndoAndItsWalRecordStillRecoversCleanly`,
`testACrashBetweenARollbacksUndoAndItsWalRecordDoesNotDuplicateARestoredRow`,
`testRecoveryRepairsAnIndexLeftBehindByAHalfAppliedUndo`,
`testRecoveryRemovesAnIndexEntryLeftBehindByAHalfAppliedUndo`.

Живые перезапуски — `tests/Integration/TransactionTest.php`, 4 теста на 8
утверждений: `testAnUncommittedTransactionIsUndoneAfterACrashAndRestart`,
`testACommittedTransactionSurvivesACrashAndRestart`,
`testARollbackToASavepointBeforeACrashLeavesOnlyThePreSavepointWork`,
`testMultipleRestartsEachRecoverCleanlyFromTheLastCommittedState`.

### `php-mini-search`

`tests/Storage/LogTest.php` — 11 тестов на 131 утверждение, и это эксперимент 4
в виде тестов: `testATornLastRecordIsCutOffAtEveryPossibleByte` (обрезает файл
по **каждому** возможному смещению внутри последней записи),
`testALastRecordWithABadChecksumIsATornTail`,
`testADamagedBodyInTheMiddleIsKeptAsideAndTheLogStartsOver`,
`testADamagedLengthFieldIsDamageNotATornTail`,
`testAHeaderCutOffWhileBeingCreatedCountsAsNoLog`,
`testAFileThatIsNotALogIsDamage`, `testScanningNeverChangesTheFile`,
`testRewriteReplacesTheWholeLog`,
`testAnInterruptedRewriteLeavesTheOldLogWhole`.

`tests/Reliability/` — 6 тестов на 809 утверждений, и это самая дорогая часть
набора. `KillDuringWritesTest::testNoAcknowledgedWriteIsLostToAKill` запускает
настоящий сервер, держит четыре соединения под записью, убивает процесс
`kill -9` в случайный момент и перезапускает; по `docs/FAILURE-MODEL.md` пять
прогонов подтверждают от 197 до 557 записей каждый, и каждая подтверждённая
обязана найтись.
`FullDiskTest::testAFullDiskStopsTheServiceAndLosesNoAcknowledgedWrite`
заполняет tmpfs до последних 150 КБ: запрос получает `503`, процесс выходит, а
после освобождения места и перезапуска все подтверждённые записи на месте.

### Прогоны при написании главы

Docker, PHP 8.5.11. `WalTest` — OK, 16/29; `TransactionManagerTest` — OK, 34/49;
`ExecutorTransactionTest` — OK, 21/41; `tests/Integration/TransactionTest.php` —
OK, 4/8; набор модульных тестов `php-mini-database` (`make test`) — OK, **1082
теста, 3338 утверждений** (полный `phpunit` — 1093/3354). В `php-mini-search`:
`tests/Storage/LogTest.php` — OK, 11/131; `tests/Reliability` — OK, 6/809;
полный набор — OK, **180 тестов, 47 230 утверждений**.

## 7. Цифры

| Файл                                        | Строк | Что делает                                                    |
| ------------------------------------------- | ----- | ------------------------------------------------------------- |
| `php-mini-database/src/Transaction/Wal.php` | 257   | журнал: JSON-строки, `fsync()` на каждой записи, усечение     |
| `…/WalRecord.php`                           | 114   | одна логическая запись: `before`, `after`, `row_id`           |
| `…/WalOperation.php`                        | 19    | девять операций журнала                                       |
| `…/TransactionManager.php`                  | 379   | `BEGIN`/`COMMIT`/`ROLLBACK`, восстановление, два хендлера     |
| `…/Storage/PageManager.php`                 | 135   | `write()` (без `fsync()`) и `sync()` (`fflush()` + `fsync()`) |
| `…/Storage/HeapFile.php`                    | 272   | куча; `sync()` делегирует в `PageManager`                     |
| `…/Storage/BTreeIndex.php`                  | 633   | индекс; `sync()` тоже делегирует                              |
| `…/Schema/Database.php`                     | 474   | `syncStorage()` — хендлер, синхронизирующий всё открытое      |
| `…/Execution/Executor.php`                  | 1261  | подключение хендлеров, `recover()` в конструкторе, `undo()`   |
| `php-mini-search/src/Storage/Log.php`       | 221   | единственное надёжное состояние: `open`, `append`, `rewrite`  |
| `…/LogFormat.php`                           | 72    | заголовок, кадр `length + crc32 + body`, предел 256 КБ        |
| `…/LogScanner.php`                          | 134   | обрыв против порчи, четыре состояния                          |
| `…/LogScan.php`                             | 37    | `missing`/`clean`/`torn_tail`/`damaged`                 |
| `…/Durable.php`                             | 76    | `fflush` + `fsync`, синхронизация каталога, полный `write`    |

Живые прогоны (все детерминированы, два прогона каждого совпали байт-в-байт):

| Что                                    | Значение                                                                                                                            |
| -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| `ch18-wal`                             | 34 строки; `lsn` 1–6; после `COMMIT` журнал — 0 байт                                                                                |
| `ch18-crash` + `ch18-recover`          | 8 строк; 255 байт журнала до сбоя, 0 после; код возврата `137`; `1=CHANGED, 2=beta, 3=gamma, 10=never` → `1=alpha, 2=beta, 3=gamma` |
| `ch18-log-bytes`                       | 12 строк; заголовок 9 байт; файл 58 байт после первой записи и 106 после второй; `crc32` обеих записей совпадает                    |
| `ch18-log-recovery`                    | 24 строки; `clean` 3/156, `torn tail` 2/107 из 150, `damaged middle` 1/58 из 156, обрезанный заголовок 5 байт → `missing`           |
| `WalTest`                              | 16 тестов, 29 утверждений                                                                                                           |
| `LogTest`                              | 11 тестов, 131 утверждение                                                                                                          |
| `Reliability` (`kill -9`, полный диск) | 6 тестов, 809 утверждений                                                                                                           |
| Все тесты `php-mini-database`          | 1082 / 3338 (модульные), 1093 / 3354 (полный прогон)                                                                                |
| Все тесты `php-mini-search`            | 180 / 47 230                                                                                                                        |

## 8. Упражнения

1. **Посчитайте.** В эксперименте 1 после `ROLLBACK TO SAVEPOINT s1` в журнале
   шесть записей, хотя строка `id=2` уже удалена. Какие ровно записи отменит
   восстановление, если процесс умрёт на этом шаге, и почему запись `lsn=5` не
   будет отменена дважды?
2. **Объясните.** Почему `syncStorage()` обязан идти **до** записи маркера
   `COMMIT`, а не после? Что именно увидит `recover()` в каждом из двух
   порядков, если сбой случится между этими двумя действиями?
3. **Найдите границу.** Опишите окно между мутацией кучи и записью в журнал в
   терминах конкретных вызовов `Executor`. Почему `RecordId` делает это окно
   неизбежным при выбранном порядке и что пришлось бы изменить, чтобы его
   закрыть?
4. **Сравните.** В `php-mini-search` журнал — единственное состояние, а индекс
   производный; в `php-mini-database` — наоборот. Какой из двух проектов
   переживёт сбой в момент, когда журнал уже усечён, а страницы ещё нет, и
   почему?
5. **Спроектируйте.** Восстановление в `php-mini-database` уже идемпотентно, но
   всё ещё не crash-safe «посередине». Что нужно добавить, чтобы сбой между
   двумя отменами не повторял уже сделанные, — и почему идемпотентности для
   этого недостаточно?
6. **Разберите отказ.** Почему длина записи больше 256 КБ — это `damaged`, а не
   `torn_tail`, даже если такая запись стоит последней? Что произошло бы, если
   бы сканер доверял полю длины без проверки предела?
7. **Проверьте тезис.** `kill -9` не доказывает, что `fsync()` вообще вызывался.
   Придумайте тест, который отличит «вызвали `fsync()`» от «не вызвали, но кэш
   всё равно доехал», и объясните, почему он требует инжектора отказов уровня
   файловой системы.

## Что дальше

- **Глава 19** — анализ текста: Unicode, токены, стемминг. Другой предмет, но та
  же дисциплина: сначала разобрать вход в данные, потом что-то с ними делать.
- **Глава 20** — инвертированный индекс и BM25: индекс, который здесь строится
  из журнала при каждом старте, там считается главным артефактом.
- **Глава 21** — версии, надгробия и переиндексация: что делать, когда журнал
  отложен в сторону и индекс нужно построить заново, не потеряв подтверждённые
  документы.
- **Глава 26** — как проверять сами тесты: `kill -9`, полный диск и инжекция
  ошибок — ровно тот инструментарий, которым проверяются утверждения этой главы.
