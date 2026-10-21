# Глава 14. Keep-alive, таймауты, обратное давление, медленные клиенты

> Проекты:
> [`php-mini-http-server`](https://github.com/Researcher86/php-mini-http-server)
> (релиз `v1.1.0`, коммит `1f73404`) и
> [`php-mini-search`](https://github.com/Researcher86/php-mini-search) (релиз
> `v1.0.0`, коммит `7ecddc0`). Нужно знать: соединения и буферы (→ глава 10),
> протокол и pipelining (→ глава 11), инкрементальный разбор и фрейминг (→ глава
> 13). Пригодится дальше: страницы, записи и B-деревья (→ глава 15) и базовая
> линия с одной оптимизацией (→ глава 25). Связанное уже пройденное: корректное
> завершение, `drainTimeout` и `SIGTERM` (→ глава 7).

Глава 13 закончилась тем, что сервер научился отвечать на вопрос «где кончается
запрос». Но ответ предполагал и вторую половину, которую она оставляла за
кадром: **соединение живёт дольше одного запроса**. Пока сервер разбирал ровно
один запрос и закрывал сокет, весь жизненный цикл соединения был предсказуем —
открылось, прочитало, ответило, закрылось. Как только появился keep-alive,
соединение стало объектом, у которого есть **простой**: состояние, в котором
ничего не происходит и никто никому ничего не должен.

Именно в этом состоянии и живёт вся глава. Вопрос, на который она отвечает,
звучит так: **сколько один клиент вправе занимать у сервера, пока не делает
ничего полезного?**

Ответ в лаборатории устроен одинаково — ограничение по времени плюс ограничение
по объёму — но собран из разного числа деталей. `php-mini-http-server` держит
**двое часов**: одни для простоя, другие для заголовков. `php-mini-search`
держит **четверо**: заголовки, тело, простой, запись. И оба отказываются
наказывать медленного клиента закрытием, пока он не перестал двигаться вовсе.

## 1. Проблема

### Соединение — это ресурс с жизнью длиннее запроса

Без keep-alive клиент платит за соединение на каждый запрос: `connect()` →
запрос → ответ → `close()`. В главе 10 на этом была построена вся модель
`php-mini-cache`, и там же было видно, во что обходится сама установка
соединения: три рукопожатия, буферы ядра, объект на стороне сервера.

Keep-alive убирает эту плату — и переносит проблему с «сколько стоит открыть» на
«сколько стоит держать открытым». Открытое соединение, которое ничего не делает,
продолжает занимать: один файловый дескриптор, один объект `Connection`, буфер
на чтение, буфер на запись и **две записи в списках наблюдения цикла** (глава
10). Пока таких соединений единицы, это незаметно. Проблема появляется, когда их
становится столько же, сколько может открыть один клиент, — а один клиент может
открыть очень много.

Это и есть форма атаки, которую глава разбирает под именем **Slowloris**: не
«сломать сервер запросом», а «занять сервер соединениями, каждое из которых
почти ничего не делает». Занимать можно не только простаивая.

### Четыре способа удержать сокет

Разложим «почти ничего не делает» на то, что сервер действительно может
наблюдать. На соединении после установки возможны ровно четыре фазы, и в каждой
клиент волен замедлиться:

1. **соединился и молчит** — байтов нет вовсе;
2. **начал заголовки и не кончил** — байты есть, но `\r\n\r\n` не наступает;
3. **объявил тело и не прислал** — заголовки кончились, `Content-Length`
   известен, тело не приходит;
4. **не читает ответ** — сервер уже написал ответ, а клиент его не забирает.

Первые три атакуют **чтение**, четвёртая — **запись**, и она опаснее прочих,
потому что здесь медлит не притворщик, а самый обычный клиент: тот, у которого
замедлилась сеть, или тот, кто поставил `read()` на паузу. Серверу нечего
обвинить — и всё же он не имеет права копить ответы для него без конца.

Сломать можно и вовсе без медлительности: достаточно прислать много запросов
разом, чтобы ответы на них не влезли в сокет. Это — **обратное давление**, пятая
ситуация, но устроена она иначе: клиент не медлит, сервер отказывается
производить быстрее, чем сокет успевает отдавать.

### `stream_select()` — это вопрос, а не ответ

Вся эта глава держится на одном свойстве `stream_select()`, которое в главе 4
было названо, но не было доведено до последствий: он **уровневый**
(level-triggered), а не событийный. Он не «сообщает о событии»; он отвечает на
вопрос «какие из перечисленных мной дескрипторов сейчас готовы». Значит, решение
о том, **кого перечислять**, сервер принимает сам, каждую итерацию, для каждого
соединения.

Отсюда — все механизмы главы:

- соединение, которому нечего читать, просто **не попадает** в набор на чтение;
- соединение, которому нечего писать, не попадает в набор на запись;
- соединение, которое не попадает ни туда ни туда, не может занять цикл;
- соединение, которое попадает, обязано быть опрошено в этом же проходе.

Отсюда и ловушка. Готовность на чтение **не означает**, что там есть байты:
готовность может прийтись на уже прочитанный буфер, и на неблокирующем сокете
`fread()` тогда вернёт `''`. Если принять пустое чтение за обрыв, каждая такая
ложная готовность обернётся закрытием живого соединения. Проекты узнали об этом
двумя разными путями и записали оба.

## 2. Устройство

### Цикл состояний и место простоя в нём

Keep-alive превращает жизненный цикл соединения в **цикл**, а не в линию.
`ConnectionState` у `php-mini-http-server` рисует его так:

```text
NEW → CONNECTED → READING → PROCESSING → WRITING → READING → CLOSED
                                                     ↑        │
                                                     └────────┘
```

Переход `WRITING → READING` — и есть keep-alive: закончив писать один ответ,
соединение не закрывается, а возвращается в чтение следующего запроса. Стрелка
назад — единственная, что отличает сервер с keep-alive от сервера с
`Connection: close`, и она же создаёт все состояния простоя, которых раньше не
существовало.

`php-mini-search` описывает тот же цикл через три предиката на парсере, и они
буквально и есть вопросы про фазу:

| вопрос                            | метод                                 | смысл                          |
| --------------------------------- | ------------------------------------- | ------------------------------ |
| байты заголовка пришли, но не все | `RequestParser::inHead()`             | часы заголовков идут           |
| заголовки кончились, тело нет     | `RequestParser::inBody()`             | часы тела идут                 |
| запрос собран и лежит в буфере    | `RequestParser::hasBufferedRequest()` | читать нечего — надо обслужить |

### Двое часов против четырёх

Различие между проектами по этой главе — не в строгости, а в **количестве
часов**.

`php-mini-http-server` держит двое:

- **часы простоя** — `lastActivityAt`, обновляются при каждом чтении и каждой
  записи. Их чтение — периодический проход `closeIdleConnections()`;
- **часы заголовков** — `waitingForHeadersSince`, ставятся, когда в буфере есть
  байты, но полного запроса ещё нет, и снимаются, когда запрос разобран. Проход
  `closeSlowHeaderReads()`.

Отдельных часов на тело у него нет. Тело охраняет всё тот же общий таймаут
соединения: пока байты идут, `lastActivityAt` свежий, и медленное тело живёт
столько, сколько шлёт байты. Это согласованный выбор, а не пропуск, — и именно
поэтому третья ситуация из списка выше у `php-mini-http-server` не отличается от
первой, а у `php-mini-search` отличается.

`php-mini-search` держит четверо, по одному на фазу:

| часы      | поле             | стартуют                                  | `ServerConfig`        |
| --------- | ---------------- | ----------------------------------------- | --------------------- |
| заголовки | `headStartedAt`  | парсер в `inHead()`                       | `headTimeout = 10.0`  |
| тело      | `bodyStartedAt`  | парсер в `inBody()`                       | `bodyTimeout = 30.0`  |
| простой   | `idleSince`      | при `accept()` и когда ответ ушёл целиком | `idleTimeout = 30.0`  |
| запись    | `writeStartedAt` | на первом байте ответа в буфер            | `writeTimeout = 30.0` |

Ключевое здесь — что часы не «тикают». Они хранят **момент**, а решение
принимается сравнением с текущим временем; «сколько осталось» никуда не пишется.
Нет накапливаемого состояния, которое можно забыть или потерять: есть только
момент и текущее время, и `tick()` пересчитывает срок для каждого соединения
заново.

Из этого следует второй, менее очевидный вопрос: **как долго цикл имеет право
ждать в `select()`?** Если ждать секунду, а у соединения дедлайн через 10 мс,
оно переживёт свой таймаут почти на секунду. `php-mini-search` отвечает на это
честно: минимальный из сроков **и есть** таймаут ожидания. Не таймер, не
периодический проход — само ожидание укорачивается под ближайший дедлайн.
Проверить это можно одной функцией: `untilNextDeadline()` возвращает ровно то,
что подставляется в `stream_select()`.

`php-mini-http-server` выбрал противоположное: проходы по таймерам раз в
секунду. Плата — точность около секунды (об этом прямо сказано в
`docs/FAILURE-MODEL.md`: «reaped on a 1s tick, so accurate to about a second»),
выигрыш — простота: список дедлайнов не нужно сканировать на каждом проходе.
Отдельная тонкость в том, что оба прохода запускаются **одним** таймером, и это
ответ на вопрос, откуда вообще берётся работа, которую не породил ни один сокет.

### Пауза, а не наказание

Обратное давление — самое интересное решение главы, потому что оно формулируется
как **отказ от действия**.

Клиент, не читающий ответ, отличается от медленного клиента тем, что сервер
производит данные быстрее, чем сокет их отдаёт. Наивное решение — продолжать
читать и копить: клиент присылает ещё запросы, сервер считает ещё ответы, буфер
ответов растёт. Именно это и есть атака, только на память вместо соединений.
`php-mini-http-server` не делает этого. Как только объём непереданных ответов
превысил потолок (`MAX_BUFFERED_RESPONSE_BYTES = 65536`), сервер **снимает
соединение с наблюдения за чтением** и ждёт, пока буфер уйдёт. Он не закрывает
соединение и не теряет ни одного ответа.

Формально: клиент шлёт новые запросы, а сервер делает вид, что не видит их. Это
не «отказ», а перенос стоимости обратно на клиента. Пока клиент не читает, новые
его байты стоят в TCP-окне _на его стороне_; как только он прочитает, сокет
опустеет, сервер допишет остаток и снова начнёт слушать.

Здесь кроется ошибка, которую легко не заметить и которая в проекте имеет
отдельный тест. Пока сервер читал, в буфере на чтение могли уже лежать целые
запросы — они пришли одним пакетом вместе с теми, что вызвали паузу. Эти запросы
**никогда не разбудят цикл новым сетевым событием**, потому что новых байтов не
будет. Их надо обслужить явно в момент возобновления. `php-mini-http-server`
делает это внутри колбэка, который выполняется по завершении слива.

У `php-mini-search` обратное давление решено зеркально — не через потолок на
буфер, а через **предикат готовности**. Соединение читается только тогда, когда
есть куда девать байты:

```text
wantsRead()  =  выходной буфер пуст
             &&  соединение не помечено «закрыть после записи»
             &&  в буфере парсера нет целого необработанного запроса
```

Три условия — три причины не читать. Первая: ответ ещё пишется, а следующий
запрос обязан ждать его (pipelining последовательный). Вторая: ответом на этот
запрос будет `close`, новые читать некуда. Третья: запрос уже здесь — пока он не
обслужен, чтение лишь скопирует байты из сокета в буфер парсера, из которого их
никто не заберёт в этом проходе.

Второе и третье условия — это не про медленного клиента, а про то, что сервер
**не должен спрашивать о готовности то, что не готов обслужить**. Уровневый
`select()` задаст вопрос на следующем проходе снова; до тех пор соединение из
набора просто исключено.

### Потолок соединений

Есть предел и у самой паузы. Обратное давление ограничивает, сколько сервер
накопит **для одного** соединения; против тысяч соединений оно не работает,
потому что каждое ведёт себя идеально и просто занимает память. Спасает только
жёсткий потолок.

Оба проекта его ставят, и оба объясняют выбор механикой `stream_select()`:
дескриптор с номером `>= FD_SETSIZE` (1024 в стандартной сборке PHP) заставляет
`select()` отказать **целиком**, а не пропустить один поток. Цикл, который не
может ждать, не обслуживает никого. Поэтому потолок ставится заметно ниже стены:
512 у `php-mini-http-server`, 256 у `php-mini-search`.

Дальше проекты расходятся в том, **что делать у потолка**, и это расхождение
учебное:

- `php-mini-http-server` соединение **принимает и тут же закрывает**, а отказ
  считает в `refused_connections` (`GET /metrics`). Клиент узнаёт сразу;
- `php-mini-search` **не вызывает `accept()` вовсе**, пока потолок занят, и
  новые клиенты ждут в очереди ядра (backlog). Клиент видит медленный сервер, а
  не отказ.

Второй вариант дешевле (не отдаёт дескриптор ради того, чтобы его закрыть), но
хуже диагностируется. Первый честнее: клиент, которому отказано, не путает отказ
с медлительностью.

### Пустое чтение — не обрыв, а срыв ожидания — не «всё готово»

Две ловушки `stream_select()` в лаборатории обе задокументированы, и обе стоили
проектам реальных ошибок.

**Первая.** Готовность на чтение не гарантирует байтов. На неблокирующем сокете
`fread()` может вернуть `''`, и это значит «прямо сейчас читать нечего», а не
«клиент ушёл». Отличить одно от другого умеет только `feof()`. Пока
`ConnectionHandler` этого не делал, каждая пустая готовность закрывала живое
соединение. `docs/DECISIONS.md` формулирует это коротко: у блокирующего сокета
равенство `fread() === ''` и обрыв совпадают; у неблокирующего — нет.

Заодно находится и родственная ошибка, которую глава 13 предугадала как правило,
а здесь она становится часовым механизмом: **`''` от `fread()` — это ещё и
half-close**. Клиент вида `printf 'GET …' | nc` закрывает свою сторону записи
сразу после запроса и ждёт ответ. Обрыв на чтении поэтому не то же самое, что
обрыв на запись: уже посчитанный ответ **надо отдать**. `ConnectionHandler`
различает это по тому, пуст ли буфер ответов: пуст — закрыть, не пуст —
до-писать и только потом закрыть.

**Вторая.** `stream_select()` умеет вернуть `false`, и это не ошибка, а
прерванный сигналом вызов (`EINTR`) — ровно то, как выглядит изнутри `SIGTERM`
(→ глава 7). Массивы при этом остаются **в том виде, в каком их передали**, и
наивное чтение этого результата — «все наблюдаемые потоки готовы». Дальше каждый
простаивающий клиент получает чтение, которое возвращает `''`, а пустое чтение —
это обрыв; сервер закрывает всех тихих клиентов в момент получения сигнала.
Поэтому `false` возвращается как «ничего не готово», и цикл ждёт снова.

## 3. Код

### Чтение: пустое чтение и half-close

```php
private function handleReadable(mixed $stream): void
{
    $data = fread($stream, 8192);

    // On a non-blocking socket an empty read only means "nothing to
    // read right now" — a readiness that came to nothing. Only feof()
    // tells that apart from the peer having hung up.
    if ($data === '' && !feof($stream)) {
        return;
    }

    if ($data === false || $data === '') {
        // EOF: the client has stopped sending, which is not the same as
        // having stopped listening — `printf 'GET …' | nc` half-closes
        // right after its request and then waits for the answer. So
        // whatever is still queued is written out first.
        if ($this->connection->writeBuffer()->isEmpty()) {
            $this->close();
        } else {
            $this->closeAfterFlush($stream);
        }

        return;
    }

    $this->connection->appendRead($data);
    $this->metrics->recordBytesRead(strlen($data));

    $this->serviceRequests($stream);
}
```

[`src/Server/ConnectionHandler.php#L112-L141`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Server/ConnectionHandler.php#L112-L141)

**Перевод комментариев:**
- `// On a non-blocking socket an empty read only means "nothing to...` — на
  неблокирующем сокете пустое чтение означает лишь «сейчас читать нечего» —
  готовность, которая ни к чему не привела. Только `feof()` отличает это от
  того, что peer повесил трубку.
- `// EOF: the client has stopped sending, which is not the same as...` — EOF:
  клиент перестал отправлять, и это не то же самое, что перестал слушать —
  `printf 'GET …' | nc` делает half-close сразу после запроса и затем ждёт
  ответа. Поэтому всё, что ещё стоит в очереди, сначала дописывается.

Три исхода — `''` без `feof` (ничего не делать), `''` с `feof` (закрыться или
до-писать), байты (накопить и обслужить). Порядок проверок здесь несущий:
`feof()` спрашивают **только** у пустого чтения, потому что у непустого вопроса
уже нет — байты есть, значит соединение живо.

### Обратное давление: порог и возобновление

Цикл обслуживания запросов из одного буфера завершается именно здесь: после
каждого обслуженного запроса проверяется, не вырос ли выходной буфер выше
потолка.

```php
        // Phase 18 backpressure: past the ceiling we stop pulling more
        // requests off the socket until the buffer drains, then resume.
        if ($this->connection->hasBufferedMoreThan($this->maxBufferedResponseBytes)) {
            $this->pauseReadsUntilDrained($stream);

            return;
        }

        $queued = true;
    }

    // Phase 14+15: once the queued responses are fully written a
    // keep-alive connection goes back to READING.
    if ($queued) {
        $this->connection->startWriting();
        $this->drain($this->connection->backToReading(...));
    }
}
```

[`src/Server/ConnectionHandler.php#L205-L222`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Server/ConnectionHandler.php#L205-L222)

**Перевод комментариев:**
- `// Phase 18 backpressure: past the ceiling we stop pulling more` — фаза 18,
  обратное давление: за потолком перестаём вытягивать новые запросы из сокета,
  пока буфер не сольётся, затем возобновляем.
- `// Phase 14+15: once the queued responses are fully written a` — фазы 14+15:
  как только очередь ответов полностью записана, keep-alive-соединение
  возвращается в `READING`.

`return` в середине цикла — не ошибка. Он выходит из метода, оставляя в буфере
чтения необслуженный хвост; следующий проход цикла доберёт его по возобновлении.
Если бы проверки не было, цикл дошёл бы до конца буфера: клиент мог бы
подставить ещё запросов, и каждый породил бы ещё ответы, — а толку от них
никакого, пока сокет забит.

### Пауза и явный добор оставшихся запросов

```php
private function pauseReadsUntilDrained(mixed $stream): void
{
    $this->logger->log(sprintf(
        '#%d write buffer at %d bytes — pausing reads',
        $this->connection->id,
        $this->connection->writeBufferLength(),
    ));

    $this->connection->startWriting();
    $this->loop->removeReadable($stream);

    $this->drain(function () use ($stream): void {
        $this->connection->backToReading();
        $this->loop->onReadable($stream, $this->handleReadable(...));
        $this->logger->log(sprintf('#%d write buffer drained — resuming reads', $this->connection->id));

        // Requests already sitting in the buffer when the pause hit
        // would never be woken by new network bytes — serve them now.
        if (!$this->connection->readBuffer()->isEmpty()) {
            $this->serviceRequests($stream);
        }
    });
}
```

[`src/Server/ConnectionHandler.php#L344-L366`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Server/ConnectionHandler.php#L344-L366)

**Перевод комментариев:**
- `// Requests already sitting in the buffer when the pause hit` — запросы, уже
  лежавшие в буфере к моменту паузы, никогда не разбудило бы новыми сетевыми
  байтами — обслужить их сейчас.

`removeReadable()` и `onReadable()` — это и есть «решение о том, кого
перечислять». Пауза не блокирует и не закрывает: она вычёркивает соединение из
одного набора и добавляет в другой. Две строки лога — единственный внешний след
паузы, и по ним видно, что запросы, стоявшие в буфере (последний `if`),
обслужатся **без** нового байта из сети.

### Двое часов: что делает проход по таймаутам

```php
/**
 * Close every connection that has been idle longer than $idleSeconds.
 *
 * The periodic connection sweep (Phase 17) calls this on a timer, so a
 * client that connects and sends nothing — or goes quiet mid-request —
 * is eventually reclaimed instead of holding a socket forever.
 *
 * @return list<Connection> the connections that were closed
 */
public function closeIdleConnections(float $idleSeconds): array
{
    $now = $this->clock->now();

    return $this->closeWhere(static fn (Connection $c): bool => $now - $c->lastActivityAt() > $idleSeconds);
}

/**
 * Close every connection whose request headers have been arriving too
 * slowly to ever complete — the Slowloris guard.
 *
 * A trickling client stays alive under the idle sweep (it sends bytes),
 * but it never finishes a header block; this sweep bounds how long a
 * connection may sit mid-header.
 *
 * @return list<Connection> the connections that were closed
 */
public function closeSlowHeaderReads(float $timeoutSeconds): array
{
    $now = $this->clock->now();

    return $this->closeWhere(static function (Connection $c) use ($now, $timeoutSeconds): bool {
        $since = $c->waitingForHeadersSince();

        return $since > 0.0 && $now - $since > $timeoutSeconds;
    });
}
```

[`src/Server/Server.php#L122-L157`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Server/Server.php#L122-L157)

**Перевод комментариев:**
- `closeIdleConnections()` (PHPDoc): закрывает каждое соединение, простаивавшее
  дольше `$idleSeconds`. Периодический проход по соединениям (фаза 17) вызывает
  его по таймеру, поэтому клиент, который подключился и ничего не шлёт — или
  замолчал посреди запроса — рано или поздно освобождается, а не держит сокет
  вечно. `@return` — соединения, которые были закрыты.
- `closeSlowHeaderReads()` (PHPDoc): закрывает каждое соединение, чьи заголовки
  запроса приходят слишком медленно и никогда не завершатся, — защита от
  Slowloris. Клиент, который подсыпает байты понемногу, выживает под проходом
  простоя (он шлёт байты), но никогда не заканчивает блок заголовков; этот
  проход ограничивает, сколько соединение может сидеть посреди заголовков.
  `@return` — закрытые соединения.

Два предиката различаются ровно тем, **какую метку времени** они сравнивают.
Первый смотрит на последнюю активность, второй — на начало ожидания заголовков.
Их расхождение и есть разница между тихим клиентом и трюкачом: у трюкача первая
метка всегда свежая, а вторая — нет.

### Откуда берётся работа без сокета

```php
/**
 * Phase 16+17: scheduled work alongside read/write events. The heartbeat
 * tick keeps the loop awake, and the periodic sweep closes connections that
 * have been idle past the configured timeout, so dead clients do not hold a
 * socket forever.
 */
$loop->every(2.0, static function () use ($server, $logger): void {
    $logger->log(sprintf('tick: %d active connection(s)', $server->connectionCount()));
});

$loop->every(1.0, static function () use ($server, $logger): void {
    foreach ($server->closeIdleConnections($server->config()->connectionTimeout) as $connection) {
        $logger->log(sprintf('#%d closed: idle timeout', $connection->id));
    }

    // Slowloris guard: connections stuck mid-header past the header timeout
    // are reaped even though they keep dribbling bytes.
    foreach ($server->closeSlowHeaderReads($server->config()->headerTimeout) as $connection) {
        $logger->log(sprintf('#%d closed: header timeout', $connection->id));
    }
});
```

[`bin/server.php#L151-L171`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/bin/server.php#L151-L171)

**Перевод комментариев:**
- `/** Phase 16+17: scheduled work alongside read/write events...` — фазы 16+17:
  запланированная работа рядом с событиями чтения и записи. Тик heartbeat не
  даёт циклу спать, а периодический проход закрывает соединения, простаивавшие
  дольше настроенного таймаута, чтобы мёртвые клиенты не держали сокет вечно.
- `// Slowloris guard: connections stuck mid-header past the header timeout` —
  защита от Slowloris: соединения, застрявшие посреди заголовков дольше таймаута
  заголовков, прибираются, даже если они продолжают подсыпать байты.

Два прохода — в одном обработчике, с интервалом в секунду. Поверх него есть
второй таймер, с интервалом в две секунды, который пишет только
`tick: N active connection(s)` — тот самый текст, который при чтении лога в
эксперименте ниже пришлось вырезать. Таймер цикла и есть причина, по которой
такие «медленные» вещи, как проход по всем соединениям, вообще возможны: цикл
событий ждёт не только на сокетах. Демонстрационный сервер (`bin/server.php`)
запускается со `connectionTimeout: 5.0` и `headerTimeout: 5.0` — это те самые
секунды, которые видны в эксперименте ниже.

### Часы сохраняются в соединении

```php
/**
 * The connection is now waiting for the rest of a request header block.
 *
 * The Slowloris guard keys off this instant: a client may keep the idle
 * sweep happy by dribbling bytes, but it cannot keep the header-read
 * deadline at bay forever.
 */
public function noteWaitingForHeaders(): void
{
    if ($this->waitingForHeadersSince === 0.0) {
        $this->waitingForHeadersSince = $this->clock->now();
    }
}

/**
 * A complete request was parsed — the header wait is over.
 */
public function doneWaitingForHeaders(): void
{
    $this->waitingForHeadersSince = 0.0;
}
```

[`src/Connection/Connection.php#L127-L147`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/Connection/Connection.php#L127-L147)

**Перевод комментариев:**
- `noteWaitingForHeaders()` (PHPDoc): соединение теперь ждёт остаток блока
  заголовков запроса. Защита от Slowloris привязана к этому моменту: клиент
  может радовать проход простоя, сочась байтами, но не может вечно отодвигать
  дедлайн чтения заголовков.
- `doneWaitingForHeaders()` (PHPDoc): полный запрос разобран — ожидание
  заголовков окончено.

`=== 0.0` — часть контракта, а не оптимизация: «начало ожидания» пишется один
раз и не сдвигается, сколько бы байтов ни пришло следом. Именно поэтому трюкач
не спасётся. `doneWaitingForHeaders()` — противоположный конец: запрос разобран,
часы обнулены, следующее ожидание начнёт отсчёт заново.

Отдельная история, записанная в `docs/DECISIONS.md`, относится к строке
`doneWaitingForHeaders()` напрямую. Одно время после обслуживания keep-alive
запроса обработчик разбирал уже пустой буфер, получал «полного запроса нет» и
**запускал часы заголовков**. Соединение, отдыхающее между запросами, выглядело
как не успевающее прислать заголовки: его закрывало через 5 с вместо 30, а
сервер в режиме draining переставал считать его отдыхающим. Починка проста и
записана как правило: **часы заголовков стартуют, только когда есть байты
запроса**.

### Четверо часов у `php-mini-search`

Часы — это поля конфигурации, и подписи к ним уже отвечают на вопрос «откуда до
куда»:

```php
public function __construct(
    public string $host = '127.0.0.1',
    public int $port = 8080,
    /** Beyond this, accept() is not called and new clients wait in the kernel's backlog. */
    public int $maxConnections = 256,
    /** Request line plus headers; more is 431. */
    public int $maxHeadBytes = 8_192,
    /** More is 413, decided from Content-Length before the body is read. */
    public int $maxBodyBytes = 262_144,
    /** From the first byte of a request to the end of its headers (Slowloris). */
    public float $headTimeout = 10.0,
    /** From the end of the headers to the end of the body. */
    public float $bodyTimeout = 30.0,
    /** An open keep-alive connection with nothing in flight. */
    public float $idleTimeout = 30.0,
    /** From the first byte of a response to the last; a client that stops reading is dropped. */
    public float $writeTimeout = 30.0,
    public int $readChunkBytes = 65_536,
) {
}
```

[`src/Http/ServerConfig.php#L15-L34`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/ServerConfig.php#L15-L34)

**Перевод комментариев:**
- `$maxConnections` (PHPDoc): за этим пределом `accept()` не вызывается, и новые
  клиенты ждут в backlog ядра.
- `$maxHeadBytes` (PHPDoc): строка запроса плюс заголовки; больше — `431`.
- `$maxBodyBytes` (PHPDoc): больше — `413`, решается по `Content-Length` до
  чтения тела.
- `$headTimeout` (PHPDoc): от первого байта запроса до конца его заголовков
  (Slowloris).
- `$bodyTimeout` (PHPDoc): от конца заголовков до конца тела.
- `$idleTimeout` (PHPDoc): открытое keep-alive-соединение, по которому ничего не
  в полёте.
- `$writeTimeout` (PHPDoc): от первого байта ответа до последнего; клиент,
  который перестал читать, отбрасывается.

Первая строка — не таймаут: потолок соединений. Второй комментарий объясняет,
что именно происходит у потолка: `accept()` не вызывается, и клиенты ждут **в
очереди ядра**. Это тот же выбор, что и у `php-mini-http-server`, но сделан в
противоположную сторону (там — принять и закрыть).

### Готовность читать и готовность быть простаивающим

```php
/**
 * Read only when there is somewhere for the bytes to go: not while a
 * response is still being written (the next pipelined request waits for
 * it), and not while a whole request is already buffered.
 */
public function wantsRead(): bool
{
    return $this->output === '' && !$this->closeAfterWrite && !$this->parser->hasBufferedRequest();
}

public function isIdle(): bool
{
    return $this->output === '' && !$this->parser->inHead() && !$this->parser->inBody();
}
```

[`src/Http/Connection.php#L33-L46`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/Connection.php#L33-L46)

**Перевод комментариев:**
- `wantsRead()` (PHPDoc): читать, только когда байтам есть куда деться: не пока
  ответ ещё пишется (следующий pipelined-запрос ждёт его) и не пока в буфере уже
  лежит целый запрос.

`wantsRead()` — обратное давление как **предикат**: соединение не попадает в
набор на чтение, когда читать некуда. `isIdle()` — то же самое, но для часов
простоя: простаивает только то, у чего пусто на запись и нет незавершённой фазы
разбора. Обратите внимание, чего в `isIdle()` **нет**: отдельной проверки «в
буфере уже лежит целый запрос». Она и не нужна: пока `next()` ни разу не вызван
для нового запроса, поле `head` в парсере равно `null`, а буфер при этом непуст
— значит `inHead()` вернёт `true`, и соединение не станет простаивающим.
Готовый, но не обслуженный запрос выглядит для парсера как **начатые**
заголовки, а не как покой: часы простоя на него не идут, и читать его
`wantsRead()` и не велит.

### Один `tick()`: дедлайн как таймаут ожидания

```php
public function tick(float $maxWait = 1.0): void
{
    $now = ($this->clock)();
    $this->expire($now);

    $read = [];
    $write = [];
    if ($this->listener !== null && count($this->connections) < $this->config->maxConnections) {
        $read[] = $this->listener;
    }
    foreach ($this->connections as $connection) {
        if ($connection->wantsRead()) {
            $read[] = $connection->socket;
        }
        if ($connection->output !== '') {
            $write[] = $connection->socket;
        }
    }

    if ($read === [] && $write === []) {
        usleep((int) (min($maxWait, $this->untilNextDeadline($now)) * 1e6));

        return;
    }

    $wait = max(0.0, min($maxWait, $this->untilNextDeadline($now)));
    $except = null;
    // A signal interrupts select() and makes it return false; that is not
    // an error, the next tick simply looks again.
    if (@stream_select($read, $write, $except, (int) $wait, (int) (fmod($wait, 1.0) * 1e6)) === false) {
        return;
    }

    $now = ($this->clock)();
    foreach ($read as $socket) {
        if ($socket === $this->listener) {
            $this->accept($now);
        } elseif (isset($this->connections[(int) $socket])) {
            $this->read($this->connections[(int) $socket], $now);
        }
    }
    foreach ($write as $socket) {
        if (isset($this->connections[(int) $socket])) {
            $this->write($this->connections[(int) $socket], $now);
        }
    }
}
```

[`src/Http/Server.php#L80-L126`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/Server.php#L80-L126)

**Перевод комментариев:**
- `// A signal interrupts select() and makes it return false; that is not` —
  сигнал прерывает `select()` и заставляет его вернуть `false`; это не ошибка,
  следующий тик просто посмотрит снова.

Три решения в одном методе. Первое: слушающий сокет добавляется в набор **только
ниже потолка** — это и есть «у потолка `accept()` не вызывается». Второе: и
чтение, и запись собираются через `wantsRead()` и `output !== ''`, то есть
уровневое ожидание строится из состояния соединений, а не из прошлых событий.
Третье: `$wait` — это `min($maxWait, untilNextDeadline())`. Дедлайн не будит
цикл, он **укорачивает сон**: цикл просыпается сам ровно тогда, когда пора.

Отдельно — обработка `false` от `stream_select()`: не ошибка, а прерванный
сигналом вызов; следующий проход просто посмотрит снова. Это тот же случай,
который у `php-mini-http-server` требует отдельной проверки в цикле.

### Четыре дедлайна — четыре ветки одной функции

```php
private function deadline(Connection $connection): ?float
{
    if ($connection->writeStartedAt !== null) {
        return $connection->writeStartedAt + $this->config->writeTimeout;
    }
    if ($connection->parser->inBody() && $connection->bodyStartedAt !== null) {
        return $connection->bodyStartedAt + $this->config->bodyTimeout;
    }
    if ($connection->parser->inHead() && $connection->headStartedAt !== null) {
        return $connection->headStartedAt + $this->config->headTimeout;
    }
    if ($connection->isIdle()) {
        return $connection->idleSince + $this->config->idleTimeout;
    }

    return null;
}

private function untilNextDeadline(float $now): float
{
    $nearest = PHP_FLOAT_MAX;
    foreach ($this->connections as $connection) {
        $deadline = $this->deadline($connection);
        if ($deadline !== null) {
            $nearest = min($nearest, $deadline - $now);
        }
    }

    return max(0.0, $nearest);
}
```

[`src/Http/Server.php#L304-L333`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Http/Server.php#L304-L333)

Порядок ветвей — приоритет, а не случайность: запись важнее тела, тело важнее
заголовков, заголовки важнее простоя. Сортировка нужна именно потому, что поля
не исключают друг друга: пока пишется ответ на первый pipelined-запрос, буфер
парсера уже может содержать второй, и тогда `writeStartedAt` и `headStartedAt`
выставлены вместе. Первое подходящее условие выигрывает, `null` означает
«дедлайна нет». `untilNextDeadline()` возвращает `PHP_FLOAT_MAX`, если дедлайнов
нет вовсе; `min()` с `$maxWait` превращает это в обычное ожидание в секунду.
Возврат `max(0.0, …)` защищает от отрицательного времени ожидания: соединение
уже просрочено, `expire()` закроет его в начале следующего прохода.

### Срыв ожидания — не «все готовы»

```php
        // false means the wait was cut short — in practice a handled signal
        // (EINTR), which is exactly what a SIGTERM asking for a graceful
        // shutdown looks like from in here. The arrays are then left as
        // they were passed in, which reads identically to "every watched
        // stream is ready". Dispatching on that hands every idle
        // connection a read that returns nothing, and a read that returns
        // nothing is how a handler recognises a closed peer — so the
        // server would drop every quiet client the moment it was signalled.
        // Nothing is ready; the next pass waits again.
        if ($ready === false) {
            return [[], []];
        }
```

[`src/EventLoop/SelectLoop.php#L280-L291`](https://github.com/Researcher86/php-mini-http-server/blob/1f73404b193b6d7dda29f7d82bf97360d5b75e03/src/EventLoop/SelectLoop.php#L280-L291)

**Перевод комментариев:**
- `// false means the wait was cut short — in practice a handled signal` —
  `false` означает, что ожидание прервано — на практике обработанным сигналом
  (`EINTR`), а именно так выглядит изнутри `SIGTERM`, просящий корректного
  завершения. Массивы при этом остаются такими, какими их передали, а это
  читается одинаково с «все наблюдаемые потоки готовы». Диспетчеризация по
  такому результату выдаёт каждому простаивающему соединению чтение, которое
  ничего не возвращает, а ничего не вернувшее чтение — так обработчик распознаёт
  закрытого peer'а — поэтому сервер отбросил бы каждого тихого клиента в момент
  получения сигнала. Ничего не готово; следующий проход ждёт снова.

Это тот же принцип, что и «пустое чтение — не обрыв», только применённый к
самому `select()`. Комментарий прямо связывает обе ошибки: чтение, вернувшее
пусто, обработчик принимает за закрытие peer'а — а значит, `false` от `select()`
без этой проверки обернётся закрытием **всех** тихих соединений в момент прихода
сигнала.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-http-server
make build install
make run-server
```

Во втором терминале выполняются команды ниже. Вывод снят при написании главы.
Сервер запускается с `connectionTimeout: 5.0` и `headerTimeout: 5.0`, то есть
таймауты на нём — пятисекундные, и их можно наблюдать вживую.

### 1. Keep-alive: что стоит соединение

```bash
make example EXAMPLE=keep-alive
```

```text
First response says: Connection: keep-alive
Server sees 2 connection(s): this one, plus the /metrics probe.

20 requests over one kept-alive connection:    1.80 ms
20 requests, a new connection each:           2.42 ms

Keep-alive saved 0.61 ms — 25% — and it is all connection setup.
```

Скрипт проверяет обе половины утверждения: что сервер **действительно** отвечает
`Connection: keep-alive` и что соединение остаётся открытым (сервер видит его
одним активным соединением), а затем — что переиспользование соединения ощутимо.
Разница в 25% на двадцати запросах мала в абсолюте (0.61 мс) — и это ровно та
цена, которую глава 10 называла «установка соединения».

Абсолютные числа здесь плавают заметно: на семи прогонах, сделанных при
написании главы, переиспользование экономило от 10% до 27%, а сами величины
колебались в полтора раза. Постоянен **знак** — на всех семи прогонах keep-alive
оказался быстрее. Поэтому за величиной этой разницы смотреть нельзя; за
величиной смотрят в пункте 2, где на результатах лежат две тысячи запросов, а не
двадцать.

### 2. Тот же вопрос над двумя тысячами запросов

Двадцать запросов — слишком мало, чтобы отделить установку от шума. Проект
держит для этого отдельный скрипт, который поднимает **свой** сервер (без
логирования каждого запроса, иначе измерялся бы логгер) и прогоняет одинаковую
нагрузку двумя способами:

```bash
docker compose exec php php benchmarks/connection-reuse.php
```

```text
mode                   requests     requests/sec   mean ms    p95 ms     p99 ms
keep-alive             2000         15331          0.065      0.078      0.108
connection: close      2000         10245          0.097      0.119      0.154
```

На двух тысячах запросов переиспользование даёт **15331 против 10245
запросов/с**, то есть примерно на 50% больше пропускной способности. Важнее
среднего — хвост: p99 растёт с 0.108 до 0.154 мс, то есть на 43%. Это ровно то,
что предсказывает сам бенчмарк:

> The difference is accept, socket setup and teardown — and it shows up most in
> the tail, because that is the part that occasionally waits.

Разница — это `accept()`, установка сокета и его закрытие, и заметнее всего она
там, где иногда приходится подождать.

### 3. Медленный клиент: пауза вместо наказания

Скрипт `examples/slow-client.php` соединяет две вещи главы: pipelining из главы
11 и обратное давление. Один клиент отправляет **восемь** запросов `/big` (по 32
КиБ тело) одним пакетом и **не читает ни одного ответа**. Параллельно второй,
обычный клиент десять раз просит `/hello`, и каждое обращение замеряется.

```bash
make example EXAMPLE=slow-client
```

```text
[slow] pipelined 8 requests for a 32 KiB body, and will not read a single reply

[healthy] request  1 → 200 in  0.748 ms
[healthy] request  2 → 200 in  0.449 ms
[healthy] request  3 → 200 in  0.977 ms
[healthy] request  4 → 200 in  1.128 ms
[healthy] request  5 → 200 in  0.926 ms
[healthy] request  6 → 200 in  0.953 ms
[healthy] request  7 → 200 in  1.834 ms
[healthy] request  8 → 200 in  0.349 ms
[healthy] request  9 → 200 in  0.313 ms
[healthy] request 10 → 200 in  0.268 ms

[healthy] worst request: 1.834 ms — the slow connection cost it nothing
Server still up, 3 connection(s) open, loop max lag 1.972 ms

Now draining the slow client, to show nothing was lost:
  read 8 of 8 responses back, in order, none dropped
```

Два числа здесь важнее остальных. **Восемь из восьми** ответов вернулись — по
порядку и без единой потери, хотя сервер на время перестал читать от этого
клиента. И **худший запрос здорового клиента** занял 1.834 мс, то есть медленный
сосед не стоил ему ничего.

Что при этом происходило на сервере, видно в его логе (`make run-server`). Ниже
приведены строки целиком, кроме периодических `tick: N active connection(s)` —
они идут каждые две секунды и к делу не относятся:

```text
[2026-10-16 11:39:27] #23 connected from 127.0.0.1:43622
[2026-10-16 11:39:27] #24 connected from 127.0.0.1:43626
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] #23 write buffer at 65800 bytes — pausing reads
[2026-10-16 11:39:27] GET /hello → start
[2026-10-16 11:39:27] GET /hello → 200
[2026-10-16 11:39:27] #23 write buffer drained — resuming reads
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] #23 write buffer at 65800 bytes — pausing reads
[2026-10-16 11:39:27] #23 write buffer drained — resuming reads
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] #23 write buffer at 65800 bytes — pausing reads
[2026-10-16 11:39:27] #23 write buffer drained — resuming reads
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] GET /big → start
[2026-10-16 11:39:27] GET /big → 200
[2026-10-16 11:39:27] #23 write buffer at 65800 bytes — pausing reads
[2026-10-16 11:39:27] #23 write buffer drained — resuming reads
```

Здесь видно сразу обе половины механизма. Во-первых, **четыре цикла
«пауза/возобновление»** на одном соединении `#23`. Каждый раз буфер достигал
65800 байт — это два ответа по 32 КиБ плюс заголовки, и это **больше** потолка
в 65536. Порог сработал ровно там, где объявлен. После каждой паузы соединение
возвращалось в чтение, докапывало оставшиеся в буфере запросы (тот самый явный
`serviceRequests()` после слива) и снова упиралось в потолок. Восемь запросов,
поданных одним пакетом, потребовали четырёх циклов — по два ответа в каждом.

Во-вторых, между строками `#23` видна работа здорового клиента: первая пара
`GET /hello → start`/`GET /hello → 200` стоит **между** `pausing reads` и
`resuming reads`. Пауза не остановила цикл: она сняла одно соединение с
наблюдения за чтением, а не приостановила обработку.

### 4. Двое часов против трюкача

Три клиента подключаются одновременно, и ни один из них никогда не присылает
полного запроса:

- **silent** — подключается и молчит;
- **trickling** — раз в 1.2 с посылает **один байт** строки запроса;
- **at rest** — один раз присылает целый запрос и потом молчит.

Первый и третий должны попасть под проход по простою: они перестали двигаться,
их `lastActivityAt` стареет. Второй — под проход по заголовкам: каждый его байт
освежает `lastActivityAt`, но не сдвигает `waitingForHeadersSince`.

Скрипт `/tmp/two-clocks.php` кладётся в контейнер и запускается оттуда:

```bash
docker compose cp /tmp/two-clocks.php php:/tmp/two-clocks.php
docker compose exec php php /tmp/two-clocks.php
```

```php
<?php

declare(strict_types=1);

/**
 * One question: what actually stops a client that refuses to finish?
 *
 * Three clients connect at the same instant, and not one of them ever sends
 * a complete request:
 *
 *   silent     connects and says nothing at all
 *   trickling  dribbles one byte of a request line every 1.2 s, for ever
 *   at rest    completes one request, then goes quiet
 *
 * The first and the third are caught by the idle sweep: they stop moving, so
 * their last-activity clock runs out.
 *
 * The second is the interesting one. Every byte it sends refreshes that same
 * clock, so a server with a single idle timeout would keep it for ever — that
 * is the whole Slowloris attack. It is caught by the second clock instead:
 * the one that measures how long a connection has been sitting in the middle
 * of a header block, and that no amount of trickling can push back.
 *
 * The demo server runs with connectionTimeout = headerTimeout = 5.0, so all
 * three should die around the same moment for two different reasons, and its
 * log says which one caught each.
 */

const HOST = '127.0.0.1';
const PORT = 8080;

/** A request line and a Host, sent one byte at a time and never finished. */
const TRICKLE = "GET / HTTP/1.1\r\nHost: x\r\n";

function connect(): mixed
{
    $socket = @stream_socket_client('tcp://' . HOST . ':' . PORT, $errno, $errstr, 3.0);

    if ($socket === false) {
        fwrite(STDERR, 'Cannot reach ' . HOST . ':' . PORT . ": {$errstr}\n");

        exit(1);
    }

    stream_set_blocking($socket, false);

    return $socket;
}

$startedAt = hrtime(true);
$elapsed = fn (): float => (hrtime(true) - $startedAt) / 1e9;

$clients = ['silent' => connect(), 'trickling' => connect(), 'at rest' => connect()];
$sentBytes = ['silent' => 0, 'trickling' => 0, 'at rest' => strlen("GET /hello HTTP/1.1\r\nHost: " . HOST . "\r\n\r\n")];

// The third client behaves normally exactly once, then stops.
fwrite($clients['at rest'], "GET /hello HTTP/1.1\r\nHost: " . HOST . "\r\n\r\n");

echo "Three clients connected. None of them will ever finish a request.\n";
echo "The demo server reaps on a 1-second tick, so timings come in whole seconds.\n\n";

$closed = [];
$position = 0;
$nextDribbleAt = 0.0;

while (count($closed) < count($clients)) {
    $now = $elapsed();

    // Dribble one byte at a time, for as long as this connection lives. Every
    // byte moves the idle clock, which is exactly why the idle clock is not
    // the one that ends it.
    if ($now >= $nextDribbleAt && !isset($closed['trickling'])) {
        fwrite($clients['trickling'], TRICKLE[$position % strlen(TRICKLE)]);
        $sentBytes['trickling'] = ++$position;
        $nextDribbleAt = $now + 1.2;
    }

    $read = [];

    foreach ($clients as $name => $socket) {
        if (!isset($closed[$name])) {
            $read[] = $socket;
        }
    }

    if ($read === []) {
        break;
    }

    $write = [];
    $except = [];

    if (stream_select($read, $write, $except, 0, 200_000) === 0) {
        continue;
    }

    foreach ($read as $socket) {
        $name = array_search($socket, $clients, true);

        // Whatever the server had already written is drained first; the
        // closure itself shows up as an empty read with feof() true.
        fread($socket, 65_536);

        if (feof($socket)) {
            $closed[$name] = true;

            printf(
                "%-10s closed after %5.2f s, having sent %2d byte(s)\n",
                $name,
                $elapsed(),
                $sentBytes[$name],
            );
        }
    }
}

printf(
    "\nThe trickling client moved %d times and was never quiet, yet it lasted no\n"
    . "longer than the one that never spoke. Watch the server's log: it names\n"
    . "the clock that caught each of them.\n",
    $position,
);
```

**Перевод комментариев:**
- `/** One question: what actually stops a client that refuses to finish?...` —
  вопрос один: что на самом деле останавливает клиента, который отказывается
  заканчивать? Три клиента подключаются в один момент, и ни один из них никогда
  не присылает полный запрос: `silent` — подключается и ничего не говорит;
  `trickling` — подсыпает по одному байту строки запроса каждые 1.2 с, вечно;
  `at rest` — завершает один запрос, затем затихает. Первого и третьего ловит
  проход простоя: они перестают двигаться, и их часы последней активности
  истекают. Второй — интересный: каждый его байт освежает те же часы, поэтому
  сервер с одним только таймаутом простоя держал бы его вечно — в этом и состоит
  атака Slowloris. Его ловят вторые часы: те, что измеряют, сколько соединение
  сидит посреди блока заголовков и которые не отодвинуть никаким подсыпанием
  байтов. Демонстрационный работает с `connectionTimeout = headerTimeout = 5.0`,
  поэтому все трое должны умереть примерно в один момент по двум разным
  причинам, и его лог говорит, какие часы поймали каждого.
- `TRICKLE` (PHPDoc): строка запроса и `Host`, отправленные по одному байту и
  так и не законченные.
- `// The third client behaves normally exactly once, then stops.` — третий
  клиент ведёт себя нормально ровно один раз, потом останавливается.
- `// Dribble one byte at a time, for as long as this connection lives. Every` —
  подсыпать по одному байту, пока живо это соединение. Каждый байт двигает часы
  простоя, и именно поэтому не они его заканчивают.
- `// Whatever the server had already written is drained first; the` — всё, что
  сервер уже написал, сначала вычитывается; само закрытие проявляется как пустое
  чтение с `feof() === true`.

```text
Three clients connected. None of them will ever finish a request.
The demo server reaps on a 1-second tick, so timings come in whole seconds.

silent     closed after  5.07 s, having sent  0 byte(s)
at rest    closed after  5.07 s, having sent 40 byte(s)
trickling  closed after  5.07 s, having sent  5 byte(s)

The trickling client moved 5 times and was never quiet, yet it lasted no
longer than the one that never spoke. Watch the server's log: it names
the clock that caught each of them.
```

Все три закрыты **в один момент** — 5.07 с от старта скрипта. Метки времени в
логе дают те же секунды: `11:42:18` — соединения, `11:42:23` — закрытие, то есть
**первый тик после** пятисекундного таймаута. Но закрыты они **двумя разными
часами**, и лог называет, какими (снова без `tick:`):

```text
[2026-10-16 11:42:18] #26 connected from 127.0.0.1:56256
[2026-10-16 11:42:18] #27 connected from 127.0.0.1:56268
[2026-10-16 11:42:18] #28 connected from 127.0.0.1:56280
[2026-10-16 11:42:18] GET /hello → start
[2026-10-16 11:42:18] GET /hello → 200
[2026-10-16 11:42:23] #26 closed: idle timeout
[2026-10-16 11:42:23] #28 closed: idle timeout
[2026-10-16 11:42:23] #27 closed: header timeout
```

`#26` и `#28` — это `silent` и `at rest`: их закрыл проход по простою. `#27` —
`trickling`, закрытый проходом по заголовкам. Порядок строк в одном и том же
тике соответствует порядку проходов в `bin/server.php`: сначала
`closeIdleConnections()` (два), потом `closeSlowHeaderReads()` (один). Заметьте:
трюкач успел послать **пять байтов**, и каждый из них обновил `lastActivityAt`.
Если бы у сервера были только часы простоя, он жил бы вечно — именно это и есть
Slowloris. Вторые часы сравнивают не «когда он последний раз что-то делал», а
«когда он начал эту попытку»; последнее трюкач не может сдвинуть.

Причина, по которой закрытие пришло не ровно в 5.00 с, — цена периодического
прохода: проект закрывает не в момент дедлайна, а на первом тике после него. В
`docs/FAILURE-MODEL.md` это названо прямо: «reaped on a 1s tick, so accurate to
about a second».

Тик привязан к началу работы сервера, а не к моменту подключения, и это свойство
таймера, а не совпадение: `docs/FAILURE-MODEL.md` называет его прямо — «A
periodic timer is anchored to its original schedule rather than to its last
firing, so a slow pass makes it late once instead of drifting further behind for
ever». Поэтому номер первого тика после дедлайна случайно попадает в секундное
окно. Запустив тот же скрипт повторно, второй прогон уложился в 5.38 с, и лог
повторил себя один в один — с другим номером секунды:

```text
[2026-10-16 11:58:53] #277 connected from 127.0.0.1:39150
[2026-10-16 11:58:53] #278 connected from 127.0.0.1:39154
[2026-10-16 11:58:53] #279 connected from 127.0.0.1:39170
[2026-10-16 11:58:53] GET /hello → start
[2026-10-16 11:58:53] GET /hello → 200
[2026-10-16 11:58:58] #277 closed: idle timeout
[2026-10-16 11:58:58] #279 closed: idle timeout
[2026-10-16 11:58:58] #278 closed: header timeout
```

Два прогона — 5.07 с и 5.38 с — оба легли внутри одного секундного окна после
дедлайна, а не в одной точке. И то, и другое сохраняется: в обоих закрыты трое,
в обоих порядок строк одинаков (два простоя в `closeIdleConnections()`, потом
один заголовок в `closeSlowHeaderReads()`), и в обоих `trickling` ловит другие
часы, чем два остальных.

## 5. Отказы

| Что ломается                                     | Чем обнаруживается                                              | Что происходит                                                                                                                      |
| ------------------------------------------------ | --------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- |
| Клиент подключился и молчит                      | `closeIdleConnections()`/`deadline()` → `isIdle()`            | закрытие по таймауту простоя                                                                                                        |
| Заголовки дописываются вечно (Slowloris)         | `closeSlowHeaderReads()`/`deadline()` → `inHead()`            | закрытие по таймауту заголовков                                                                                                     |
| Тело объявлено, но не приходит                   | `deadline()` → `inBody()`                                       | `php-mini-search`: закрытие по таймауту тела; `php-mini-http-server`: только общий таймаут соединения                               |
| Клиент не читает ответ                           | `hasBufferedMoreThan(65536)`/`output !== ''`                  | `php-mini-http-server` — пауза чтения и возобновление; `php-mini-search` — соединение не читается, пока `output` не пуст            |
| Ответы копятся на одном соединении               | потолок 64 КиБ                                                  | запросы из буфера обслуживаются **после** слива, без нового сетевого события                                                        |
| Клиент не читает и ответ не влезает в сокет      | `deadline()` → `writeStartedAt`                                 | `php-mini-search`: закрытие по таймауту записи                                                                                      |
| Пустое чтение, но соединение живо                | `$data === '' && !feof($stream)`                                | ничего не делается; соединение не закрывается                                                                                       |
| Half-close после запроса                         | `feof()` при пустом буфере ответов                              | ответ дописывается, потом закрытие                                                                                                  |
| `stream_select()` вернул `false` (EINTR)         | `$ready === false`                                              | ничего не готово; цикл ждёт снова                                                                                                   |
| Соединение с готовым, но не обслуженным запросом | третье условие `wantsRead()`; в `isIdle()` это ловит `inHead()` | не читается и не считается простаивающим, пока запрос не обслужен                                                                   |
| Дедлайна нет ни у одного соединения              | `untilNextDeadline()` → `PHP_FLOAT_MAX`                         | ожидание в `maxWait` (1 с), не бесконечное                                                                                          |
| Потолок соединений достигнут                     | `count($connections) >= maxConnections`                         | `php-mini-http-server`: принять и закрыть, `refused_connections`; `php-mini-search`: не вызывать `accept()`, клиенты ждут в backlog |
| Медленный обработчик                             | `loop_max_lag_ms` в `/metrics`                                  | таймауты срабатывают **позже** дедлайна: цикл не может вытеснить обработчик                                                         |

Последняя строка — общая для обоих проектов и самая неприятная. Все часы этой
главы устроены так, что решение принимается **между** обработчиками. Обработчик,
который спит 500 мс, сдвигает на 500 мс и срабатывание таймаута. Мера против
этого не механизм, а дисциплина, и оба проекта называют её одинаково: держите
обработчики короткими.

## 6. Как это проверено

`php-mini-http-server` — `tests/Server/ConnectionHandlerTest.php`. Обратное
давление: `testBackpressurePausesReadsThenResumesKeepingTheBatchIntact` (пауза
срабатывает, затем батч возвращается целиком и по порядку). Часы:
`testAKeepAliveConnectionBetweenRequestsIsAtRestNotMidHeader` — тот самый тест
из истории выше, где отдыхающее соединение не должно считаться «посреди
заголовков». Пустое чтение и half-close:
`testAnEmptyReadThatIsNotEofLeavesTheConnectionOpen`,
`testClientThatHalfClosesAfterItsRequestStillGetsTheResponse`. Прочее:
`testNothingIsServedAfterARequestThatAskedToClose`,
`testClientThatVanishesMidWriteClosesOnlyItsOwnConnection`,
`testMetricsCountRequestsAndBytesOfARealExchange`.

Завершение — `tests/Server/GracefulShutdownTest.php`: все шесть его тестов —
`testRestingConnectionsAreReapedAndTheLoopStopsOnItsOwn`,
`testAnInFlightRequestFinishesAndANewOneIsRefusedBeforeTheServerStops`,
`testASecondRequestForcesTheEnd`,
`testTheDrainDeadlineClosesConnectionsThatNeverFinish`,
`testASignalChangesNothingUntilTheLoopHandlesIt`,
`testTwoSignalsThatArriveBeforeTheLoopWakesStillForceTheEnd`. Тесты про паузу
требуют цикла, у которого обработчики чтения можно вызвать вручную — настоящий
`select()` никогда не сообщает о молчащем сокете, — и именно это даёт типизация
`ConnectionHandler` против интерфейса `EventLoop`, а не против `SelectLoop`.

Цикл и таймеры — `tests/EventLoop/`: `SelectLoopTest`, `TimerTest`
(периодический таймер привязан к исходному расписанию, а не к последнему
срабатыванию), `RoundTripTimeoutTest`, а также `ServerRoundTripTest` и два
интеграционных теста на частичное чтение и частичную запись. Буферы —
`tests/Connection/{ReadBufferTest,WriteBufferTest,ConnectionTest}`.

Запускаем:

```text
$ vendor/bin/phpunit tests/Server tests/EventLoop tests/Connection
OK (91 tests, 333 assertions)

$ vendor/bin/phpunit
OK (259 tests, 598 assertions)
```

`php-mini-search` — `tests/Http/ServerTest.php`. По одному тесту на каждые часы,
и названия — это буквально перечень того, что глава разобрала:

- `testASlowHeadIsDroppedAfterTheHeadTimeout` — трюкач;
- `testASlowBodyIsDroppedAfterTheBodyTimeout` — тело;
- `testAnIdleKeepAliveConnectionIsClosed` — простой;
- `testAClientThatStopsReadingIsDroppedAfterTheWriteTimeout` — запись.

Keep-alive и pipelining: `testKeepAliveServesSeveralRequestsOnOneConnection`,
`testPipelinedRequestsAreAnsweredInOrder`, `testConnectionCloseIsHonoured`,
`testHttp10ClosesUnlessAskedToKeepAlive`. Потолок:
`testBeyondTheConnectionCapNewClientsWait`.

Ключевая особенность этих тестов — таймауты проверяются **управляемыми часами**,
а не `sleep()`: `HttpHarness` строит `Server` с замыканием
`fn (): float => $this->now`, и тест просто пишет в `$harness->now` нужную
секунду. Поэтому весь набор `tests/Http` проходит меньше чем за секунду, хотя
проверяет четыре разных таймаута длительностью в десятки секунд.

Запускаем:

```text
$ vendor/bin/phpunit tests/Http
OK (50 tests, 324 assertions)

$ vendor/bin/phpunit
OK (180 tests, 47230 assertions)
```

Эксперименты выше — четыре живых прогона: микротайминги keep-alive, бенчмарк на
две тысячи запросов, восьмизапросный батч к не читающему клиенту и три клиента
против двух часов.

## 7. Цифры

Живые прогоны этой главы (Docker, PHP 8.5.11):

| Что                                                     | Значение                                                                        |
| ------------------------------------------------------- | ------------------------------------------------------------------------------- |
| 20 запросов: keep-alive против нового соединения каждый | 1.80 мс против 2.42 мс; экономия 0.61 мс (25%) — на семи прогонах от 10% до 27% |
| 2000 запросов, keep-alive                               | 15 331 запрос/с, среднее 0.065 мс, p95 0.078, p99 0.108                         |
| 2000 запросов, `Connection: close`                      | 10 245 запросов/с, среднее 0.097 мс, p95 0.119, p99 0.154                       |
| Медленный клиент: 8 запросов `/big`, ответы не читаются | 4 цикла «пауза/возобновление», каждый на 65 800 байт                            |
| Медленный клиент: ответы в итоге                        | 8 из 8, по порядку, ни один не потерян                                          |
| Здоровый клиент рядом с медленным                       | худший запрос 1.834 мс, максимальная задержка цикла 1.972 мс                    |
| Три клиента, `connectionTimeout = headerTimeout = 5.0`  | все закрыты в один момент; 5.07 с на первом прогоне, 5.38 с на втором           |
| Из них: `silent` (0 байт) и `at rest` (40 байт)         | `idle timeout`                                                                  |
| Из них: `trickling` (5 байт за 5 с)                     | `header timeout`                                                                |
| Тесты сервера/цикла/соединений `php-mini-http-server`   | 91 тест, 333 утверждения                                                        |
| Весь набор `php-mini-http-server`                       | 259 тестов, 598 утверждений                                                     |
| Тесты HTTP `php-mini-search`                            | 50 тестов, 324 утверждения                                                      |
| Весь набор `php-mini-search`                            | 180 тестов, 47 230 утверждений                                                  |

Отдельно — **документированные** числа `php-mini-search` из
`docs/BENCHMARKS.md`: двести медленных клиентов, каждый из которых шлёт байт раз
в полсекунды, обошлись сервису в 17% пропускной способности (8772 против 10 575
запросов/с) и **ни во что** по задержке: p99 63.4 мс против 66.3 мс на
синтетическом корпусе и 6.3 мс против 6.0 мс на Википедии. Проект формулирует
вывод как «медленные клиенты не стоят ничего измеримого» и объясняет: каждый
такой клиент будит цикл на один байт раз в полсекунды, а его заголовки упёрлись
бы в десятисекундный таймаут задолго до того, как сложатся в запрос.

Таймауты и потолки, задающие границы простоя:

|                              | `php-mini-http-server`                 | `php-mini-search`                        |
| ---------------------------- | -------------------------------------- | ---------------------------------------- |
| заголовки                    | `headerTimeout = 5.0`                  | `headTimeout = 10.0`                     |
| тело                         | отдельного нет                         | `bodyTimeout = 30.0`                     |
| простой соединения           | `connectionTimeout = 30.0` (демо: 5.0) | `idleTimeout = 30.0`                     |
| запись ответа                | отдельного нет                         | `writeTimeout = 30.0`                    |
| потолок непереданных ответов | `MAX_BUFFERED_RESPONSE_BYTES = 65536`  | нет отдельного поля: `wantsRead()`       |
| соединений                   | 512                                    | 256 (у потолка `accept()` не вызывается) |
| точность срабатывания        | тик 1 с                                | ровно дедлайн (ожидание укорачивается)   |

Значения `php-mini-http-server` — из `ServerConfig` и `ConnectionHandler`;
демонстрационный `bin/server.php` переопределяет `connectionTimeout` и
`headerTimeout` на 5.0 (и `drainTimeout` на 10). Значения `php-mini-search` — из
`ServerConfig` и таблицы `docs/ARCHITECTURE.md`.

Сравнение двух столбцов — это и есть содержание главы. Разница не в строгости
(числа одного порядка), а в **форме**: у `php-mini-http-server` ограничение
выражено проходами по списку соединений раз в секунду, у `php-mini-search` — той
же функцией `deadline()`, которая вызывается дважды за проход (один раз для
`expire()`, один раз для `untilNextDeadline()`) и потому не даёт циклу проспать
срок. Первое проще и требует лишь одного таймера; второе точнее и платит за это
линейным сканом на каждом проходе.

## 8. Упражнения

1. **Объясните.** `closeSlowHeaderReads()` требует `$since > 0.0`, а
   `noteWaitingForHeaders()` пишет время только если поле равно `0.0`. Что
   сломается, если убрать первую половину условия? Опишите конкретную
   последовательность запросов, после которой соединение закроется через
   `headerTimeout` вместо `connectionTimeout`.
2. **Найдите ошибку.** В `pauseReadsUntilDrained()` уберите последний `if` с
   `serviceRequests()`. Какой сценарий начнёт терять запросы — и почему их не
   спасёт следующий сетевой байт, ведь их сокет как раз вернули в набор на
   чтение?
3. **Сравните.** `php-mini-search` закрывает клиента по `writeTimeout`, если
   ответ не удаётся записать за 30 с; `php-mini-http-server` вместо этого паузит
   чтение. Придумайте клиента, которого первое поведение наказывает, а второе
   нет. Что при этом происходит с ответом в каждом из вариантов?
4. **Спроектируйте.** Тело у `php-mini-http-server` защищено только общим
   таймаутом соединения. Добавьте `bodyTimeout` по образцу `php-mini-search`:
   какие новые поля в `Connection`, где их ставить и снимать, нужен ли новый
   проход или достаточно расширить `closeSlowHeaderReads()`.
5. **Посчитайте.** `untilNextDeadline()` вызывает `deadline()` по разу на каждое
   соединение, и то же самое делает `expire()` — то есть два раза за проход. На
   256 простаивающих соединениях и секундном тике это 512 вызовов в секунду.
   Посчитайте полную стоимость за минуту и объясните, при каком числе соединений
   линейный пересчёт перестанет быть бесплатным — и почему откладывать его всё
   равно нельзя.
6. **Разберите отказ.** Обработчик, спящий 500 мс, сдвигает срабатывание любого
   таймаута на те же 500 мс. Опишите, как это наблюдаемо снаружи через
   `GET /metrics` (`loop_max_lag_ms`) — и почему увеличения числа обработчиков
   недостаточно, чтобы справиться с этим.
7. **Спроектируйте отказ.** У `php-mini-http-server` потолок — 512 соединений:
   512 × `MAX_BUFFERED_RESPONSE_BYTES` — это верхняя оценка памяти на буферы
   ответов. Посчитайте её и сравните с реальным RSS процесса на пустом сервере.
   Почему это сравнение даёт заведомо завышенное число, а не занижённое?

## Что дальше

- **Глава 4** — цикл событий и неблокирующий ввод-вывод: `stream_select()` и
  уровневые готовности, на которых стоит вся эта глава; `EINTR` — в главе 7.
- **Глава 7** — корректное завершение: почему `SIGTERM`, пришедший в середину
  `select()`, роняет всех тихих клиентов без проверки `false`, и как
  `drainTimeout` замыкает это на те же часы.
- **Глава 10** — соединения и буферы: во что обходится держать соединение
  открытым — дескриптор, объект и две записи в списках наблюдения.
- **Глава 11** — протокол и pipelining: откуда взялся батч из восьми запросов и
  почему ответы обязаны идти в порядке запросов — а значит, и почему читать
  дальше можно только после того, как дописан предыдущий.
- **Глава 15** — страницы, записи и B-деревья: `php-mini-database` начинает
  Часть IV, где предметом становится уже не сокет, а страница на диске.
