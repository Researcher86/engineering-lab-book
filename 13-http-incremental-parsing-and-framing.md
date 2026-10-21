# Глава 13. HTTP: инкрементальный разбор и фрейминг

> Проекты:
> [`php-mini-http-server`](https://github.com/Researcher86/php-mini-http-server)
> (релиз `v1.1.0`, коммит `1f73404`) и
> [`php-mini-search`](https://github.com/Researcher86/php-mini-search) (релиз
> `v1.0.0`, коммит `7ecddc0`). Нужно знать: соединения и буферы (→ глава 10) и
> протокол с pipelining (→ глава 11). Пригодится дальше: keep-alive, таймауты,
> обратное давление и медленные клиенты (→ глава 14).

В главах 10 и 11 сервер научился превращать поток байтов в команды, и RESP за
границы отвечал сам: приставка `$5\r\n` объявляла «следующие пять байтов и есть
значение», парсер читал одно число и знал, где стоп. HTTP такого подарка не
делает. Клиент присылает произвольный поток, в котором нет ни метки начала, ни
метки конца запроса — есть только `\r\n\r\n` в конце заголовков и **число,
которое клиент сам написал в `Content-Length`**.

Из этого вырастают два вопроса, вокруг которых построена глава:

- **фрейминг** — где заканчивается запрос;
- **инкрементальный разбор** — что делать, когда байты приходят по частям.

Ответы в лаборатории есть дважды, и они разные. `php-mini-http-server` разбирает
запрос **чистой функцией** `parse($raw)`, которая не помнит ничего между
вызовами. `php-mini-search` держит **потоковый автомат** с состоянием `feed()` /
`next()`. Оба проекта независимо пришли к одному и тому же запрету: фреймить
только по `Content-Length`, а `Transfer-Encoding` отвергать целиком.

## 1. Проблема

### TCP не отдаёт границы сообщений

TCP гарантирует доставку и порядок байтов — и ничего больше. Он не знает, что
такое «запрос». `fread()` на сервере может вернуть половину строки метода, а
может и три готовых запроса сразу, и какой из двух случаев случится, решает не
приложение, а сетевой стек и момент, в который читатель оказался наготове.

В `examples/partial-request.php` это сформулировано прямо: «TCP does not
preserve message boundaries, so `"GET /hel"` really can be everything that
arrives for a while». Фраза `GET /hel` — это не половина запроса, это **всё**,
что пока что пришло.

Поэтому «прочитал → разобрал → ответил» работает, только если разбор умеет
отвечать на вопрос «а хватает ли мне байтов?». А ещё — потому что в HTTP тело
запроса объявляет сам клиент. Сервер получает не самодостаточный кадр, а
**декларацию отправителя**, которой ему предлагают поверить.

### Три вопроса, на которые парсер обязан ответить

На каждом шаге разбора парсер отдаёт ровно одно из трёх значений:

1. **байтов не хватает** — молчать и ждать;
2. **запрос собран** — отдать его и указать, сколько байтов он занял;
3. **запросу не бывает ответа** — отказаться вслух, со статусом.

Второе значение неотделимо от первого: `parse()` не только находит запрос, но и
говорит, **сколько байтов он съел**. Остаток буфера принадлежит следующему
запросу — именно так устроен pipelining из главы 11, и именно так же он работает
в HTTP. Отдать запрос, не сказав его длину, значило бы отдать буфер целиком и
потерять всё, что шло следом.

Третий ответ — тот, который стоит защищать. Парсер, который тихо «нормализует»
сомнительный вход в нечто похожее на валидный запрос, устраивает ровно то, из-за
чего две машины начинают расходиться во мнении о запросе, который обе только что
обработали. В `HttpParserFuzzTest` это названо прямо: «a parser that quietly
normalises questionable input into something valid-looking is how this server
and whatever sits in front of it end up disagreeing about the request they both
just handled — which is request smuggling, not a style problem».

### Фрейминг — это граница безопасности

**Request smuggling** возникает, когда прокси и бэкенд разнятся в том, где
кончается запрос. Прокси насчитал один запрос, бэкенд — два; подделанный второй
запрос приписывается уже **следующему** клиенту.

Классические триггеры — `Transfer-Encoding: chunked` **вместе с**
`Content-Length`, `Content-Length` в двух экземплярах с разными значениями, и
`Transfer-Encoding`, который бэкенд не умеет раскодировать. Именно последний
случай и записан в `docs/DECISIONS.md` проекта как пережитое:

> What is _not_ reasonable is what the code did before: ignore
> `Transfer-Encoding` entirely and frame by `Content-Length` anyway. That is not
> "partial support", it is request smuggling. A chunked request with no
> `Content-Length` parsed as a request with an empty body — and the chunk
> payload stayed in the read buffer, where Phase 15 pipelining then served it as
> the next request.

То есть ошибка не в том, что chunked не поддерживается. Ошибка в том, чтобы
**делать вид**, что он поддерживается: заявить `Content-Length`, разобрать тело
как пустое, а оставшиеся в буфере чанки отдать как следующий запрос. Отсюда
правило, которое оба проекта формулируют одинаково: **фрейминг — это
`Content-Length`, или ничего**.

## 2. Устройство

### Контракт из трёх ответов

`php-mini-http-server` записывает контракт в docblock парсера:

```text
parse($raw) === null                 → incomplete, wait for more bytes
parse($raw) instanceof ParsedRequest → one full request parsed
RequestException                     → refuse, loudly, with the status
```

`null` означает «пока не решено ничего», и это **не** ошибка: так парсер
отвечает на половину заголовка, на обрезанный терминатор и на объявленное, но
ещё не пришедшее тело. `ParsedRequest` несёт `HttpRequest` **плюс**
`consumedBytes`.

### Две формы одного парсера

|                        | `php-mini-http-server`                       | `php-mini-search`                                          |
| ---------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| форма                  | чистая функция `parse($raw): ?ParsedRequest` | автомат `feed($bytes)`/`next()`                          |
| состояние              | ни одного поля                               | `$buffer`, `$head`, `$continueWanted`, `?HttpError $error` |
| заголовки              | ищутся заново при каждом вызове              | разбираются один раз и запоминаются в `$head`              |
| отказ                  | `RequestException` с `HttpStatusCode`        | `HttpError` из `next()`, кэшируется в `$error`             |
| `Expect: 100-continue` | нет                                          | `takeContinue()` отвечает `100 Continue`                   |
| буфер                  | вызывающий сам срезает `consumedBytes`       | парсер срезает сам и оставляет хвост внутри                |

Повторный разбор всего буфера — это O(длина запроса) на каждый вызов. Он
допустим по двум причинам: блок заголовков ограничен 8 КиБ, а тело не
пересканировывается вовсе — длина тела **сравнивается** с числом имеющихся
байтов, а не ищется в них. Автомат с состоянием платит за отказ от повторного
разбора обязательством: **состояние должно быть согласованным в любой точке
нарезки**.

Именно это и есть инвариант главы: не имеет значения, пришёл запрос целиком, по
одному байту или случайными кусками — результат обязан совпадать. Обе тестовые
базы построены вокруг одного и того же утверждения.

### Фрейминг: терминатор, затем длина

Разбор тела укладывается в четыре шага:

```text
headEnd  = позиция "\r\n\r\n" + 4
contentLength = Content-Length, иначе 0
если contentLength > лимита тела   → отказ (413), ещё до чтения тела
если (len(raw) - headEnd) < contentLength → null, ждём байтов
иначе → запрос, consumed = headEnd + contentLength
```

Первый из них — «есть ли во всём буфере `\r\n\r\n`?», и он задаётся **до** того,
как парсер попытается что-нибудь разобрать. Пока терминатора нет, дальше нечего
делать. Второй — «а хватает ли байтов тела?», и он задаётся уже по числу, а не
поиском.

Обратите внимание, что лимит тела проверяется **между** этими двумя вопросами.
Клиент, объявивший `Content-Length: 99999999`, получает отказ, не отправив ни
одного байта тела: серверу не нужно буферизовать тело, чтобы решить, что оно
слишком большое.

### Одинаковый `Content-Length`: разная строгость

HTTP объединяет повторяющиеся заголовки через запятую, и `Content-Length: 5` в
двух строках превращается в `"5, 5"`. Что с этим делать — проекты решили
по-разному, и оба решения законны:

- `php-mini-http-server` **принимает** повторяющиеся идентичные значения и
  отвергает только расходящиеся: «identical values name the same one boundary»;
- `php-mini-search` **отвергает любой повтор**, даже совпадающий:
  `docs/ARCHITECTURE.md` говорит об этом без исключений — «A repeated
  `Content-Length` is `400` even when both values agree».

Это не расхождение по сути, а расхождение по **цени ошибки**. Первый проект
покупает совместимость со строгими клиентами, второй — более простое правило,
которое нельзя перепутать. Оба варианта видно в эксперименте ниже: один и тот же
запрос отдаёт `201` и `400`.

### Лимиты считаются по заголовкам

`php-mini-search` перечисляет их таблицей в `docs/ARCHITECTURE.md`: строка
запроса с заголовками — 8 КиБ → `431`, тело — 256 КиБ → `413`, одновременных
соединений — 256, чтение заголовков — 10 с, чтение тела — 30 с, простоя
keep-alive — 30 с, отправка ответа — 30 с. `php-mini-http-server` держит свои
пределы в `ServerConfig`: 8 192 и 1 048 576 байт, таймаут заголовков 5 с,
таймаут соединения 30 с, до 512 соединений.

Общая идея у таблиц одинаковая: в однопроцессном сервере любой неограниченный
ресурс — это отказ в обслуживании **всех** клиентов сразу. Поэтому лимит,
который можно вынести на шаг раньше (решить по заголовкам), выносится на шаг
раньше.

### Отказ окончателен

После ошибки фрейминга никто не знает, где начинается следующий запрос. Поэтому
соединение закрывается, а не «продолжается с середины».

В `php-mini-http-server` это видно в `serviceRequests()`: `RequestException`
превращается в ответ с нужным статусом и в `closeAfterFlush()`, а сам метод
возвращается из цикла. В `php-mini-search` отказ **кэшируется** в поле `$error`
и `next()` возвращает его сколько угодно раз, пока сервер не закроет соединение.
Комментарий к `HttpError` звучит так: «the parser stops, because after a framing
error nobody knows where the next request would begin».

Заметьте, что и там и там отказ **не роняет сервер**. Ошибка входа — это
нормальный исход для сервера, который слушает недоверенную сеть.

### Ответ тоже надо фреймить

Фрейминг — это вопрос не только входящего тела. На keep-alive-соединении «тело
кончается, когда закрылся сокет» не работает: клиент ждал бы тело, которого
никогда не придёт. Поэтому оба проекта всегда пишут `Content-Length` — в том
числе в пустом ответе.

У `php-mini-http-server` есть одно отступление и один отказ. Отступление —
статусы 204 и 304: их пустота **записана в смысле статуса**, поэтому
`Content-Length` для них не выводится вообще. Отказ — защита от **response
splitting**: если обработчик протащит в значении заголовка `\r\n`, заголовок
превратится в два, и часть ответа уйдёт чужому клиенту. Код отвергает такой
ответ до отправки. `php-mini-search` короче: он пишет `Content-Length` всегда и
вручную, из `strlen($this->body)`.

## 3. Код

### Чистая функция: `HttpParser::parse()`

```php
public function parse(string $raw): ?ParsedRequest
{
    $headerEnd = strpos($raw, self::HEADER_TERMINATOR);

    // Refuse to buffer an unbounded header block, whether or not its
    // terminator has arrived yet: this is the read-side memory guard
    // against header floods.
    if (($headerEnd === false ? strlen($raw) : $headerEnd) > $this->maxHeaderBytes) {
        throw new HeaderTooLargeException(sprintf('Header block exceeds %d bytes.', $this->maxHeaderBytes));
    }

    if ($headerEnd === false) {
        return null;
    }

    $headEnd = $headerEnd + strlen(self::HEADER_TERMINATOR);

    // A head with no CRLF carries no header lines at all, so the first
    // line is also the last. Otherwise the rest of the head is headers.
    [$requestLine, $headerLines] = explode("\r\n", substr($raw, 0, $headerEnd), 2) + [1 => ''];

    [$method, $target, $version] = $this->parseRequestLine($requestLine);
    $headers = Headers::fromLines($headerLines);

    $this->assertHostIsUsable($headers, $version);
    $this->assertDecodableBody($headers);

    $contentLength = $this->contentLength($headers);

    if ($contentLength > $this->maxBodyBytes) {
        throw new BodyTooLargeException(sprintf(
            'Content-Length %d exceeds the %d-byte body limit.',
            $contentLength,
            $this->maxBodyBytes,
        ));
    }

    $availableBody = strlen($raw) - $headEnd;

    if ($availableBody < $contentLength) {
        return null;
    }

    $request = new HttpRequest($method, $target, $version, $headers, substr($raw, $headEnd, $contentLength));

    return new ParsedRequest($request, $headEnd + $contentLength);
}
```

[`src/Http/Protocol/HttpParser.php#L33-L79`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Http/Protocol/HttpParser.php#L33-L79)

**Перевод комментариев:**
- `// Refuse to buffer an unbounded header block, whether or not its...` — отказ
  буферизовать неограниченный блок заголовков, даже если его терминатор ещё не
  пришёл: это защита памяти на стороне чтения от наводнения заголовками (header
  flood).
- `// A head with no CRLF carries no header lines at all, so the first...` —
  голова без CRLF не содержит строк заголовков вовсе, поэтому первая строка —
  она же последняя. Иначе остаток головы — это заголовки.

Здесь важен **порядок**, а не набор проверок. Сначала лимит блока заголовков —
он проверяется даже тогда, когда терминатора ещё нет (`$headerEnd === false`),
потому что иначе клиент мог бы заставить сервер буферизовать мегабайты «начала
заголовка», так и не отправив `\r\n\r\n`. Потом — сам терминатор, и только при
найденном терминаторе разбирается запросная строка и заголовки. И лишь затем
длина тела: лимит → сравнение → возврат `null`.

Два разных места возвращают `null` и означают разное: «терминатора ещё нет» и
«заголовки целы, но тела меньше, чем объявлено». Оба — это «ждём», но первый
запускает часы защиты от Slowloris, а второй — часы чтения тела.

### Где кончается тело: `assertDecodableBody()` и `contentLength()`

```php
/**
 * Refuse any transfer coding other than "identity".
 *
 * Content-Length is the only framing this parser implements. A request
 * that announces another coding (chunked, gzip, …) does not end where
 * Content-Length says it does, so accepting it would leave the encoded
 * payload in the read buffer to be parsed as the next pipelined request.
 *
 * @throws UnsupportedTransferEncodingException
 */
private function assertDecodableBody(Headers $headers): void
{
    $encoding = $headers->get('transfer-encoding');

    if ($encoding === null) {
        return;
    }

    $isIdentity = static fn (string $coding): bool => strtolower(trim($coding)) === 'identity';

    if (!array_all(explode(',', $encoding), $isIdentity)) {
        throw new UnsupportedTransferEncodingException(sprintf('Unsupported Transfer-Encoding: %s', $encoding));
    }
}

private function contentLength(Headers $headers): int
{
    $value = $headers->get('content-length');

    if ($value === null) {
        return 0;
    }

    // Repeated Content-Length lines are comma-joined by Headers. They
    // only break framing when the values disagree (RFC 7230), so several
    // identical lines are accepted; a single invalid value is the same
    // unrecoverable framing error as two conflicting ones.
    $parts = array_map(trim(...), explode(',', $value));

    if (!array_all($parts, static fn (string $part): bool => ctype_digit($part))) {
        throw new MalformedRequestException(sprintf('Malformed Content-Length header: %s', $value));
    }

    if (count(array_unique($parts)) !== 1) {
        throw new MalformedRequestException(sprintf(
            'Conflicting Content-Length headers: %s',
            $value,
        ));
    }

    return (int) $parts[0];
}
```

[`src/Http/Protocol/HttpParser.php#L126-L177`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Http/Protocol/HttpParser.php#L126-L177)

**Перевод комментариев:**
- `assertDecodableBody()` (PHPDoc): отказ от любого transfer coding, кроме
  `identity`. `Content-Length` — единственный фрейминг, который реализует
  парсер. Запрос, объявивший другое кодирование (`chunked`, `gzip`, …),
  кончается не там, где указывает `Content-Length`, поэтому принять его значило
  бы оставить закодированные данные в буфере чтения и разобрать их как следующий
  пайплайнованный запрос. Тег `@throws` объявляет
  `UnsupportedTransferEncodingException`.
- `// Repeated Content-Length lines are comma-joined by Headers. They...` —
  повторяющиеся строки `Content-Length` склеиваются запятой в `Headers`. Они
  ломают фрейминг, только когда значения расходятся (RFC 7230), поэтому
  несколько одинаковых строк принимаются; одно невалидное значение — та же
  неисправимая ошибка фрейминга, что и два конфликтующих.

`assertDecodableBody()` — это и есть запрет request smuggling, выраженный в
шести строках. `identity` значит «кодирования не было», всё остальное — отказ.
Обратите внимание, что `Transfer-Encoding` проверяется **раньше**, чем читается
`Content-Length`: если способ конца запроса неизвестен, его собственные числа
бесполезны.

`contentLength()` показывает, как одна и та же проверка `Headers` ловит три
разных отказа: нецифровое значение (`abc`, `-1`, `+5`, `0x5`), запятая в
значении (`2, 2`) и расхождение повторов. А непустое значение при отсутствии
заголовка невозможно — метод сначала отдаёт `0`, и пустое тело здесь **явное**
решение, а не пропуск, как требует RFC 9112.

### Заголовки: каждое правило — правило отказа

```php
/**
 * Parse a header block — the lines between the request line and the
 * blank line — into a collection.
 *
 * Every rule here is a rule about what to refuse. A lenient header
 * parser is how two machines end up disagreeing about the request they
 * both just handled, and that disagreement is request smuggling.
 */
public static function fromLines(string $head): self
{
    $headers = new self();

    foreach (explode("\r\n", $head) as $line) {
        if ($line === '') {
            continue;
        }

        $colon = strpos($line, ':');

        if ($colon === false || $colon === 0) {
            throw new MalformedRequestException(sprintf('Malformed header line: %s', $line));
        }

        $name = substr($line, 0, $colon);

        // Not trimmed, checked. RFC 7230 3.2.4 requires a server to
        // reject whitespace between the name and the colon outright,
        // rather than quietly trimming it into a valid-looking name.
        // The same check catches an obsolete folded continuation line
        // ("  more value") that happens to contain a colon and would
        // otherwise read as a header of its own.
        if (!self::isToken($name)) {
            throw new MalformedRequestException(sprintf('Malformed header name: %s', $name));
        }

        // Only spaces and tabs are stripped — the optional whitespace
        // the grammar allows around a value. Anything else that shows
        // up at an edge is a control character, and it is refused
        // below rather than silently removed.
        $value = trim(substr($line, $colon + 1), " \t");

        if (!self::isFieldValue($value)) {
            throw new MalformedRequestException(sprintf('Malformed value for header %s', $name));
        }

        $headers->add($name, $value);
    }

    return $headers;
}
```

[`src/Http/Headers/Headers.php#L88-L137`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Http/Headers/Headers.php#L88-L137)

**Перевод комментариев:**
- `Headers::fromLines()` (PHPDoc): разобрать блок заголовков — строки между
  запросной строкой и пустой строкой — в коллекцию. Каждое правило здесь —
  правило о том, что отвергать. Снисходительный парсер заголовков — так две
  машины начинают расходиться во мнении о запросе, который обе только что
  обработали, а это расхождение и есть request smuggling.
- `// Not trimmed, checked. RFC 7230 3.2.4 requires a server to...` — не
  обрезается, а проверяется. RFC 7230 3.2.4 требует, чтобы сервер сразу отвергал
  пробелы между именем и двоеточием, а не тихо срезал их в похожее на валидное
  имя. Та же проверка ловит устаревшую сложенную строку-продолжение
  (`" more value"`), которая случайно содержит двоеточие и иначе прочиталась
  бы как отдельный заголовок.
- `// Only spaces and tabs are stripped — the optional whitespace...` —
  срезаются только пробелы и табы — необязательные пробельные символы, которые
  грамматика допускает вокруг значения. Всё остальное на краю — управляющий
  символ, и его отвергают ниже, а не удаляют молча.

Самый показательный здесь комментарий «Not trimmed, checked». Разделитель `: `
встречается и у **продолжения** устаревшего сложенного заголовка
(`  folded: yes`), которое не имеет двоеточия в начале — наивный парсер прочитал
бы его как самостоятельный заголовок, то есть как голос клиента, которого не
было. Правило `isToken($name)` не «чинит» имя, а **отвергает** его.

### Цикл соединения: сколько байтов съедено

```php
private function serviceRequests(mixed $stream): void
{
    $queued = false;

    // Phase 15: one read may carry several pipelined requests. Keep
    // parsing while the buffer holds complete requests, queueing their
    // responses in order; stop only when a request says "close", there
    // is nothing complete left, or the write buffer hits the ceiling.
    // Each early return says what happens to the connection next.
    while (true) {
        try {
            $parsed = $this->parser->parse((string) $this->connection->readBuffer());
        } catch (RequestException $e) {
            // Parsing failed, so where this request ends is unknown and
            // every byte after it is suspect. Answer with the status the
            // exception carries, then close.
            $this->queueError($e);
            $this->closeAfterFlush($stream);

            return;
        }

        if ($parsed === null) {
            // No complete request yet — the header clock starts (or
            // keeps running) so the Slowloris sweep can reap a client
            // that never finishes its header block. An empty buffer is
            // not that: it is a keep-alive client between requests,
            // which the idle timeout governs, not the header one.
            if (!$this->connection->readBuffer()->isEmpty()) {
                $this->connection->noteWaitingForHeaders();
            }

            break; // wait for more bytes
        }

        $this->connection->doneWaitingForHeaders();
        $this->connection->readBuffer()->consume($parsed->consumedBytes);
        $this->connection->startProcessing();
```

[`src/Server/ConnectionHandler.php#L143-L180`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Server/ConnectionHandler.php#L143-L180)

**Перевод комментариев:**
- `// Phase 15: one read may carry several pipelined requests. Keep...` — фаза
  15: одно чтение может принести несколько пайплайнованных запросов. Парсить,
  пока буфер держит полные запросы, ставя их ответы в очередь по порядку;
  останавливаться, только когда запрос просит `close`, полных запросов не
  осталось или буфер записи упёрся в потолок. Каждый досрочный `return` говорит,
  что дальше произойдёт с соединением.
- `// Parsing failed, so where this request ends is unknown and...` — разбор не
  удался, поэтому где кончается этот запрос — неизвестно, и каждый байт после
  него подозрителен. Ответить статусом, который несёт исключение, затем закрыть.
- `// No complete request yet — the header clock starts (or...` — полного
  запроса ещё нет — запускаются (или продолжают идти) часы заголовков, чтобы
  проход Slowloris мог прибрать клиента, который никогда не закончит блок
  заголовков. Пустой буфер — это не он: это keep-alive-клиент между запросами,
  которым управляет таймаут простоя, а не таймаут заголовков.
- `// wait for more bytes` — ждать больше байтов.

Это место, где чистая функция становится инкрементальной. Сам парсер ничего не
помнит, но **вызывающий** хранит буфер между чтениями и срезает с него ровно
`consumedBytes`. Один `while` решает сразу три задачи: несколько пайплайнованных
запросов в одном чтении, ожидание догоняющих байтов и отказ со закрытием.

Отдельно стоит комментарий при `null`: если буфер непустой — это клиент, который
ещё дописывает заголовки, и на него запускаются часы Slowloris; если пустой —
это клиент на keep-alive между запросами, и на него действует таймаут простоя.
Одно и то же «ничего не разобралось», но два разных источника и два разных
таймера.

### Автомат с состоянием: `RequestParser`

```php
/**
 * Turns the bytes of one connection into requests, however TCP slices them.
 *
 * TCP is a byte stream: one read may hold half a request line, or the end of
 * one request and the start of the next. So the parser never assumes a read
 * is a request. feed() appends bytes; next() returns a complete Request, an
 * HttpError, or null for "not enough bytes yet". Bytes after a request stay
 * in the buffer for the next call — that is all keep-alive and pipelining
 * need from a parser.
 *
 * The supported subset is deliberately narrow, and everything outside it is
 * refused, never guessed at (docs/ARCHITECTURE.md, HTTP subset):
 *
 *  - lines end in CRLF; a bare CR or LF, a NUL, or a folded header line is 400;
 *  - HTTP/1.0 and HTTP/1.1 only, origin-form targets only, Host required in 1.1;
 *  - the body is framed by Content-Length only. Any Transfer-Encoding is
 *    501, and a repeated or non-numeric Content-Length is 400 — ambiguous
 *    framing is how request smuggling starts. No Content-Length means an
 *    empty body (RFC 9112), which is what `curl -X POST` sends to
 *    /reindex;
 *  - a head over the limit is 431, a body over the limit 413 — decided from
 *    the headers, before the body is read.
 *
 * An error is final: the parser stops, because after a framing error nobody
 * knows where the next request would begin.
 */
final class RequestParser
{
    private const string TOKEN = '/^[!#$%&\'*+.^_`|~0-9A-Za-z-]+$/';

    private string $buffer = '';

    /** @var array{method: string, target: string, version: string, headers: array<string, string>, length: int}|null */
    private ?array $head = null;

    private bool $continueWanted = false;

    private ?HttpError $error = null;

    public function __construct(
        private readonly int $maxHeadBytes = 8_192,
        private readonly int $maxBodyBytes = 262_144,
    ) {
    }
```

[`src/Http/RequestParser.php#L7-L50`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/RequestParser.php#L7-L50)

**Перевод комментариев:**
- `RequestParser` (PHPDoc): превращает байты одного соединения в запросы, как бы
  TCP их ни нарезал. TCP — поток байтов: одно чтение может держать половину
  запросной строки либо конец одного запроса и начало следующего. Поэтому парсер
  никогда не считает, что чтение — это запрос. `feed()` добавляет байты;
  `next()` возвращает полный `Request`, `HttpError` или `null` — «байтов пока не
  хватает». Байты после запроса остаются в буфере для следующего вызова — это
  всё, что keep-alive и pipelining требуют от парсера. Поддерживаемое
  подмножество намеренно узкое, и всё вне его отвергается, а не угадывается
  (`docs/ARCHITECTURE.md`, HTTP subset): строки кончаются CRLF, голый CR или LF,
  NUL или сложенная строка заголовка — `400`; только HTTP/1.0 и HTTP/1.1, только
  origin-form цели, `Host` обязателен в 1.1; тело фреймится только по
  `Content-Length`, любой `Transfer-Encoding` — `501`, повторный или нечисловой
  `Content-Length` — `400`: неоднозначный фрейминг — начало request smuggling;
  нет `Content-Length` — пустое тело (RFC 9112), именно это `curl -X POST` шлёт
  на `/reindex`; голова сверх лимита — `431`, тело сверх лимита — `413`,
  решается по заголовкам, до чтения тела. Ошибка окончательна: парсер
  останавливается, потому что после ошибки фрейминга никто не знает, где
  начнётся следующий запрос.
- `$head` (`@var`): уже разобранные заголовки — `method`, `target`, `version`,
  `headers`, `length` — или `null`, пока голова не разобрана.

Контраст с `HttpParser` виден в одном наборе полей. `$head` — это **уже
разобранные** заголовки: автомат нашёл `\r\n\r\n` один раз и запомнил результат,
поэтому повторный вызов `next()` не ищет терминатор заново. `$error` —
кэшированный отказ, из-за которого «ошибка окончательна» превращается из
комментария в две строки кода. `$continueWanted` — единственное поле, которого у
чистой функции быть не может: `100 Continue` отправляется **между** вызовами,
когда заголовки целы, а тела ещё нет.

Docblock резюмирует главу одной фразой: «TCP decides how bytes are split, and
the parser must not care».

### `next()`: три ответа в одном методе

```php
/**
 * @phpstan-impure
 */
public function next(): Request|HttpError|null
{
    if ($this->error !== null) {
        return $this->error;
    }

    if ($this->head === null) {
        $this->buffer = $this->skipEmptyLines($this->buffer);

        $end = strpos($this->buffer, "\r\n\r\n");
        if ($end === false) {
            return strlen($this->buffer) > $this->maxHeadBytes
                ? $this->fail(431, 'headers_too_large', 'The request line and headers exceed ' . $this->maxHeadBytes . ' bytes.')
                : null;
        }
        if ($end + 4 > $this->maxHeadBytes) {
            return $this->fail(431, 'headers_too_large', 'The request line and headers exceed ' . $this->maxHeadBytes . ' bytes.');
        }

        $head = $this->parseHead(substr($this->buffer, 0, $end));
        if ($head instanceof HttpError) {
            return $this->fail($head->status, $head->code, $head->message);
        }

        $this->buffer = substr($this->buffer, $end + 4);
        $this->head = $head;
        $this->continueWanted = $head['version'] === '1.1'
            && $head['length'] > 0
            && strtolower($head['headers']['expect'] ?? '') === '100-continue';
    }

    if (strlen($this->buffer) < $this->head['length']) {
        return null;
    }

    $request = new Request(
        $this->head['method'],
        $this->head['target'],
        $this->head['version'],
        $this->head['headers'],
        substr($this->buffer, 0, $this->head['length']),
    );
    $this->buffer = (string) substr($this->buffer, $this->head['length']);
    $this->head = null;
    $this->continueWanted = false;

    return $request;
}
```

[`src/Http/RequestParser.php#L57-L107`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/RequestParser.php#L57-L107)

**Перевод комментариев:**
- `next()` (PHPDoc): `@phpstan-impure` — метод меняет состояние парсера, поэтому
  его нельзя считать чистым и кэшировать результат.

Метод разделён на две половины по состоянию `$this->head`. Если заголовков ещё
нет — ищем терминатор, проверяем лимит и разбираем голову, **затем сразу срезаем
её из буфера**. Если заголовки уже есть — остаётся единственное сравнение
`strlen($buffer) < head['length']`.

Порядок среза важен: буфер уменьшается в момент, когда заголовки разобраны, а
тело ещё ждёт. Поэтому вызов `next()` после `feed()` с половиной тела вернёт
`null` и оставит в буфере ровно то, что пришло, — а следующий `feed()` дописает
к тому же остатку. Именно это делает нарезку неважной.

`skipEmptyLines()` — компромисс: один случайный `\r\n` перед запросной строкой
не ошибка, а мусор, который кто-то оставил. Но **внутри** заголовков пустые
строки уже не прощаются, и `parseHead()` отвергает их.

### `parseHead()`: все отказы в одном месте

```php
/**
 * @return array{method: string, target: string, version: string, headers: array<string, string>, length: int}|HttpError
 */
private function parseHead(string $head): array|HttpError
{
    if (preg_match('/[\0]|\r(?!\n)|(?<!\r)\n/', $head) === 1) {
        return $this->malformed('A line does not end in CRLF, or contains a NUL byte.');
    }

    $lines = explode("\r\n", $head);
    $parts = explode(' ', array_shift($lines));
    if (count($parts) !== 3) {
        return $this->malformed('The request line is not "METHOD target HTTP/x.y".');
    }
    [$method, $target, $protocol] = $parts;

    if (preg_match(self::TOKEN, $method) !== 1) {
        return $this->malformed('The method is not a token.');
    }
    if (!str_starts_with($target, '/')) {
        return $this->malformed('Only origin-form targets ("/path") are supported.');
    }
    if ($protocol !== 'HTTP/1.1' && $protocol !== 'HTTP/1.0') {
        return $this->malformed('Only HTTP/1.0 and HTTP/1.1 are supported.');
    }

    $headers = [];
    $lengths = [];
    foreach ($lines as $line) {
        if ($line === '' || $line[0] === ' ' || $line[0] === "\t") {
            return $this->malformed('Folded header lines are not supported.');
        }
        $colon = strpos($line, ':');
        if ($colon === false) {
            return $this->malformed('A header line has no colon.');
        }
        $name = substr($line, 0, $colon);
        if (preg_match(self::TOKEN, $name) !== 1) {
            return $this->malformed('A header name is not a token, or has whitespace before the colon.');
        }

        $name = strtolower($name);
        $value = trim(substr($line, $colon + 1), " \t");
        if ($name === 'content-length') {
            $lengths[] = $value;
        }
        $headers[$name] = isset($headers[$name]) ? $headers[$name] . ', ' . $value : $value;
    }

    $version = substr($protocol, 5);
    if ($version === '1.1' && !isset($headers['host'])) {
        return $this->malformed('HTTP/1.1 requires a Host header.');
    }
    if (isset($headers['transfer-encoding'])) {
        return new HttpError(501, 'unsupported_transfer_encoding', 'Transfer-Encoding is not supported; send Content-Length.');
    }
    if (count($lengths) > 1) {
        return $this->malformed('Content-Length is sent more than once.');
    }

    $length = 0;
    if ($lengths !== []) {
        if (preg_match('/^[0-9]{1,15}$/', $lengths[0]) !== 1) {
            return $this->malformed('Content-Length is not a plain decimal number.');
        }
        $length = (int) $lengths[0];
    }

    if ($length > $this->maxBodyBytes) {
        return new HttpError(413, 'payload_too_large', 'The body exceeds ' . $this->maxBodyBytes . ' bytes.');
    }

    return ['method' => $method, 'target' => $target, 'version' => $version, 'headers' => $headers, 'length' => $length];
}
```

[`src/Http/RequestParser.php#L149-L222`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/RequestParser.php#L149-L222)

**Перевод комментариев:**
- `parseHead()` (PHPDoc): возвращает разобранную голову — `method`, `target`,
  `version`, `headers`, `length` — либо `HttpError`.

Здесь лежит вся та же политика, что и у соседнего проекта, но выраженная не
исключениями, а возвратами: `$this->malformed()` даёт `400`, а
`Transfer-Encoding` и превышение тела возвращают `HttpError` напрямую, потому
что статус у них другой.

Три места заслуживают внимания:

- **первая же проверка** — про `\r`, `\n` и NUL по всему блоку. Она ловит и
  «строка не кончается CRLF», и обрезанный `\r`, и вставку NUL. Сделана она
  раньше, чем `explode("\r\n", ...)`, то есть до того, как строка вообще станет
  «строкой»;
- **`count($lengths) > 1`** — отказ при **любом** повторе, до сравнения
  значений. Это и есть та разная строгость, о которой шла речь выше;
- **`413` возвращается после вычисления `$length`, но до `return` с данными** —
  то есть отказ действительно принимается по одному заголовку, не дожидаясь
  тела.

Отдельно стоит `preg_match('/^[0-9]{1,15}$/', ...)`: ровно цифры, ровно от
одного до пятнадцати. И `+2`, и `-1`, и `2, 2`, и пустое значение не проходят.
Ограничение в 15 разрядов заодно не даёт прочитать в `int` число, которое уже не
поместится.

### Состояние цикла: `Server::process()`

```php
/**
 * Runs the next buffered request, if there is one and the previous
 * response has gone out.
 */
private function process(Connection $connection, float $now): void
{
    $parser = $connection->parser;

    if ($connection->output === '' && !$connection->closeAfterWrite) {
        if ($parser->takeContinue()) {
            $this->send($connection, "HTTP/1.1 100 Continue\r\n\r\n", $now);
        } else {
            $next = $parser->next();

            if ($next instanceof HttpError) {
                $this->respond($connection, $this->handler->reject($next), false, true, false, $now);
            } elseif ($next instanceof Request) {
                $connection->headStartedAt = null;
                $connection->bodyStartedAt = null;
                $this->respond(
                    $connection,
                    $this->handle($next),
                    $next->keepAlive(),
                    $next->method !== 'HEAD',
                    $next->version === '1.0',
                    $now,
                );
            } elseif ($parser->takeContinue()) {
                $this->send($connection, "HTTP/1.1 100 Continue\r\n\r\n", $now);
            }
        }
    }

    if ($parser->inHead() && $connection->headStartedAt === null) {
        $connection->headStartedAt = $now;
    }
    if ($parser->inBody() && $connection->bodyStartedAt === null) {
        $connection->bodyStartedAt = $now;
    }
}
```

[`src/Http/Server.php#L224-L263`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/Server.php#L224-L263)

**Перевод комментариев:**
- `process()` (PHPDoc): запускает следующий буферизованный запрос, если он есть
  и предыдущий ответ уже ушёл.

Условие `$connection->output === ''` — это **обратное давление и порядок ответов
в одном условии**: пока предыдущий ответ не ушёл целиком, следующий запрос
вообще не разбирается. Нет чтения — нет и нарастающего списка разобранных, но не
отправленных запросов.

`inHead()` и `inBody()` — низкоуровневые флаги парсера. Сама логика таймеров
живёт в `Server` (это уже глава 14), а парсер лишь сообщает, в какой фазе он
находится. Разделение не случайно: парсер знает про границы запроса, но не знает
про дедлайны.

### Ответ: фрейминг в обратную сторону

```php
public function encode(HttpResponse $response): string
{
    $statusLine = sprintf(
        "%s %d %s\r\n",
        $response->version->wire(),
        $response->statusCode(),
        $response->reasonPhrase(),
    );

    $headers = $response->headers->normalized();
    $body = $response->body;

    if (!$response->status->framesBody()) {
        // 204 and 304 are always bodyless. Dropping a body supplied by an
        // application here keeps an invalid response from corrupting the
        // next response on a persistent connection. This server
        // deliberately does not emit Content-Length for either status.
        $body = '';
        $headers = array_filter(
            $headers,
            static fn (string $name): bool => strtolower($name) !== 'content-length',
            ARRAY_FILTER_USE_KEY,
        );
    } elseif (!$response->headers->has('Content-Length')) {
        // Framing: every response must state how many bytes to expect,
        // empty ones included — on a kept-alive connection "the body
        // ends when the socket closes" does not hold, so a bare 200
        // would leave the client waiting for a body that never comes.
        //
        // The "already framed?" question goes through Headers, which
        // knows names are case-insensitive: asking the normalized array
        // directly would miss a handler's "content-length" and emit a
        // second, capital copy — two Content-Length lines, which
        // recipients must reject.
        $headers['Content-Length'] = (string) $response->framedContentLength();
    } else {
        $this->assertDeclaredLengthMatches($response);
    }

    $head = $statusLine;

    foreach ($headers as $name => $value) {
        // Response splitting guard: a header value is a single line on
        // the wire, so CR/LF smuggled in by a handler must never reach
        // it. ConnectionHandler turns this refusal into a 500 — it
        // cannot be the pipeline's error handler, which has already
        // returned by the time encoding starts.
        if (!Headers::isToken($name) || !Headers::isFieldValue($value)) {
            throw new RuntimeException(sprintf('Malformed response header: %s', $name));
        }

        $head .= sprintf("%s: %s\r\n", $name, $value);
    }

    return $head . "\r\n" . $body;
}
```

[`src/Http/Protocol/ResponseEncoder.php#L21-L76`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Http/Protocol/ResponseEncoder.php#L21-L76)

**Перевод комментариев:**
- `// 204 and 304 are always bodyless. Dropping a body supplied by an...` —
  `204` и `304` всегда без тела. Отбрасывание тела, которое поставило
  приложение, не даёт невалидному ответу испортить следующий ответ на постоянном
  соединении. Для обоих статусов сервер намеренно не выводит `Content-Length`.
- `// Framing: every response must state how many bytes to expect,...` —
  фрейминг: каждый ответ обязан сообщать, сколько ждать байтов, включая пустые:
  на keep-alive-соединении «тело кончается, когда закрывается сокет» не
  работает, поэтому голый `200` оставил бы клиента ждать тело, которого не
  будет. Вопрос «уже фреймлено?» идёт через `Headers`, который знает, что имена
  регистронезависимы: прямой запрос к нормализованному массиву пропустил бы
  обработчиков `content-length` и вывел бы вторую копию с заглавной буквы — две
  строки `Content-Length`, которые получатели обязаны отвергать.
- `// Response splitting guard: a header value is a single line on...` — защита
  от response splitting: значение заголовка — одна строка на проводе, поэтому
  CR/LF, протащенные обработчиком, не должны до неё дойти. `ConnectionHandler`
  превращает этот отказ в `500` — он не может быть обработчиком ошибок
  конвейера, который уже вернулся к началу кодирования.

Экодер — зеркало парсера, и у него те же три исхода, только в обратную сторону:
`204/304` → длины нет; длины нет → проставить; длина есть → **проверить, что она
совпадает с телом**. Третий случай не даёт обработчику объявить
`Content-Length: 100`, а отдать 10 байтов — такую пару приёмник обязан
отвергнуть, и лучше отвергнуть её у источника.

Комментарий про `Headers` — записанная в коде история бага: раньше вопрос
задавался обычному массиву, то есть **с учётом регистра**, и обработчик с
`content-length` получал свои заголовок **плюс** каноническую копию. Два
`Content-Length` — это ровно тот случай, который `contentLength()` в парсере
считает ошибкой.

А `isToken()`/`isFieldValue()` на стороне ответа закрывают **response
splitting**: значение заголовка — это одна строка на проводе, и `\r\n`,
принесённое из тела запроса, обязано остановиться здесь.

```php
/**
 * @param bool $keepAlive false adds `Connection: close`
 * @param bool $withBody false for HEAD: the headers describe the body, which is not sent
 */
public function serialize(bool $keepAlive, bool $withBody = true, bool $http10 = false): string
{
    $headers = $this->headers;
    $headers['Content-Length'] = (string) strlen($this->body);
    $headers['Date'] = gmdate('D, d M Y H:i:s') . ' GMT';
    if (!$keepAlive) {
        $headers['Connection'] = 'close';
    } elseif ($http10) {
        $headers['Connection'] = 'keep-alive';
    }

    $head = sprintf("HTTP/1.1 %d %s\r\n", $this->status, self::REASONS[$this->status] ?? 'Unknown');
    foreach ($headers as $name => $value) {
        $head .= "{$name}: {$value}\r\n";
    }

    return $head . "\r\n" . ($withBody ? $this->body : '');
}
```

[`src/Http/Response.php#L44-L65`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/Response.php#L44-L65)

**Перевод комментариев:**
- `serialize()` (PHPDoc): `$keepAlive = false` добавляет `Connection: close`;
  `$withBody = false` — для `HEAD`: заголовки описывают тело, которое не
  отправляется.

У соседнего проекта та же гарантия, но выраженная проще: длина берётся из
`strlen($this->body)` и **всегда** записывается поверх, без веток. Обоснование —
в docblock: сервис всегда держит ответ целиком в памяти, поэтому chunked не
нужен принципиально. `204`/`304` здесь не обрабатываются, потому что роуты,
которые их возвращают, и так не кладут тело.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-http-server
make build install
make run-server
```

Во втором терминале (или через `make shell`) выполняются команды ниже. Вывод
снят при написании главы.

### 1. Пол-запроса: сервер молчит до последнего байта

```bash
docker compose exec php php examples/partial-request.php
```

```text
Sending a 40-byte request one byte at a time:

   1/40  sent "G"      → server said nothing
   2/40  sent "E"      → server said nothing
   3/40  sent "T"      → server said nothing
   4/40  sent " "      → server said nothing
   5/40  sent "/"      → server said nothing
   6/40  sent "h"      → server said nothing
   7/40  sent "e"      → server said nothing
   8/40  sent "l"      → server said nothing
   9/40  sent "l"      → server said nothing
  10/40  sent "o"      → server said nothing
  11/40  sent " "      → server said nothing
  12/40  sent "H"      → server said nothing
  13/40  sent "T"      → server said nothing
  14/40  sent "T"      → server said nothing
  15/40  sent "P"      → server said nothing
  16/40  sent "/"      → server said nothing
  17/40  sent "1"      → server said nothing
  18/40  sent "."      → server said nothing
  19/40  sent "1"      → server said nothing
  20/40  sent "\r"     → server said nothing
  21/40  sent "\n"     → server said nothing
  22/40  sent "H"      → server said nothing
  23/40  sent "o"      → server said nothing
  24/40  sent "s"      → server said nothing
  25/40  sent "t"      → server said nothing
  26/40  sent ":"      → server said nothing
  27/40  sent " "      → server said nothing
  28/40  sent "1"      → server said nothing
  29/40  sent "2"      → server said nothing
  30/40  sent "7"      → server said nothing
  31/40  sent "."      → server said nothing
  32/40  sent "0"      → server said nothing
  33/40  sent "."      → server said nothing
  34/40  sent "0"      → server said nothing
  35/40  sent "."      → server said nothing
  36/40  sent "1"      → server said nothing
  37/40  sent "\r"     → server said nothing
  38/40  sent "\n"     → server said nothing
  39/40  sent "\r"     → server said nothing
  40/40  sent "\n"     → server said SOMETHING

39 partial reads sat in the buffer unanswered. The 40th byte completed
the request, and only then:

HTTP/1.1 200 OK
Content-Type: text/plain; charset=utf-8
Content-Length: 5
X-Response-Time: 0.0000
Connection: keep-alive

Hello
```

Скрипт не просто наблюдает — он **проверяет**:

```php
    // Only the last byte may draw an answer. Anything earlier would mean
    // the server decided where the request ended before it got there.
    $expected = $position === $last;

    printf(
        "  %2d/%d  sent %-8s → server said %s%s\n",
        $position + 1,
        count($bytes),
        '"' . addcslashes($byte, "\r\n") . '"',
        $answered ? 'SOMETHING' : 'nothing',
        $answered === $expected ? '' : '   ← WRONG',
    );

    if ($answered !== $expected) {
        fclose($client);

        exit(1);
    }
```

[`examples/partial-request.php#L42-L59`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/examples/partial-request.php#L42-L59)

**Перевод комментариев:**
- `// Only the last byte may draw an answer. Anything earlier would mean...` —
  ответ может вызвать только последний байт. Всё раньше означало бы, что сервер
  решил, где кончается запрос, до того, как это наступило.

Два наблюдения. Первое: **39 ответов «нет» подряд** — это и есть работа
`parse() === null`, повторённая 39 раз на одном и том же растущем буфере.
Второе: ответа нет и после 38-го байта, то есть после завершённых заголовков —
запрос без тела считается целым только с последним `\r\n` пустой строки. Ответ
раньше означал бы, что сервер определил конец запроса раньше, чем тот наступил.

`stream_select()` здесь используется **с нулевым таймаутом**: спросить «есть
что-то?», не читая. Чтение потребовало бы ответ, который напечатывается в конце.

### 2. Семь решений о фрейминге

Скрипт `/tmp/http-framing.php` шлёт по одному сырому запросу на соединение — по
одному фрейминговому решению на запрос:

```php
<?php

declare(strict_types=1);

/**
 * One question: where does a request end?
 *
 * Raw HTTP/1.1 over a socket, so nothing between this script and the wire.
 * Every request below is one framing decision the parser has to make.
 */

const HOST = '127.0.0.1';
const PORT = 8080;

function connect(): mixed
{
    $socket = @stream_socket_client('tcp://' . HOST . ':' . PORT, $errno, $errstr, 3.0);

    if ($socket === false) {
        fwrite(STDERR, 'Cannot reach ' . HOST . ':' . PORT . ": {$errstr}\n");

        exit(1);
    }

    stream_set_timeout($socket, 2);

    return $socket;
}

/** Reads until the server closes (every request below asks for Connection: close). */
function readAll(mixed $socket): string
{
    $raw = '';

    while (true) {
        $chunk = fread($socket, 8192);

        if ($chunk === false || $chunk === '') {
            break;
        }

        $raw .= $chunk;
    }

    return $raw;
}

/**
 * Reads exactly one response: the head up to the blank line, then as many
 * body bytes as Content-Length declares. The same boundary discipline the
 * server applies in the other direction.
 *
 * @return array{status: string, body: string}
 */
function readOneResponse(mixed $socket): array
{
    $head = '';

    while (!str_ends_with($head, "\r\n\r\n")) {
        $byte = fread($socket, 1);

        if ($byte === false || $byte === '') {
            return ['status' => '(nothing)', 'body' => ''];
        }

        $head .= $byte;
    }

    $head = substr($head, 0, -4);
    $length = 0;

    if (preg_match('/^Content-Length:[ \t]*(\d+)[ \t]*\r?$/mi', $head, $matches) === 1) {
        $length = (int) $matches[1];
    }

    $body = '';

    while (strlen($body) < $length) {
        $chunk = fread($socket, $length - strlen($body));

        if ($chunk === false || $chunk === '') {
            break;
        }

        $body .= $chunk;
    }

    $status = preg_match('#^HTTP/1\.[01] (\d{3})#', $head, $matches) === 1 ? $matches[1] : '(none)';

    return ['status' => $status, 'body' => $body];
}

function statusLine(string $raw): string
{
    $line = strtok($raw, "\r\n");

    return $line === false ? '(no response)' : $line;
}

// 1. A body split across two writes. The headers announce Content-Length,
//    so the server must say nothing until that many body bytes arrive.
$body = 'incremental';
$socket = connect();
fwrite($socket, "POST /users HTTP/1.1\r\nHost: x\r\nContent-Length: " . strlen($body) . "\r\nConnection: close\r\n\r\n");
usleep(300_000);

$read = [$socket];
$write = [];
$except = [];
$answered = stream_select($read, $write, $except, 0, 0) === 1;

echo "== 1. body split across two writes ==\n";
echo 'after the headers (body not sent yet): ' . ($answered ? 'answered' : 'nothing yet') . "\n";
fwrite($socket, $body);
echo 'after the remaining ' . strlen($body) . ' bytes: ' . statusLine(readAll($socket)) . "\n\n";

// 2. Pipelining: two whole requests in one write. One read may carry
//    several requests; both must be answered, in order.
$socket = connect();
fwrite($socket, "GET /hello HTTP/1.1\r\nHost: x\r\n\r\nGET / HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n");
$first = readOneResponse($socket);
$second = readOneResponse($socket);
echo "== 2. two requests in one write ==\n";
printf("first:  %s, body %s\n", $first['status'], json_encode($first['body']));
printf("second: %s, body %s\n\n", $second['status'], json_encode($second['body']));

// 3. Chunked: a transfer coding this parser does not decode. Framing it by
//    Content-Length would treat the chunk payload as the next request.
$socket = connect();
fwrite($socket, "POST /users HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n");
echo "== 3. Transfer-Encoding: chunked ==\n" . statusLine(readAll($socket)) . "\n\n";

// 4. Two Content-Length values that disagree: two possible request ends.
$socket = connect();
fwrite($socket, "POST /users HTTP/1.1\r\nHost: x\r\nContent-Length: 3\r\nContent-Length: 4\r\nConnection: close\r\n\r\nabc");
echo "== 4. conflicting Content-Length ==\n" . statusLine(readAll($socket)) . "\n\n";

// 5. Two Content-Length values that agree. RFC 7230 allows this, and the
//    parser accepts it: identical values name the same one boundary.
$socket = connect();
fwrite($socket, "POST /users HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 5\r\nConnection: close\r\n\r\nabcde");
echo "== 5. two identical Content-Length lines ==\n" . statusLine(readAll($socket)) . "\n\n";

// 6. HTTP/1.1 without Host, which makes the request ambiguous on a shared
//    address (RFC 7230 5.4).
$socket = connect();
fwrite($socket, "GET / HTTP/1.1\r\nConnection: close\r\n\r\n");
echo "== 6. HTTP/1.1 without Host ==\n" . statusLine(readAll($socket)) . "\n\n";

// 7. A body over the limit. The header itself is enough to refuse it, so
//    413 arrives before a single body byte does.
$socket = connect();
fwrite($socket, "POST /users HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999\r\nConnection: close\r\n\r\n");
usleep(300_000);

$read = [$socket];
$write = [];
$except = [];
$early = stream_select($read, $write, $except, 0, 0) === 1;

echo "== 7. Content-Length over the 1 MiB body limit ==\n";
echo 'answered before the body arrived: ' . ($early ? 'yes' : 'no') . "\n";
echo statusLine(readAll($socket)) . "\n";
```

**Перевод комментариев:**
- `/** One question: where does a request end?...` — вопрос один: где кончается
  запрос? Сырой HTTP/1.1 поверх сокета, так что между скриптом и проводом ничего
  нет. Каждый запрос ниже — одно решение о фрейминге, которое должен принять
  парсер.
- `readAll()` (PHPDoc): читает, пока сервер не закроет соединение (каждый запрос
  ниже просит `Connection: close`).
- `readOneResponse()` (PHPDoc): читает ровно один ответ — голову до пустой
  строки, затем столько байтов тела, сколько объявил `Content-Length`. Та же
  дисциплина границ, которую сервер применяет в обратную сторону. `@return` —
  статус и тело.
- `// 1. A body split across two writes. The headers announce Content-Length,` —
  тело, разделённое на две записи. Заголовки объявляют `Content-Length`, поэтому
  сервер должен молчать, пока не придут все байты тела.
- `// 2. Pipelining: two whole requests in one write. One read may carry` —
  pipelining: два целых запроса в одной записи. Одно чтение может принести
  несколько запросов; оба должны быть отвечены по порядку.
- `// 3. Chunked: a transfer coding this parser does not decode...` — `chunked`:
  transfer coding, который парсер не декодирует. Фрейминг по `Content-Length`
  счёл бы данные чанка следующим запросом.
- `// 4. Two Content-Length values that disagree: two possible request ends.` —
  два расходящихся значения `Content-Length`: два возможных конца запроса.
- `// 5. Two Content-Length values that agree. RFC 7230 allows this, and the` —
  два совпадающих значения `Content-Length`. RFC 7230 это разрешает, и парсер
  принимает: одинаковые значения называют одну и ту же границу.
- `// 6. HTTP/1.1 without Host, which makes the request ambiguous on a shared` —
  HTTP/1.1 без `Host`, что делает запрос неоднозначным на общем адресе (RFC 7230
  5.4).
- `// 7. A body over the limit. The header itself is enough to refuse it, so` —
  тело сверх лимита. Одного заголовка достаточно, чтобы отказать, поэтому `413`
  приходит до первого байта тела.

Скопируйте файл в контейнер и запустите:

```bash
docker compose cp /tmp/http-framing.php php:/tmp/http-framing.php
docker compose exec php php /tmp/http-framing.php
```

```text
== 1. body split across two writes ==
after the headers (body not sent yet): nothing yet
after the remaining 11 bytes: HTTP/1.1 201 Created

== 2. two requests in one write ==
first:  200, body "Hello"
second: 200, body "Hello, world!\n"

== 3. Transfer-Encoding: chunked ==
HTTP/1.1 501 Not Implemented

== 4. conflicting Content-Length ==
HTTP/1.1 400 Bad Request

== 5. two identical Content-Length lines ==
HTTP/1.1 201 Created

== 6. HTTP/1.1 without Host ==
HTTP/1.1 400 Bad Request

== 7. Content-Length over the 1 MiB body limit ==
answered before the body arrived: yes
HTTP/1.1 413 Payload Too Large
```

Разбор:

1. **Заголовки ушли, тело — нет, ответа нет.** `usleep()` даёт серверу 300 мс,
   чтобы впечататься в вопрос, — и `stream_select(0)` показывает, что отвечать
   нечего. После 11 байтов тела — `201`. Это буквально тот же `null`, но в
   другом месте: ждём не заголовки, а тело.
2. **Два запроса в одной записи → два ответа.** `readOneResponse()` читает ровно
   столько байтов, сколько объявляет `Content-Length`, — и поэтому способен
   увидеть границу между ответами. Второй ответ приходит с `Connection: close` и
   телом `"Hello, world!\n"`; первый ещё keep-alive.
3. **`chunked` → `501`.** Статус не `400`: парсер не считает запрос невалидным,
   он считает его **неразрешимым** — способ конца запроса неизвестен.
4. **Расходящиеся длины → `400`.** Две возможные границы, выбрать — значит
   выбрать за атакующего.
5. **Совпадающие длины → `201`.** Тот же запрос, что в п. 4, но значения равны.
   Это решение `php-mini-http-server`; у `php-mini-search` он даёт `400` (см.
   эксперимент 4).
6. **HTTP/1.1 без `Host` → `400`.** Запрос некорректен по RFC 7230 5.4.
7. **`413` до тела.** `answered before the body arrived: yes` — и это самое
   важное слово в выводе: предел прочитан из заголовка.

### 3. Нарезка не меняет результат

Теперь второй проект. `/tmp/slicing.php` гонит **один и тот же** буфер через
настоящий `RequestParser` тремя способами:

```php
<?php

declare(strict_types=1);

/**
 * One question: does the result depend on how the bytes arrive?
 *
 * The same RequestParser, fed the same bytes three ways - whole, one byte at
 * a time, and in irregular chunks - must produce the same requests. Then the
 * same parser against the two framings it refuses.
 */

use PhpMiniSearch\Http\HttpError;
use PhpMiniSearch\Http\Request;
use PhpMiniSearch\Http\RequestParser;

require '/app/vendor/autoload.php';

function describe(Request|HttpError $result): string
{
    if ($result instanceof HttpError) {
        return "HttpError {$result->status} {$result->code}";
    }

    return "Request {$result->method} {$result->target} body=" . json_encode($result->body);
}

/**
 * Feeds $raw in the given slice sizes and returns every result in order.
 * An HttpError is final, so it stops the walk.
 *
 * @param list<int> $sizes
 *
 * @return list<string>
 */
function feedInSlices(string $raw, array $sizes): array
{
    $parser = new RequestParser();
    $results = [];
    $offset = 0;

    foreach ($sizes as $size) {
        $parser->feed(substr($raw, $offset, $size));
        $offset += $size;

        while (true) {
            $next = $parser->next();

            if ($next === null) {
                break;
            }

            $results[] = describe($next);

            if ($next instanceof HttpError) {
                break 2;
            }
        }
    }

    return $results;
}

/** A deterministic irregular split, so the run is reproducible. */
function irregularSizes(int $length): array
{
    $pattern = [7, 1, 13, 3, 2, 11, 5, 4];
    $sizes = [];
    $covered = 0;
    $i = 0;

    while ($covered < $length) {
        $size = min($pattern[$i % count($pattern)], $length - $covered);
        $sizes[] = $size;
        $covered += $size;
        $i++;
    }

    return $sizes;
}

$put = "PUT /documents/a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello";

echo "== slicing invariance: a PUT with a 5-byte body ==\n";
printf("whole:             %s\n", feedInSlices($put, [strlen($put)])[0]);
printf("one byte at a time: %s\n", feedInSlices($put, array_fill(0, strlen($put), 1))[0]);
printf("irregular chunks:  %s\n", feedInSlices($put, irregularSizes(strlen($put)))[0]);
echo "\n";

$pipelined = $put . "GET /documents/a HTTP/1.1\r\nHost: x\r\n\r\n";

echo "== two requests, one buffer ==\n";

foreach (feedInSlices($pipelined, [strlen($pipelined)]) as $line) {
    echo $line . "\n";
}

echo "\n";

$chunked = "POST /reindex HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n";
$twoLengths = "PUT /documents/a HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello";

echo "== Transfer-Encoding: chunked ==\n" . feedInSlices($chunked, [strlen($chunked)])[0] . "\n\n";
echo "== two Content-Length lines (even identical) ==\n" . feedInSlices($twoLengths, [strlen($twoLengths)])[0] . "\n";
```

**Перевод комментариев:**
- `/** One question: does the result depend on how the bytes arrive?...` —
  вопрос один: зависит ли результат от того, как приходят байты? Один и тот же
  `RequestParser`, получив одни и те же байты тремя способами — целиком, по
  одному байту и нерегулярными кусками — должен выдать те же запросы. Затем тот
  же парсер против двух фреймингов, которые он отвергает.
- `feedInSlices()` (PHPDoc): кормит `$raw` кусками заданных размеров и
  возвращает все результаты по порядку. `HttpError` окончателен, поэтому обход
  останавливается. `@param list<int> $sizes` — размеры кусков,
  `@return list<string>` — описания результатов.
- `irregularSizes()` (PHPDoc): детерминированная нерегулярная нарезка, чтобы
  прогон был воспроизводим.

Второй проект не требует сервера — парсеру хватает автозагрузчика:

```bash
cd php-mini-search
make build install
docker compose cp /tmp/slicing.php php:/tmp/slicing.php
docker compose exec php php /tmp/slicing.php
```

```text
== slicing invariance: a PUT with a 5-byte body ==
whole:             Request PUT /documents/a body="hello"
one byte at a time: Request PUT /documents/a body="hello"
irregular chunks:  Request PUT /documents/a body="hello"

== two requests, one buffer ==
Request PUT /documents/a body="hello"
Request GET /documents/a body=""

== Transfer-Encoding: chunked ==
HttpError 501 unsupported_transfer_encoding

== two Content-Length lines (even identical) ==
HttpError 400 malformed_request
```

Три строки первого блока **оканчиваются одинаково** — и в этом весь тезис. Одно
и то же `PUT /documents/a` с телом `hello` было нарезано один раз целиком, по
одному байту (65 раз) и кусками 7, 1, 13, 3, 2, 11, 5, 4; автомат в каждом
случае дошёл до одного и того же результата. Разные префиксы в начале строк —
это подписи, а не различия в ответе парсера.

`irregularSizes()` специально **детерминирована**: случайные куски пришлось бы
фиксировать сидом, а так прогон воспроизводим байт в байт. Тот же принцип
используют тесты проекта, только вместо фиксированного паттерна —
`mt_srand(crc32($raw))`.

Второй блок показывает, что хвост после запроса не теряется: `PUT` с телом и
`GET` без тела лежали в одном буфере, и парсер вернул оба, в порядке следования.
Третий и четвёртый — те же отказы, что и в эксперименте 2, но в виде объектов
`HttpError`, из которых сервер уже сделает статусы `501` и `400`.

### 4. Те же отказы на живом сервисе

`php-mini-search` можно поднять как обычный сервер и посмотреть, во что
превращаются эти `HttpError` на проводе:

```bash
cd php-mini-search
make run-server
```

Во втором терминале — `/tmp/mini-search-wire.php`:

```php
<?php

declare(strict_types=1);

/**
 * One question: what does the wire look like when framing is refused?
 *
 * Raw HTTP/1.1 against the running service. Each request below is one of the
 * limits in docs/ARCHITECTURE.md (HTTP subset), answered as a real status
 * line rather than an exception the test never sees.
 */

const HOST = '127.0.0.1';
const PORT = 8080;
const TOKEN = 'demo-token-demo-token-demo-token-0';

function connect(): mixed
{
    $socket = @stream_socket_client('tcp://' . HOST . ':' . PORT, $errno, $errstr, 3.0);

    if ($socket === false) {
        fwrite(STDERR, 'Cannot reach ' . HOST . ':' . PORT . ": {$errstr}\n");

        exit(1);
    }

    stream_set_timeout($socket, 2);

    return $socket;
}

function readAll(mixed $socket): string
{
    $raw = '';

    while (true) {
        $chunk = fread($socket, 8192);

        if ($chunk === false || $chunk === '') {
            break;
        }

        $raw .= $chunk;
    }

    return $raw;
}

function statusLine(string $raw): string
{
    $line = strtok($raw, "\r\n");

    return $line === false ? '(no response)' : $line;
}

// 1. The head split across two writes: the server says nothing until the
//    blank line lands, then answers.
$socket = connect();
fwrite($socket, "GET /health/live HTTP/1.1\r\nHost: x\r\n");
usleep(300_000);

$read = [$socket];
$write = [];
$except = [];
$answered = stream_select($read, $write, $except, 0, 0) === 1;

echo "== 1. request line + headers in two writes ==\n";
echo 'after the first half: ' . ($answered ? 'answered' : 'nothing yet') . "\n";
fwrite($socket, "Connection: close\r\n\r\n");
echo 'after the blank line: ' . statusLine(readAll($socket)) . "\n\n";

// 2. A transfer coding other than identity: refused with 501, before any
//    body is read.
$socket = connect();
fwrite($socket, "POST /reindex HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\nhello\r\n0\r\n\r\n");
echo "== 2. Transfer-Encoding: chunked ==\n" . statusLine(readAll($socket)) . "\n\n";

// 3. Two Content-Length lines, identical: a repeated length is refused even
//    when the values agree (docs/ARCHITECTURE.md, HTTP subset).
$socket = connect();
fwrite($socket, "POST /search HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " . TOKEN . "\r\nContent-Length: 2\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}");
echo "== 3. two identical Content-Length lines ==\n" . statusLine(readAll($socket)) . "\n\n";

// 4. A body over the limit, announced by Content-Length: 413 comes from the
//    headers, so the body never has to arrive to be refused.
$socket = connect();
fwrite($socket, "POST /search HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer " . TOKEN . "\r\nContent-Type: application/json\r\nContent-Length: 999999999\r\nConnection: close\r\n\r\n");
usleep(300_000);

$read = [$socket];
$write = [];
$except = [];
$early = stream_select($read, $write, $except, 0, 0) === 1;

echo "== 4. Content-Length over the 256 KiB limit ==\n";
echo 'answered before the body arrived: ' . ($early ? 'yes' : 'no') . "\n";
echo statusLine(readAll($socket)) . "\n\n";

// 5. A normal request, to show the refusals above sit in an otherwise
//    working service.
$socket = connect();
fwrite($socket, "GET /health/live HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n");
echo "== 5. an ordinary request ==\n" . statusLine(readAll($socket)) . "\n";
```

**Перевод комментариев:**
- `/** One question: what does the wire look like when framing is refused?...` —
  вопрос один: как выглядит провод, когда фрейминг отвергнут? Сырой HTTP/1.1
  против запущенного сервиса. Каждый запрос ниже — один из лимитов
  `docs/ARCHITECTURE.md` (HTTP subset), отвеченный настоящей статусной строкой,
  а не исключением, которого тест никогда не видит.
- `// 1. The head split across two writes: the server says nothing until the` —
  голова, разделённая на две записи: сервер молчит, пока не придёт пустая
  строка, затем отвечает.
- `// 2. A transfer coding other than identity: refused with 501, before any` —
  transfer coding, отличный от `identity`: отказ с `501` до чтения любого тела.
- `// 3. Two Content-Length lines, identical...` — две одинаковые строки
  `Content-Length`: повторная длина отвергается, даже когда значения совпадают
  (`docs/ARCHITECTURE.md`, HTTP subset).
- `// 4. A body over the limit, announced by Content-Length...` — тело сверх
  лимита, объявленное через `Content-Length`: `413` берётся из заголовков,
  поэтому телу вовсе не нужно прийти, чтобы получить отказ.
- `// 5. A normal request, to show the refusals above sit in an otherwise` —
  обычный запрос, чтобы показать: отказы выше живут во вполне работающем
  сервисе.

```bash
docker compose cp /tmp/mini-search-wire.php php:/tmp/mini-search-wire.php
docker compose exec php php /tmp/mini-search-wire.php
```

```text
== 1. request line + headers in two writes ==
after the first half: nothing yet
after the blank line: HTTP/1.1 200 OK

== 2. Transfer-Encoding: chunked ==
HTTP/1.1 501 Not Implemented

== 3. two identical Content-Length lines ==
HTTP/1.1 400 Bad Request

== 4. Content-Length over the 256 KiB limit ==
answered before the body arrived: yes
HTTP/1.1 413 Content Too Large

== 5. an ordinary request ==
HTTP/1.1 200 OK
```

Здесь видно всё то же самое, но уже как **ответ клиенту**. Четыре отличия от
эксперимента 2, и все четыре показывают решения, а не случайности:

- п. 1: `null` у `next()` на живом сервисе превращается в **молчание**, а
  завершение `\r\n\r\n` — в `200`;
- п. 3: тот же запрос, что дал `201` у соседнего проекта, здесь получает `400` —
  это и есть та разная строгость;
- п. 4: `Content-Length: 999999999` отвергнут **до** тела, и `usleep(300_000)`
  это доказывает;
- п. 5: отказы сидят в полностью рабочем сервисе — `501`, `400` и `413` не
  ломают ни текущее соединение, ни следующее.

## 5. Отказы

| Что ломается                                              | Чем обнаруживается                                                        | Что происходит                                                     |
| --------------------------------------------------------- | ------------------------------------------------------------------------- | ------------------------------------------------------------------ |
| Запрос пришёл по частям                                   | `parse()` → `null`/`next()` → `null`                                    | байты лежат в буфере; ответа нет до полного запроса                |
| Тело меньше объявленного                                  | `$availableBody < $contentLength`                                         | `null`; часы чтения тела идут дальше                               |
| `Transfer-Encoding: chunked` (и рядом с `Content-Length`) | `assertDecodableBody()`/`isset($headers['transfer-encoding'])`          | `501`, соединение закрывается                                      |
| Два расходящихся `Content-Length`                         | `count(array_unique($parts)) !== 1`                                       | `400`, соединение закрывается                                      |
| `Content-Length` не число (`abc`, `-1`, `+5`, `0x5`)      | `ctype_digit()`/`^[0-9]{1,15}$`                                         | `400`                                                              |
| Два одинаковых `Content-Length`                           | `php-mini-http-server` → `201`; `php-mini-search` → `count($lengths) > 1` | осознанное расхождение двух проектов                               |
| Повторяющийся `Content-Length: 2, 2`                      | запятая в склеенном значении                                              | `400` в обоих проектах                                             |
| Блок заголовков больше лимита                             | лимит проверяется **до** поиска терминатора                               | `431`                                                              |
| Тело больше лимита                                        | длина сравнивается с лимитом **до** чтения тела                           | `413`                                                              |
| HTTP/1.1 без `Host`                                       | `assertHostIsUsable()`/`version === '1.1'`                              | `400`                                                              |
| `Host : x` (пробел до двоеточия)                          | `isToken($name)`/`preg_match(TOKEN)`                                    | `400` — не «чиним», а отказываем                                   |
| Сложенная строка заголовка                                | `$line[0] === ' '`                                                        | `400`                                                              |
| Строка не кончается CRLF, есть NUL                        | первая проверка `parseHead()`                                             | `400`                                                              |
| Ошибка фрейминга                                          | `queueError()` → `closeAfterFlush()`; кэш `$error`                        | отказ окончательный; сервер жив                                    |
| `Content-Length` ответа не совпадает с телом              | `assertDeclaredLengthMatches()`                                           | отказ **до** отправки                                              |
| `\r\n` в значении заголовка ответа                        | `isFieldValue()` в `encode()`                                             | отказ → `500`, а не отправленный ответ                             |
| Буфер пуст и заголовков нет                               | `readBuffer()->isEmpty()`                                                 | это keep-alive, а не Slowloris; часы заголовков **не** запускаются |
| Заголовки дописываются бесконечно                         | `headerTimeout`/`headTimeout`                                           | отключение по таймауту (→ глава 14)                                |
| `chunked`-ответ, стриминг, SSE                            | `docs/FAILURE-MODEL.md`                                                   | не поддерживаются осознанно: обработчик возвращает ответ целиком   |

Четыре статуса, которыми разбор заканчивается, — `400`, `431`, `413`, `501` —
перечислены в `docs/DECISIONS.md` одним предложением: «Four things can go wrong
while parsing: a malformed request, a header block over the limit, a body over
the limit, an undecodable transfer coding. Each has a status».

Отдельно стоит граница, которой **нет** в таблице. У `php-mini-http-server`
запрос целиком буферизуется до того, как обработчик увидит первый байт, — это
записано в `docs/FAILURE-MODEL.md`: «a handler never sees the first byte until
the last has arrived». То есть у проекта нет ни стриминга запроса, ни стриминга
ответа. Это не упущение, а свойство: пока тело не собрано, о нём нечего судить;
пока ответ не посчитан, его нельзя фреймить.

## 6. Как это проверено

`php-mini-http-server` — `tests/Http/Protocol/HttpParserTest.php`:
`testReturnsNullWhileHeadersAreIncomplete`,
`testReturnsNullWhileBodyIsIncomplete`, `testParsesPostWithContentLengthBody`,
`testConsumedBytesExcludesBytesOfTheNextRequest`,
`testPipelinedRequestsAreParsedOneByOneFromOneBuffer`, а также весь блок
отказов: `testRejectsMalformedContentLength`,
`testAcceptsRepeatedIdenticalContentLength`,
`testRejectsConflictingContentLength`, `testRejectsChunkedTransferEncoding`,
`testRejectsTransferEncodingEvenAlongsideContentLength`,
`testAcceptsTransferEncodingIdentity`, `testRejectsHttp11RequestWithoutHost`,
`testRejectsWhitespaceBetweenHeaderNameAndColon`,
`testRejectsHeaderLineStartingWithWhitespace`,
`testRejectsControlCharactersInHeaderValues`. Лимиты —
`testThrowsWhenIncompleteHeadersExceedTheLimit`,
`testThrowsWhenDeclaredBodyExceedsTheLimit`, `testBodyAtTheLimitIsAccepted`,
`testHeaderBlockAtTheLimitIsAccepted`.

Границы «должен вернуть `null` / должен бросить исключение / должен разобрать»
покрыты отдельным файлом с таблицей —
`tests/Http/Protocol/HttpParserFuzzTest.php`:
`testIncompleteInputAsksForMoreBytes` (12 строк в `needsMoreBytesProvider`,
включая «терминатор разрезан пополам» и «без последнего байта тела»),
`testMalformedInputIsRefused` (35 строк в `refusedProvider` по четырём разделам:
запросная строка — 12, грамматика заголовков — 9, `Host` — 3, фрейминг — 11) и
`testValidInputStillParses` (12 строк — чтобы проверки не стали настолько
жёсткими, что отвергают валидный HTTP).

Ответный фрейминг — `tests/Http/Protocol/ResponseEncoderTest.php`:
`testAddsContentLengthWhenMissing`,
`testEmptyResponseFramesZeroContentLengthForKeepAlive`,
`testNoContentStatusOmitsContentLength`,
`testNotModifiedStatusOmitsContentLength`,
`testRejectsContentLengthThatDoesNotMatchTheBody`,
`testRejectsHeaderValueWithCrLf`, `testRejectsMalformedHeaderName`,
`testHandSetContentLengthIsRespectedWhateverItsCasing`.

`php-mini-search` — `tests/Http/RequestParserTest.php`. Ключевой тест называется
буквально так, как сформулирована глава:
`testTheResultDoesNotDependOnHowTheBytesArrive`. Он прогоняет каждую строку
корпуса **тремя** способами (целиком, по одному байту, пять раундов случайных
чанков с сидом `crc32($raw)`) и требует одинаковый результат. Корпус — 29 строк:
8 валидных, 20 отказов и одна «тихая» — `bare LF line endings`, где парсер
обязан **переждать**, а не решать, потому что строка без CRLF ещё не строка.
Отказы покрывают весь диапазон статусов: `400 malformed_request` (от двойного
`Content-Length` до сложенной строки заголовка),
`501 unsupported_transfer_encoding`, `413 payload_too_large`,
`431 headers_too_large`.

Последняя строка корпуса — `nothing after an error` — проверяет отдельное
свойство: в буфере лежит невалидный запрос, а следом **валидный**. Ожидаемый
результат — ровно один отказ, то есть второй запрос не разбирается вовсе. Именно
это и значит «отказ окончателен»: после ошибки фрейминга никто не знает, где
начинается следующий запрос.

`Expect: 100-continue` — `testExpectContinueIsOfferedOnceBeforeTheBody` (и
`takeContinue()` больше не отдаёт тот же флаг повторно),
`testExpectContinueIsNotOfferedWhenTheBodyIsAlreadyHere`.

Уже на уровне сервера, то есть с сокетами и таймерами, —
`tests/Http/ServerTest.php`:
`testARequestGetsAResponseWithAnExactContentLength`,
`testPipelinedRequestsAreAnsweredInOrder`,
`testKeepAliveServesSeveralRequestsOnOneConnection`,
`testARejectedRequestGetsItsErrorAndTheConnectionCloses`,
`testExpectContinueWithAnOversizedBodyIsRefusedBeforeTheBodyIsSent`,
`testASlowHeadIsDroppedAfterTheHeadTimeout`,
`testASlowBodyIsDroppedAfterTheBodyTimeout`.

Эксперименты выше — это четыре живых прогона: поштучный запрос, семь
фрейминговых решений, нарезка буфера и отказы на живом сервисе.

Запускаем:

```text
$ vendor/bin/phpunit tests/Http/Protocol
OK (104 tests, 142 assertions)

$ vendor/bin/phpunit
OK (259 tests, 598 assertions)
```

```text
$ vendor/bin/phpunit tests/Http
OK (50 tests, 324 assertions)

$ vendor/bin/phpunit
OK (180 tests, 47230 assertions)
```

## 7. Цифры

Живые прогоны этой главы (Docker, PHP 8.5.11):

| Что                                           | Значение                                                              |
| --------------------------------------------- | --------------------------------------------------------------------- |
| Запрос в 40 байтов, по одному байту за запись | 39 частичных чтений без ответа, ответ на 40-м байте                   |
| 7 запросов → 8 ответов                        | `201`, `200`, `200`, `501`, `400`, `201`, `400`, `413`                |
| Тело в два приёма                             | молчание после заголовков, `201` после 11 байтов                      |
| `Transfer-Encoding: chunked`                  | `501 Not Implemented` в обоих проектах                                |
| Два одинаковых `Content-Length`               | `201` (`php-mini-http-server`) против `400` (`php-mini-search`)       |
| Нарезка одним буфером                         | целиком, 65 байт по одному, нерегулярные куски — результаты совпадают |
| `Content-Length` больше лимита                | `413` **до** прихода тела, в обоих проектах                           |
| Тесты протокола `php-mini-http-server`        | 104 теста, 142 утверждения                                            |
| Весь набор `php-mini-http-server`             | 259 тестов, 598 утверждений                                           |
| Тесты HTTP `php-mini-search`                  | 50 тестов, 324 утверждения                                            |
| Весь набор `php-mini-search`                  | 180 тестов, 47 230 утверждений                                        |

Лимиты, задающие границы разбора:

|                    | `php-mini-http-server` | `php-mini-search`       |
| ------------------ | ---------------------- | ----------------------- |
| блок заголовков    | 8 192 байта            | 8 192 байт              |
| тело               | 1 048 576 байт (1 МиБ) | 262 144 байта (256 КиБ) |
| соединений         | 512                    | 256                     |
| таймаут заголовков | 5 с                    | 10 с                    |
| таймаут тела       | отдельного нет         | 30 с                    |
| простоя соединения | 30 с                   | 30 с                    |
| отправки ответа    | отдельного нет         | 30 с                    |

Первые два числа `php-mini-search` взяты из таблицы `docs/ARCHITECTURE.md`,
остальные для него же — из конструктора `ServerConfig`; у `php-mini-http-server`
все значения оттуда же. Разница в **количестве** часов, а не в строгости: у
`php-mini-http-server` их ровно два — `headerTimeout = 5.0` и
`connectionTimeout = 30.0`, поэтому чтение тела охраняет лишь общий таймаут
соединения. У `php-mini-search` часов четыре, и каждый привязан к своей фазе:
`headTimeout`, `bodyTimeout`, `idleTimeout`, `writeTimeout`.

Отдельно стоит напомнить, что статус `501` — это **не** «фича не готова». Проект
пишет об этом прямо: игнорировать `Transfer-Encoding` и фреймить по
`Content-Length` — это не «частичная поддержка», это request smuggling. Поэтому
отказ — не заглушка на время, а постоянная граница: пока декодирование не
реализовано целиком, безопаснее не начинать.

## 8. Упражнения

1. **Объясните.** В `parse()` проверка лимита заголовков стоит **раньше** поиска
   `\r\n\r\n`. Перенесите её ниже и опишите, что получится: какой запрос станет
   возможен, каким кодом отказа он закончится и почему именно этим.
2. **Найдите ошибку.** Представьте парсер, который при `null` **обрезает** буфер
   до последнего `\r\n`, чтобы «не копить». Какие байты будут потеряны и какой
   тест — `testPipelinedRequestsAreParsedOneByOneFromOneBuffer` или
   `testReturnsNullWhileBodyIsIncomplete` — упадёт первым?
3. **Сравните.** `php-mini-http-server` принимает два одинаковых
   `Content-Length`, `php-mini-search` — нет. Найдите последовательность
   запросов, после которой это расхождение станет **видимым** клиенту, то есть
   не просто два разных статуса на один запрос.
4. **Спроектируйте.** Сейчас `assertDecodableBody()` отвергает всё, кроме
   `identity`. Опишите, что придётся добавить для настоящего `chunked`: какая
   нужна машина состояний, какие статусы должны появиться на ней самой и что
   станет с `consumedBytes`, когда чанк-данные лежат между двумя запросами.
5. **Посчитайте.** `parse()` заново ищет `\r\n\r\n` во всём буфере на каждом
   вызове. Сколько сравнений сделает парсер, если клиент пришлёт заголовки в 8
   КиБ по одному байту? А сколько сделает автомат с `$head`? Почему при этом
   первый вариант остаётся допустимым?
6. **Разберите отказ.** В `ResponseEncoder::encode()` есть три ветки: нет
   `framesBody()` → выкинуть длину; нет `Content-Length` → проставить; есть →
   проверить. Что произойдёт с keep-alive-соединением, если вторую ветку
   заменить на «ничего не делать»? Какой из тестов `ResponseEncoderTest` это
   поймает?
7. **Спроектируйте отказ.** Оба проекта закрывают соединение после ошибки
   фрейминга. Обоснуйте: почему «продолжить со следующей строки» невозможно в
   принципе, даже если бы сервер знал, что заголовки уже разобраны? Что именно в
   этом случае неизвестно?

## Что дальше

- **Глава 10** — соединения и буферы: `ReadBuffer`, в который складываются
  частичные чтения, и `WriteBuffer`, который отвечает за то, чтобы ответ ушёл.
- **Глава 11** — протокол и pipelining на RESP: та же задача «байты →
  сообщение», но с самодостаточным кадром из приставки длины.
- **Глава 14** — keep-alive, таймауты, обратное давление и медленные клиенты:
  часы `headTimeout`/`bodyTimeout`/`idleTimeout`/`writeTimeout`, о которых
  здесь лишь упоминалось, и судьба соединения, которое дописывает заголовки
  вечно.
- **Глава 26** — как проверить сами тесты: почему зелёный фаззинг-тест ещё не
  доказывает, что он способен упасть.
