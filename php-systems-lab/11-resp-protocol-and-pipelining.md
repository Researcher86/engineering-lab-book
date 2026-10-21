# Глава 11. Протокол и pipelining на примере RESP

> Проект: [`php-mini-cache`](https://github.com/Researcher86/php-mini-cache),
> релиз `v1.1.0`, коммит `5838655`. Нужно знать: соединения и буферы (→ глава
> 10). Пригодится дальше: TTL, Pub/Sub и снапшоты (→ глава 12).

В главе 10 мы разобрали, как сервер принимает байты и как отдаёт их обратно, но
оставили в стороне главный вопрос: **как из потока байт получается команда?**
`ReadBuffer` — это просто строка. Парсер в главе 10 упоминался вскользь, как
чёрный ящик, который «возвращает значение или `null`». Эта глава — про то, что
внутри, и про то, зачем вообще нужен формат передачи данных, который мы называем
протоколом.

Заодно мы разберём вторую половину названия: **pipelining** — умение клиента
отправить много команд, не дожидаясь ответа на каждую. Это самый дешёвый способ
ускорить обмен с сервером, и он напрямую опирается на то, как устроен протокол.

## 1. Проблема

### Байты надо во что-то превратить

TCP даёт **поток байт** и ничего больше. В нём нет ни границ сообщений, ни
типов: `+OK` и `OK+` — это одни и те же четыре байта в разном порядке. Чтобы две
программы поняли друг друга, им нужен договор о том, как из байт собираются
сообщения. Этот договор и есть **протокол**.

Представим самый очевидный вариант: «команда — это строка до перевода строки».
Тогда `SET user:1 Tanat\n`. Он ломается о первую же задачу, которую кэш обязан
решать: **значение может содержать перевод строки**. Если значение — это
многострочный текст, произвольные бинарные данные или просто строка с `\n`, то
«читать до `\n`» отрежет значение на середине.

Значит, договор должен описывать длину, а не полагаться на «до разделителя».
RESP (REdis Serialization Protocol) делает именно это: у каждой строки данных
`$` впереди стоит её **длина в байтах**. Тогда `\n` внутри значения — это просто
байт, и он ничего не ломает.

### Парсер должен уметь «подождать»

Вторая проблема вырастает прямо из главы 10. Один `fread()` не обязан содержать
целое значение — он может вернуть половину. И тогда парсер попадает в неудобную
ситуацию: байты, которые он видит, **выглядят** неполными, но это ещё не ошибка.
Отличить «пришло мало» от «пришло что-то не то» — центральная задача парсера
потокового протокола:

```text
"*3\r\n$3\r\nSE"          → мало байт, ждать ещё       (null)
"!boom\r\n"               → это вообще не RESP        (исключение)
"*3\r\n$3\r\nSET\r\n..."  → целое значение, разбирать
```

Спутать эти случаи — значит либо рвать живые соединения на каждой половинке
команды, либо бесконечно ждать байты, которых не будет. `php-mini-cache`
разводит их на уровне типа возврата: `null` — «мало данных», `ProtocolException`
— «это не RESP и сколько ни жди, RESP не станет».

### Почему pipelining что-то экономит

Третья часть — про задержку. Обычный обмен выглядит так:

```text
клиент → сервер:  PING
клиент ← сервер:  PONG         ← клиент ждёт этот ответ
клиент → сервер:  PING
клиент ← сервер:  PONG
...
```

Между отправкой и приходом ответа клиент **простаивает**. Это не цена работы
сервера — это цена сети и планировщика (round trip). Если отправить сразу много
команд, не дожидаясь ответов, то простаивание будет одно на всю пачку:

```text
клиент → сервер:  PING PING PING ... PING     (500 команд подряд)
клиент ← сервер:  PONG PONG PONG ... PONG
```

Работы на сервере столько же; экономится **ожидание**. Ответы приходят в том же
порядке, что и команды, — потому что на одном соединении всё строго
последовательно. Именно это RESP и позволяет естественным образом, а
`processBufferedCommands()` из главы 10 — уже готовая к этому машина,
разбирающая буфер команду за командой.

## 2. Устройство

### Пять типов RESP

RESP, который реализован здесь, состоит из пяти типов. У каждого — свой первый
байт-префикс:

| Префикс | Тип          | На проводе                 | Значение                              |
| ------- | ------------ | -------------------------- | ------------------------------------- |
| `+`     | SimpleString | `+OK\r\n`                  | строка без переводов строки           |
| `-`     | Error        | `-ERR unknown command\r\n` | ошибка                                |
| `:`     | Integer      | `:42\r\n`                  | целое                                 |
| `$`     | BulkString   | `$5\r\nTanat\r\n`          | строка длины 5 (или `$-1\r\n` = nil)  |
| `*`     | Array        | `*3\r\n...`                | список значений (или `*-1\r\n` = nil) |

Команда — это **всегда** массив из bulk-строк. `SET user:1 Tanat` на проводе
выглядит так:

```text
*3\r\n
$3\r\nSET\r\n
$6\r\nuser:1\r\n
$5\r\nTanat\r\n
```

`*3` — «дальше три значения», а каждое `$N` — «дальше N байт данных, потом
CRLF». Именно длина делает формат бинарно-безопасным: значение `$5\r\nTanat\r\n`
может содержать внутри что угодно, включая `\r\n`, — парсер отсчитывает ровно 5
байт и не смотрит на их содержимое.

### Простой тип даёт простой парсер

Строки (`+`, `-`, `:`), а также **заголовки** bulk-строк и массивов читаются
одинаково — «до ближайшего CRLF». Только тела `$` и элементы `*` требуют другой
обработки. Отсюда естественная структура парсера:

```text
parseValue:
  depth > max?          → ошибка
  байта нет?            → null (мало данных)
  прочитать строку до CRLF
  строки нет?           → null (мало данных)
  по префиксу:
    + → SimpleString(содержимое)
    - → Error(содержимое)
    : → Integer(строгий int)
    $ → BulkString(читаем length байт + проверяем CRLF)
    * → Array(читаем count значений, рекурсивно)
    иначе → ошибка (не RESP)
```

Ключевое здесь — **курсор**. `parse()` принимает `$offset` и возвращает
(`RespValue`, новый offset). Это позволяет разбирать одно и то же содержимое
буфера много раз, не вырезая каждый раз префикс: `processBufferedCommands()` в
цикле двигает offset вперёд и лишь в конце «съедает» разобранный префикс
целиком. Иначе пайплайн из тысяч команд копировал бы буфер O(n²) раз.

### Когда ошибка, а когда «мало данных»

Граница между двумя исходами проведена очень аккуратно, и её лучше читать как
правило:

- **данных пока нет** → `null`. Сюда попадает всё, что может «дорасти»: нет
  байта типа, нет CRLF в строке, тело bulk-строки короче заявленного, нет
  закрывающего CRLF у тела, не разобран последний элемент массива;
- **данные есть, но неверны** → `ProtocolException`. Сюда попадает всё, что уже
  нельзя исправить добавлением байт: неизвестный префикс, нечисловая длина,
  длина `< -1`, тело с неправильным терминатором, превышение предела.

Показательный пример — тело bulk-строки. Убедиться, что оно «закончилось»,
недостаточно: заявленные `$N` байт могут прийти в буфер, а **следующие два
байта** ещё нет. Если принять тело по одной длине, не проверив терминатор, то
`$5\r\nhelloXX` (где `XX` — начало совсем другой команды) разберётся как
«hello», и поток потеряет синхронизацию. Поэтому проверяются оба байта
терминатора, и если они не CRLF — это ошибка, а не «нужно ещё данных».

### Границы: почему они на этапе разбора

У парсера есть три предела, и все три срабатывают **до** того, как будет
выделена память под данные:

- `maxBulkStringBytes` (1 МиБ) — заявленная длина выше предела отклоняется сразу
  по заголовку. Иначе `$999999999999\r\n` заставил бы сервер искать тело такого
  размера ещё до того, как оно придёт.
- `maxArrayElements` (1 000 000) — жёсткий потолок на число элементов. Уровень
  команд имеет свой, **мягкий** предел числа аргументов (на него отвечают
  ошибкой, но соединение не рвут); этот — страховка на этапе разбора.
- `maxNestingDepth` (32) — глубина вложенности массивов. Разбор массива
  рекурсивен, а неограниченная рекурсия — это исчерпание стека само по себе.

### Кодирование: ответ не должен подделать себя

Обратная сторона — `RespEncoder`. Для значений с известной длиной (`$`, `*`) всё
просто. Но у `+` и `-` длина **не** передаётся: клиент читает такую строку до
первого CRLF. А в тексте ответа встречаются байты, которые выбрал клиент: имя
неизвестной команды, кривое имя из ошибки. Если записать их как есть, а внутри
случайно окажется `\r\n`, ответ оборвётся на нём, и остаток текста превратится в
**второй, подделанный ответ**. Поэтому encoder перед записью заменяет `\r` и
`\n` на пробелы. На это есть прямой тест — «имя команды с переводом строки не
может подделать второй ответ».

### Команда: только массив bulk-строк

Между парсером и обработчиком стоит `Command::fromRespValue()`. Он принимает
**ровно одну** форму — массив непустых bulk-строк — и превращает её в имя и
аргументы. Всё остальное (простая строка, целое, вложенный массив, `nil`)
отвергается: это не «команда, которую сервер ещё не знает», а ошибка клиента, и
нести её дальше как имя, которое никто не сможет найти, нет смысла. Имя
приводится к верхнему регистру **один раз**, здесь; все последующие сравнения
(диспетчер, транзакции, метрики) — простое равенство против одного написания.
Аргументы регистр сохраняют: это данные, а не имена.

### Pipelining: экономия на ожидании

Теперь, когда протокол понятен, pipelining — почти следствие. Клиент
`pipeline()` кодирует **все** команды в один буфер и пишет его одним `write()`,
а затем читает ровно **N** ответов:

```text
pipeline([[...], [...], ...]): 
   batch = encode(cmd1) . encode(cmd2) . ... . encode(cmdN)
   write(batch)                        ← одна отправка, одно ожидание
   replies = [ readReply() × N ]       ← N ответов по порядку
```

На стороне сервера ничего специального не нужно: `processBufferedCommands()` и
так разбирает буфер по одному значению в цикле, складывает ответы в пачку и
отправляет их. Порядок ответов гарантирован тем, что на одном соединении всё
выполняется строго последовательно: ответ на команду №5 не может уйти раньше
ответа на команду №4, потому что оба пишутся в один и тот же `WriteBuffer` в
порядке выполнения.

Обратите внимание, чего pipelining **не** делает. Он не даёт параллелизма —
сервер по-прежнему один поток и выполняет команды по очереди. Он не меняет
работу, только убирает простои клиента. И у него есть честная цена: один клиент,
вливший огромный пайплайн, держит цикл, пока его обслуживает. Но граница уже
есть, и она в байтах: `READ_CHUNK_SIZE` (64 КиБ) ограничивает, сколько байт один
`readable`-событие может внести в буфер, — это около 4 600 команд `PING` на
проход. Поэтому отдельного «бюджета команд на проход» в проекте нет: связанное
измерение (`benchmarks/fairness.php`) показало, что обычный клиент при пайплайне
в 100 000 команд ждёт около одного прохода, а худший проход — 4.5 мс.

И клиентская оговорка: `pipeline()` возвращает **ошибки как значения**, а не
исключения. Одна отклонённая команда в пачке из ста не должна выбрасывать ответы
на остальные девяносто девять.

## 3. Код

### Модель значения

```php
/**
 * One value of the RESP wire protocol: a simple string, an
 * error, an integer, a bulk string (or null), or an array of values
 * (or null).
 */
final readonly class RespValue
{
    /**
     * @param ($type is RespType::Array ? list<self>|null : string|int|null) $value
     */
    private function __construct(
        public RespType $type,
        public string|int|array|null $value,
    ) {
    }

    public static function simpleString(string $value): self
    {
        return new self(RespType::SimpleString, $value);
    }

    public static function error(string $message): self
    {
        return new self(RespType::Error, $message);
    }

    public static function integer(int $value): self
    {
        return new self(RespType::Integer, $value);
    }

    public static function bulkString(?string $value): self
    {
        return new self(RespType::BulkString, $value);
    }

    /** @param list<self>|null $items */
    public static function array(?array $items): self
    {
        return new self(RespType::Array, $items);
    }
}
```

[`src/Protocol/RespValue.php#L7-L48`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespValue.php#L7-L48)

**Перевод комментариев:**
- `RespValue` (PHPDoc): одно значение проводного протокола RESP: простая строка,
  ошибка, целое, bulk-строка (или `null`) либо массив значений (или `null`).
- `__construct()` (PHPDoc): параметр `$value` — либо `list<self>|null`, когда
  `$type` равен `RespType::Array`, либо `string|int|null` в остальных случаях.
- `array()` (PHPDoc): `$items` — список значений либо `null` для nil-массива.

Одно значение — одно `RespValue`, у которого есть тип и полезная нагрузка.
Конструктор приватный, а фабрики (`simpleString`, `error`, `integer`,
`bulkString`, `array`) не дают создать невозможную комбинацию: тип и форма
значения задаются вместе. `readonly` фиксирует значение после создания —
разобранная команда не должна меняться по дороге к обработчику.

### Контракт парсера

```php
/**
 * Parses RESP values out of a byte buffer.
 *
 * A single TCP read is not guaranteed to contain a whole value, so parse()
 * returns null - rather than failing - whenever the buffer does not yet
 * hold enough bytes. Anything that is present but not valid RESP - a bad
 * type byte, a non-integer length, a bulk string whose trailing CRLF is
 * missing, a value past the configured protocol limits - is reported as a
 * ProtocolException: no amount of additional bytes can turn it into a
 * valid value.
 */
final readonly class RespParser
{
    /**
     * @param int $maxBulkStringBytes The largest single bulk string body
     *     a connection may legitimately carry. Shorter-than-limit values
     *     whose bytes have not all arrived yet keep returning null; a
     *     declared length above the limit is a protocol error up front,
     *     before the body is looked at, so a hostile header cannot force
     *     the server to scan for a multi-hundred-megabyte body.
     * @param int $maxArrayElements A hard ceiling on the number of
     *     elements in a single array. The command layer has its own, lower
     *     *soft* argument-count limit (replied with an error, connection
     *     kept); this one is the parse-time backstop that keeps a
     *     structurally absurd array from ever being built.
     * @param int $maxNestingDepth How deep arrays may nest. Unbounded
     *     recursion in parseArray() is a stack-exhaustion vector on its
     *     own, so divergence here is stopped while it is still shallow.
     */
    public function __construct(
        private int $maxBulkStringBytes = 1024 * 1024,
        private int $maxArrayElements = 1_000_000,
        private int $maxNestingDepth = 32,
    ) {
    }
```

[`src/Protocol/RespParser.php#L7-L41`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespParser.php#L7-L41)

**Перевод комментариев:**
- `RespParser` (PHPDoc): разбирает значения RESP из байтового буфера. Одно
  TCP-чтение не гарантирует целое значение, поэтому `parse()` возвращает `null`
  — а не падает, — когда в буфере ещё недостаточно байт. Всё, что присутствует,
  но не является корректным RESP (плохой байт типа, нецелая длина, bulk-строка
  без завершающего CRLF, значение за настроенными пределами протокола),
  сообщается как `ProtocolException`: никакое добавление байт не превратит это в
  корректное значение.
- `__construct()` (PHPDoc): `$maxBulkStringBytes` — наибольшее тело одной
  bulk-строки, которое соединение может законно нести; значения меньше предела,
  чьи байты ещё не все пришли, продолжают возвращать `null`, а заявленная длина
  выше предела — ошибка протокола сразу, до просмотра тела, чтобы враждебный
  заголовок не мог заставить сервер искать тело на сотни мегабайт.
  `$maxArrayElements` — жёсткий потолок числа элементов в одном массиве; у
  уровня команд есть свой, более низкий *мягкий* предел числа аргументов (ответ
  ошибкой, соединение сохраняется), а этот — страховка на этапе разбора, не
  дающая построить структурно абсурдный массив. `$maxNestingDepth` — насколько
  глубоко могут вкладываться массивы; неограниченная рекурсия в `parseArray()`
  сама по себе вектор исчерпания стека, поэтому расхождение здесь
  останавливается, пока оно ещё неглубоко.

Docblock и есть спецификация: «`null` — мало байт», «`ProtocolException` — это
не RESP, и лишние байты этого не изменят». Три предела вынесены в конструктор, а
не зашиты в код, — потому что их обоснование тоже в docblock: каждый закрывает
вектор исчерпания ресурса.

### Разбор значения: префикс и строка до CRLF

```php
/** @return array{0: RespValue, 1: int}|null */
private function parseValue(string $buffer, int $pos, int $depth): ?array
{
    if ($depth > $this->maxNestingDepth) {
        throw new ProtocolException('Maximum nesting depth reached.');
    }

    if (!isset($buffer[$pos])) {
        return null;
    }

    $type = $buffer[$pos];
    $line = $this->readLine($buffer, $pos + 1);

    if ($line === null) {
        return null;
    }

    [$content, $pos] = $line;

    return match ($type) {
        '+' => [RespValue::simpleString($content), $pos],
        '-' => [RespValue::error($content), $pos],
        ':' => [RespValue::integer($this->parseStrictInt($content, 'integer')), $pos],
        '$' => $this->parseBulkString($buffer, $pos, $this->parseLength($content, 'bulk string')),
        '*' => $this->parseArray($buffer, $pos, $this->parseLength($content, 'array'), $depth),
        default => throw new ProtocolException(sprintf('Unknown RESP type byte "%s".', $type)),
    };
}

/**
 * @return array{0: string, 1: int}|null The line content (without \r\n) and the position right after it.
 */
private function readLine(string $buffer, int $pos): ?array
{
    $crlf = strpos($buffer, "\r\n", $pos);

    if ($crlf === false) {
        return null;
    }

    return [substr($buffer, $pos, $crlf - $pos), $crlf + 2];
}
```

[`src/Protocol/RespParser.php#L51-L93`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespParser.php#L51-L93)

**Перевод комментариев:**
- `parseValue()` (PHPDoc): возвращает разобранное значение и позицию после него
  (`array{0: RespValue, 1: int}`) либо `null`, если данных пока мало.
- `readLine()` (PHPDoc): возвращает содержимое строки без `\r\n` и позицию сразу
  после неё либо `null`, если CRLF ещё не пришёл.

Весь разбор — это `match` по одному байту. `$content` — это строка **без** CRLF
(её отрезал `readLine`), а `$pos` уже указывает на байт за CRLF, то есть на
начало тела. `match` возвращает либо готовое значение, либо — для `$` и `*` —
передаёт управление методам, которым нужен доступ к самим байтам, а не только к
строке заголовка. `readLine` — единственное место, где ищется разделитель, и
именно она задаёт, что «строки нет» — это `null`, а не ошибка.

### Строгий разбор целого: почему не `(int)`

```php
/**
 * A strict integer parse. Casting is not enough: (int) "abc" is 0 and
 * (int) of an over-64-bit literal silently wraps, so both would turn
 * an invalid integer line into a value instead of a protocol error.
 */
private function parseStrictInt(string $content, string $what): int
{
    if (preg_match('/^-?(0|[1-9][0-9]*)$/', $content) !== 1) {
        throw new ProtocolException(sprintf('Invalid %s: "%s".', $what, $content));
    }

    $value = filter_var($content, FILTER_VALIDATE_INT);

    if ($value === false) {
        throw new ProtocolException(sprintf('Invalid %s: "%s".', $what, $content));
    }

    return $value;
}
```

[`src/Protocol/RespParser.php#L112-L130`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespParser.php#L112-L130)

**Перевод комментариев:**
- `parseStrictInt()` (PHPDoc): строгий разбор целого. Приведения типа
  недостаточно: `(int) "abc"` равно `0`, а `(int)` литерала больше 64 бит молча
  переполняется, поэтому оба превратили бы недопустимую строку целого в значение
  вместо ошибки протокола.

Двухступенчатость — не перестраховка, а исправление конкретных ошибок приведения
типа. `(int) "abc"` молча даёт `0`, а `(int)` для литерала больше 64 бит молча
«сворачивается». Обе ошибки превратили бы недопустимую строку в значение.
Регулярное выражение сначала проверяет **форму** (никаких `+1`, `01`, пробелов,
точек), а `filter_var` — что значение влезает в `int`. Оба провала —
`ProtocolException`.

### Тело bulk-строки: длина плюс терминатор

```php
/** @return array{0: RespValue, 1: int}|null */
private function parseBulkString(string $buffer, int $pos, int $length): ?array
{
    if ($length === -1) {
        return [RespValue::bulkString(null), $pos];
    }

    if ($length > $this->maxBulkStringBytes) {
        throw new ProtocolException(sprintf('Bulk string length %d exceeds the configured maximum of %d.', $length, $this->maxBulkStringBytes));
    }

    $end = $pos + $length;

    // The two bytes of the trailing CRLF must be fully present before
    // the body is accepted; otherwise more data is needed. Once they
    // are in, they must actually be CRLF - "$5\r\nhelloXX" is not a
    // bulk string, regardless of how much more data follows it.
    if (!isset($buffer[$end + 1])) {
        return null;
    }

    if ($buffer[$end] !== "\r" || $buffer[$end + 1] !== "\n") {
        throw new ProtocolException('Bulk string body is not terminated by CRLF.');
    }

    return [RespValue::bulkString(substr($buffer, $pos, $length)), $end + 2];
}
```

[`src/Protocol/RespParser.php#L132-L158`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespParser.php#L132-L158)

**Перевод комментариев:**
- `parseBulkString()` (PHPDoc): возвращает разобранную bulk-строку и позицию
  после неё (`array{0: RespValue, 1: int}`) либо `null`, если данных пока мало.
- `// The two bytes of the trailing CRLF must be fully present before` — оба
  байта завершающего CRLF должны полностью присутствовать, прежде чем тело будет
  принято; иначе нужно больше данных. Когда они есть, это действительно должен
  быть CRLF — `"$5\r\nhelloXX"` не bulk-строка, независимо от того, сколько
  данных за ней следует.

Здесь всё, о чём говорилось в разделе «Устройство», в четырёх проверках: `-1` —
это nil; длина выше предела — ошибка **до** чтения тела; байты терминатора ещё
не пришли — `null`; терминатор не CRLF — ошибка. Последняя развилка и есть то
место, где `$5\r\nhelloXX` перестаёт быть «hello». Проверка
`isset($buffer[$end + 1])` (а не `$end`) гарантирует, что оба байта `\r\n` уже в
буфере, — иначе половинка терминатора считалась бы неполными данными.

### Кодирование и защита от подделки ответа

```php
public function encode(RespValue $value): string
{
    return match ($value->type) {
        RespType::SimpleString => '+' . $this->singleLine($value->value) . "\r\n",
        RespType::Error => '-' . $this->singleLine($value->value) . "\r\n",
        RespType::Integer => ':' . $value->value . "\r\n",
        RespType::BulkString => $this->encodeBulkString($value->value),
        RespType::Array => $this->encodeArray($value->value),
    };
}

/**
 * A simple string or error ends at the first CRLF, and its text can carry
 * bytes a client chose (an unknown command's name, a malformed header).
 * Written raw, a line break in it ends the reply early and turns the rest
 * into a forged reply of its own, so CR and LF become spaces instead.
 */
private function singleLine(mixed $text): string
{
    /** @var string $text RespValue::simpleString()/error() only ever hold a string. */
    return strtr($text, "\r\n", '  ');
}
```

[`src/Protocol/RespEncoder.php#L9-L30`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Protocol/RespEncoder.php#L9-L30)

**Перевод комментариев:**
- `singleLine()` (PHPDoc): простая строка или ошибка заканчивается на первом
  CRLF, а её текст может нести байты, выбранные клиентом (имя неизвестной
  команды, испорченный заголовок). Записанный как есть перевод строки обрывает
  ответ раньше и превращает остаток в собственный поддельный ответ, поэтому CR и
  LF заменяются пробелами.
- `$text` (`@var`): `RespValue::simpleString()` и `RespValue::error()` всегда
  хранят строку, поэтому здесь тип `string`.

`match` по типу — зеркало парсера. Но интересна здесь `singleLine()`: у `+` и
`-` нет длины, поэтому единственная защита от «ответа внутри ответа» — не дать
переводу строки туда попасть. `strtr($text, "\r\n", '  ')` заменяет и `\r`, и
`\n` на пробел, так что имя вроде `foo\r\n+OK` не превратится в два ответа. Это
дешёвая операция на маленьких строках (имена, сообщения об ошибках), и она
делает фрейминг ответа неподделываемым со стороны клиента.

### Команда: одна допустимая форма

```php
/**
 * Builds a Command from a parsed RESP array of bulk strings.
 *
 * That shape is the only one accepted: real clients send commands as an
 * array of bulk strings, and anything else - a bare simple string, an
 * integer, a nested array - is a client bug rather than a command this
 * server has not implemented yet, so it is refused here instead of
 * being carried further as a name nobody can dispatch.
 */
public static function fromRespValue(RespValue $value): self
{
    if ($value->type !== RespType::Array || $value->value === null) {
        throw new CommandException('A command must be a non-null RESP array.');
    }

    if ($value->value === []) {
        throw new CommandException('A command must contain at least a name.');
    }

    $parts = array_map(self::bulkStringOf(...), $value->value);
    $name = array_shift($parts);

    // Uppercased once, here, so every later comparison - the dispatcher's
    // lookup, the transaction layer's MULTI/EXEC/DISCARD check, the
    // per-command metrics - is a plain equality against one spelling.
    // Arguments keep their case: they are data, not names.
    return new self(strtoupper($name), $parts);
}
```

[`src/Command/Command.php#L24-L51`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Command/Command.php#L24-L51)

**Перевод комментариев:**
- `fromRespValue()` (PHPDoc): строит `Command` из разобранного RESP-массива
  bulk-строк. Эта форма — единственная принимаемая: настоящие клиенты шлют
  команды массивом bulk-строк, а всё остальное (голая простая строка, целое,
  вложенный массив) — ошибка клиента, а не команда, которую этот сервер ещё не
  реализовал, поэтому здесь она отклоняется, а не несётся дальше как имя,
  которое никто не сможет диспетчеризовать.
- `// Uppercased once, here, so every later comparison - the dispatcher's` —
  приведение к верхнему регистру один раз, здесь, чтобы каждое следующее
  сравнение — поиск в диспетчере, проверка `MULTI` /`EXEC`/`DISCARD` в слое
  транзакций, метрики по командам — было простым равенством против одного
  написания. Аргументы сохраняют регистр: это данные, а не имена.

`bulkStringOf()` (в выдержке он вызывается через `array_map`) для каждого
элемента проверяет, что это непустая bulk-строка, — иначе `CommandException`.
Важно, что это **не** `ProtocolException`: поток байт остался корректным RESP,
просто клиент прислал не ту форму. Поэтому соединение не рвётся — обработчик
получит ошибку и ответит `-ERR ...`. А `strtoupper($name)` переносит
нормализацию имени в единственное место.

### Клиентский pipeline

```php
/**
 * Sends every command before reading any reply - one round trip for the
 * whole batch instead of one each.
 *
 * Errors come back as error RespValues instead of exceptions: a batch
 * is a batch, and one refused command should not throw away the replies
 * to the other ninety-nine.
 *
 * @param list<list<string>> $commands each an argument list, e.g. ['SET', 'k', 'v']
 *
 * @return list<RespValue> one reply per command, in order
 *
 * @throws CacheClientException
 */
public function pipeline(array $commands): array
{
    if ($commands === []) {
        return [];
    }

    $batch = '';

    foreach ($commands as $arguments) {
        $batch .= $this->encode($arguments);
    }

    $this->write($batch);

    $replies = [];

    foreach ($commands as $ignored) {
        $replies[] = $this->readReply($this->deadline(null));
    }

    return $replies;
}
```

[`src/Sdk/CacheClient.php#L311-L346`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Sdk/CacheClient.php#L311-L346)

**Перевод комментариев:**
- `pipeline()` (PHPDoc): отправляет все команды до чтения любого ответа — один
  round trip на всю пачку вместо одного на каждую. Ошибки возвращаются как
  `RespValue` ошибок, а не исключения: пачка есть пачка, и одна отклонённая
  команда не должна выбрасывать ответы остальным девяноста девяти. `$commands` —
  список списков аргументов, например `['SET', 'k', 'v']`; возвращается один
  ответ на команду по порядку; бросает `CacheClientException`.

Вся суть pipelining — в трёх строках: сначала цикл кодирования в `$batch`, затем
**один** `write($batch)`, и только потом цикл чтения N ответов. Между записью и
чтением нет ни одного `readReply`. Docblock объясняет и вторую часть: ошибки не
бросаются исключением, а возвращаются значениями. Если бы `pipeline()` бросал на
первой же ошибке, остальные ответы были бы уже прочитаны из сокета, но
выброшены, и клиент потерял бы синхронизацию.

### Клиентский readReply: буфер и парсер

```php
/**
 * Returns the next complete reply, reading more bytes only when the
 * ones already buffered do not hold one.
 *
 * @throws CacheClientException
 */
private function readReply(float $deadline): RespValue
{
    while (true) {
        try {
            $parsed = $this->parser->parse($this->buffer);
        } catch (ProtocolException $exception) {
            $this->close();

            throw new ConnectionLostException(
                sprintf('The server sent something that is not RESP: %s', $exception->getMessage()),
            );
        }

        if ($parsed !== null) {
            [$value, $consumed] = $parsed;
            $this->buffer = substr($this->buffer, $consumed);

            return $value;
        }

        $this->buffer .= $this->readChunk($deadline);
    }
}
```

[`src/Sdk/CacheClient.php#L420-L448`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Sdk/CacheClient.php#L420-L448)

**Перевод комментариев:**
- `readReply()` (PHPDoc): возвращает следующий полный ответ, читая
  дополнительные байты, только когда уже буферизованные не содержат целого.
  Бросает `CacheClientException`.

Клиент использует **тот же самый** парсер, что и сервер, — RESP симметричен: то,
что сервер кодирует, клиент разбирает, и наоборот. И контракт тот же: пока буфер
не содержит целого ответа, `parse()` вернул `null`, и нужно добавить байт. Здесь
видно и практическое следствие `$offset`: после разбора клиент срезает ровно
`$consumed` байт, оставляя в буфере начало следующего ответа — так `pipeline()`
извлекает N ответов из одной кучи байт.

## 4. Эксперимент

Поднимаем контейнер и сервер (как в главе 10):

```bash
make build install
make run-server          # в одном терминале — сервер на 127.0.0.1:6380
```

### Сколько стоит одно ожидание

Встроенный пример сравнивает 500 `PING` по одному с 500 `PING` в одном
пайплайне:

```bash
docker compose exec php php examples/pipelining.php
```

Реальный вывод:

```text
500 PING round trips, one at a time: 0.028s
500 PINGs pipelined, replies read after:  0.005s (500 replies)
Pipelining was 5.3x faster here.
```

Работы на сервере одинаково — 500 `PING`. Разница только в том, что в первом
случае клиент 500 раз ждал ответа, а во втором — один раз. Это и есть
round-trip, вынесенный из цикла.

### Как RESP выглядит на проводе и как парсится по кускам

Скрипт ниже показывает и байты, и главное свойство парсера — что он умеет ждать.
Сохраните его как `/tmp/resp.php` внутри контейнера:

```php
<?php

declare(strict_types=1);

use PhpMiniCache\Protocol\RespEncoder;
use PhpMiniCache\Protocol\RespParser;
use PhpMiniCache\Protocol\RespType;
use PhpMiniCache\Protocol\RespValue;
use PhpMiniCache\Sdk\CacheClient;

require "/app/vendor/autoload.php";

/** Makes CRLF visible, so the framing is the thing you read. */
function show(string $bytes): string
{
    return strtr($bytes, ["\r" => "\\r", "\n" => "\\n"]);
}

function render(RespValue $value): string
{
    return match ($value->type) {
        RespType::SimpleString, RespType::Error => (string) $value->value,
        RespType::Integer => (string) $value->value,
        RespType::BulkString => $value->value === null ? "(nil)" : (string) $value->value,
        RespType::Array => $value->value === null
            ? "(nil)"
            : "[" . implode(", ", array_map(render(...), $value->value)) . "]",
    };
}

$encoder = new RespEncoder();
$parser = new RespParser();

// 1. One command, exactly as it goes on the wire.
$command = $encoder->encode(RespValue::array([
    RespValue::bulkString("SET"),
    RespValue::bulkString("user:1"),
    RespValue::bulkString("Tanat"),
]));
printf("client -> %s\n", show($command));
printf("          %d bytes on the wire\n\n", strlen($command));

// 2. One reply.
printf("server -> %s\n", show($encoder->encode(RespValue::bulkString("Tanat"))));
printf("server -> %s\n\n", show($encoder->encode(RespValue::integer(42))));

// 3. The parser is incremental: the same bytes, arriving one at a time.
$completedAt = null;
for ($n = 1; $n <= strlen($command); $n++) {
    if ($parser->parse(substr($command, 0, $n)) === null) {
        continue;
    }

    $completedAt = $n;

    break;
}
printf("parser saw %d of %d bytes before it had a complete value (first complete at %d)\n\n",
    $completedAt - 1, strlen($command), $completedAt);

// 4. A pipeline over a real connection: every command is sent before any
//    reply is read, and replies come back in order - one per command.
$client = new CacheClient("127.0.0.1", 6380, 5.0);

$replies = $client->pipeline([
    ["SET", "counter", "10"],
    ["INCR", "counter"],
    ["GET", "counter"],
    ["NOSUCHCOMMAND", "x"],
    ["GET", "user:1"],
]);

foreach ($replies as $index => $reply) {
    printf("reply %d  %-8s  %s\n", $index, $reply->type->name, render($reply));
}

$client->close();
```

**Перевод комментариев:**
- `show()` (PHPDoc): делает CRLF видимым, чтобы читался именно фрейминг.
- `// 1. One command, exactly as it goes on the wire.` — одна команда, ровно
  так, как она идёт по проводу.
- `// 2. One reply.` — один ответ.
- `// 3. The parser is incremental: the same bytes, arriving one at a time.` —
  парсер инкрементальный: те же байты, приходящие по одному.
- `// 4. A pipeline over a real connection: every command is sent before any` —
  пайплайн по реальному соединению: каждая команда отправляется до чтения любого
  ответа, а ответы приходят по порядку — по одному на команду.

Выполните:

```bash
docker compose exec php php /tmp/resp.php
```

Реальный вывод:

```text
client -> *3\r\n$3\r\nSET\r\n$6\r\nuser:1\r\n$5\r\nTanat\r\n
          36 bytes on the wire

server -> $5\r\nTanat\r\n
server -> :42\r\n

parser saw 35 of 36 bytes before it had a complete value (first complete at 36)

reply 0  SimpleString  OK
reply 1  Integer   11
reply 2  BulkString  11
reply 3  Error     ERR unknown command 'nosuchcommand'
reply 4  BulkString  (nil)
```

Разбор:

1. **Команда — это 36 байт с явными длинами.** `*3`, `$3`, `$6`, `$5` — это не
   украшение, а фрейминг: парсер знает, сколько байт читать, и `\r\n` внутри
   значения его не собьёт.
2. **Ответы тоже самоописываются**: `$5\r\nTanat\r\n` (bulk-строка) и `:42\r\n`
   (целое).
3. **Парсер ждёт.** Первые 35 из 36 байт он вернул `null` — «это ещё не целое
   значение». Только 36-й байт (последний `\n`) замкнул команду. Никакой ошибки
   на половинке не было.
4. **Пайплайн вернул пять ответов по порядку.** `SET` → `OK`, `INCR` → `11`,
   `GET` → `11`, затем `NOSUCHCOMMAND` → **ошибка как значение** (обратите
   внимание: тип `Error`, а не исключение), и последний `GET user:1` всё равно
   вернул ответ — `(nil)`, потому что ключ не задавали. Плохая команда не
   отменила остальные.

### Пропускная способность по размеру пачки

Пайплайнинг тем выгоднее, чем больше команд в пачке. `benchmarks/pipeline.php`
прогоняет одно и то же число `PING` (5 000) при разном размере пачки:

```bash
docker compose exec php php benchmarks/pipeline.php
```

```text
Pipelined PING throughput by batch size (commands/sec, higher is better)

batch      1       13596 cmd/s
batch     10       60169 cmd/s
batch    100       94432 cmd/s
batch   1000      108300 cmd/s
batch   5000      115979 cmd/s
```

Пачка из одной команды — это обычный round trip, и её результат совпадает с
последовательным клиентом. Десять команд в пачке ускоряют обмен почти **в 4.4
раза** (13 596 → 60 169): исчезло девять ожиданий из десяти. Дальше рост
замедляется и упирается в ~116 000 команд/с — там уже начинает доминировать не
сеть, а сама обработка. Это важный вывод: pipelining убирает **ожидание**, а не
работу, и ровно поэтому у него есть потолок.

## 5. Отказы

| Что ломается                               | Чем обнаруживается                     | Что происходит                                                 |
| ------------------------------------------ | -------------------------------------- | -------------------------------------------------------------- |
| Целого значения в буфере нет               | `parse()` → `null`                     | ждём ещё байт; это не ошибка                                   |
| Неизвестный тип-байт                       | ветка `default` в `parseValue`         | `ProtocolException` → `-ERR Protocol error` и разрыв           |
| Длина/целое не число                       | `parseStrictInt`                       | `ProtocolException`                                            |
| Целое переполняет 64 бита                  | `filter_var(...) === false`            | `ProtocolException`, а не молчаливое сворачивание              |
| Тело bulk-строки есть, терминатор не тот   | проверка `$buffer[$end]`/`[$end+1]`    | `ProtocolException` (`$5\r\nhelloXX` ≠ «hello»)                |
| Заявленная длина выше предела              | `maxBulkStringBytes`                   | `ProtocolException` **до** чтения тела                         |
| Массив выше предела / глубокая вложенность | `maxArrayElements`/`maxNestingDepth` | `ProtocolException`                                            |
| В простую строку/ошибку подсунули CRLF     | `singleLine()`                         | CR/LF → пробелы; второй ответ не подделывается                 |
| Команда — не массив bulk-строк             | `Command::fromRespValue`               | `CommandException` → `-ERR ...`, соединение **живёт**          |
| Одна команда пайплайна плохая              | цикл `processBufferedCommands`         | остальные выполняются; ошибка возвращается значением           |
| Сервер прислал не-RESP клиенту             | `readReply` → `ProtocolException`      | клиент закрывает соединение, бросает `ConnectionLostException` |
| Валидная команда перед плохим байтом       | один разбор за шаг                     | применяется; затем `-ERR Protocol error` и разрыв              |

Ключевая граница проходит между строками «`CommandException` — соединение живёт»
и «`ProtocolException` — разрыв». Это два разных уровня:

- **уровень команды** — поток байт корректен, но команда бессмысленна (не то
  число аргументов, неизвестное имя, `INCR` по не-числу). Ответ — обычная
  `-ERR ...`, соединение продолжается;
- **уровень протокола** — потерян фрейминг байт. Восстановить его нельзя:
  неизвестно, где начинается следующее значение. Поэтому всё, что было разобрано
  **до** плохого байта, уже применено по порядку, клиент получает
  `-ERR Protocol error`, и соединение закрывается.

## 6. Как это проверено

Протокол покрыт четырьмя наборами.

**`tests/Protocol/RespParserTest.php`** — по одному тесту на каждый тип и на
каждую форму «мало данных»: `testParsesSimpleString`, `testParsesError`,
`testParsesInteger`, `testParsesBulkString`, `testParsesNullBulkString`,
`testParsesArrayOfBulkStrings`, `testParsesNullArray`,
`testReturnsNullWhenTheLineIsIncomplete`,
`testReturnsNullWhenABulkStringBodyIsIncomplete`,
`testReturnsNullWhenAnArrayElementIsIncomplete`, а также
`testConsumedBytesLeaveTheRemainderForTheNextParse` (offset!) и
`testRoundTripsThroughTheEncoder`.

**`tests/Protocol/RespParserFuzzTest.php`** — это, по сути, таблица всех краевых
случаев, сгруппированная по **трём исходам**, и именно она делает контракт
парсера проверяемым:

- «нужно ещё байт» → `null`: пустой ввод, `+`, `+OK` без CRLF, `:100`, `$5`,
  `$5\r\nhel`, `$5\r\nhello\r` (терминатор разрезан пополам), `*2`, неполный
  элемент массива;
- «не RESP» → `ProtocolException`: `!boom\r\n` (чужой префикс), `:abc\r\n`,
  `:\r\n`, `:9223372036854775808\r\n` (переполнение 64 бит), `:1.5\r\n`,
  `: 1\r\n`, `$abc\r\n`, `\r\n`, `$-2\r\n`, `*-2\r\n`, `*lol\r\n`, `*\r\n`,
  `*--1\r\n`, `$5\r\nhelloXX`, `$5\r\nhello\rX`, `$5\r\nhello\nX`, превышение
  `maxBulkStringBytes`, `maxArrayElements` и вложенности;
- «валидно» → не `null`: `$-1\r\n`, `$0\r\n\r\n`, `*0\r\n`, `*-1\r\n`, ровно
  предельная вложенность и вложенный массив в пределах лимита.

**`tests/Protocol/RespEncoderTest.php`** — все пять типов плюс
`testEncodesNullBulkStringAsNegativeOne`, `testEncodesNullArrayAsNegativeOne` и
главный здесь `testLineBreaksInsideSimpleStringsAndErrorsAreFlattenedToSpaces`.

**`tests/Command/`** — `CommandTest`
(`testBuildsACommandFromAnArrayOfBulkStrings`,
`testNormalizesTheCommandNameToUppercase`, четыре теста на отказ от неправильной
формы) и `CommandDispatcherTest` (`testRoutingIsCaseInsensitive`,
`testReturnsAnErrorForAnUnregisteredCommand`).

Плюс на уровне сервера — `CacheServerTest`:
`testProcessesMultiplePipelinedCommandsFromOneRead`,
`testPipelinedCommandsAreAppliedInOrderWithoutWaitingForEachReply`,
`testAValidCommandBeforeMalformedInputIsStillExecuted`,
`testMalformedInputGetsARespErrorBeforeOnlyThatClientDisconnects` и
`testAnUnknownCommandNameWithALineBreakCannotForgeASecondReply` (тот самый тест
на `singleLine()`).

Запускаем:

```text
$ vendor/bin/phpunit tests/Protocol tests/Command
OK (102 tests, 211 assertions)

$ vendor/bin/phpunit
OK (261 tests, 1759 assertions)
```

Ценность `RespParserFuzzTest` именно в том, что он табличный. Каждая строка
провайдера — это заявление «вот такая последовательность байт обязана дать
именно этот исход». Из-за этого правила парсера нельзя тихо поменять: тест,
перечисляющий `$5\r\nhelloXX` среди ошибок, не даст будущему изменению «решить»,
что после тела можно не проверять терминатор.

## 7. Цифры

Замеры живых прогонов этой главы (Docker, PHP 8.5.11):

| Что                                   | Значение                                                       |
| ------------------------------------- | -------------------------------------------------------------- |
| Команда `SET user:1 Tanat` на проводе | 36 байт (`*3\r\n$3\r\nSET\r\n$6\r\nuser:1\r\n$5\r\nTanat\r\n`) |
| Ответ `Tanat` на проводе              | `$5\r\nTanat\r\n`                                              |
| Парсер до полного значения            | увидел 35 из 36 байт (первые 35 → `null`)                      |
| 500 `PING` по одному                  | 0.028 с                                                        |
| 500 `PING` в пайплайне                | 0.005 с                                                        |
| Ускорение здесь                       | 5.3×                                                           |
| Тесты протокола и команд              | 102 теста, 211 утверждений                                     |
| Весь набор проекта                    | 261 тест, 1759 утверждений                                     |

Пропускная способность пайплайна по размеру пачки (5 000 `PING` на каждый):

| Размер пачки | команд/с | Относительно пачки 1 |
| -----------: | -------: | -------------------: |
|            1 |   13 596 |                 1.0× |
|           10 |   60 169 |                 4.4× |
|          100 |   94 432 |                 6.9× |
|        1 000 |  108 300 |                 8.0× |
|        5 000 |  115 979 |                 8.5× |

Цена честности, из документации проекта (`benchmarks/fairness.php`): пока один
клиент обслуживает пайплайн в 100 000 команд (≈0.83 с, 807 проходов цикла),
обычный `PING` стоит 2.5 мс на p50 вместо 0.11 мс в простое — то есть около
одного прохода. Предел исходит из байт, а не из счётчика команд:
`READ_CHUNK_SIZE` (64 КиБ) — это около 4 600 `PING` за один проход. Проект
измерил и решил, что отдельный «бюджет команд» не нужен.

## 8. Упражнения

1. **Объясните.** Почему `RespParser::parse()` возвращает `null` на неполном
   значении, а не бросает исключение? Разберите конкретно строку
   `"*2\r\n$3\r\nGET\r\n$3\r\nfo"` — почему это ещё не ошибка и что должно
   произойти при следующем `parse()` с добавленными байтами?
2. **Найдите ошибку.** В `parseBulkString()` убрали проверку
   `!isset($buffer[$end + 1])` и оставили только проверку терминатора
   `$buffer[$end] === "\r"`. Какой вход теперь разберётся неправильно? Приведите
   конкретную строку байт и объясните, что произойдёт.
3. **Спроектируйте.** `singleLine()` заменяет `\r` и `\n` на пробелы. Что
   сломается, если убрать эту замену для типа `Error` (сообщение об ошибке),
   учитывая, что имя неизвестной команды приходит от клиента? Опишите, как
   клиент увидел бы один ответ вместо одного.
4. **Посчитайте.** Допустим, `parseBulkString()` не проверяла длину против
   `maxBulkStringBytes`, а сразу делала `substr($buffer, $pos, $length)` под
   заголовок `$999999999\r\n`. Сколько памяти попытается выделить сервер и
   почему проверка «по заголовку до чтения тела» дешевле?
5. **Сравните.** В `CommandTest` есть отдельные тесты на отказ от простой
   строки, целого и вложенного массива на месте команды. Почему это
   `CommandException` (соединение живёт), а не `ProtocolException` (разрыв)? Где
   проходит граница между «плохая команда» и «плохой поток»?
6. **Разберите аудит.** `pipeline()` возвращает ошибки значениями, а не бросает
   исключения. Придумайте два сценария, где бросание исключения на первой ошибке
   привело бы к потере синхронизации клиента, и объясните, как именно остальные
   ответы остались бы в сокете непрочитанными.

## Что дальше

- **Глава 12** — TTL, Pub/Sub и снапшоты: как те же соединения и буферы
  обслуживают время жизни ключей, рассылку сообщений и сохранение на диск.
- **Глава 10** — соединения и буферы: `ReadBuffer`, `WriteBuffer` и обратное
  давление, на которые опирается весь этот разбор.
- **Глава 14** — keep-alive, таймауты и медленные клиенты в HTTP: та же задача
  фрейминга, но в `php-mini-http-server`.
