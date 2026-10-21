# Глава 26. Как проверить сами тесты: оракулы, мутации, `kill -9`

> Проект: [`php-mini-search`](https://github.com/Researcher86/php-mini-search)
> (тег `v1.0.0`, `7ecddc0`), с выходом на остальную лабораторию
> (`php-job-queue`, `php-systems-platform`). Нужно знать: индекс и его
> инварианты (→ главы 19–20), журнал и восстановление (→ главы 8, 18, 21),
> измерение (→ глава 25).
>
> Последняя глава. Она про инструмент, которым проверялось всё предыдущее, и про
> вопрос, который до сих пор оставался без ответа: **а кто проверяет тесты?**

## 1. Проблема

Зелёный прогон тестов означает ровно одно: тесты согласны с кодом. Он не
означает ни того, что тесты знают правильный ответ, ни того, что они вообще
способны упасть. Полсотни зелёных тестов могут быть полсотней тавтологий — и ни
один из них не покраснеет от этого. Проверить тесты можно тремя способами, и все
три использованы в лаборатории:

1. **Оракул.** Написать **вторую**, намеренно наивную реализацию и сравнить её с
   настоящей на случайных данных. Настоящая гоняется за скоростью, наивная — за
   очевидностью; если они расходятся, ошибка хотя бы у одной из них.
2. **Мутация.** Сломать код — и посмотреть, заметят ли тесты. Незамеченная
   поломка это не «поломка, которую не надо делать», а **дыра в тестах**.
3. **`kill -9`.** Убить процесс в момент записи и проверить обещание, которое
   тесты проверить не могут: подтверждённая запись переживает смерть процесса.

У каждого способа есть свой способ быть бесполезным, и все они встречаются:

- **Тест, который не может упасть.** `assertTrue(true)`, проверка «файл
  существует» вместо его содержимого, оракул, который сравнивает код с самим
  собой. Такой тест — не проверка, а её имитация.
- **Оракул, который слабее, чем кажется.** Сравнивать нечего, если данные не
  задевают интересный случай. В лаборатории у этого есть прямая формулировка:
  «сравнение так же сильно, как то, что оно сравнило». Без документов-копий
  перевёрнутый порядок при равных очках **проходил** оракул незамеченным.
- **Флаки-тест.** Тест, который иногда падает, — это не тест: на него нельзя
  опереться и нельзя воспроизвести. Поэтому все тесты лаборатории с генерацией
  данных сеются (`mt_srand`), а провал называет сид и шаг.
- **Проверка не того обещания.** `kill -9` проверяет, что процесс переживает
  **смерть процесса**. Он не проверяет, что запись переживает **пропажу
  питания**: страничный кеш доходит до диска и после убийства процесса. Это
  записано как ограничение теста, а не забыто.

Правило главы, стало быть, одно: **у каждого теста должно быть написано, что он
проверяет и чего он не проверяет.** Тест без второго пункта — это утверждение,
что зелёный цвет что-то значит.

## 2. Устройство

### Три способа, три разных вопроса

```text
чем вообще можно проверить тест

оракул                мутация                 kill -9
вторая, наивная       сломайте код и          убейте процесс в момент
реализация;           посмотрите, упадут      записи и проверьте
сравнение на          ли тесты;               обещание: подтверждённое
случайных данных      выжившая мутация        пережило смерть
истина вычислима      = дыра в тестах         истина в восстановлении
```

Вопросы разные, и путать их нельзя. Оракул отвечает «знает ли тест верный
ответ». Мутация — «умеет ли тест падать». `kill -9` — «переживает ли система то,
что тест воспроизвести не может».

### Оракул: вторая реализация, которая не имеет права быть умной

`NaiveSearch` — прямая реализация BM25 без индекса: хранит документы как есть и
на каждый запрос честно проходит по всем. Она медленная и не оптимизируется
**намеренно**: оптимизируй её — и она начнёт повторять ошибки той реализации,
которую проверяет.

Истина берётся не из кода, а из **сравнения**: индекс и оракул получают один и
тот же поток операций, и на каждом поиске сверяются `total`, идентификаторы в
порядке убывания и оценки. Три решения делают это сравнение сильным:

- **Данные случайны, но сид зафиксирован.** 2500 операций на сид, пять сидов в
  тесте: провал называет сид и шаг, поэтому воспроизводится точно.
- **Повторы специально подстроены.** Четверть записей копирует ранее записанный
  документ: случайные документы почти никогда не совпадают по очкам, а без
  равных очков **порядок при равенстве** не проверяется вовсе.
- **Сравнение проверяет себя.** В конце тест требует, чтобы поисков было больше
  500, а групп с равными очками — больше 100. Оракул, который ничего не сравнил,
  падает так же, как оракул, который нашёл расхождение.

### Мутации: сломать на одну строку

Мутация — это одна правка, которая меняет смысл: строгий знак на нестрогий, `<`
на `>`, убранный `array_reverse`. После каждой правки прогоняются тесты. Правка,
которую заметили, — **убита**; которую не заметили — **выжила**.

Из этого правила есть три следствия, каждое видно в эксперименте 2:

- **Выжившая мутация — не всегда дыра.** Правка в недостижимой ветке
  (`capacity >= 0`, тогда как ёмкость никогда не бывает нулём) ничего не меняет:
  это **эквивалентная** мутация. Отличить её от дыры можно только глазами —
  поэтому мутация не приговор, а вопрос.
- **Одного набора тестов мало.** Примеры («человек написал каждый случай») и
  оракул (случайный поток) ловят разное. Правка, которую убил только оракул, —
  это ровно тот класс ошибок, который примеры не находят.
- **Если правка не применилась, это надо сказать.** Строка, которой нет в файле,
  — не «выжившая мутация», а несостоявшаяся: иначе отчёт врёт в свою пользу.
  Поэтому проверяется, что замена нашлась **ровно один раз**.

### `kill -9`: обещание, а не таймер

Лог — это последовательность **кадров**: заголовок `MSLOG` и версия, затем на
каждую запись — длина и контрольная сумма (`crc32`) и тело JSON. `append()`
пишет **один** кадр и делает `fflush` + `fsync` — и только после этого запись
считается подтверждённой.

Теперь представим, что процесс убили в момент записи. На диске может оказаться:
заголовок без записей; целое число кадров; целые кадры и **половина
следующего**. Это три разных состояния, и различать их обязательно:

```text
то, что осталось на диске после kill -9

[заголовок][кадр 1][кадр 2][кадр 3][кад
                    ▲               ▲
                    │               └─ недописанный кадр: никогда не подтверждался
                    └─ последний подтверждённый кадр

scan: clean | torn_tail | damaged
      └────────┴────────────┴────────── «не дописано» никогда не путается с «испорчено»
```

Отсюда четыре состояния чтения (`LogScan`), и четвёртое существует только ради
того, чтобы **ничего не потерять**:

| Состояние   | Что это                                          | Что делает `open()`                          |
| ----------- | ------------------------------------------------ | -------------------------------------------- |
| `missing`   | лога нет: первый запуск                          | создаёт                                      |
| `clean`     | каждый байт принадлежит записи                   | открывает как есть                           |
| `torn_tail` | последняя запись коротка или не сошлась по сумме | **отрезает** хвост по последней целой записи |
| `damaged`   | запись, которую прерывание не объясняет          | **уносит файл в сторону**, создаёт новый     |

Разница между двумя последними — вся суть. «Недописано» можно отрезать: это
запись, которая никогда не была подтверждена. «Испорчено» отрезать нельзя: за
ним могут стоять подтверждённые записи, и отрезание потеряло бы их молча.
Поэтому порча не чинится, а откладывается в файл `.damaged-<дата>` — вместе с
шансом разобраться.

И предел честно записан в самом тесте: **`SIGKILL` — не пропажа питания.**
Страничный кеш дойдёт до диска и после убитого процесса, поэтому `kill -9` не
доказывает, что `fsync` был вызван. Эту сторону закрывают другие тесты —
`LogTest` и тест полного диска.

### Что ещё этим пользуется

- `tests/Chaos/ChaosTest.php` в `php-job-queue` (семнадцать тестов) убивает и
  теряет процессы вокруг очереди: доставка, повтор, идемпотентность.
- `tests/Reliability/FullDiskTest.php` в `php-mini-search` проверяет то, что
  `kill -9` проверить не может: запись, для которой **нет места**.
- Оракул-подход — там, где есть вторая модель: `PlanAssertions` в
  `php-mini-database` сверяет план с ожидаемой формой, `php-mini-cache` — с
  прямым словарём.

### Чего это стоит

- **Оракул — это вторая реализация, и она тоже может быть неверна.** Расхождение
  говорит «где-то ошибка», а не «ошибка в индексе». Разбирать приходится руками.
- **Мутации медленные.** Каждая правка — полный прогон тестов; пять правок на
  двух наборах — это десять прогонов. Для большой системы это часы.
- **Эквивалентные мутации тратят время.** Их не отличить от дыр автоматически;
  понять, достижима ли ветка, может только человек.
- **`kill -9`-тест по природе гонка.** Момент убийства случаен, поэтому он
  проверяет **инвариант** («подтверждённое пережило»), а не конкретный
  результат. Детерминированную половину — недописанный кадр — приходится
  проверять отдельно, собирая файл байт за байтом.
- **Гарантия ограничена моделью.** Тест утверждает то, что утверждает его модель
  отказа; за её границей (пропажа питания, ядро ОС, диск) он не значит ничего —
  и обязан это сказать.

## 3. Код

### Оракул: наивно по замыслу

```php
final class NaiveSearch
{
    /** @var array<string, array<string, list<string>>> id → field → terms */
    private array $documents = [];

    /**
     * @param array<string, float> $weights
     */
    public function __construct(
        private readonly array $weights = ['title' => 2.0, 'body' => 1.0],
        private readonly float $k1 = 1.2,
        private readonly float $b = 0.75,
    ) {
    }

    /**
     * @param array<string, list<string>> $fields
     */
    public function put(string $id, array $fields): void
    {
        $this->documents[$id] = $fields;
    }

    public function delete(string $id): void
    {
        unset($this->documents[$id]);
    }

    /**
     * @param list<string> $terms
     * @return array{hits: list<array{string, float}>, total: int}
```

[`tests/Support/NaiveSearch.php#L20-L50`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/tests/Support/NaiveSearch.php#L20-L50)

**Перевод комментариев:**
- `$documents` (`@var`): `id → field → terms` — документы, хранимые как есть.
- `__construct()` (PHPDoc): `@param array<string, float> $weights` — веса полей;
  по умолчанию `title` весит `2.0`, `body` — `1.0`.
- `put()` (PHPDoc): `@param array<string, list<string>> $fields` — поля
  документа.
- `search()` (PHPDoc): `@param list<string> $terms` — термы запроса;
  `@return array{hits: list<array{string, float}>, total: int}` — попадания
  (идентификатор и счёт) и общее число совпадений.

### Оракул-тест: поток, сид и подстроенные повторы

```php
use PHPUnit\Framework\TestCase;

/**
 * Random puts, replacements, deletes and searches, run against the index and
 * against the naive oracle side by side. Every search must return the same
 * ids in the same order, with the same scores, and the index must pass its
 * invariant check after every single operation.
 *
 * The vocabulary is small and skewed, and ids come from a small pool, so terms
 * collide constantly, documents are replaced over and over, and ties in score
 * are common — the cases where stale postings, wrong statistics or a sloppy
 * tie-break show up. A quarter of the writes copy an earlier document under a
 * new id: random documents almost never tie to the last bit, and without
 * copies a reversed tie-break passed this test unnoticed. A failure names its
 * seed and step, so it can be replayed.
 */
final class InvertedIndexAgainstOracleTest extends TestCase
{
```

[`tests/Index/InvertedIndexAgainstOracleTest.php#L10-L27`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/tests/Index/InvertedIndexAgainstOracleTest.php#L10-L27)

**Перевод комментариев:**
- `InvertedIndexAgainstOracleTest` (PHPDoc): случайные `put`, замены, удаления и
  поиски прогоняются по индексу и по наивному оракулу параллельно. Каждый поиск
  должен вернуть те же идентификаторы в том же порядке с теми же оценками, а
  индекс должен проходить проверку инвариантов после каждой операции. Словарь
  маленький и смещённый, идентификаторы берутся из маленького пула, поэтому
  термы постоянно сталкиваются, документы снова и снова заменяются, а равные
  оценки часты — именно случаи, где проявляются устаревшие постинги, неверная
  статистика или неаккуратный tie-break. Четверть записей копирует более ранний
  документ под новым идентификатором: случайные документы почти никогда не
  совпадают до последнего бита, а без копий перевёрнутый tie-break проходил этот
  тест незамеченным. Провал называет свой сид и шаг, поэтому его можно
  воспроизвести.

И конец того же теста — проверка, что сравнивать было что:

```php
    // The comparison is only as strong as what it compared.
    $this->assertGreaterThan(500, $searches);
    $this->assertGreaterThan(100, $ties, 'too few equal scores to exercise the tie-break');
}
```

[`tests/Index/InvertedIndexAgainstOracleTest.php#L94-L97`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/tests/Index/InvertedIndexAgainstOracleTest.php#L94-L97)

**Перевод комментариев:**
- `// The comparison is only as strong as what it compared.` — сравнение так же
  сильно, как то, что оно сравнило.

### Тест, который называет свой предел

```php
/**
 * The promise of docs/FAILURE-MODEL.md, on the real process: a write the
 * service acknowledged survives a crash.
 *
 * Four connections keep PUTs in flight against a running service, each one
 * with a newer source version than the last, and SIGKILL lands at a random
 * moment. After a restart, every write that got `200 applied:true` must be
 * there — at its version or a newer one, since a write can be applied and
 * fsynced with its response lost in the crash — and nothing may be there
 * that was never sent.
 *
 * Limit, stated in FAILURE-MODEL.md: SIGKILL is not a power cut. The page
 * cache still reaches the disk after a killed process, so this cannot prove
 * that fsync is called; LogTest and the full-disk test cover the log's side.
 */
final class KillDuringWritesTest extends TestCase
{
    private const int CONNECTIONS = 4;
```

[`tests/Reliability/KillDuringWritesTest.php#L12-L29`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/tests/Reliability/KillDuringWritesTest.php#L12-L29)

**Перевод комментариев:**
- `KillDuringWritesTest` (PHPDoc): обещание `docs/FAILURE-MODEL.md` на реальном
  процессе: запись, которую сервис подтвердил, переживает падение. Четыре
  соединения держат `PUT` в полёте против работающего сервиса, каждое с более
  новой версией источника, чем предыдущее, а `SIGKILL` случается в случайный
  момент. После перезапуска каждая запись, получившая `200 applied:true`, должна
  быть на месте — в своей версии или новее, поскольку запись могла примениться и
  синхронизироваться, а ответ потеряться при падении, — и не должно быть ничего,
  что никогда не отправляли. Предел, записанный в `FAILURE-MODEL.md`: `SIGKILL`
  — не пропажа питания. Страничный кеш доходит до диска и после убитого
  процесса, поэтому это не может доказать, что `fsync` вызван; сторону журнала
  покрывают `LogTest` и тест полного диска.

### Четыре состояния чтения — не два

```php
namespace PhpMiniSearch\Storage;

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
```

[`src/Storage/LogScan.php#L5-L27`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogScan.php#L5-L27)

**Перевод комментариев:**
- `LogScan` (PHPDoc): что нашло чтение журнала. Чтение никогда не меняет файл;
  действовать по результату — работа `Log::open()`, а `bin/mini-search check`
  только печатает его.
- `MISSING` (PHPDoc): файла журнала нет — первый запуск.
- `CLEAN` (PHPDoc): каждый байт принадлежит корректной записи.
- `TORN_TAIL` (PHPDoc): последняя запись коротка или не проходит контрольную
  сумму — запись, которая никогда не завершилась и потому никогда не
  подтверждалась.
- `DAMAGED` (PHPDoc): запись, которую нельзя объяснить прерванной записью.
  Усечение потеряло бы подтверждённые записи.
- `__construct()` (PHPDoc):
  `@param self::MISSING|self::CLEAN|self::TORN_TAIL|self::DAMAGED $status` —
  состояние чтения; `@param int $validBytes` — где кончается последняя
  корректная запись; всё после неё — хвост или порча.

### Что делает `open()` с каждым состоянием

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

/**
 * Reads the log without changing it, under the same lock as open(), so a
 * running service's half-written record is never mistaken for a torn
 * tail. This is `bin/mini-search check`.
```

[`src/Storage/Log.php#L43-L79`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L43-L79)

**Перевод комментариев:**
- `open()` (PHPDoc): `@param Closure(array<string, mixed>): void $onRecord` —
  воспроизводит каждую корректную запись по порядку.
- `inspect()` (PHPDoc): читает журнал, не меняя его, под той же блокировкой, что
  и `open()`, чтобы недописанная запись работающего сервиса никогда не была
  принята за оборванный хвост. Это `bin/mini-search check`.

### Кадр: длина, сумма, тело

```php
final class LogFormat
{
    public const string MAGIC = 'MSLOG';

    public const int VERSION = 1;

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

    /**
     * @return array<string, mixed>|null null when the body is not a JSON object
```

[`src/Storage/LogFormat.php#L20-L55`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogFormat.php#L20-L55)

**Перевод комментариев:**
- `MAX_BODY` (PHPDoc): ни одна корректная запись не приближается к этому:
  документ — не более 1 КБ заголовка и 64 КБ тела. Большая длина — это
  испорченное поле длины, а не оборванная запись (`docs/FAILURE-MODEL.md`).
- `decode()` (PHPDoc): `@return array<string, mixed>|null` — `null`, когда тело
  не является JSON-объектом.

### Почему «оборвано» и «испорчено» — разные вещи

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
final class LogScanner
{
    /**
     * @param Closure(array<string, mixed>): void $onRecord called for each valid record, in order
     */
    public static function scan(string $path, Closure $onRecord): LogScan
    {
        if (!is_file($path)) {
            return new LogScan(LogScan::MISSING, 0, 0, 0);
        }

        $handle = @fopen($path, 'rb');
        if ($handle === false) {
            throw new StorageFailure("Could not open the log {$path}.");
        }

        try {
            return self::read($handle, (int) filesize($path), $onRecord);
        } finally {
            fclose($handle);
        }
    }

    /**
     * @param resource $handle
     * @param Closure(array<string, mixed>): void $onRecord
```

[`src/Storage/LogScanner.php#L9-L55`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/LogScanner.php#L9-L55)

**Перевод комментариев:**
- `LogScanner` (PHPDoc): читает журнал спереди назад, передаёт каждую корректную
  запись в callback и решает, что означает конец файла. Никогда не пишет.
  Единственное важное суждение — оборванный хвост против порчи. Каждая запись
  синхронизируется `fsync` до подтверждения, поэтому падение может оставить
  недописанной не более одной записи, и только последнюю. Отсюда: последняя
  запись коротка или не проходит контрольную сумму → оборванный хвост, она
  никогда не подтверждалась и её безопасно отрезать; длина больше
  `LogFormat::MAX_BODY` → порча, где бы она ни была: испорченное поле длины в
  середине иначе могло бы указать за конец файла и сойти за оборванный хвост, а
  отрезание там молча потеряло бы все подтверждённые записи после него; сбой
  контрольной суммы до последней записи или корректная сумма вокруг тела, не
  являющегося JSON-объектом → порча. Файл короче заголовка, но являющийся его
  префиксом, был обрезан при создании, до первой записи: он считается
  отсутствующим.
- `scan()` (PHPDoc): `@param Closure(array<string, mixed>): void $onRecord` —
  вызывается для каждой корректной записи по порядку.

### `fsync` и его предел

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
final class Durable
{
    /**
     * @param resource $handle
     */
    public static function sync($handle, string $what): void
    {
        if (!@fflush($handle) || !@fsync($handle)) {
            throw new StorageFailure("Could not sync {$what}." . self::lastError());
        }
    }
```

[`src/Storage/Durable.php#L7-L33`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Durable.php#L7-L33)

**Перевод комментариев:**
- `Durable` (PHPDoc): несколько файловых операций, от долговечности которых
  зависит журнал, написанные один раз со всеми проверками, которые допускает
  PHP. Рецепт проверен по `php-src` и `strace` на PHP 8.5
  (`docs/ARCHITECTURE.md`, Storage): `fflush()` перед `fsync()`, потому что
  после первого `fsync()` PHP буферизует поток в libc и ошибка записи всплывает
  только на следующей синхронизации; неудачный `fsync()` возвращает голый
  `false` без `errno` и предупреждения; каталог синхронизируется через
  `fopen($dir, 'r')`; `rename()` атомарен только в пределах одной файловой
  системы — между устройствами PHP молча копирует без синхронизации, — поэтому
  временные файлы лежат рядом с файлом, который заменяют. На macOS PHP не
  использует `F_FULLFSYNC`, поэтому там ничто из этого не переживает пропажу
  питания; на Linux — переживает.

### Атомарная замена: компактация, которую нельзя прервать

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
```

[`src/Storage/Log.php#L97-L120`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Storage/Log.php#L97-L120)

**Перевод комментариев:**
- `rewrite()` (PHPDoc): заменяет журнал ровно этими записями: пишет во временный
  файл рядом, синхронизирует, переименовывает поверх и синхронизирует каталог.
  Падение на любом шаге оставляет один целый журнал — старый или новый. Только
  для остановленного сервиса: это офлайн-компактация.
  `@param iterable<array<string, mixed>> $records` — записи.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-search && make build install
```

Все четыре эксперимента выполняются в контейнере `php-mini-search` (проект в
`/app`). Они проверяют не индекс, а **инструмент проверки**: второй реализации
сравнение, реакцию тестов на поломку, обещание при `SIGKILL` и собственную
детерминированность. Все четыре вывода детерминированы: каждый скрипт прогонялся
дважды и совпал байт-в-байт. Это не совпадение: третий специально устроен так,
чтобы даже случайный момент убийства не попадал в вывод, а четвёртый — чтобы
доказать, что набор тестов, на котором всё это стоит, воспроизводим.

### 1. Оракул: сравнить с тем, что заведомо медленно и просто

Настоящий индекс и наивная реализация получают один и тот же поток из 2000
случайных операций: запись (иногда — копия ранее записанного документа),
удаление, поиск. На каждом поиске сверяются и количество совпадений, и
идентификаторы в порядке, и оценки.

```bash
docker compose cp /tmp/ch26-oracle.php php:/tmp/ch26-oracle.php
docker compose exec php php /tmp/ch26-oracle.php
```

```php
<?php

declare(strict_types=1);

/**
 * The oracle, run as an experiment: a seeded stream of puts, replaces,
 * deletes and searches against the real index and against the naive
 * reference, compared on every search.
 *
 * The test suite has this too (tests/Index/InvertedIndexAgainstOracleTest,
 * seeds 1-5). This run uses a different seed and prints what was compared
 * rather than only failing when it disagrees: how many operations, how many
 * searches, and how many equal scores the comparison actually met. A
 * comparison that met no ties has not tested the tie-break.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch26-oracle.php php:/tmp/ch26-oracle.php
 *   docker compose exec php php /tmp/ch26-oracle.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Index\InvertedIndex;
use PhpMiniSearch\Tests\Support\NaiveSearch;

const OPERATIONS = 2_000;

$vocabulary = ['usb', 'кабел', '3', 'cabl', 'black', 'red', '42', 'fast', 'charg', 'adapt', 'long', 'short', 'pro', 'mini', 'max', 'lite'];

mt_srand(20251019);

$index = new InvertedIndex();
$oracle = new NaiveSearch();

$searches = 0;
$ties = 0;
$disagreements = [];
$violations = 0;
$written = [];

$randomId = static fn (): string => 'doc-' . mt_rand(1, 40);

$randomTerms = static function (int $min, int $max) use ($vocabulary): array {
    $terms = [];

    for ($i = mt_rand($min, $max); $i > 0; $i--) {
        $position = (int) floor((mt_rand() / mt_getrandmax()) ** 2 * count($vocabulary));
        $terms[] = $vocabulary[min($position, count($vocabulary) - 1)];
    }

    return $terms;
};

for ($step = 0; $step < OPERATIONS; $step++) {
    $where = "step {$step}";
    $roll = mt_rand(1, 100);

    if ($roll <= 45) {
        $id = $randomId();
        // A copy of an earlier document now and then, so equal scores are
        // common and the tie-break is exercised rather than assumed.
        $fields = $written !== [] && mt_rand(1, 4) === 1
            ? $written[mt_rand(0, count($written) - 1)]
            : ['title' => $randomTerms(0, 6), 'body' => $randomTerms(0, 25)];

        $written[] = $fields;
        $index->put($id, $fields);
        $oracle->put($id, $fields);
    } elseif ($roll <= 60) {
        $id = $randomId();
        $index->delete($id);
        $oracle->delete($id);
    } else {
        $searches++;
        $terms = $randomTerms(1, 3);
        $limit = mt_rand(1, 10);
        $offset = mt_rand(0, 5);

        $actual = $index->search($terms, $limit, $offset);
        $expected = $oracle->search($terms, $limit, $offset);

        if ($expected['total'] !== $actual->total) {
            $disagreements[] = "{$where}: total";
        }

        $expectedIds = array_map(static fn (array $hit): string => $hit[0], $expected['hits']);
        $actualIds = array_map(static fn ($hit): string => $hit->id, $actual->hits);

        if ($expectedIds !== $actualIds) {
            $disagreements[] = "{$where}: ids and order";
        }

        foreach ($actual->hits as $position => $hit) {
            $expectedScore = $expected['hits'][$position][1] ?? null;

            if ($expectedScore === null || abs($expectedScore - $hit->score) > 1e-9) {
                $disagreements[] = "{$where}: score of {$hit->id}";
            }
        }

        $scores = array_map(static fn ($hit): float => $hit->score, $actual->hits);
        $ties += count($scores) - count(array_unique(array_map('strval', $scores)));
    }

    if ($index->invariantViolations() !== []) {
        $violations++;
    }
}

printf("operations: %d\n", OPERATIONS);
printf("documents the index holds: %d\n", $index->documentCount());
printf("terms in the index: %d\n", $index->termCount());
printf("\nsearches compared against the oracle: %d\n", $searches);
printf("equal-score groups the comparison met: %d\n", $ties);
printf("disagreements: %d\n", count($disagreements));
printf("invariant violations after %d operations: %d\n", OPERATIONS, $violations);

if ($disagreements !== []) {
    printf("first disagreement: %s\n", $disagreements[0]);
}
```

**Перевод комментариев:**
- `ch26-oracle.php` (PHPDoc): оракул, запущенный как эксперимент: seeded-поток
  `put`, замен, удалений и поисков против настоящего индекса и наивной
  реализации, сравнение на каждом поиске. Набор тестов тоже это делает
  (`tests/Index/InvertedIndexAgainstOracleTest`, сиды 1–5). Этот прогон
  использует другой сид и печатает, что именно сравнивалось, а не только падает
  при расхождении: сколько операций, сколько поисков и сколько равных оценок
  сравнение действительно встретило. Сравнение, не встретившее ни одной ничьей,
  не проверило tie-break. Запуск внутри контейнера, где проект смонтирован в
  `/app`: `docker compose cp /tmp/ch26-oracle.php php:/tmp/ch26-oracle.php`;
  `docker compose exec php php /tmp/ch26-oracle.php`.
- `// A copy of an earlier document now and then, so equal scores are` — время
  от времени копия более раннего документа, чтобы равные оценки были часты и
  tie-break проверялся, а не предполагался.

```text
operations: 2000
documents the index holds: 32
terms in the index: 16

searches compared against the oracle: 805
equal-score groups the comparison met: 139
disagreements: 0
invariant violations after 2000 operations: 0
```

`disagreements: 0` — это и есть результат оракула: две независимые реализации
согласились на 805 поисках. Но отдельного внимания стоит строка
`equal-score groups the comparison met: 139`: она говорит, что проверка
**дошла** до равных очков. Без этого числа «0 расхождений» можно было бы
прочитать и так, что интересный случай просто не встретился.

Обратите внимание и на то, чего в выводе нет: времени. Оракул отвечает на вопрос
«верный ли ответ», а не «быстрый ли он». Наивная реализация обязана быть
медленной — иначе она повторяла бы ошибки индекса, а не проверяла его. Числа
здесь — счётчики (`operations`, `searches`, `ties`, `disagreements`), поэтому
вывод и воспроизводится на любой машине.

### 2. Мутации: что заметили, а что нет

Пять однострочных поломок вносятся по очереди в настоящий `TopHits.php`, и после
каждой прогоняются **два** набора тестов: примеры (`InvertedIndexTest`) и
оракул. Файл восстанавливается в `finally`, и последняя строка отчёта
подтверждает, что восстановление байт-в-байт.

```bash
docker compose cp /tmp/ch26-mutants.php php:/tmp/ch26-mutants.php
docker compose exec php php /tmp/ch26-mutants.php
```

```php
<?php

declare(strict_types=1);

/**
 * Who tests the tests: single-line changes are made to the real index and the
 * real suites are run against each changed copy. A change no test notices is
 * a hole in the tests, not a change nobody should make.
 *
 * Two suites are used, because they answer differently. InvertedIndexTest is
 * examples: a person wrote each case. InvertedIndexAgainstOracleTest is the
 * seeded comparison. A change killed by the oracle but not by the examples is
 * exactly the kind of bug examples are bad at - which is the point of having
 * both.
 *
 * The file is patched in place and restored in a finally block; the last line
 * says whether the restore was byte for byte.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch26-mutants.php php:/tmp/ch26-mutants.php
 *   docker compose exec php php /tmp/ch26-mutants.php
 */

require '/app/vendor/autoload.php';

const SOURCE = '/app/src/Index/TopHits.php';

$suites = [
    'examples' => '/app/tests/Index/InvertedIndexTest.php',
    'oracle' => '/app/tests/Index/InvertedIndexAgainstOracleTest.php',
];

/**
 * label => [original line, changed line]. Each must appear exactly once, so a
 * "mutation" that did not apply is reported as such instead of being counted
 * as a survivor.
 */
$mutations = [
    'the tie-break is reversed (id < becomes id >)' => [
        'return strcmp($a->id, $b->id) < 0;',
        'return strcmp($a->id, $b->id) > 0;',
    ],
    'a draw counts as better (score > becomes score >=)' => [
        'return $a->score > $b->score;',
        'return $a->score >= $b->score;',
    ],
    'the page holds one more hit (< becomes <=)' => [
        'if ($this->count() < $this->capacity) {',
        'if ($this->count() <= $this->capacity) {',
    ],
    'the full-page path ignores a zero capacity (> 0 becomes >= 0)' => [
        'if ($this->capacity > 0 && self::better($hit, $this->top())) {',
        'if ($this->capacity >= 0 && self::better($hit, $this->top())) {',
    ],
    'the page is not put back in order' => [
        'return array_reverse($hits);',
        'return $hits;',
    ],
];

$original = (string) file_get_contents(SOURCE);
$rows = [];

try {
    foreach ($mutations as $label => [$from, $to]) {
        $patched = str_replace($from, $to, $original, $count);

        if ($count !== 1) {
            $rows[] = [$label, 'not applied', 'not applied'];

            continue;
        }

        file_put_contents(SOURCE, $patched);

        $killed = [];

        foreach ($suites as $name => $path) {
            $output = [];
            $code = 0;
            exec(
                PHP_BINARY . ' /app/vendor/bin/phpunit --do-not-cache-result ' . escapeshellarg($path) . ' 2>&1',
                $output,
                $code,
            );
            $killed[$name] = $code === 0 ? 'survives' : 'killed';
        }

        $rows[] = [$label, $killed['examples'], $killed['oracle']];
    }
} finally {
    file_put_contents(SOURCE, $original);
}

printf("%-52s %-10s %-10s\n", 'change made to TopHits.php', 'examples', 'oracle');
printf("%-52s %-10s %-10s\n", str_repeat('-', 52), str_repeat('-', 10), str_repeat('-', 10));

foreach ($rows as $row) {
    printf("%-52s %-10s %-10s\n", $row[0], $row[1], $row[2]);
}

$surviving = count(array_filter($rows, static fn (array $row): bool => $row[1] === 'survives' && $row[2] === 'survives'));
$killedByOracleOnly = count(array_filter($rows, static fn (array $row): bool => $row[1] === 'survives' && $row[2] === 'killed'));

printf("\nchanges made: %d\n", count($rows));
printf("changes no suite noticed at all: %d\n", $surviving);
printf("changes the examples missed and the oracle caught: %d\n", $killedByOracleOnly);
printf("the source was restored byte for byte: %s\n", file_get_contents(SOURCE) === $original ? 'yes' : 'no');
```

**Перевод комментариев:**
- `ch26-mutants.php` (PHPDoc): кто тестирует тесты: в настоящий индекс вносятся
  однострочные изменения, и против каждой изменённой копии прогоняются настоящие
  наборы. Изменение, которого не замечает ни один тест, — дыра в тестах, а не
  изменение, которого никто не должен делать. Используются два набора, потому
  что они отвечают по-разному. `InvertedIndexTest` — примеры: каждый случай
  написал человек. `InvertedIndexAgainstOracleTest` — seeded-сравнение.
  Изменение, убитое оракулом, но не примерами, — ровно тот вид бага, в котором
  примеры слабы; ради этого и нужны оба. Файл патчится на месте и
  восстанавливается в блоке `finally`; последняя строка говорит, было ли
  восстановление побайтовым. Запуск внутри контейнера, где проект смонтирован в
  `/app`: `docker compose cp /tmp/ch26-mutants.php php:/tmp/ch26-mutants.php`;
  `docker compose exec php php /tmp/ch26-mutants.php`.
- `$mutations` (PHPDoc): `label => [исходная строка, изменённая строка]`. Каждая
  должна встречаться ровно один раз, чтобы «мутация», которая не применилась,
  была отчитана как таковая, а не посчитана выжившей.

```text
change made to TopHits.php                           examples   oracle    
---------------------------------------------------- ---------- ----------
the tie-break is reversed (id < becomes id >)        killed     killed    
a draw counts as better (score > becomes score >=)   survives   survives  
the page holds one more hit (< becomes <=)           killed     killed    
the full-page path ignores a zero capacity (> 0 becomes >= 0) survives   survives  
the page is not put back in order                    killed     killed    

changes made: 5
changes no suite noticed at all: 2
changes the examples missed and the oracle caught: 0
the source was restored byte for byte: yes
```

Три поломки убиты обоими наборами. Перевёрнутый порядок при равных очках (`id <`
вместо `id >`) ловится даже примерами — на это есть отдельный тест
`testEqualScoresAreOrderedById`. Убранный `array_reverse` и лишний элемент на
странице ловятся тоже.

А две выжили — и это самое интересное в эксперименте, потому что выжившая
мутация означает **дыру**, и у этих двух дыр разная природа.

`a draw counts as better (score > becomes score >=)` — реальная дыра. Компаратор
кучи становится противоречивым именно при **равных** очках: `better(a, b)` и
`better(b, a)` отвечают «да» одновременно, и порядок начинает зависеть от
порядка вставки, а не от идентификатора. Оракул с его 139 равными группами этого
не поймал — доказательство, что «много равных очков» ещё не значит «порядок при
равенстве проверен».

`the full-page path ignores a zero capacity (> 0 becomes >= 0)` — **не** дыра.
Эта ветка недостижима: `InvertedIndex::search()` возвращается раньше, если
`limit < 1` или `offset < 0`, поэтому ёмкость страницы всегда хотя бы `1`.
Мутация ничего не меняет — это эквивалентная мутация. Автоматически её от дыры
не отличить; отличить может только человек, прочитавший условие вызова. Поэтому
строка `changes the examples missed and the oracle caught: 0` не приговор, а
наблюдение: в данном файле наборы тестов оказались одинаково сильными.

Наконец, строка `changes made: 5` — не формальность. Если бы строки для замены
не нашлось, эксперимент сказал бы `not applied`, а не записал поломку в
выжившие: отчёт о мутациях легко подделать в свою пользу, и единственная защита
— проверять, что замена действительно произошла.

### 3. `kill -9`: обещание и его точная половина

Первая половина — настоящая: дочерний процесс пишет записи в реальный лог,
каждая подтверждается `fsync`, после двадцатой он оставляет файл-маркер и
продолжает писать. Родитель дожидается маркера, даёт ему написать ещё немного —
и убивает `SIGKILL`. Затем лог открывается **настоящим** восстановлением и
проверяется обещание.

Вторая половина — та же ситуация, но собранная байт за байтом: один целый кадр и
половина следующего. Здесь нет ни таймера, ни случайности.

```bash
docker compose cp /tmp/ch26-kill-nine.php php:/tmp/ch26-kill-nine.php
docker compose exec php php /tmp/ch26-kill-nine.php
```

```php
<?php

declare(strict_types=1);

/**
 * kill -9 in the middle of a write, twice: once for real, once exactly.
 *
 * A child appends records to the real log, each one synced, and writes a
 * marker file once the first twenty have been acknowledged. The parent waits
 * for that marker, lets it write a little more, and kills it with SIGKILL -
 * no cleanup, no chance to flush, no chance to say anything. What recovery
 * must then promise is narrow and checkable: every acknowledged record is
 * there, byte for byte; every recovered record is one the writer could have
 * written; and nothing was moved aside as damaged.
 *
 * The second half is the same question asked deterministically: a file with
 * one whole frame and half of the next. That is a write that never finished -
 * bit for bit, with no timing involved - and it must scan as a torn tail and
 * be cut, not treated as damage.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch26-kill-nine.php php:/tmp/ch26-kill-nine.php
 *   docker compose exec php php /tmp/ch26-kill-nine.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Storage\Log;
use PhpMiniSearch\Storage\LogFormat;
use PhpMiniSearch\Storage\LogScan;

const ACKNOWLEDGED = 20;

$directory = sys_get_temp_dir() . '/ch26-kill-' . getmypid() . '/';

$removeTree = static function (string $path) use (&$removeTree): void {
    if (is_dir($path) && !is_link($path)) {
        foreach ((array) scandir($path) as $entry) {
            if ($entry !== '.' && $entry !== '..') {
                $removeTree($path . '/' . $entry);
            }
        }

        @rmdir($path);

        return;
    }

    @unlink($path);
};

@mkdir($directory . 'data', 0o775, true);
@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory, $removeTree): void {
    $removeTree($directory);
});

$expected = static function (int $seq): array {
    return ['seq' => $seq, 'payload' => 'record-' . $seq];
};

// --- a child that keeps writing until it is killed -------------------------
$writerPath = $directory . 'writer.php';

$writer = <<<'PHP'
<?php

declare(strict_types=1);

require '/app/vendor/autoload.php';

use PhpMiniSearch\Storage\Log;

$logPath = $argv[1];
$markerPath = $argv[2];
$acknowledged = (int) $argv[3];

$log = Log::open($logPath, static function (array $record): void {
});

for ($seq = 1; ; $seq++) {
    $log->append(['seq' => $seq, 'payload' => 'record-' . $seq]);

    if ($seq === $acknowledged) {
        file_put_contents($markerPath, (string) $acknowledged);
    }
}
PHP;

file_put_contents($writerPath, $writer);

$logPath = $directory . 'data/search.log';
$markerPath = $directory . 'acknowledged';

$child = proc_open(
    [PHP_BINARY, $writerPath, $logPath, $markerPath, (string) ACKNOWLEDGED],
    [1 => ['file', $directory . 'logs/writer.out', 'a'], 2 => ['file', $directory . 'logs/writer.err', 'a']],
    $pipes,
);

if (!is_resource($child)) {
    printf("could not start the writer\n");
    exit(1);
}

$deadline = microtime(true) + 20.0;
$running = true;

while (!is_file($markerPath) && microtime(true) < $deadline) {
    usleep(10_000);
    $running = proc_get_status($child)['running'];
}

$acknowledged = is_file($markerPath) && $running;

// Let it write beyond the acknowledged point, so the log almost certainly
// ends somewhere in the middle of something.
usleep(200_000);

proc_terminate($child, SIGKILL);

$status = proc_get_status($child);

while ($status['running']) {
    usleep(10_000);
    $status = proc_get_status($child);
}

// --- what recovery promises, after a real SIGKILL --------------------------
$recovered = [];
$log = Log::open($logPath, static function (array $record) use (&$recovered): void {
    $recovered[] = $record;
});

$scan = $log->recovery();
$damagedCopy = $log->damagedCopy();
$log->close();

$survivedExactly = 0;

foreach ($recovered as $position => $record) {
    if ($position < ACKNOWLEDGED && $record === $expected($position + 1)) {
        $survivedExactly++;
    }
}

$wellFormed = array_filter(
    $recovered,
    static fn (array $record): bool => is_int($record['seq'] ?? null)
        && ($record['payload'] ?? null) === 'record-' . ($record['seq'] ?? 0),
);

printf("the writer acknowledged and synced %d records before the kill: %s\n", ACKNOWLEDGED, $acknowledged ? 'yes' : 'no');
printf("recovery read back at least the acknowledged records: %s\n", count($recovered) >= ACKNOWLEDGED ? 'yes' : 'no');
printf("of the acknowledged %d, recovered byte for byte: %d\n", ACKNOWLEDGED, $survivedExactly);
printf("recovered records that no writer could have written: %d\n", count($recovered) - count($wellFormed));
printf("the log was moved aside as damaged: %s\n", $damagedCopy === null ? 'no' : 'yes');
printf(
    "the end of the log was a whole record or an unfinished one, never damage: %s\n",
    in_array($scan->status, [LogScan::CLEAN, LogScan::TORN_TAIL], true) ? 'yes' : 'no',
);

// --- the same question, with no timing in it ------------------------------
$tornPath = $directory . 'data/torn.log';
$frame = LogFormat::frame($expected(1));

file_put_contents($tornPath, LogFormat::header() . $frame . substr(LogFormat::frame($expected(2)), 0, 12));

$inspect = Log::inspect($tornPath);

printf("\na file with one whole record and half of the next scans as: %s\n", $inspect->status);
printf("records that scan can still read: %d\n", $inspect->records);
printf("where the last whole record ends: %d of %d bytes\n", $inspect->validBytes, $inspect->fileBytes);

$replayed = [];
$torn = Log::open($tornPath, static function (array $record) use (&$replayed): void {
    $replayed[] = $record;
});

printf("records replayed while opening it: %d\n", count($replayed));
printf("the unfinished record is among them: %s\n", in_array($expected(2), $replayed, true) ? 'yes' : 'no');

$torn->append($expected(2));
$torn->close();

$after = Log::inspect($tornPath);

printf("after the tail was cut and a record appended, the log holds: %d\n", $after->records);
printf("and it is a whole log again: %s\n", $after->status === LogScan::CLEAN ? 'yes' : 'no');

proc_close($child);
```

**Перевод комментариев:**
- `ch26-kill-nine.php` (PHPDoc): `kill -9` посреди записи, дважды: один раз
  по-настоящему, один раз точно. Дочерний процесс дописывает записи в настоящий
  журнал, каждую синхронизирует, и пишет файл-маркер, когда первые двадцать
  подтверждены. Родитель ждёт этот маркер, даёт записать ещё немного и убивает
  процесс через `SIGKILL` — без очистки, без шанса сбросить буферы, без шанса
  что-то сказать. Тогда восстановление должно обещать немногое и проверяемое:
  каждая подтверждённая запись на месте, байт в байт; каждая восстановленная
  запись — та, которую писатель мог написать; и ничего не отложено в сторону как
  повреждённое. Вторая половина — тот же вопрос, заданный детерминированно: файл
  с одним целым кадром и половиной следующего. Это запись, которая никогда не
  завершилась, — бит в бит, без всякого тайминга, — и она должна сканироваться
  как оборванный хвост и быть отрезана, а не принята за повреждение. Запуск
  внутри контейнера, где проект смонтирован в `/app`:
  `docker compose cp /tmp/ch26-kill-nine.php php:/tmp/ch26-kill-nine.php`;
  `docker compose exec php php /tmp/ch26-kill-nine.php`.
- `// --- a child that keeps writing until it is killed` — дочерний процесс,
  который пишет, пока его не убьют.
- `// Let it write beyond the acknowledged point, so the log almost certainly` —
  дать ему записать дальше подтверждённой точки, чтобы журнал почти наверняка
  кончался где-то посреди чего-то.
- `// --- what recovery promises, after a real SIGKILL` — что обещает
  восстановление после настоящего `SIGKILL`.
- `// --- the same question, with no timing in it` — тот же вопрос, но без
  тайминга.

```text
the writer acknowledged and synced 20 records before the kill: yes
recovery read back at least the acknowledged records: yes
of the acknowledged 20, recovered byte for byte: 20
recovered records that no writer could have written: 0
the log was moved aside as damaged: no
the end of the log was a whole record or an unfinished one, never damage: yes

a file with one whole record and half of the next scans as: torn_tail
records that scan can still read: 1
where the last whole record ends: 47 of 59 bytes
records replayed while opening it: 1
the unfinished record is among them: no
after the tail was cut and a record appended, the log holds: 2
and it is a whole log again: yes
```

Первые строки — обещание целиком: все двадцать подтверждённых записей
восстановлены байт-в-байт, ни одной «выдуманной» записи, файл не унесён как
испорченный, а конец лога оказался либо целой записью, либо недописанной — но
никогда порчей.

Здесь важно, каких чисел в выводе **нет**. В первой половине восстановление
прочитало и больше двадцати записей (процесс успел написать после маркера) —
точное число зависит от того, где именно пришёлся `SIGKILL`, поэтому в выводе
только проверки, а не счётчики. Это принципиально: **инвариант** можно печатать,
момент убийства — нет.

Вторая половина даёт то, что первая дать не может, — точные числа: `47 из 59`
байт, `torn_tail`, одна читаемая запись, недописанная в неё не попала. В
`LogScanner` эта разница держится на порядке разбора: длина с испорченным полем
могла бы указать за конец файла и сойти за недописанный кадр — и тогда
отрезалось бы всё, что после, включая подтверждённое.

И главное: после того как хвост отрезан, лог принимает новую запись и снова
становится `clean`. Восстановление здесь не «починило файл как получилось», а
вернуло систему в рабочее состояние, потеряв ровно то, что никогда не было
подтверждено.

### 4. Набор тестов доказывает собственную детерминированность

Оракул-тест сеется (`mt_srand`) и потому обязан быть воспроизводимым. Это
проверяется прямо: один и тот же файл тестов прогоняется пять раз, и от каждого
прогона читается отчёт.

```bash
docker compose cp /tmp/ch26-reruns.php php:/tmp/ch26-reruns.php
docker compose exec php php /tmp/ch26-reruns.php
```

```php
<?php

declare(strict_types=1);

/**
 * The suite proving a property of itself: the same seeded oracle test, run
 * five times, must report the same work.
 *
 * This is the book's own discipline (run1 == run2) applied to the test suite
 * that carries the oracle. A seeded test whose assertion count changes from
 * run to run is not deterministic - it is reading something outside its seed,
 * a clock, a directory listing, an iteration order - and a test like that
 * cannot be used to reproduce anything.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch26-reruns.php php:/tmp/ch26-reruns.php
 *   docker compose exec php php /tmp/ch26-reruns.php
 */

require '/app/vendor/autoload.php';

$suite = '/app/tests/Index/InvertedIndexAgainstOracleTest.php';

$reported = [];
$counts = [];
$runs = 5;

for ($run = 1; $run <= $runs; $run++) {
    $output = [];
    $code = 0;

    exec(
        PHP_BINARY . ' /app/vendor/bin/phpunit --do-not-cache-result ' . escapeshellarg($suite) . ' 2>&1',
        $output,
        $code,
    );

    $text = implode("\n", $output);

    if (preg_match('/OK \((\d+) tests?, (\d+) assertions?\)/', $text, $match) !== 1) {
        $reported[] = "run {$run}: no result line";

        continue;
    }

    $reported[] = "run {$run}: {$match[1]} tests, {$match[2]} assertions";
    $counts[] = $match[1] . ' tests, ' . $match[2] . ' assertions';
}

foreach ($reported as $line) {
    printf("%s\n", $line);
}

printf("\nthe suite is what ran: %s\n", basename($suite));
printf("runs: %d\n", $runs);
printf("runs that reported the same work: %d of %d\n", count(array_unique($counts)), $runs);
printf("the counts are stable: %s\n", count(array_unique($counts)) === 1 ? 'yes' : 'no');
```

**Перевод комментариев:**
- `ch26-reruns.php` (PHPDoc): набор, доказывающий свойство самого себя: один и
  тот же seeded-тест оракула, прогнанный пять раз, должен сообщать одну и ту же
  работу. Это собственная дисциплина книги (`run1 == run2`), применённая к
  набору тестов, который несёт оракул. Seeded-тест, у которого число утверждений
  меняется от прогона к прогону, не детерминирован — он читает что-то вне своего
  сида: часы, листинг каталога, порядок итерации, — и такой тест нельзя
  использовать, чтобы что-то воспроизвести. Запуск внутри контейнера, где проект
  смонтирован в `/app`:
  `docker compose cp /tmp/ch26-reruns.php php:/tmp/ch26-reruns.php`;
  `docker compose exec php php /tmp/ch26-reruns.php`.

```text
run 1: 5 tests, 45607 assertions
run 2: 5 tests, 45607 assertions
run 3: 5 tests, 45607 assertions
run 4: 5 tests, 45607 assertions
run 5: 5 tests, 45607 assertions

the suite is what ran: InvertedIndexAgainstOracleTest.php
runs: 5
runs that reported the same work: 1 of 5
the counts are stable: yes
```

Пять прогонов, одно и то же число тестов и утверждений. Утверждения здесь важнее
тестов: их количество — это **сколько сравнений реально произошло**, и именно
оно зависит от ветвей случайного потока. Совпадение числа утверждений означает,
что поток воспроизвёлся целиком. Если бы тест читал из файловой системы, брал
время или полагался на порядок обхода массива, это число поехало бы.

Это та же дисциплина, которой подчинены все эксперименты книги (`run1 == run2`),
применённая к тестам: **набор тестов, который нельзя воспроизвести, нельзя и
использовать как доказательство**.

## 5. Отказы

| Что ломается                             | Чем обнаруживается                           | Что происходит                                                            |
| ---------------------------------------- | -------------------------------------------- | ------------------------------------------------------------------------- |
| Тест не может упасть                     | мутация не убита                             | дыра в тестах: поломка проходит незамеченной                              |
| Оракул сравнивает код с самим собой      | он повторяет ту же ошибку                    | расхождений нет и быть не может — сравнение бессмысленно                  |
| Оракул не встретил интересный случай     | «слишком мало равных очков»                  | сравнение так же сильно, как то, что оно сравнило                         |
| Случайный поток без сида                 | провал не воспроизводится                    | тест нельзя перезапустить, чтобы разобраться                              |
| Мутация в недостижимой ветке             | чтение условия вызова                        | эквивалентная мутация: не дыра, но и не польза                            |
| Мутация не применилась                   | замены не нашлось                            | отчёт о мутациях врёт в свою пользу, если это не сказать                  |
| Процесс убит посреди записи              | `kill -9` в момент `append()`                | недописанный хвост: отрезается, подтверждённое остаётся                   |
| Недописанное приняли за порчу            | унесённый файл `.damaged-*`                  | без нужды потерян весь лог                                                |
| Порчу приняли за недописанное            | отрезание по ложной длине                    | молча потеряны подтверждённые записи после места порчи                    |
| Длина кадра указывает за конец файла     | разбор идёт по порядку, а не по полям        | иначе оборванный кадр сойдёт за порчу, и наоборот                         |
| Запись подтверждена, но не на диске      | `fsync` (через `fflush`)                     | подтверждение обязано быть после синхронизации, а не до                   |
| Кадр больше допустимого                  | `MAX_BODY`                                   | это порча поля длины, а не недописанная запись                            |
| Питание пропало, а тест ставил `kill -9` | ограничение записано в тесте                 | страничный кеш дойдёт до диска и после убийства — тест этого не проверяет |
| Компактацию прервали                     | временный файл + `rename` + `fsync` каталога | на диске остаётся один целый лог: старый или новый                        |
| Тест читает время, файлы или порядок     | пять прогонов, разное число утверждений      | тест недетерминирован и как доказательство не годится                     |
| Оракул медленный                         | он и должен быть медленным                   | быстрый оракул повторит оптимизации и перестанет ловить ошибки            |

## 6. Как это проверено

Эту главу проверяет сама лаборатория — её наборы тестов и есть предмет.

**Оракул.** `tests/Index/InvertedIndexAgainstOracleTest.php` — пять сидов по
2500 операций. Отдельный прогон даёт **5 тестов, 45 607 утверждений**; двадцать
пять тысяч операций с пятьюдесятью тысячами сравнений — это и есть цена «второй
реализации». Тесты анализатора и индекса вместе (`tests/Analysis` и
`tests/Index`) — **39 тестов, 45 653 утверждения, OK**: почти все утверждения
приходят от оракула.

**Надёжность.** `tests/Reliability` в `php-mini-search` — **6 тестов, 809
утверждений, OK** за 3.2 секунды. `KillDuringWritesTest` (пять прогонов, по
одному на сид: четыре соединения, тридцать идентификаторов, `SIGKILL` в
случайный момент) и `FullDiskTest` —
`testAFullDiskStopsTheServiceAndLosesNoAcknowledgedWrite`. Второй отвечает ровно
на тот вопрос, который первый честно оставляет за скобками: `SIGKILL` не пропажа
питания, а кончившееся место — это отказ, который обязан быть обработан явно.

**Мутации.** У этой практики в лаборатории нет инструмента: мутации в
эксперименте 2 вносятся скриптом, а прогоны — это обычный `phpunit` на одном
файле. Это не пробел, а разделение: мутационный анализ — свойство **процесса
разработки** (его гоняют, когда меняют код), а не книги; в книгу попадает его
результат, воспроизводимый двумя командами.

**Отсутствие нужды в инструменте не отменяет практики.** В `php-job-queue` есть
`tests/Chaos/ChaosTest.php` (семнадцать тестов) — набор, который убивает и
теряет процессы вокруг очереди: доставка, повтор, идемпотентность. Это то же
`kill -9`, применённое не к логу, а к очереди целиком.

## 7. Цифры

| Файл                                             | Строк | Что делает                     |
| ------------------------------------------------ | ----- | ------------------------------ |
| `tests/Support/NaiveSearch.php`                  | 106   | наивный оракул BM25            |
| `tests/Index/InvertedIndexAgainstOracleTest.php` | 123   | случайный поток против оракула |
| `tests/Reliability/KillDuringWritesTest.php`     | 169   | `SIGKILL` под нагрузкой записи |
| `tests/Reliability/FullDiskTest.php`             | 115   | кончившееся место              |
| `src/Storage/Log.php`                            | 221   | открытие, чтение, компактация  |
| `src/Storage/LogScanner.php`                     | 134   | где кончается запись           |
| `src/Storage/LogFormat.php`                      | 72    | кадр: длина, сумма, тело       |
| `src/Storage/Durable.php`                        | 76    | `fflush`, `fsync`, каталог     |
| `src/Storage/LogScan.php`                        | 37    | четыре состояния чтения        |

| Константа или предел                                  | Значение                         |
| ----------------------------------------------------- | -------------------------------- |
| Операций на сид в оракул-тесте                        | 2500                             |
| Сидов в оракул-тесте                                  | 5                                |
| Поисков, которые обязан сделать оракул                | > 500                            |
| Групп с равными очками, которые он обязан встретить   | > 100                            |
| Доля записей-копий (чтобы были равные очки)           | 1 из 4                           |
| Магия и версия лога                                   | `MSLOG`, версия 1                |
| Заголовок кадра                                       | 8 байт (длина `N` + `crc32` `N`) |
| Максимум тела записи                                  | 262 144 байта                    |
| Соединений и идентификаторов в `KillDuringWritesTest` | 4 и 30                           |
| Прогонов `KillDuringWritesTest`                       | 5 (по одному на сид)             |
| Тестов в `ChaosTest` (`php-job-queue`)                | 17                               |

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что              | Значение                                                                          |
| ---------------- | --------------------------------------------------------------------------------- |
| `ch26-oracle`    | 121 строка; 2000 операций, 805 поисков, 139 равных групп, 0 расхождений           |
| `ch26-mutants`   | 109 строк; 5 поломок, 3 убиты, 2 выжили, 0 поймал только оракул                   |
| `ch26-kill-nine` | 193 строки; 20 из 20 подтверждённых пережили `SIGKILL`; недописанный кадр отрезан |
| `ch26-reruns`    | 58 строк; 5 прогонов, 45 607 утверждений каждый, счёт стабилен                    |

## 8. Упражнения

1. **Проследите.** Выжившая мутация `score >=` меняет поведение только при
   равных очках. Объясните, почему при этом порядок становится зависимым от
   порядка вставки, и придумайте, какой **сценарий данных** её убил бы надёжно
   (а не случайно).
2. **Докажите гипотезу.** «Оракул должен быть медленным» — это утверждение о
   проверке, а не о производительности. Что произойдёт с силой проверки, если
   наивный оракул заменить на второй экземпляр того же оптимизированного
   индекса? Приведите конкретную ошибку, которую перестанет ловить сравнение.
3. **Найдите правило.** Почему `torn_tail` отрезается, а `damaged` уносится в
   сторону? Постройте файл, в котором эти два состояния отличаются на один байт,
   и объясните, что потеряется, если поменять их обработку местами.
4. **Сравните.** `KillDuringWritesTest` и `FullDiskTest` проверяют разные отказы
   самой записи. Сформулируйте, какой третий отказ остаётся непроверенным ни
   одним из них, и каким тестом его можно было бы закрыть.
5. **Спроектируйте тест.** В эксперименте 4 детерминированность доказывается
   повторным прогоном. Как отличить недетерминированный тест от
   детерминированного **до** того, как он начнёт падать редко? Какие швы в коде
   делают тест недетерминированным, и как их обнаружить в своём коде?
6. **Разберитесь в цене.** Мутации в этой лаборатории делаются скриптом.
   Оцените, сколько времени занял бы полный мутационный анализ книги — девяти
   проектов и десятков тысяч утверждений, — и решите, что важнее: гонять его на
   каждой правке или раз в неделю по расписанию.

## Что дальше

Книга кончилась, и лаборатория вместе с ней. Девять проектов, двадцать шесть
глав, и в каждой — один и тот же порядок: сначала задача, потом устройство,
потом код, потом **эксперимент**, который можно повторить. Все выводы в книге
получены прогонами, все прогоны дважды совпали байт-в-байт, и каждое число либо
измерено здесь, либо названо с указанием документа, откуда оно взято.

Дальше — то, для чего всё это было: свои задачи. Правила переносятся целиком, и
их стоит держать в голове, когда очередная система начнёт падать непонятно как:

1. **Сначала измерь.** Базовая линия — счётчик, который воспроизводится, а не
   секундомер, который зависит от машины.
2. **Одна правка.** Две сразу — и непонятно, которая сработала.
3. **Ответ не меняется.** Оптимизация имеет право менять время и память и не
   имеет права менять результат.
4. **Отказ виден на своей границе** и не превращается в чужой.
5. **Никогда не молчать.** Проглоченная ошибка хуже ошибки.
6. **Владей тем, что запустил.** Иначе однажды остановишь чужое.
7. **Проверяй и проверку.** Оракул, мутация, `kill -9` — и написанное
   ограничение: что этот тест **не** доказывает.

Удачи. Падать оно будет — но теперь вы будете знать, где именно.
