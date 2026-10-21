# Глава 2. `fork` и copy-on-write: что на самом деле копируется

> Проект: [`php-memory-lab`](https://github.com/Researcher86/php-memory-lab),
> коммит `7c8d365`. Нужно знать: RSS, PSS и `Private_/Shared_` (→ глава 1).
> Пригодится дальше: процессы и IPC (→ глава 3), пул воркеров на `fork` (→ глава
> 6).

Представьте, что процессу нужно ещё восемь «себя». Первое, что приходит в голову
по цене: восемь копий процесса — восемь копий всей памяти. Восемь копий
200-мегабайтного PHP-процесса это 1.6 ГБ, и ошибка кажется очевидной.

На самом деле `fork()` не копирует **ни одного байта** данных. Он копирует
таблицы страниц и помечает все страницы read-only; данные остаются общими, пока
кто-нибудь не начнёт в них писать. Тогда и только тогда ядро отдаёт писателю
приватную копию одной страницы. Это **copy-on-write** (CoW), и почти всё, что
люди знают про цену `fork`, относится не к нему, а к записи.

Отсюда два вопроса главы:

1. Почему `fork` дешёвый, а мониторинг при этом врёт?
2. Когда общие страницы становятся приватными — и как это увидеть?

Первый ответ — таблицы страниц вместо данных, второй — `Private_Dirty` /
`Shared_Dirty` из `smaps_rollup`, которых не видит RSS.

---

## 1. Проблема

`fork()` возвращает `0` в ребёнке и pid ребёнка в родителе, и после него
существуют два процесса, каждый со своим адресным пространством. Интуиция
подсказывает, что раз процессов стало два, памяти стало вдвое. Измерение эту
интуицию не подтверждает:

```text
$ make experiment ARGS="process:fork"
--- child (pid 91, parent 85) ---
  PHP usage: 2.14 MiB
  RSS:       16.52 MiB
  PSS:        7.70 MiB
  VmSize:    62.90 MiB
fork() took 0.32 ms
```

Ребёнок показывает те же `VmSize`, что и родитель, и RSS, близкий к
родительскому, — но `PSS` ребёнка (7.70 MiB) примерно **вдвое меньше** его же
RSS. Это не ошибка счётчика. Общие страницы RSS считает целиком у каждого
процесса, а PSS делит между всеми, кто на них смотрит. Одна физическая страница,
видимая из двух адресных пространств, — это **одна** страница оперативной
памяти, а не две.

Отсюда три вещи, которые ломают наивный взгляд:

- **`fork` не копирует память.** Копируется структура её описания — таблица
  страниц. 0.32 ms на процесс — это стоимость клонирования таблиц, а не сотен
  мегабайт данных.
- **Складывать RSS по дереву процессов нельзя.** Сумма посчитает общие страницы
  столько раз, сколько процессов их видят.
- **Счёт «когда начнётся писать» — отдельный и постраничный.** Пока никто не
  пишет, делить нечего; первый писатель платит за те страницы, которых коснулся,
  и только за них.

## 2. Устройство: клонирование таблицы, а не данных

### Что делает `fork()`

Адресное пространство процесса описывается таблицей страниц: «адрес N этого
процесса ведёт на физическую страницу P». `fork()` создаёт ребёнку **копию этой
таблицы**, а страницы, на которые обе таблицы указывают, помечает read-only **в
обеих**:

```text
до fork():                     после fork():
parent: [стр.1 rw][стр.2 rw]   parent: [стр.1 r-][стр.2 r-]
                                      ▲          ▲
                                      │  общие те же физические страницы
                                      ▼          ▼
                               child:  [стр.1 r-][стр.2 r-]   (своя таблица, те же страницы)

первая запись в child:         после неё:
parent: [стр.1 r-][стр.2 r-]   parent: [стр.1 r-][стр.2 r-]   ← ничего не изменилось
                               child:  [стр.1' rw][стр.2 r-]  ← только эта страница скопирована
```

Пока обе стороны только читают, обе видят одни и те же физические страницы.
Первая запись в общую страницу вызывает **page fault**: ядро делает приватную
копию страницы, кладёт её в таблицу писателя и повторяет инструкцию записи.
Вторая сторона продолжает смотреть на исходную страницу и не замечает, что
что-то произошло.

### Что стоит «копия» и почему её не видит RSS

Приватная копия — это ровно одна страница, обычно 4 KiB, независимо от того,
сколько байт в ней изменили. `$data[0] = 999` копирует страницу, на которой
лежит элемент 0, а не весь массив и не элемент. Отсюда правило: **цена CoW — за
страницу, а не за элемент и не за массив**.

И отсюда же — почему RSS бесполезен для CoW: страница была резидентной до записи
и остаётся резидентной после. Она просто меняет владельца: из общей становится
частной. RSS не двигается. Двигаются два других поля `smaps_rollup`:

| Поле            | Значение                               | Что показывает про CoW                 |
| --------------- | -------------------------------------- | -------------------------------------- |
| `Shared_Dirty`  | грязные страницы, общие с кем-то       | падает, когда страница приватизируется |
| `Private_Dirty` | грязные страницы только этого процесса | растёт на ту же величину               |
| `Rss`           | все резидентные страницы               | **молчит**                             |
| `Pss`           | резидентные с делением общих           | в сумме по дереву даёт верную картину  |

«Грязная» (dirty) — значит изменённая с момента отображения и ещё не сброшенная
на носитель. Для анонимной памяти это просто «сюда писали».

### Жизненный цикл ребёнка

`fork` — это ещё и обязательства, которые легко забыть:

| Стадия             | Что видно                                         | Кто владеет                       |
| ------------------ | ------------------------------------------------- | --------------------------------- |
| `pcntl_fork()`     | клон таблицы страниц, ни одной копии данных       | родитель и ребёнок делят страницы |
| ребёнок читает     | общие страницы, `Private_Dirty` не двигается      | никто новый                       |
| ребёнок пишет      | первая запись расщепляет страницу                 | только писатель                   |
| ребёнок завершился | **зомби** (`State: Z`), держит pid и код возврата | зомби держит pid                  |
| `pcntl_waitpid()`  | ребёнок «собран», код возврата прочитан           | родитель                          |

Ребёнок, который завершился и не был собран, не исчезает: он остаётся зомби и
держит свой pid, пока родитель не вызовет `waitpid()`. Супервизор, который
никогда не собирает детей, течёт pid'ами, а не памятью.

## 3. Код

### Почему не `sleep`, а сокет

Измерять CoW — значит застать момент, когда ребёнок уже что-то сделал, но ещё не
вышел, и снять снимок **пока состояние существует**. Первая версия экспериментов
расставляла пары `sleep()`: ребёнок спит дольше родителя. Это угадывание
планировщика, и на загруженной машине оно ошибается в обе стороны. `Rendezvous`
заменяет угадывание на два факта:

```php
/**
 * A one-shot meeting point between a parent and the children it forks.
 *
 * The forking experiments all have the same shape: a child gets itself into
 * the state being measured - wrote its pages, read its array, mapped its file
 * - and the parent takes its snapshot while that state still exists. They
 * used to arrange that with a pair of sleeps, the child's long enough to
 * outlast the parent's, which is a guess about scheduling that a loaded
 * machine is free to get wrong: the parent measures before the child has
 * done anything, or after it has already gone.
 *
 * A Unix socket pair replaces the guess with two facts. A child writes one
 * byte when it has arrived and then reads until EOF; the parent reads one
 * byte per child, measures, and closes its end, which is the EOF every child
 * is waiting for. Created before the fork, used once, and only between one
 * parent and its own children.
 */
```

[`src/Experiment/Rendezvous.php#L10-L26`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Experiment/Rendezvous.php#L10-L26)

**Перевод комментариев:**
- `Rendezvous` (PHPDoc): одноразовая точка встречи родителя и форкнутых им
  детей. Эксперименты с форком устроены одинаково: ребёнок приводит себя в
  измеряемое состояние — записывает свои страницы, читает свой массив,
  отображает свой файл, — а родитель снимает снимок, пока это состояние ещё
  существует. Раньше это организовывали парой `sleep()`: сон ребёнка длиннее
  родительского, а это догадка о планировщике, которую загруженная машина вольна
  не оправдать — родитель измерит до того, как ребёнок что-то сделал, или после
  того, как тот уже ушёл. Пара Unix-сокетов заменяет догадку двумя фактами:
  ребёнок пишет один байт, когда прибыл, и читает до EOF; родитель читает по
  байту на ребёнка, измеряет и закрывает свой конец — это и есть EOF, которого
  ждёт каждый ребёнок. Создаётся до `fork()`, используется один раз и только
  между одним родителем и его собственными детьми.

Сокет-пара создаётся **до** `fork()`, чтобы оба конца унаследовались ребёнком:

```php
/** Must be called before pcntl_fork(), so that both ends are inherited. */
public static function create(): self
{
    $sockets = [];

    if (!socket_create_pair(AF_UNIX, SOCK_STREAM, 0, $sockets)) {
        throw new RuntimeException('socket_create_pair() failed: ' . socket_strerror(socket_last_error()));
    }

    /** @var array{Socket, Socket} $sockets */
    return new self($sockets[0], $sockets[1]);
}
```

[`src/Experiment/Rendezvous.php#L41-L52`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Experiment/Rendezvous.php#L41-L52)

**Перевод комментариев:**
- `create()` (PHPDoc): метод нужно вызвать до `pcntl_fork()`, чтобы оба конца
  унаследовались.
- `$sockets` (`@var`): массив из двух сокетов — оба конца пары.

Родитель ждёт по байту на ребёнка, и таймаут превращает «ребёнок умер, не дойдя»
в ошибку, а не в вечное ожидание:

```php
public function awaitArrivals(int $children = 1): void
{
    $this->closeChildEnd();

    if ($this->parentEnd === null) {
        throw new RuntimeException('awaitArrivals() is the parent side');
    }

    socket_set_option($this->parentEnd, SOL_SOCKET, SO_RCVTIMEO, [
        'sec' => self::ARRIVAL_TIMEOUT_SECONDS,
        'usec' => 0,
    ]);

    $arrived = 0;

    while ($arrived < $children) {
        $read = @socket_read($this->parentEnd, $children - $arrived);

        if ($read === false || $read === '') {
            throw new RuntimeException(sprintf(
                '%d of %d children arrived; the rest exited or took longer than %d s',
                $arrived,
                $children,
                self::ARRIVAL_TIMEOUT_SECONDS,
            ));
        }

        $arrived += strlen($read);
    }
}
```

[`src/Experiment/Rendezvous.php#L92-L121`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Experiment/Rendezvous.php#L92-L121)

Обратите внимание: `closeChildEnd()` вызывается первым. Пока родитель держит
свою копию дочернего конца, EOF на его конце не наступит никогда — типичная
ошибка с `socketpair`, из-за которой «ожидание ребёнка» превращается в
самозагадка. Закрыть свой лишний конец — часть протокола, а не уборка.

### Основной цикл форка и `waitpid`

```php
$start = microtime(true);
$pid = pcntl_fork();
$forkTimeMs = (microtime(true) - $start) * 1000;

if ($pid === -1) {
    fwrite(STDERR, "fork() failed\n");
    exit(1);
}

if ($pid === 0) {
    $child = $reporter->snapshot();
    $out->write(sprintf("\n--- child (pid %d, parent %d) ---\n", getmypid(), posix_getppid()));
    $out->write(sprintf("  PHP usage: %s\n", ByteFormatter::format($child->phpUsage)));
    $out->write(sprintf("  RSS:       %s\n", ByteFormatter::format($child->rss ?? 0)));
    $out->write(sprintf("  PSS:       %s\n", ByteFormatter::format($child->pss ?? 0)));
    $out->write(sprintf("  VmSize:    %s\n", ByteFormatter::format($child->virtualMemory ?? 0)));
    exit(0);
}

$waited = pcntl_waitpid($pid, $status);

$out->write(sprintf(
    "\nparent waitpid(%d) returned %d, raw status %d, exit status %d\n",
    $pid,
    $waited,
    $status,
    pcntl_wexitstatus($status),
));
```

[`experiments/04-fork/basic-fork.php#L22-L49`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/experiments/04-fork/basic-fork.php#L22-L49)

Две ветки — это два независимых процесса после `fork`: ребёнок снимает снимок и
выходит, родитель ждёт и читает код возврата. Ребёнок обязан завершиться через
`exit()`, иначе он продолжит выполнять тот же код, что родитель.

### Разбор `smaps_rollup`

Числа CoW берутся из `/proc/<pid>/smaps_rollup`. Читатель — маленький
однообразный парсер: строка вида `Ключ: N kB`, всё в байты по одному множителю.

```php
$values = [];

foreach (explode("\n", $content) as $line) {
    if (preg_match('/^(\w+):\s+(\d+)\s+kB$/', $line, $matches) === 1) {
        $values[$matches[1]] = (int) $matches[2] * 1024;
    }
}
```

[`src/Memory/SmapsRollupReader.php#L21-L27`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/SmapsRollupReader.php#L21-L27)

Ключи, которых нет в текущем ядре, просто не попадут в `$values`, а поля DTO,
которых не оказалось, останутся нулями. Именно это свойство и делает
`smaps_rollup` удобным источником: ядро добавляет поля, парсер этого не боится.
Чтение `smaps_rollup` обходит все отображения процесса, поэтому его берут
**вокруг** фазы работы, а не в каждой итерации.

### Ловушка «сумма RSS по процессам»

```php
$childrenRss = 0;
foreach ($children as $pid) {
    $status = $statusReader->read($pid);
    $childrenRss += (int) $status['VmRSS'];
}

$totalRss = (int) $during->rss + $childrenRss;
```

[`experiments/04-fork/fork-many.php#L57-L63`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/experiments/04-fork/fork-many.php#L57-L63)

`totalRss` — это «приблизительная сумма», и в самом имени содержится
предупреждение. Она складывает RSS родителя и всех детей, а общие страницы
входят в каждое слагаемое целиком. В выводе эксперимента так и написано:
`approx total RSS`. Честная сумма по дереву — это PSS.

## 4. Эксперимент

### `process:fork` — клон без копии

Уже видели выше: ребёнок наследует `VmSize` родителя (62.90 MiB), его RSS 16.52
MiB, а PSS — 7.70 MiB. Родитель после ожидания изменил RSS на +20 KiB: снимки
показывают, что **память не удвоилась**. 0.32 ms — это работа с таблицей
страниц.

### `process:fork-with-data` — 16 MiB наследуются бесплатно

Родитель строит массив на миллион целых, потом форкает ребёнка, который его
только читает:

```text
$ make experiment ARGS="process:fork-with-data"

Delta parent allocated:
  PHP usage: +16.00 MiB
  RSS:       +16.02 MiB
  Private:   +16.02 MiB

--- child after fork (read-only on the inherited array) ---
  PHP usage: 18.23 MiB
  RSS:       32.49 MiB
  PSS:       15.69 MiB
  read check (first + last element): 1000001

parent while child lives (read-only child):
  RSS: 45.15 MiB    PSS: 21.78 MiB
```

Ребёнок прочитал первый и последний элемент — ни одна страница не скопирована.
`Private` родителя остался нулевым дельтой после аллокации. PSS родителя, пока
ребёнок жив (21.78 MiB), **ниже половины суммы**: массив — это одна физическая
картина, видимая из двух адресных пространств. Если бы `fork` копировал, RSS
родителя и ребёнка сложились бы в 32.49 × 2, а не в 45.15.

### `cow:readonly` — чтение не платит

Тот же опыт, но массив читается целиком, все миллион элементов:

```text
$ make experiment ARGS="cow:readonly"
parent, after allocating 1M ints: RSS 45.14 MiB | PSS 35.94 MiB | Shared_Dirty 0 B | Private_Dirty 26.23 MiB
child, after reading whole array: RSS 32.71 MiB | PSS 15.60 MiB | Shared_Dirty 25.69 MiB | Private_Dirty 564.00 KiB
parent, while child is reading (RSS should not move): RSS 45.15 MiB | PSS 21.79 MiB | Shared_Dirty 25.65 MiB | Private_Dirty 608.00 KiB
parent, after child exited: RSS 45.15 MiB | PSS 35.95 MiB | Shared_Dirty 0 B | Private_Dirty 26.24 MiB
```

Чтение всей миллионной таблицы целиком: `Private_Dirty` ребёнка — 564 KiB, и это
не массив, а служебные страницы самого ребёнка. 16 MiB массива остались в
`Shared_Dirty` (25.69 MiB — массив плюс разделяемые библиотеки). RSS родителя не
сдвинулся: страницы остаются резидентными, меняется только владелец.

### `cow:single-write` — одна запись, одна страница

```text
$ make experiment ARGS="cow:single-write"
child, BEFORE writing one element: RSS 32.49 MiB | PSS 15.49 MiB | Shared_Dirty 25.71 MiB | Private_Dirty 536.00 KiB
child, AFTER  writing $data[0] = 999: RSS 33.05 MiB | PSS 15.72 MiB | Shared_Dirty 25.67 MiB | Private_Dirty 580.00 KiB
child Private_Dirty delta: 44.00 KiB (+45056 bytes) - the shared page holding $data[0] became private
parent verifies data still intact: $data[0] = 0, $data[999999] = 999999
```

`$data[0] = 999` приватизировал 44 KiB — те страницы, где лежат первые элементы,
— а не 16 MiB. И родитель по-прежнему видит `$data[0] = 0`: у него своя
страница, и изменение ребёнка его не коснулось. Это и есть CoW целиком:
разделение по умолчанию, копия при первой записи, независимость после неё.

### `cow:many-writes` — платят за страницы, не за записи

Четыре строки, каждая — свежий форк; ребёнок пишет каждый `N`-й элемент:

```text
$ make experiment ARGS="cow:many-writes"
[1 writes] across 1000000 inherited elements. Writes took 0.01 ms | Private_Dirty +60.00 KiB (+61440B) | Shared_Dirty -48.00 KiB (-49152B) | RSS +484.00 KiB
[1000 writes] across 1000000 inherited elements. Writes took 1.25 ms | Private_Dirty +3.96 MiB (+4153344B) | Shared_Dirty -3.95 MiB (-4141056B) | RSS +484.00 KiB
[10000 writes] across 1000000 inherited elements. Writes took 5.45 ms | Private_Dirty +15.31 MiB (+16056320B) | Shared_Dirty -15.30 MiB (-16044032B) | RSS +484.00 KiB
[1000000 writes] across 1000000 inherited elements. Writes took 48.78 ms | Private_Dirty +15.32 MiB (+16060416B) | Shared_Dirty -15.30 MiB (-16048128B) | RSS +484.00 KiB
```

Здесь три отдельных урока, и третий — самый важный.

- **RSS во всех строках вырос на одни и те же +484 KiB.** Это служебный рабочий
  набор самого ребёнка, а не массив. Если бы доверяли RSS, запись в один элемент
  и запись в миллион выглядели бы одинаково. `Private_Dirty` различает их: 60
  KiB против 15.32 MiB.
- **`Private_Dirty` и `Shared_Dirty` двигаются зеркально.** Сколько страниц
  стало частным, столько же перестало быть общим. Физической памяти не
  прибавилось ни на байт — это видно по неподвижному RSS.
- **Не важно, сколько записей, важно, сколько разных страниц они задевают.** 10
  000 записей с шагом 100 бьют по элементам `0, 100, 200, …` — то есть по
  **всему** 16-мегабайтному массиву, и расщепляют почти все его страницы (+15.31
  MiB). А миллион записей с шагом 1 — те же самые страницы (+15.32 MiB). Как
  только страница стала частной, дальнейшие записи в неё бесплатны.

Проверим арифметику: массив на миллион целых — это 16.00 MiB = 16 384 KiB; при
странице 4 KiB это 4 096 страниц. Полная приватизация — 16.00 MiB, что и
показывает строка на миллион записей (15.32 MiB `Private_Dirty` плюс ~0.5 MiB,
которые были частными изначально). Строка на 1 000 записей с шагом 1000 даёт ~1
000 расщеплённых страниц ≈ 3.96 MiB — почти ровно 1000 × 4 KiB.

### `cow:rewrite` — две стратегии, две разные цены

```text
$ make experiment ARGS="cow:rewrite"
in-place: $data[$k] ++       time    35.47 ms | PHP 480 B | RSS 0 B | Private_Dirty 15.27 MiB (+15632 kB) | Shared_Dirty -15.27 MiB
fresh:    $data = range(...) time     2.95 ms | PHP 480 B | RSS 128.00 KiB | Private_Dirty 16.00 MiB (+16388 kB) | Shared_Dirty -16.00 MiB
```

Обе строки переписывают массив ребёнка и дают примерно одинаковый
`Private_Dirty`. Но **in-place в 12 раз медленнее**: каждое присваивание
попадает в общую страницу и вызывает page fault с копированием. `fresh` строит
новый приватный массив с нуля — общая страница никогда не трогается, копирования
нет, только новые аллокации. Память в итоге у обеих стратегий частная (16 MiB),
но путь к ней разный, и он виден в секундах.

Это важный практический вывод: в ребёнке после `fork` **строить новое дешевле,
чем править унаследованное**, если правка идёт по всему массиву. Правка
окупается, только если она локальна.

### `cow:multiple-children` — разделяйте регионы

Четыре ребёнка пишут четыре непересекающиеся четверти одного массива:

```text
$ make experiment ARGS="cow:multiple-children"
parent BEFORE forking: RSS 45.14 MiB | PSS 35.94 MiB | Shared_Dirty 0 B | Private_Dirty 26.23 MiB
child 0 wrote [0..250000): Private_Dirty 4.36 MiB
...
parent WHILE children rewrite disjoint regions: RSS 45.15 MiB | PSS 13.91 MiB | Shared_Dirty 25.62 MiB | Private_Dirty 636.00 KiB
parent AFTER all children exited: RSS 45.15 MiB | PSS 35.95 MiB | Shared_Dirty 0 B | Private_Dirty 26.24 MiB
```

Каждый ребёнок приватизировал примерно четверть массива (4.36 MiB), никто не
копировал данные соседа. `Shared_Dirty` родителя почти не сдвинулся (осталось
25.62 MiB общих), PSS родителя упал с 35.94 до 13.91, а после выхода детей
**вернулся** к 35.95. Именно так работает «форкнуть прогретый сервис»: каждый
воркер владеет своим куском общего дерева, и страницы не расщепляются дважды.

### `process:multiple-forks` и `process:lifecycle`

Цена форка растёт с числом процессов, но не с объёмом данных:

```text
$ make experiment ARGS="process:multiple-forks"
children  1 | forks 0.37 ms (0.37 ms/fork) | parent RSS 29.16 MiB | parent PSS 13.96 MiB | children RSS 16.72 MiB | approx total RSS 45.88 MiB
children 16 | forks 4.39 ms (0.27 ms/fork) | parent RSS 29.16 MiB | parent PSS  8.59 MiB | children RSS 240.44 MiB | approx total RSS 269.60 MiB
```

RSS родителя не меняется, его PSS падает с каждым новым «дольщиком», а
`approx total RSS` растёт до 269.60 MiB — цифра, которой в реальности нет: общие
страницы посчитаны в каждой строке по разу. PSS родителя — вот честный
показатель.

Жизненный цикл:

```text
$ make experiment ARGS="process:lifecycle"
1) normal exit (exit(0)):  waitpid -> 142, exit status 0
2) non-zero exit (exit(3)): waitpid -> 143, exit status 3
3) zombie: child exits, parent delays waitpid:
   child still unreaped, /proc/144/status: State:	Z (zombie)
4) killed by signal (SIGTERM):
   waitpid -> 145 (after SIGTERM), terminated by signal 15
```

Код возврата читается `pcntl_wexitstatus()`, факт убийства сигналом —
`pcntl_wifsignaled()`, имя сигнала — `pcntl_wtermsig()`. Зомби между выходом и
`waitpid()` виден как `State: Z` в `/proc/<pid>/status`; памяти он не держит, но
pid занят.

## 5. Отказы

| Что ломается                             | Чем это проявится                                                    | Что мешает                                                       |
| ---------------------------------------- | -------------------------------------------------------------------- | ---------------------------------------------------------------- |
| Сумма RSS по дереву процессов            | общий расход завышен, планирование ёмкости ошибочно                  | берут PSS и `Shared_*`/`Private_*`, а не сумму RSS               |
| Ребёнок, который не читает и не пишет    | «наследовал 16 MiB» ничего не стоит, но это не видно в PHP-счётчиках | смотреть RSS/PSS ребёнка, а не `memory_get_usage()`              |
| Правка унаследованного массива в цикле   | тихо копируются целые страницы, время в разы больше                  | писать в свой регион или строить новое значение                  |
| Ожидание ребёнка через `sleep()`         | родитель снимает снимок до или после нужного состояния               | `Rendezvous` на сокет-паре с таймаутом                           |
| Не собрать ребёнка                       | зомби держит pid, супервизор течёт pid'ами                           | `pcntl_waitpid()` на каждого ребёнка                             |
| Забыть закрыть лишний конец `socketpair` | `awaitRelease()`/`awaitArrivals()` висят вечно                       | закрывать свой конец до EOF (в `Rendezvous` это часть протокола) |
| Дети пишут в одни и те же страницы       | одну страницу расщепляют двое, работа и память дублируются           | разбивать данные на непересекающиеся регионы                     |
| Ребёнок продолжил код родителя           | эффект родителя выполняется дважды                                   | ребёнок заканчивает через `exit()`                               |

## 6. Как это проверено

CoW и форк — область, где ошибка измерения неотличима от отсутствия эффекта,
поэтому тесты бьют по двум местам: синхронизации и разбору.

- [`tests/Experiment/RendezvousTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Experiment/RendezvousTest.php)
  форкает трёх детей, ждёт их прибытия и проверяет через
  `waitpid(..., WNOHANG)`, что **ни один не ушёл до `release()`** (иначе окно
  измерения не существует), а второй тест требует, чтобы ребёнок, умерший не
  дойдя, дал `RuntimeException`, а не вечное ожидание.
- [`tests/Memory/SmapsRollupReaderTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Memory/SmapsRollupReaderTest.php)
  разбирает фикстуру `smaps_rollup` и требует, чтобы **каждое** поле DTO
  (включая `Pss_Shmem`, `Private_Dirty`, `Anonymous`) превратилось в байты по
  множителю 1024, пустой текст дал значения по умолчанию, а чтение
  несуществующего pid бросило исключение.
- [`tests/Experiment/ExperimentRunnerTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Experiment/ExperimentRunnerTest.php)
  проверяет, что опция, которую эксперимент не объявил в `supports`, отвергается
  — так `--children` не может молча ничего не сделать.
- [`tests/DocumentationTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/DocumentationTest.php)
  держит синхронными документацию и список экспериментов: команда
  `make experiment ARGS="process:fork"` не может ссылаться на эксперимент,
  которого больше нет.

Все эксперименты форка требуют расширения `pcntl`; они запускаются в контейнере
проекта, а не на хосте, — поэтому и числа, и `/proc` всегда одни и те же.

## 7. Цифры

Записанные в проекте измерения и правила
([`docs/fork-and-cow.md`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/docs/fork-and-cow.md)):

| Что                                       | Значение                                   | Источник                                 |
| ----------------------------------------- | ------------------------------------------ | ---------------------------------------- |
| Стоимость одного `fork()`                 | ≈ 0.3–0.4 ms                               | `process:fork`, `process:multiple-forks` |
| Стоимость `fork` с ростом числа процессов | линейна по числу, **не** по объёму данных  | `process:multiple-forks`                 |
| Массив 1 000 000 целых                    | 16.00 MiB = 4 096 страниц                  | `cow:*`                                  |
| ~128 хеш-бакетов на страницу              | одна 4 KiB страница                        | `cow:single-write`                       |
| Полная приватизация массива               | ≈ 16 MiB `Private_Dirty`; RSS не двигается | `cow:many-writes`                        |

Сравните с собственным прогоном этой главы: 0.32 ms на `process:fork` и
0.26–0.37 ms на форк в `process:multiple-forks` — то же поведение, другие машина
и версия PHP (8.5.11 против 8.5.10 в докладе). Совпадать должны **порядки и
закономерности**, а не байты.

## 8. Упражнения

1. **Посчитайте.** Массив на миллион целых занимает 16 MiB. Сколько это страниц
   по 4 KiB? Сколько нужно записей, чтобы приватизировать его целиком, если
   писать каждый элемент подряд? Ответ: 4 096 страниц; примерно 4 096 записей —
   по одной на первую запись в каждую страницу.
2. **Объясните.** Почему «1 000 записей» дают ~3.96 MiB `Private_Dirty`, а «10
   000 записей с шагом 100» — уже 15.31 MiB, хотя записей всего в десять раз
   больше? Ответ: шаг 100 разносит записи по всему массиву, и они задевают почти
   все страницы.
3. **Предскажите.** Ребёнок только читает унаследованный массив. Что покажут
   RSS, PSS и `Private_Dirty` ребёнка? А родителя, пока ребёнок жив?
4. **Спроектируйте.** Восемь воркеров обрабатывают миллион записей, каждый —
   свою восьмую. Почему это дешевле, чем восемь воркеров, каждый из которых
   сортирует весь массив? Какие поля это покажут?
5. **Найдите ошибку.** В выводе `process:multiple-forks` для 16 детей написано
   `approx total RSS 269.60 MiB`, хотя реально занято около 29 MiB. Откуда
   взялось 269.60 и почему это не ошибка кода, а ошибка метрики?
6. **Сравните.** В `cow:rewrite` `fresh` в 12 раз быстрее `in-place`, но
   `Private_Dirty` у них почти одинаковый. Что именно оплачивает `in-place`?

## Что дальше

- **Глава 3** — процессы и IPC: пайпы, сокеты, разделяемая память, семафоры. Тот
  же `Rendezvous` — простейший случай связи процессов; дальше — передача данных
  и синхронизация.
- **Глава 6** — пул воркеров: почему сервисы форкают родительский процесс, как
  воркеры делят память и что происходит с CoW под нагрузкой.
- **Глава 18** — WAL и `fsync`: почему «грязная» страница в `Private_Dirty` это
  про память, а долговечность — отдельная история про диск.
