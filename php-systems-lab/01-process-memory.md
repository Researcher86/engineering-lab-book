# Глава 1. Память процесса: виртуальная память, RSS, `mmap`

> Проект: [`php-memory-lab`](https://github.com/Researcher86/php-memory-lab),
> коммит
> [`7c8d365`](https://github.com/Researcher86/php-memory-lab/tree/7c8d365692bcc8756653695b88a62d3412128b88).
> Это первая глава — предыдущих знаний не требуется. Пригодится дальше: `fork` и
> Copy-on-Write (→ глава 2), процессы и IPC (→ глава 3).

Долгоживущий PHP-процесс (пул воркеров, очереди, RoadRunner, FrankenPHP) живёт
часами и сутками. У него спрашивают: «сколько ты занимаешь памяти?» — и получают
**два разных ответа**:

```bash
# в контейнере проекта
php -r 'echo memory_get_usage(true), " ", file_get_contents("/proc/self/status");' \
  | grep -E 'VmRSS|RssAnon|^[0-9]+ '
```

`memory_get_usage()` говорит про единицы мегабайт, `/proc` — про десятки. Оба
числа верны и означают разное. Мониторинг, который смотрит только на одно из
них, ошибается: видит «утечку» там, где её нет, и пропускает её там, где она
есть.

Эта глава — про то, что именно считает каждый из счётчиков, почему они не
сходятся и как это измерить. `mmap` здесь не отдельная тема, а самый резкий
случай расхождения: он меняет адресное пространство на гигабайты, не трогая
физическую память вообще.

---

## 1. Проблема

Наивный способ узнать память процесса — позвать `memory_get_usage()`. Он растёт,
когда код выделяет память, и **падает**, когда код её освобождает:

```php
$before = memory_get_usage();
$bucket = range(1, 1_000_000);
$during = memory_get_usage();   // выросло
unset($bucket);
$after  = memory_get_usage();   // вернулось к $before
```

Из этого легко сделать вывод, что процесс «освободил память». Но это счётчик
**аллокатора PHP**, а не операционной системы. Аллокатор освободил байты у себя
— а страницы, которые он под них получил у ядра, он обычно **оставляет себе**,
чтобы отдать их следующему запросу. Ядро про это не знает: для него эти страницы
по-прежнему принадлежат процессу и занимают планки памяти.

Отсюда два практических следствия, из-за которых всё это важно:

- процесс, освободивший память, **остаётся большим по RSS** — и это не утечка,
  но мониторинг, следящий за RSS, будет об этом кричать;
- и наоборот: то, что выделено **в обход** PHP-аллокатора (`mmap`, SysV shared
  memory, `malloc` через FFI), для `memory_get_usage()` не существует вовсе —
  счётчик его не заметит.

Значит, нужен слой, который измеряет **оба** взгляда сразу и их разность. Именно
он и есть первая фаза проекта `php-memory-lab`.

## 2. Устройство: два взгляда на один процесс

### Виртуальное адресное пространство

Процесс видит непрерывное **виртуальное** адресное пространство: код, стек,
кучу, отображённые файлы и библиотеки. У каждого адреса есть перевод в
физическую страницу — через **таблицу страниц** (page table). Всё это
пространство целиком называется `VmSize` (а его пик — `VmPeak`), и оно может
быть огромным, **ничего не занимая в оперативной памяти**.

```text
Виртуальное пространство (VmSize)                 физические страницы (RSS)
┌───────────────────────────────────────┐
│ text (код)                            │──────► [P][P]
│ heap: память движка PHP               │──────► [P][P][P]  ← это видит memory_get_usage()
│ библиотеки (libc, libphp)             │──────► [P][P]
│ mmap файла                            │──────► [P] …
│ стек                                  │──────► [P]
│ ┆ зарезервировано, но не тронуто ┆    │        (физической страницы нет)
└───────────────────────────────────────┘
     VmSize = весь прямоугольник          VmRSS = только страницы с [P]
```

Стрелка от кучи PHP к физическим страницам показывает отображение, а не
измерение: `memory_get_usage()` считает байты, которыми владеет аллокатор, а не
резидентные страницы. Одна и та же PHP-куча может занимать разное число страниц,
а страницы могут оставаться резидентными после того, как движок вернул память
аллокатору.

`mmap` здесь показателен: он добавляет в таблицу страниц регион «эти адреса
приходят из того файла» и на этом заканчивается. Пока ни один байт не прочитан,
**физической памяти за этим регионом нет**. Пока страница не резидентна,
обращение к ней вызывает **page fault**: ядро подкладывает под адрес реальную
страницу. Если содержимое уже лежит в page cache, достаточно обновить таблицу
страниц — это **minor fault**; он требует работы ядра, но не чтения страницы с
диска. Minor fault не обязательно означает попадание в page cache: первое
касание анонимной страницы тоже minor, потому что ядро выделяет её без диска.
Если страницы в кэше нет, ядро читает её с диска — это **major fault**.

### Что значит «резидентная»

`VmRSS` (resident set size) — число страниц, которые **прямо сейчас** отображены
в физическую память. Ядро не знает, что в них лежит: страница с освобождёнными
байтами арены считается ровно так же, как страница живых объектов. Поэтому RSS
растёт на первом обращении к странице и **не спешит падать**: страницы
возвращаются ядру только когда аллокатор отдаёт целые регионы (`munmap`) или
ядро само отбирает их под давлением.

В `/proc/<pid>/status` резидентные страницы разложены по источнику:

| Поле                | Что это                                                                        |
| ------------------- | ------------------------------------------------------------------------------ |
| `VmSize`/`VmPeak` | всё виртуальное пространство (текущее / пиковое) — может быть огромным без RAM |
| `VmRSS`             | резидентные страницы, общие или нет                                            |
| `RssAnon`           | резидентные анонимные страницы (в основном куча; не вся приватная память)      |
| `RssFile`           | резидентные страницы, привязанные к файлам (код, `mmap`)                       |
| `RssShmem`          | резидентные страницы разделяемой памяти                                        |

### PSS: честный счёт общих страниц

После `fork()` родитель и ребёнок делят одни и те же физические страницы
(Copy-on-Write). Каждый из них показывает эти страницы **целиком** в своём RSS —
то есть RSS общие страницы считает дважды:

```text
          parent RSS   child RSS    сумма    на самом деле
общая      20 MiB       20 MiB     40 MiB     20 MiB RAM
```

`PSS` (proportional set size) делит общую страницу между всеми, кто на неё
смотрит. Именно поэтому для дерева процессов (`fork`, воркеры) нужно брать PSS и
разбивку `Shared_*`/`Private_*`, а не сырой RSS. Для **одного** процесса в
изоляции верен `VmRSS`.

### Где на этой картинке PHP

`memory_get_usage()` следит только за одной областью — кучей, из которой
аллокатор движка (`emalloc`) выдаёт память под zval'ы, хеш-таблицы, строки и
объекты. Он ничего не знает про:

- нативные буферы, отданные `ffi`/`shmop` в обход движка;
- `mmap`-регионы, библиотеки, стек, таблицы страниц;
- разницу между «освобождено движком» и «возвращено ядру».

```text
memory_get_usage()            RSS (VmRSS)
  байты, которыми владеет       резидентные физические страницы
  аллокатор движка
  знает про zval'ы              ничего не знает про zval'ы
  возвращается вниз при free    редко возвращается вниз
```

Отсюда правило проекта: **`MemoryReporter` записывает оба взгляда на каждом
снимке** и никогда не рассуждает по одному из них.

## 3. Код

### Один снимок несёт оба взгляда

```php
final readonly class MemorySnapshot
{
    public function __construct(
        public int $phpUsage,
        public int $phpPeakUsage,
        public int $phpRealUsage,
        public int $phpRealPeakUsage,
        public ?int $rss = null,
        public ?int $virtualMemory = null,
        public ?int $sharedMemory = null,
        public ?int $privateMemory = null,
        public ?int $pss = null,
    ) {
    }
}
```

[`src/Memory/MemorySnapshot.php#L13-L27`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/MemorySnapshot.php#L13-L27)

`MemorySnapshot` — обычный DTO. Стоит заметить: поля `?int` и по умолчанию
`null`. `/proc` есть только на Linux, и на другой машине снимок всё равно должен
строиться — из одних PHP-полей. Отсутствие данных о памяти процесса лучше, чем
невозможность измерить хоть что-то.

### Точка соединения: `MemoryReporter::snapshot()`

```php
public function snapshot(): MemorySnapshot
{
    $status = $this->safeStatus();
    $pss = $this->safePss();

    return new MemorySnapshot(
        phpUsage: memory_get_usage(),
        phpPeakUsage: memory_get_peak_usage(),
        phpRealUsage: memory_get_usage(true),
        phpRealPeakUsage: memory_get_peak_usage(true),
        rss: self::intOrNull($status['VmRSS'] ?? null),
        virtualMemory: self::intOrNull($status['VmSize'] ?? null),
        sharedMemory: self::intOrNull($status['RssShmem'] ?? null),
        privateMemory: self::intOrNull($status['RssAnon'] ?? null),
        pss: $pss,
    );
}
```

[`src/Memory/MemoryReporter.php#L33-L49`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/MemoryReporter.php#L33-L49)

Четыре детали:

- **PHP-счётчики берутся здесь напрямую** из `memory_get_usage()` и её
  вариантов. `memory_get_usage(true)` — это уже не «живые» байты, а весь
  footprint аллокатора, запрошенный у системы.
- **`rss` — это `VmRSS`, `virtualMemory` — `VmSize`, `sharedMemory` —
  `RssShmem`.** Имена короче полей `/proc`, потому что дальше по коду длинные
  читать неудобно.
- **`privateMemory` — это `RssAnon`**, а не вообще «частные» страницы. Это
  упрощение: анонимные страницы — основная часть частного, но частные
  file-backed страницы сюда не попали. В выводе эксперимента (см. ниже) эта
  величина печатается как `Private`.
- **PSS берётся отдельным чтением** `smaps_rollup` (в `$pss`), потому что это
  другой файл и другой парсер.

Чтение `/proc` может провалиться — тогда `safeStatus()`/`safePss()` возвращают
`null`, и OS-поля снимка остаются пустыми, а не бросают исключение.

### `kB` → байты в одном месте

`/proc/self/status` — плоский список `ключ: значение`, где память измеряется в
килобайтах, а `Threads` — просто число:

```php
private function convertValue(string $value): int|string
{
    if (preg_match('/^(\d+)\s+kB$/', $value, $matches) === 1) {
        return (int) $matches[1] * 1024;
    }

    if (is_numeric($value)) {
        return (int) $value;
    }

    return $value;
}
```

[`src/Memory/ProcStatusReader.php#L51-L62`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/ProcStatusReader.php#L51-L62)

Всякий `… kB` превращается в байты по единственному множителю `1024`. Это
сделано намеренно: держать в снимке одни байты и не смешивать единицы проще, чем
помнить, какие поля в килобайтах, а какие нет. Ошибку «забыл умножить на 1024»
ловит unit-тест на фикстуре (раздел 6).

### `smaps_rollup`: кому принадлежат страницы

```php
final readonly class SmapsRollup
{
    public function __construct(
        public int $rss = 0,
        public int $pss = 0,
        public int $pssAnon = 0,
        public int $pssFile = 0,
        public int $pssShmem = 0,
        public int $sharedClean = 0,
        public int $sharedDirty = 0,
        public int $privateClean = 0,
        public int $privateDirty = 0,
        public int $anonymous = 0,
        public int $anonHugePages = 0,
        public int $swap = 0,
    ) {
    }
}
```

[`src/Memory/SmapsRollup.php#L14-L31`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/SmapsRollup.php#L14-L31)

Отдельный DTO под отдельный файл. Поля, которых в текущем ядре нет, просто
остаются нулями — парсер не падает от незнакомой строки. Именно на этом файле
держится вся глава 2: при записи в общую страницу `Shared_Dirty` падает, а
`Private_Dirty` растёт, **хотя `Rss` не шевелится** — так CoW видно напрямую.

### Единый формат чисел

```php
public static function format(int $bytes, int $precision = 2): string
{
    if ($bytes < 0) {
        return '-' . self::format(-$bytes, $precision);
    }

    $units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    $value = (float) $bytes;
    $index = 0;

    while ($value >= 1024 && $index < count($units) - 1) {
        $value /= 1024;
        ++$index;
    }

    $unit = $units[$index];

    $digits = $unit === 'B' ? 0 : $precision;

    return number_format($value, $digits, '.', '') . ' ' . $unit;
}
```

[`src/Memory/ByteFormatter.php#L14-L34`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Memory/ByteFormatter.php#L14-L34)

Все эксперименты печатают числа через один форматтер, поэтому `MiB` в одном
месте означает то же, что `MiB` в другом. Единицы — двоичные (`KiB`/`MiB`,
деление на 1024), а не десятичные `KB`/`MB`: это то, в чём считают страницы и
`/proc`.

### Почему снимок печатает не всё

Эксперимент печатает снимки через `Output`:

```php
public function snapshot(string $label, MemorySnapshot $snapshot): void
{
    $this->line();
    $this->line($label . ':');
    $this->printf("  PHP usage: %s\n", ByteFormatter::format($snapshot->phpUsage));
    $this->printf("  RSS:       %s\n", self::bytes($snapshot->rss));
    $this->printf("  Private:   %s\n", self::bytes($snapshot->privateMemory));
}

public function delta(string $label, MemoryDiff $diff): void
{
    $this->line();
    $this->line(sprintf('Delta %s:', $label));
    $this->printf("  PHP usage: %s\n", ByteFormatter::formatSigned($diff->phpUsage));
    $this->printf("  RSS:       %s\n", self::signedBytes($diff->rss));
    $this->printf("  Private:   %s\n", self::signedBytes($diff->privateMemory));
}

public function smaps(string $label, SmapsRollup $rollup): void
{
    $this->printf(
        "%s: RSS %s | PSS %s | Shared_Dirty %s | Private_Dirty %s\n",
        $label,
        ByteFormatter::format($rollup->rss),
        ByteFormatter::format($rollup->pss),
        ByteFormatter::format($rollup->sharedDirty),
        ByteFormatter::format($rollup->privateDirty),
    );
}
```

[`src/Experiment/Output.php#L70-L98`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Experiment/Output.php#L70-L98)

Обратите внимание на асимметрию. `snapshot()`/`delta()` печатают только три
числа — PHP usage, RSS и замаскированный под `Private` `RssAnon`; PSS и разбивка
`Shared_/Private_` появляются только там, где они нужны, через `smaps()`. Это не
случайность: в обычном (однопроцессном) снимке PSS не равен RSS только из-за
общих страниц с ядром и библиотеками, и выводить его рядом с каждым снимком было
бы шумом.

## 4. Эксперимент

### `memory:empty` — куда уходит освобождённое

Эксперимент Phase 1 выделяет один массив, снимает память до, после и после
`unset()`:

```php
$elements = $options->elements(1_000_000);
$reporter = new MemoryReporter();

$before = $reporter->snapshot();
$out->snapshot('Before', $before);

$bucket = range(1, $elements);
$afterAllocation = $reporter->snapshot();
$out->snapshot('After allocation', $afterAllocation);

$allocation = $reporter->diff($before, $afterAllocation);
$out->delta('allocation', $allocation);

unset($bucket);
$afterCleanup = $reporter->snapshot();
$out->snapshot('After cleanup', $afterCleanup);

$cleanup = $reporter->diff($afterAllocation, $afterCleanup);
$out->delta('cleanup', $cleanup);

$out->measure('elements', $elements);
$out->measure('allocation_php_bytes', $allocation->phpUsage);
$out->measure('allocation_rss_bytes', $allocation->rss);
$out->measure('cleanup_php_bytes', $cleanup->phpUsage);
$out->measure('cleanup_rss_bytes', $cleanup->rss);
```

[`experiments/01-memory-basics/empty.php#L23-L47`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/experiments/01-memory-basics/empty.php#L23-L47)

`range()` кладёт массив в PHP-кучу, а снимки «до», «после» и «после cleanup» с
дельтами между ними снимает один `MemoryReporter`. Запустим эксперимент с
массивом поменьше, чтобы блок уместился в арену аллокатора:

```bash
make experiment ARGS="memory:empty --elements=50000"
```

```text
Experiment: empty-process baseline (Phase 1)
PID: 61

Before:
  PHP usage: 2.14 MiB
  RSS:       29.13 MiB
  Private:   10.23 MiB

After allocation:
  PHP usage: 3.15 MiB
  RSS:       29.14 MiB
  Private:   10.25 MiB

Delta allocation:
  PHP usage: +1.01 MiB
  RSS:       +12.00 KiB
  Private:   +12.00 KiB

After cleanup:
  PHP usage: 2.21 MiB
  RSS:       29.14 MiB
  Private:   10.25 MiB

Delta cleanup:
  PHP usage: -960.65 KiB
  RSS:       0 B
  Private:   0 B

Note: PHP gave back 983,704 bytes of the 1,058,144 it took, and RSS moved by 0 - the allocator kept the arena, so the process stays as large as its peak.

Context: values depend on the PHP version, allocator, and container limits.
```

Что здесь видно:

- **`PHP usage` ушёл вниз, `RSS` — нет.** После `unset()` аллокатор вернул
  движку 960.65 KiB, но **ноль байт** RSS: страницы остались отображёнными под
  будущие запросы. Это и есть та «утечка», которую видит мониторинг по RSS.
- **Даже рост не совпал.** Выделение — `+1.01 MiB` по PHP, но всего `+12 KiB` по
  RSS: арена уже была выкачана ранее, и ядру почти ничего не пришлось давать.
- **`Private` подрос синхронно с RSS**, потому что это `RssAnon` — анонимные
  страницы кучи.

Для контраста — тот же эксперимент со **значением по умолчанию**, миллионом
элементов:

```text
Delta cleanup:
  PHP usage: -15.94 MiB
  RSS:       -16.00 MiB
```

Здесь RSS **вернулся** вместе с PHP. Блок оказался достаточно большим, чтобы
быть отдельным `mmap`-регионом, и аллокатор отдал его ядру целиком. Один и тот
же код, один и тот же `unset()` — противоположный результат. Именно поэтому
проект **измеряет** поведение, а не утверждает его: эксперимент сам печатает,
какая из двух веток сработала:

```php
$returnedToKernel = $cleanup->rss !== null && $cleanup->rss <= $cleanup->phpUsage / 2;
```

[`experiments/01-memory-basics/empty.php#L56-L56`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/experiments/01-memory-basics/empty.php#L56-L56)

### `mmap:lazy` — адрес есть, памяти нет

Теперь самый резкий случай. Отобразим файл в 256 MiB (при `memory_limit 128M`) и
посмотрим, что скажет каждый счётчик:

```bash
make experiment ARGS="mmap:lazy"
```

```text
Experiment: mmap: a mapping is a promise, not a read
PID: 67

File: mmap-lazy-098af06b.bin of 256.00 MiB, page size 4.00 KiB, PHP memory_limit 128M

After mmap() of the whole file:
  VmSize +256.00 MiB   RSS +12.00 KiB   PHP usage +24.16 KiB   minor faults +0

Note: the address space grew by the size of the file and the resident memory did not. Nothing has been read yet.

Note: note also that a 256.00 MiB mapping succeeded under a 128M memory_limit. The limit counts what the PHP allocator hands out, and a mapping is not that - which is the first of several ways this phase and the next make memory that memory_get_usage() cannot see.
  touched      1 pages ( 4.00 KiB): RSS  +1.00 MiB   minor faults     +1    1.00 MiB per fault     0.03 ms
  touched     16 pages (64.00 KiB): RSS        0 B   minor faults     +0           - per fault     0.02 ms
  touched    256 pages ( 1.00 MiB): RSS        0 B   minor faults     +0           - per fault     0.28 ms
  touched  4,096 pages (16.00 MiB): RSS +15.00 MiB   minor faults    +15    1.00 MiB per fault     4.58 ms

Note: RSS follows what was touched, but not one page at a time: the fault count is far below the page count, and each fault brings in close to a megabyte. The kernel reads ahead and maps whole folios, so a sequential walk pays for one fault per large run rather than one per page.

Note: which is why the rows with no faults at all still cost time - those pages were already mapped by the fault before them, and reading them is just memory access.

After munmap():
  VmSize -256.00 MiB   RSS 0 B

Note: munmap() gave back every page that had been touched in one call - the RSS delta across map, touch and unmap is back to noise. Unlike freed PHP memory, which the allocator keeps, an unmapped region is gone from the process immediately.

file_get_contents() of the same 256.00 MiB (memory_limit raised to 512M to allow it):
  PHP usage +256.00 MiB   RSS +256.00 MiB   45.7 ms

Note: the mapping paid for the 16.00 MiB it touched; file_get_contents() paid for all 256.00 MiB, because a PHP string has no way to be partly present - and had to be allowed a larger memory_limit to do it.

Note: this is why a mapped file is the right shape for a large index, a database page cache or a read-mostly dataset, and the wrong shape for anything that is going to be scanned once from end to end anyway.

Context: values depend on the PHP version, allocator, and container limits.
```

Что здесь видно:

- **`VmSize +256 MiB`, `RSS +12 KiB`, `PHP usage +24 KiB`.** Отображение файла
  целиком ничего не прочитало: выросло только адресное пространство. Сама RSS
  почти не сдвинулась: 12 KiB — это не данные файла (файл не читали), а
  служебные страницы вокруг нового отображения.
- **`memory_limit 128M` не помешал** отобразить 256 MiB. Лимит считает то, что
  выдаёт аллокатор движка, а отображение — не он. Ни `memory_get_usage()`, ни
  лимит этого региона не видят.
- **RSS идёт за тем, что тронуто, но не по странице за раз.** Четыре тысячи
  страниц (16 MiB) обошлись в 15 minor-фолтов, примерно по мегабайту каждый:
  ядро читает вперёд и отображает целые «фолио». Поэтому строки, где RSS не
  двинулся (`16 pages`, `256 pages`), всё равно потратили время: их страницы уже
  были подтянуты предыдущим фолтом.
- **`munmap()` вернул всё сразу.** `VmSize -256 MiB`, RSS в ноль. Здесь прямо
  видно различие с `unset()` из первого эксперимента: освобождённая память PHP
  остаётся за процессом, а снятое отображение уходит немедленно.
- **`file_get_contents()` — противоположная модель.** Тот же файл, но в строку:
  `+256 MiB` сразу и по PHP, и по RSS, и лимит пришлось поднять до 512M. Строка
  PHP не умеет присутствовать частично — либо вся, либо никак.

Проверим арифметику фолтов: 4 096 страниц × 4 KiB = 16 384 KiB = **16.00 MiB**
(совпадает со столбцом `touched`). RSS вырос на 15.00 MiB при 15 фолтах, то есть
**15.00 MiB / 15 = 1.00 MiB на фолт** — ровно то, что печатает программа. Первое
обращение к одной странице подтянуло `+1.00 MiB`: readahead приносит не
страницу, а партию.

> **Что за `page size 4.00 KiB`?** Это свойство ядра, а не константа, поэтому
> `Libc::pageSize()` спрашивает его через `sysconf(SC_PAGESIZE)`, а не зашивает
> `4096`:
> [`src/Native/Libc.php#L158-L161`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Native/Libc.php#L158-L161).

### Откуда берётся `mmap` в PHP

В PHP нет `mmap()` и нет способа получить файловый дескриптор из потока, так что
Phase 8 идёт в libc через FFI:

```php
public static function open(string $path, int $size, bool $shared = true): self
{
    if ($size < 1) {
        throw new NativeMemoryException('A mapping needs at least one byte');
    }

    $descriptor = Libc::open($path, Libc::O_RDWR | Libc::O_CREAT, 0o644);

    if ($descriptor < 0) {
        throw new NativeMemoryException(sprintf('Unable to open %s: %s', $path, Libc::lastError()));
    }

    // The file must be at least as long as the mapping. Mapping past the
    // end of a file is allowed and then kills the process with SIGBUS on
    // first touch, which is a worse way to find out. Only ever grown:
    // ftruncate() shrinks as readily as it extends, and mapping the first
    // $size bytes of a longer file is no reason to throw away the rest.
    if (Libc::fileSize($descriptor) < $size && Libc::ftruncate($descriptor, $size) !== 0) {
        $error = Libc::lastError();
        Libc::close($descriptor);

        throw new NativeMemoryException(sprintf('Unable to size %s to %d bytes: %s', $path, $size, $error));
    }

    $address = Libc::mmap(
        $size,
        Libc::PROT_READ | Libc::PROT_WRITE,
        $shared ? Libc::MAP_SHARED : Libc::MAP_PRIVATE,
        $descriptor,
        0,
    );

    if (Libc::addressOf($address) === Libc::MAP_FAILED) {
        $error = Libc::lastError();
        Libc::close($descriptor);

        throw new NativeMemoryException(sprintf('Unable to map %s: %s', $path, $error));
    }

    return new self($path, $size, $shared, $address, $descriptor);
}
```

[`src/Native/MappedFile.php#L51-L91`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Native/MappedFile.php#L51-L91)

**Перевод комментариев:**
- `// The file must be at least as long as the mapping...` — файл должен быть не
  короче отображения. Отображать за концом файла разрешено, но первое обращение
  убьёт процесс `SIGBUS` — это худший способ узнать. Файл можно только
  увеличивать: `ftruncate()` так же легко уменьшает, а отображение первых
  `$size` байт более длинного файла — не повод выбрасывать остальное.

Три вещи, за которые здесь стоит зацепиться:

- **Файл открывают через libc, а не через PHP-поток.** Мэппить нужно дескриптор,
  а PHP его не отдаёт. Поэтому весь этот слой — FFI-обёртки в `Libc`, и они
  собраны в одном файле: FFI-вызовы динамические и невидимы статическому
  анализатору, так что держать их в одном месте — способ остаться типизированным
  во всём остальном коде.
- **Файл растят, но никогда не укорачивают.** `ftruncate()` уменьшает файл так
  же охотно, как увеличивает, а мэппинг первого `$size` байт более длинного
  файла — не повод выбрасывать остальное. Отображение за концом файла ядро
  разрешает, а потом убивает процесс `SIGBUS` при первом обращении —
  обнаруживать это так хуже.
- **Проверка границ — единственная защита.** Всё, что можно посчитать заранее
  (смещения, длины, «регион ещё жив»), `MappedFile` отказывает исключением; всё
  остальное делает железо. Об этом — в таблице отказов.

## 5. Отказы

| Что ломается                            | Чем это проявится                                              | Что мешает                                                             |
| --------------------------------------- | -------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `unset()` освободил память, RSS не упал | мониторинг по RSS докладывает об «утечке» на пике              | снимок несёт и PHP-счётчик, и RSS; план по пику, а не по остатку       |
| `mmap`/FFI/shared memory в обход движка | `memory_get_usage()` и `memory_limit` не видят расхода         | измеряют RSS и `/proc`, а не только PHP-счётчики                       |
| `kB` в `/proc` приняты за байты         | все числа в 1024 раза меньше                                   | единственная точка конверсии `convertValue()` + тест на фикстуре       |
| `/proc` недоступен (не Linux)           | исключение вместо измерения                                    | `safeStatus()`/`safePss()` дают `null`-поля, PHP-часть снимка остаётся |
| `fork()` и общие страницы               | сумма RSS по дереву процессов завышена                         | для дерева берут PSS и `Shared_*`/`Private_*`                          |
| файл укорачивают под отображением       | `SIGBUS`, процесс исчезает без исключения и стектрейса         | не укорачивать замапленные файлы; писать новый файл и переименовывать  |
| снимок в тесном цикле                   | `smaps_rollup` заставляет ядро обойти все отображения — дорого | снимок берут вокруг фазы работы, а не вокруг каждой итерации           |

## 6. Как это проверено

Слой измерения проверяется там, где его легко сломать: на разборе текста и на
арифметике дельт.

- [`tests/Memory/ProcStatusReaderTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Memory/ProcStatusReaderTest.php)
  парсит **фикстуру** — кусок настоящего `/proc/self/status` — и требует, чтобы
  `VmRSS` превратился в `6608 * 1024`, `Threads` остался целым `1`, `Name`
  остался строкой, а строка без двоеточия не попала в результат. Это ловит ровно
  ту ошибку, о которой предупреждает раздел 5.
- [`tests/Memory/SmapsRollupReaderTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Memory/SmapsRollupReaderTest.php)
  проверяет, что **каждое** поле DTO заполняется из соответствующего ключа, а
  пустой текст даёт значения по умолчанию.
- [`tests/Memory/MemoryReporterTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Memory/MemoryReporterTest.php)
  считает дельты на **сфабрикованных снимках** — положительные, отрицательные и
  случай, когда одна сторона без OS-полей: тогда дельта обязана быть `null`, а
  не нулём.
- [`tests/Memory/ByteFormatterTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Memory/ByteFormatterTest.php)
  фиксирует границы `B/KiB/MiB/GiB`, знак и округление.

Отдельная деталь: тесты, читающие **живой** `/proc` (например,
`testSnapshotReturnsLiveProcMetrics`), пропускаются через
`markTestSkipped('/proc is not available on this host')`, если файла нет. Это
делает набор переносимым, но не даёт ему молча «позеленеть» на Linux: в
контейнере проекта `/proc` есть, и живые проверки действительно выполняются.

Наконец, документация здесь — часть продукта, поэтому её тоже проверяют:
[`tests/DocumentationTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/DocumentationTest.php)
требует, чтобы каждый файл в `docs/` был связан из README, каждый упомянутый в
обратных кавычках путь существовал, а каждая команда `make experiment ARGS="…"`
из документов называла существующий эксперимент. Именно поэтому в этой главе и в
`docs/mmap.md` нельзя оставить команду от эксперимента, которого больше нет.

## 7. Цифры

Замеры этого проекта
([`docs/BENCHMARKS.md`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/docs/BENCHMARKS.md)),
набор `native` — то, что относится к отображённым файлам. Записаны в контейнере
проекта (PHP 8.5.10, Linux 7.0.12-linuxkit, aarch64 × 12, `memory_limit 128M`);
это не универсальные числа:

| Операция                    | итераций | median     | ops/sec |
| --------------------------- | -------- | ---------- | ------- |
| mapped file: read 64 KiB    | 20 000   | 54.673 ms  | 365 812 |
| mapped file: write 64 KiB   | 20 000   | 55.217 ms  | 362 207 |
| PHP string: read 64 KiB     | 20 000   | 28.850 ms  | 693 252 |
| PHP string: write 64 KiB    | 200      | 146.650 ms | 1 364   |
| mapped file: `msync` 16 MiB | 200      | 9.305 ms   | 21 493  |

`median` — суммарное время пачки из `iters` операций, поэтому `ops/sec` =
`iters` / median. У `PHP string: write` и `msync` итераций меньше: одна операция
там копирует или синхронизирует 16 MiB.

Читать это стоит так: отображённый файл и `malloc`-буфер работают одинаково —
оба являются `memcpy` в уже отображённую память, и разница между ними в
**владении и времени жизни**, а не в скорости. `substr()` быстрее обоих, потому
что это одна внутренняя копия движка без перехода через FFI. А
`PHP string: write` примерно в 250 раз медленнее записи в отображённый файл: у
PHP нет блочной записи в строку на месте, любое частичное изменение копирует все
16 MiB целиком. Это прямое следствие модели из раздела 4: строка не умеет
присутствовать частично.

## 8. Упражнения

1. **Посчитайте.** В `mmap:lazy` 4 096 страниц по 4 KiB — это сколько MiB?
   Сколько байт пришлось на один minor fault, если RSS вырос на 15.00 MiB при 15
   фолтах? Ответ: 16.00 MiB и ≈ 1.00 MiB.
2. **Объясните разницу.** Почему `memory:empty --elements=50000` не отдаёт RSS
   после `unset()`, а тот же эксперимент с миллионом элементов — отдаёт?
3. **Снимок до чтения.** Что покажут `VmSize`, `RSS` и `PHP usage` сразу после
   `MappedFile::open()` для файла 1 GiB, который ещё ни разу не читали? А
   `memory_get_usage()` после `file_get_contents()` того же файла?
4. **Границы.** В `MappedFile` есть только два слоя защиты доступа —
   `assertWithinBounds()` и железо. Что произойдёт при `read($size + 1, 1)` и
   что — при чтении смещения за концом файла, который кто-то укоротил после
   отображения? Ответ: исключение и `SIGBUS` соответственно.
5. **PSS.** Два процесса после `fork()` делят 20 MiB анонимных страниц. Что
   покажет RSS каждого, что — PSS каждого, и какая сумма верна? Ответ: RSS по 20
   MiB у обоих (сумма 40 — завышена), PSS по 10 MiB, верная сумма — 20 MiB.
6. **Мониторинг.** Алерт стоит на RSS воркера: «больше 200 MiB — утечка». Через
   час после пика RSS держится на 190 MiB, `memory_get_usage()` — 8 MiB.
   Опишите, как отличить утечку от арены аллокатора, не перезапуская процесс.
7. **Классифицируйте.** Какой fault возникнет в каждом случае: (а) первое чтение
   страницы `mmap`-файла, который уже целиком лежит в page cache; (б) первое
   чтение той же страницы после `drop_caches`; (в) запись в общую анонимную
   страницу сразу после `fork()`? Для каждого случая объясните, читает ли ядро
   диск и откуда берётся физическая страница.

## Что дальше

- **Глава 2** — `fork` и Copy-on-Write: почему ребёнок сначала бесплатен, как
  общая страница становится частной и почему это видно только через PSS и
  `Private_Dirty`.
- **Глава 3** — процессы и IPC: пайпы, сокеты, разделяемая память и семафоры; та
  же механика страниц, но уже для связи процессов.
- **Глава 15** — страницы и B-деревья: как отображённый файл становится
  страничным кэшем базы данных.
- **Глава 18** — WAL, `fsync` и восстановление: почему `msync` — про
  долговечность, а не про видимость.
