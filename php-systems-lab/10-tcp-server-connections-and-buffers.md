# Глава 10. TCP-сервер на одном цикле: соединения и буферы

> Проект: [`php-mini-cache`](https://github.com/Researcher86/php-mini-cache),
> релиз `v1.1.0`, коммит `5838655`. Нужно знать: цикл событий и неблокирующий
> ввод-вывод (→ глава 4). Пригодится дальше: протокол и pipelining (→ глава 11),
> TTL, Pub/Sub и снапшоты (→ глава 12).

В главе 4 мы разобрали цикл событий как идею: один поток ждёт готовности
множества дескрипторов и обслуживает только те, что готовы. Там это была
абстракция на маленьких примерах. Здесь она превращается в работающий
TCP-сервер, и вместе с ней появляются две вещи, без которых сервер на цикле не
работает вообще: **соединение** как объект с состоянием и **буферы** — читающий
и пишущий.

Собственно, весь `php-mini-cache` — это кэш в духе Redis: он принимает клиентов
по TCP, разбирает команды, отвечает. Но нас в этой главе интересует не то, что
он _хранит_, а то, как он вообще _принимает и отдаёт байты_. Хранилище, протокол
и Pub/Sub — темы глав 11 и 12.

## 1. Проблема

### Один поток, много клиентов

Самый простой сервер на PHP выглядит так:

```php
$socket = stream_socket_server('tcp://0.0.0.0:6380');
while ($client = stream_socket_accept($socket)) {
    $line = fgets($client);       // ждём, пока клиент что-нибудь пришлёт
    fwrite($client, handle($line)); // ждём, пока клиент это прочитает
    fclose($client);
}
```

Он обслуживает **ровно одного** клиента в каждый момент. Пока первый клиент
думает, второй, уже подключившийся, стоит в очереди `accept` и ждёт. А если
первый клиент подключился и молчит — `fgets()` ждёт вечно, и сервер превращается
в «висящий». Это не медленно, это неверно: один клиент может остановить всех.

Лекарство — **неблокирующие сокеты**: `fread()` и `fwrite()` на них не ждут, а
сразу возвращают то, что есть. Но тогда сервер обязан держать состояние между
вызовами: «сколько байт команды уже пришло», «что из ответа ещё не ушло».
Держать это состояние негде, кроме как в объекте соединения — и именно такую
роль играет `ClientConnection`.

### TCP — поток байт, а не поток сообщений

Вторая проблема тоньше и её почти всегда недооценивают. TCP не сохраняет границы
наших сообщений. Если клиент одной командой пишет `SET user:42 Tanat`, сервер
может получить это:

```text
Read #1:  SET user:
Read #2:  42 Tanat
```

— или наоборот, в одном `read()` прийти сразу три команды:

```text
Read #1:  PING\r\nPING\r\nPING\r\n
```

Нет никакой связи между «сколько раз клиент вызвал write» и «сколько раз сервер
получил read». То же самое на отправке: `fwrite()` может принять не весь ответ,
а только его часть, и остаток придётся дописывать позже, когда сокет снова
станет writable.

Отсюда два буфера, по одному на каждое направление:

- **ReadBuffer** — накапливает пришедшие байты, пока из них не соберётся целая
  команда;
- **WriteBuffer** — держит ответ, пока сокет не примет его целиком.

Без первого сервер не сможет разобрать ни одной команды, пришедшей частями. Без
второго он либо заблокируется на медленном клиенте, либо потеряет «хвост»
ответа. Это и есть тема главы.

## 2. Устройство

### Всё начинается с accept

Сервер держит один слушающий сокет. В цикле он регистрирует его как «читаемый»:
как только туда приходит новый клиент, обработчик вызывает `accept()`, переводит
принятый сокет в неблокирующий режим и заводит на него `ClientConnection`:

```text
EventLoop: слушающий сокет стал читаемым
    │
    ▼
ServerSocket::accept() ──► stream_socket_accept + stream_set_blocking(false)
    │
    ▼
new ClientConnection(socket)  ──► ConnectionManager::add()
    │
    ▼
onReadable(socket, читать байты)
```

`accept()` здесь не блокирует: вызывается он только тогда, когда цикл уже
сообщил, что соединение готово, а таймаут передаётся нулевым. Ключевая деталь —
`stream_set_blocking($connection, false)`: без неё принятый сокет остался бы
блокирующим, и первый же `fread()` на нём остановил бы весь цикл.

### Соединение — это сокет, состояние и два буфера

`ClientConnection` — маленький объект, который связывает всё вместе:

```text
ClientConnection
├── socket          ресурс сокета
├── state           New → Connected → Reading → Processing → Writing → Closed
├── readBuffer      ReadBuffer
├── writeBuffer     WriteBuffer
├── lastActivityAt  когда последний раз была активность (для idle-таймаута)
└── id()            get_resource_id(socket) — ключ во всех реестрах
```

Состояние нужно не для красоты, а чтобы отвечать на вопрос «можно ли сейчас
писать/читать на это соединение». Например, `queueForWrite()` ничего не делает,
если соединение уже `Closed`: писать в закрытый сокет — это TypeError, а не
«неудачная запись».

Идентификатор — отдельная ловушка, о которой стоит сказать сразу, потому что она
относится ко всем реестрам сразу (подписки, открытые транзакции, пауза на
чтение). Это `get_resource_id($socket)`. Номер читается и после закрытия сокета,
а главное — **переиспользуется** следующим файлом, который откроет процесс.
Реестр, который не удалил запись о закрытом соединении, не просто «течёт»: он
отдаёт эту запись постороннему соединению. Поэтому закрытие — это всегда
удаление из всех реестров сразу.

### Чтение: накопить, разобрать, съесть

Обработчик готовности к чтению делает ровно три вещи:

```text
fread(65536) ──► ReadBuffer::append ──► processBufferedCommands ──► consume
```

- `fread()` возвращает то, что реально пришло — сколько угодно байт, от одного
  до `READ_CHUNK_SIZE` (64 КиБ);
- байты добавляются в `ReadBuffer`;
- `processBufferedCommands()` в цикле просит парсер вытащить из буфера одну
  полную команду за другой. Пока парсер отдаёт значения — команды выполняются;
  как только он вернул `null` (данных на целую команду не хватает) — цикл
  останавливается, а «съеденные» байты удаляются из буфера через
  `consume($offset)`.

Порядок здесь принципиален. `fread()` **никогда** не отдаёт команды — он отдаёт
байты. Превращение байт в значения — работа парсера, и она продвигается
небольшими шагами. `ReadBuffer` — то место, где «остаток» живёт между вызовами.

Отдельно стоит вопрос границы: сколько можно копить в ReadBuffer. Клиент,
который шлёт бесконечную строку без разделителя, заставит буфер расти без
предела — это исчерпание памяти по запросу клиента. Поэтому у сервера есть
`maxReadBufferBytes` (512 КиБ): если буфер перерос лимит и всё ещё не дал
команды, соединение получает `ERR Protocol error: too big buffer` и рвётся.

### Запись: отдать сколько приняли, остальное отложить

Ответ — это тоже просто байты. Их кладут в `WriteBuffer`, а затем пытаются
«слить» на сокет:

```text
encode(reply) ──► WriteBuffer::append ──► fwrite(chunk)
                                            │
                        всё ушло? ── да ──► снять writable-подписку
                                └── нет ──► onWritable(socket, снова flush)
```

`fwrite()` на неблокирующем сокете возвращает число принятых байт — оно может
быть меньше длины куска. Тогда лишнее остаётся в буфере, а сокет регистрируется
как writable: цикл вернётся к нему, когда в сокете освободится место. Так
медленный клиент не блокирует цикл — он просто не получает ответ сразу.

### Обратное давление: вода прибывает

Если клиент вообще не читает ответы, а сервер продолжает их генерировать,
`WriteBuffer` растёт бесконечно. Это тоже исчерпание памяти, только с другой
стороны. Лечится оно **паузой на чтение**: как только `WriteBuffer` перерос
`maxWriteBufferBytes` (16 МиБ), сервер перестаёт читать _новые_ команды от этого
соединения. Раз новых команд нет — нет и новых ответов, и буфер перестаёт расти.

Возврат делается не по тому же порогу, а по **нижней ватерлинии**
`lowWriteBufferBytes` (по умолчанию — четверть верхней, 4 МиБ). Это гистерезис:
если снимать паузу ровно на 16 МиБ, клиент, который «чуть-чуть» догоняет и снова
отстаёт, будет то поставлен на паузу, то снят на каждом проходе цикла. Нижний
порог гарантирует, что после снятия паузы буфер действительно пуст настолько,
что можно поработать.

```text
WriteBuffer
   16 MiB  ─── пауза: removeReadable
    4 MiB  ─── возобновление: onReadable + перезапуск хвоста пайплайна
    0 MiB
```

У этой схемы есть тонкая дыра, которую проект закрывает отдельно. Пауза
ограничивает то, что соединение **запросило себе**. Но PUBLISH-рассылка
производится _другим_ соединением: подписчик, который не читает, ничего не
запрашивает, а сообщения к нему всё равно приходят. Пауза на чтение его не
спасает. Поэтому у подписчиков есть **жёсткий** предел —
`hardSubscriberWriteBufferBytes` (четыре верхних порога, 64 МиБ), после которого
соединение не приостанавливают, а **рвут**. (Подробнее Pub/Sub — в главе 12.)

### Завершение: закрыть — значит забыть везде

`disconnectClient()` — это не только `close()` сокета. Он снимает соединение с
цикла (и от readable, и от writable), удаляет из `ConnectionManager`, отписывает
от всех каналов, отменяет открытую транзакцию, убирает из набора пауз и только
потом закрывает. Пропустить любой из этих шагов — и номер ресурса, который
сейчас освободится, достанется следующему соединению вместе с чужой записью.

А сам цикл не полагается на то, что владельцы аккуратно отпишут закрытые стримы.
Он делает две вещи: перед `stream_select()` выбрасывает уже закрытые стримы из
своих списков, а во время обхода — проверяет `is_resource()` перед каждым
вызовом. Причина простая: один и тот же проход может закрыть соединение
_посередине_ (PUBLISH роняет сломанного подписчика, shutdown закрывает всех
сразу), и передавать закрытый стрим обработчику — это TypeError, который валит
процесс целиком.

## 3. Код

### Принятие соединения

```php
/**
 * Blocks until a client connects, or until $timeoutSeconds elapses.
 *
 * The accepted socket is switched to non-blocking mode: reads and
 * writes on it must never stall the event loop while other clients
 * are waiting to be served.
 *
 * @return resource|false
 */
public function accept(?float $timeoutSeconds = null)
{
    // A timeout is an expected outcome here, not an error worth a warning.
    $connection = @stream_socket_accept($this->socket, $timeoutSeconds ?? -1);

    if ($connection !== false) {
        stream_set_blocking($connection, false);
    }

    return $connection;
}
```

[`src/Server/ServerSocket.php#L69-L88`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/ServerSocket.php#L69-L88)

**Перевод комментариев:**
- `accept()` (PHPDoc): блокируется, пока не подключится клиент или не истечёт
  `$timeoutSeconds`. Принятый сокет переводится в неблокирующий режим: чтение и
  запись на нём не должны останавливать цикл событий, пока другие клиенты ждут
  обслуживания. Возвращает `resource` или `false`.
- `// A timeout is an expected outcome here, not an error worth a warning.` —
  таймаут здесь ожидаемый исход, а не ошибка, достойная предупреждения.

Комментарий формулирует правило одной строкой: «reads and writes on it must
never stall the event loop». Всё остальное в главе — следствия этой строки.
Таймаут — «ожидаемый исход, а не ошибка»: `accept()` со нулевым временем штатно
возвращает `false`, когда клиентов нет, и это не должно писать warning.

### Читающий буфер

```php
/**
 * Accumulates bytes read from a socket.
 *
 * A single TCP read is not guaranteed to contain a whole command, so
 * incoming bytes are appended here until a parser further up the stack
 * decides enough has arrived.
 */
final class ReadBuffer
{
    private string $buffer = '';

    public function append(string $bytes): void
    {
        $this->buffer .= $bytes;
    }

    public function contents(): string
    {
        return $this->buffer;
    }

    public function length(): int
    {
        return strlen($this->buffer);
    }

    /**
     * Drops the first $length bytes - the ones a parser has already turned
     * into values.
     */
    public function consume(int $length): void
    {
        $this->buffer = substr($this->buffer, $length);
    }
}
```

[`src/Connection/ReadBuffer.php#L7-L41`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Connection/ReadBuffer.php#L7-L41)

**Перевод комментариев:**
- `ReadBuffer` (PHPDoc): накапливает байты, прочитанные из сокета. Одно
  TCP-чтение не гарантирует целую команду, поэтому входящие байты добавляются
  сюда, пока парсер выше по стеку не решит, что данных уже достаточно.
- `consume()` (PHPDoc): отбрасывает первые `$length` байт — те, что парсер уже
  превратил в значения.

Класс намеренно примитивен: строковый буфер и три операции. Он ничего не знает о
командах и протоколе — он только «копилка байт». `consume()` отрезает уже
разобранный префикс; `contents()` отдаёт то, что осталось. Именно разделение
«буфер копит / парсер решает» позволяет одному и тому же буферу обслуживать
любой протокол.

### Пишущий буфер: смещение вместо пересборки

```php
/**
 * Queues bytes waiting to be written to a socket.
 *
 * A single write is not guaranteed to flush everything at once, so unsent
 * bytes stay here until the socket becomes writable again.
 *
 * Consuming is a moving offset rather than a re-slice. A slow reader's
 * backlog can run to megabytes and drains a few kilobytes per writable
 * event; rebuilding the remainder on each of those is O(backlog) copying
 * per write, which turns one slow client into real CPU cost. The offset
 * makes a consume O(1), and the string is rebuilt only when the dead
 * prefix in front of it is worth reclaiming.
 */
final class WriteBuffer
{
    /**
     * How much already-written data may sit in front of the offset before
     * the string is rebuilt without it. Small enough that a connection
     * cannot hold an arbitrary amount of dead bytes, large enough that the
     * rebuild happens once every many writes.
     */
    private const int COMPACTION_THRESHOLD_BYTES = 64 * 1024;

    private string $buffer = '';

    /** Where the unwritten bytes start; everything before it is gone. */
    private int $offset = 0;

    public function append(string $bytes): void
    {
        $this->buffer .= $bytes;
    }

    public function contents(): string
    {
        return substr($this->buffer, $this->offset);
    }

    /**
     * Up to $maxBytes of what is still queued, for handing to one write.
     *
     * Callers ask for a slice rather than contents() so the copy is bounded
     * by the write size instead of by the backlog: a 16 MB backlog handed
     * to fwrite() in full is a 16 MB copy per attempt, and the kernel was
     * never going to take it all in one go anyway.
     */
    public function chunk(int $maxBytes): string
    {
        return substr($this->buffer, $this->offset, $maxBytes);
    }
```

[`src/Connection/WriteBuffer.php#L7-L56`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Connection/WriteBuffer.php#L7-L56)

**Перевод комментариев:**
- `WriteBuffer` (PHPDoc): ставит в очередь байты, ожидающие записи в сокет. Одна
  запись не гарантирует сброс всего сразу, поэтому неотправленные байты остаются
  здесь, пока сокет снова не станет доступен для записи. Потребление —
  движущееся смещение, а не повторная нарезка. У медленного читателя хвост может
  достигать мегабайт и сливаться по несколько килобайт за событие готовности к
  записи; пересборка остатка на каждом таком событии — это O(хвоста) копирования
  на запись, что превращает одного медленного клиента в реальную нагрузку на
  CPU. Смещение делает потребление O(1), а строка пересобирается только когда
  мёртвый префикс перед ним стоит того, чтобы его освободить.
- `COMPACTION_THRESHOLD_BYTES` (PHPDoc): сколько уже записанных данных может
  висеть перед смещением, прежде чем строка будет пересобрана без них.
  Достаточно мало, чтобы соединение не держало произвольный объём мёртвых байт,
  и достаточно велико, чтобы пересборка случалась раз в много записей.
- `$offset` (`@var`): где начинаются незаписанные байты; всё до него уже ушло.
- `chunk()` (PHPDoc): до `$maxBytes` того, что ещё в очереди, для передачи в
  одну запись. Вызывающие просят срез, а не `contents()`, чтобы копия
  ограничивалась размером записи, а не хвостом: хвост в 16 МБ, целиком отданный
  `fwrite()`, — это копия 16 МБ на каждую попытку, а ядро всё равно никогда не
  приняло бы столько за один раз.

Здесь уже есть настоящее решение, а не минимализм. Наивный `consume()` делал бы
`$this->buffer = substr($this->buffer, $length)` — то есть копировал бы весь
остаток. Для медленного клиента с многомегабайтным «хвостом», который сливается
по несколько килобайт за раз, это O(backlog) копирования на каждую запись: один
медленный клиент превращается в реальную нагрузку на CPU. Поэтому здесь
**смещение**: `consume()` не копирует ничего, а просто двигает `$offset`. Строка
пересобирается только когда мёртвый префикс перед смещением дорастает до
`COMPACTION_THRESHOLD_BYTES` (64 КиБ).

И `chunk()`, а не `contents()`: вызывающий просит не «весь хвост», а не больше
`$maxBytes`. Иначе `fwrite()` с 16 МБ буфера копировал бы эти 16 МБ на каждую
попытку, хотя ядро всё равно не приняло бы столько за раз.

### consume: O(1), пока префикс не наберёт порог

```php
/**
 * Marks the first $length queued bytes as written, e.g. after a partial
 * write.
 */
public function consume(int $length): void
{
    $this->offset += min(max($length, 0), $this->length());

    if ($this->offset === strlen($this->buffer)) {
        $this->buffer = '';
        $this->offset = 0;

        return;
    }

    if ($this->offset >= self::COMPACTION_THRESHOLD_BYTES) {
        $this->buffer = substr($this->buffer, $this->offset);
        $this->offset = 0;
    }
}
```

[`src/Connection/WriteBuffer.php#L68-L87`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Connection/WriteBuffer.php#L68-L87)

**Перевод комментариев:**
- `consume()` (PHPDoc): помечает первые `$length` байт очереди записанными,
  например после частичной записи.

Два особых случая. Первый — буфер опустел полностью: тогда и строка, и смещение
сбрасываются в ноль (это чистая память). Второй — мёртвый префикс дорос до
порога: тогда строка наконец пересобирается без него. `min(max(...))` защищает
от отрицательного или чрезмерного `$length` — на всякий случай, хотя вызывающий
всегда даёт число реально записанных байт.

### Обработчик чтения: два разных «пусто»

```php
private function watchForIncomingData(ClientConnection $connection): void
{
    $this->eventLoop->onReadable($connection->socket(), function () use ($connection): void {
        $chunk = @fread($connection->socket(), self::READ_CHUNK_SIZE);

        // Empty means two different things on a non-blocking socket, and
        // only feof() tells them apart: the peer closed the connection,
        // or select() woke this up for a read that had nothing behind it
        // (a spurious wakeup, a connection that reset). The first ends
        // the connection; the second is a no-op.
        if ($chunk === false || ($chunk === '' && feof($connection->socket()))) {
            $this->disconnectClient($connection);

            return;
        }

        if ($chunk === '') {
            return;
        }

        $connection->setState(ConnectionState::Reading);
        $connection->appendToReadBuffer($chunk);
        $this->metrics->recordBytesRead(strlen($chunk));
        $this->processBufferedCommands($connection);

        // processBufferedCommands() may already have disconnected this
        // connection (a malformed stream) - nothing left to check.
        if ($connection->state() === ConnectionState::Closed) {
            return;
        }

        if ($connection->readBuffer()->length() > $this->maxReadBufferBytes) {
            $this->sendErrorAndDisconnect($connection, 'ERR Protocol error: too big buffer');
        }
    });
}
```

[`src/Server/CacheServer.php#L431-L466`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L431-L466)

**Перевод комментариев:**
- `// Empty means two different things on a non-blocking socket, and` — пустая
  строка на неблокирующем сокете значит две разные вещи, и различить их может
  только `feof()`: клиент закрыл соединение или `select()` разбудил чтение, за
  которым ничего не было (ложное пробуждение, сброшенное соединение). Первое
  завершает соединение; второе — no-op.
- `// processBufferedCommands() may already have disconnected this` —
  `processBufferedCommands()` мог уже отключить это соединение (испорченный
  поток) — проверять больше нечего.

Это самое важное место главы. Пустая строка от `fread()` на неблокирующем сокете
значит **две разные вещи**, и различить их может только `feof()`:

- если достигнут конец файла — клиент закрыл соединение, пора его убирать;
- если нет — это «ложное пробуждение»: `select()` сказал «готово», а читать
  нечего (соединение сбросилось, событие уже неактуально). Это no-op.

Спутать их — классическая ошибка: сервер либо рвёт живые соединения на каждом
ложном пробуждении, либо копит призраков. После добавления байт в буфер
вызывается разбор `processBufferedCommands()`, и **после него** проверяется
единственное условие, которое относится к чтению напрямую: не перерос ли буфер
свой предел.

### Разбор пачками, с остановкой на паузе

```php
/**
 * Executes the complete commands sitting in the connection's read
 * buffer, answering in batches rather than one reply per command (see
 * REPLY_BATCH_BYTES) and stopping early if the connection is dropped or
 * paused along the way.
 *
 * Stopping matters: a client can ask for far more than it reads - 2 KB
 * of `GET` against a 200 KB value is 20 MB of replies - and executing
 * the whole pipeline before handing anything over means building all of
 * that in memory first, with backpressure only consulted once it is too
 * late to matter. Commands left unexecuted stay in the read buffer,
 * which is not being read from while the connection is paused, and are
 * picked up again when it resumes.
 */
private function processBufferedCommands(ClientConnection $connection): void
{
    $buffer = $connection->readBuffer()->contents();
    $offset = 0;
    $replies = '';
    $error = null;

    while (true) {
        try {
            $parsed = $this->parser->parse($buffer, $offset);
        } catch (ProtocolException $exception) {
            $error = $exception;

            break;
        }

        // An incomplete value at the end of the buffer: it stays there
        // until the rest of it arrives.
        if ($parsed === null) {
            break;
        }

        [$value, $offset] = $parsed;
        $replies .= $this->execute($connection, $value);

        if (strlen($replies) < self::REPLY_BATCH_BYTES) {
            continue;
        }

        $connection->readBuffer()->consume($offset);
        $buffer = substr($buffer, $offset);
        $offset = 0;

        $this->queueForWrite($connection, $replies);
        $replies = '';

        // The batch just written may have dropped the connection, or
        // filled its write buffer past the pause level - either way the
        // rest of the pipeline is not this turn's work.
        if ($connection->state() === ConnectionState::Closed || $this->isPaused($connection)) {
            return;
        }
    }

    $connection->readBuffer()->consume($offset);

    if ($replies !== '') {
        $this->queueForWrite($connection, $replies);
    }

    if ($error === null || $connection->state() === ConnectionState::Closed) {
        return;
    }

    // Unlike a command-level error (wrong argument count, an unknown
    // name), a desynced byte stream cannot be resynchronized - there
    // is no reliable way to find the start of the next value once
    // framing is lost. Everything that arrived before the bad byte
    // has already been applied in order above; the client still gets
    // a proper RESP error before the
    // connection closes.
    $this->sendErrorAndDisconnect($connection, 'ERR Protocol error: ' . $error->getMessage());
}
```

[`src/Server/CacheServer.php#L468-L544`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L468-L544)

**Перевод комментариев:**
- `processBufferedCommands()` (PHPDoc): выполняет полные команды, лежащие в
  буфере чтения соединения, отвечая пачками, а не по одному ответу на команду
  (см. `REPLY_BATCH_BYTES`), и рано останавливается, если соединение отброшено
  или поставлено на паузу по пути. Остановка важна: клиент может запросить куда
  больше, чем прочитает, — 2 КБ `GET` против значения в 200 КБ — это 20 МБ
  ответов, — и выполнение всего пайплайна до выдачи хоть чего-нибудь означало бы
  сначала построить всё это в памяти, а обратное давление учитывалось бы только
  тогда, когда уже слишком поздно. Невыполненные команды остаются в буфере
  чтения, который не читается, пока соединение на паузе, и подхватываются снова,
  когда оно возобновляется.
- `// An incomplete value at the end of the buffer: it stays there` — неполное
  значение в конце буфера остаётся там, пока не придёт остаток.
- `// The batch just written may have dropped the connection, or` — только что
  записанная пачка могла отбросить соединение или заполнить его буфер записи
  выше уровня паузы — в любом случае остаток пайплайна не относится к текущему
  проходу.
- `// Unlike a command-level error (wrong argument count, an unknown` — в
  отличие от ошибки уровня команды (неверное число аргументов, неизвестное имя),
  потерявший синхронизацию поток байт нельзя восстановить — после потери
  фрейминга нет надёжного способа найти начало следующего значения. Всё, что
  пришло до плохого байта, уже применено по порядку выше; клиент всё равно
  получит корректную ошибку RESP до закрытия соединения.

Разберём три решения, которые здесь спрятаны:

1. **Ответы копятся в `$replies`, а не пишутся по одному.** Пока накопленное
   меньше `REPLY_BATCH_BYTES` (256 КиБ), цикл просто выполняется дальше; и
   только при переполнении пачка сбрасывается в `WriteBuffer`. Это не
   микрооптимизация: она нужна, чтобы пайплайн уходил целыми пакетами, а не
   потоком крошечных записей (см. главу 11 про алгоритм Нагла).
2. **Пачка сбрасывается посреди цикла, до конца разбора.** Комментарий объясняет
   почему: клиент может запросить куда больше, чем читает. Пачка ограничивает,
   сколько ответов сервер построит в памяти, прежде чем спросить обратное
   давление. Если после сброса соединение оказалось на паузе — цикл прерывается,
   а невыполненные команды **остаются в ReadBuffer** до возобновления.
3. **Неполная команда не ошибка.** `$parsed === null` — это нормальный «данных
   пока мало», цикл просто останавливается, а смещение `$offset` отрезает только
   разобранное. Именно поэтому разорванная между двумя `read()` команда
   собирается из двух кусков.

Отдельно — обработка `ProtocolException`. Поток байт, у которого потерян
фрейминг, повторной синхронизации не подлежит: нельзя надёжно найти начало
следующего значения. Поэтому всё, что было до плохого байта, уже применено по
порядку, а клиенту уходит честная `ERR Protocol error` — и соединение
закрывается. Это отличается от ошибки уровня команды (не то число аргументов,
неизвестное имя): та возвращается как обычный ответ, а соединение живёт.

### Плавная запись и нижняя ватерлиния

```php
/**
 * Writes as much of the connection's WriteBuffer as the socket accepts
 * right now. Whatever does not fit stays queued, and the socket is
 * watched for the next writable event instead of blocking on it.
 */
private function flushWriteBuffer(ClientConnection $connection): void
{
    $buffer = $connection->writeBuffer();

    if ($buffer->isEmpty()) {
        return;
    }

    $written = @fwrite($connection->socket(), $buffer->chunk(self::WRITE_CHUNK_BYTES));

    if ($written === false) {
        $this->disconnectClient($connection);

        return;
    }

    if ($written > 0) {
        $buffer->consume($written);
        $this->metrics->recordBytesWritten($written);
    }

    // Drained to the low watermark: the reader has caught up far enough
    // to be let back in, even if some of the response is still queued.
    // An empty buffer is under any watermark, so a full flush and a
    // partial one resume by the same rule.
    if ($buffer->length() <= $this->lowWriteBufferBytes) {
        $this->resumeReadingIfPaused($connection);
    }

    if ($buffer->isEmpty()) {
        $this->eventLoop->removeWritable($connection->socket());
        $connection->setState(ConnectionState::Reading);

        return;
    }

    // Still holding bytes: come back when the socket has room again,
    // rather than blocking on a peer that is not reading.
    $this->eventLoop->onWritable($connection->socket(), fn () => $this->flushWriteBuffer($connection));
}
```

[`src/Server/CacheServer.php#L622-L666`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L622-L666)

**Перевод комментариев:**
- `flushWriteBuffer()` (PHPDoc): записывает столько `WriteBuffer` соединения,
  сколько сокет принимает прямо сейчас. Что не поместилось, остаётся в очереди,
  а сокет отслеживается до следующего события готовности к записи вместо
  блокировки на нём.
- `// Drained to the low watermark: the reader has caught up far enough` — слито
  до нижней ватерлинии: читатель догнал достаточно, чтобы впустить его обратно,
  даже если часть ответа ещё в очереди. Пустой буфер ниже любой ватерлинии,
  поэтому полный и частичный сброс возобновляют чтение по одному правилу.
- `// Still holding bytes: come back when the socket has room again,` — байты
  ещё остались: вернуться, когда в сокете снова появится место, вместо
  блокировки на клиенте, который не читает.

Здесь видны сразу и частичная запись, и гистерезис. `chunk(WRITE_CHUNK_BYTES)`
отдаёт `fwrite()` не больше 256 КиБ; сколько он принял — столько помечается
записанным. Если буфер опустел — снимается подписка на writable и состояние
возвращается в `Reading`. Если нет — сокет взводится на writable, и цикл
вернётся, когда в нём освободится место. Строка
`$buffer->length() <= $this->lowWriteBufferBytes` — это и есть нижняя
ватерлиния: как только хвост ужался до 4 МиБ, чтение с соединения можно
возобновить, даже если часть ответа ещё лежит в буфере. Пустой буфер «ниже любой
ватерлинии», поэтому полная и частичная запись возобновляют чтение по одному и
тому же правилу — отдельного случая не нужно.

### Пауза на чтение

```php
/**
 * Backpressure: a connection whose queued WriteBuffer is over the
 * limit stops being read from - a slow or absent reader must not be
 * allowed to make the server buffer an unbounded amount of its own
 * responses in memory. Reading resumes once the buffer drains below
 * the low watermark (see flushWriteBuffer()).
 */
private function pauseReadingIfWriteBufferTooLarge(ClientConnection $connection): void
{
    if ($connection->writeBuffer()->length() <= $this->maxWriteBufferBytes) {
        return;
    }

    if ($this->isPaused($connection)) {
        return;
    }

    $this->pausedConnections[$connection->id()] = true;
    $this->eventLoop->removeReadable($connection->socket());
}
```

[`src/Server/CacheServer.php#L668-L687`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L668-L687)

**Перевод комментариев:**
- `pauseReadingIfWriteBufferTooLarge()` (PHPDoc): обратное давление: соединение,
  у которого очередь `WriteBuffer` выше предела, перестаёт читаться — медленному
  или отсутствующему читателю нельзя позволять заставлять сервер буферизовать
  неограниченный объём собственных ответов в памяти. Чтение возобновляется,
  когда буфер опускается ниже нижней ватерлинии (см. `flushWriteBuffer()`).

Вся обратная связь — это `removeReadable()`. Дальше сервер просто перестаёт
получать от соединения новые команды, а значит и порождать новые ответы. Пауза
идемпотентна (повторный вызов при уже стоящем соединении ничего не делает) — это
важно, потому что `queueForWrite()` вызывается из многих мест. Ключ —
`$connection->id()`, тот самый номер ресурса; поэтому все реестры и чистятся
синхронно в `disconnectClient()`.

### Цикл не доверяет владельцам

```php
/**
 * Drops streams that were closed by whoever owns them, before
 * stream_select() is handed one and raises a TypeError that takes the
 * whole process down.
 *
 * The loop does not own what it watches: CacheServer closes every
 * connection at once on shutdown, a connection is dropped from inside
 * a listener, a test closes a socket it made itself. Requiring each of
 * them to deregister first - on every path, including the ones that
 * throw - is a coupling the loop does not need: a closed stream is
 * never going to be ready again, so forgetting it is always right.
 */
private function forgetClosedStreams(): void
{
    $isOpen = static fn (array $entry): bool => is_resource($entry[0]);
    $this->readListeners = array_filter($this->readListeners, $isOpen);
    $this->writeListeners = array_filter($this->writeListeners, $isOpen);
}
```

[`src/EventLoop/SelectLoop.php#L132-L149`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/EventLoop/SelectLoop.php#L132-L149)

**Перевод комментариев:**
- `forgetClosedStreams()` (PHPDoc): выбрасывает стримы, закрытые их владельцами,
  прежде чем `stream_select()` получит такой стрим и выбросит `TypeError`,
  который уронит весь процесс. Цикл не владеет тем, за чем следит: `CacheServer`
  закрывает все соединения сразу при выключении, соединение отбрасывается
  изнутри обработчика, тест закрывает созданный им сокет. Требовать от каждого
  сначала отписаться — на каждом пути, включая те, что бросают исключение, — это
  связанность, которая циклу не нужна: закрытый стрим никогда не станет снова
  готовым, поэтому забыть его всегда правильно.

Финальная деталь, которая делает всю конструкцию живучей. Цикл не требует, чтобы
владелец стрима отписался перед закрытием: он сам выбрасывает закрытые стримы из
списков до `stream_select()`, а во время обхода проверяет `is_resource()` перед
каждым вызовом обработчика (см. `tick()`). «Закрытый стрим никогда больше не
станет готовым, поэтому забыть его — всегда правильно». Без этого `shutdown`,
закрывающий все соединения сразу, уронил бы цикл TypeError'ом прямо посреди
прохода — ровно та ошибка, от которой страдал `php-worker-pool` в главе 7.

## 4. Эксперимент

Поднимаем контейнер и сервер:

```bash
make build install
make run-server          # в одном терминале — сервер на 127.0.0.1:6380
```

Убедимся, что он отвечает:

```text
$ php bin/client.php PING
PONG
$ php bin/client.php SET name Tanat
OK
$ php bin/client.php GET name
Tanat
```

Теперь посмотрим на соединения и буферы вживую. Скрипт открывает три соединения,
спрашивает у сервера их число, а затем отправляет одну команду **двумя**
TCP-записями и смотрит, когда придёт ответ. Сохраните его как `/tmp/connect.php`
внутри контейнера:

```php
<?php

declare(strict_types=1);

/** Encodes one command the way the server expects it on the wire (RESP array). */
function enc(array $args): string
{
    $out = "*" . count($args) . "\r\n";
    foreach ($args as $arg) {
        $out .= "$" . strlen($arg) . "\r\n" . $arg . "\r\n";
    }

    return $out;
}

function connect()
{
    $socket = stream_socket_client("tcp://127.0.0.1:6380", $errno, $errstr, 2.0);
    if ($socket === false) {
        fwrite(STDERR, "connect failed: $errstr ($errno)\n");
        exit(1);
    }
    stream_set_blocking($socket, false);

    return $socket;
}

/** Reads whatever has arrived within $wait, then stops. */
function readFor($socket, float $wait = 0.2): string
{
    $out = '';
    $deadline = microtime(true) + $wait;
    while (microtime(true) < $deadline) {
        $chunk = fread($socket, 65536);
        if ($chunk !== false && $chunk !== '') {
            $out .= $chunk;
        } else {
            usleep(2000);
        }
    }

    return $out;
}

$clients = [connect(), connect(), connect()];

foreach ($clients as $socket) {
    fwrite($socket, enc(['PING']));
}
usleep(100_000);

fwrite($clients[0], enc(['INFO']));
$info = readFor($clients[0]);
foreach (explode("\r\n", $info) as $line) {
    if (str_starts_with($line, 'connected_clients:')) {
        printf("three open sockets -> %s\n", $line);
    }
}

// A command split across two TCP writes, the way a network can split it.
$command = enc(['SET', 'user:42', 'Tanat']);
$cut = intdiv(strlen($command), 2);

$split = connect();
fwrite($split, substr($command, 0, $cut));
$early = readFor($split, 0.2);
printf("sent %d of %d bytes -> reply now: %s\n", $cut, strlen($command), $early === '' ? '(none)' : trim($early));

fwrite($split, substr($command, $cut));
$late = readFor($split, 0.2);
printf("sent the remaining %d bytes -> reply now: %s\n", strlen($command) - $cut, trim($late));

// The value really did land.
$check = connect();
fwrite($check, enc(['GET', 'user:42']));
printf("GET user:42 -> %s\n", trim(readFor($check)));

foreach ([...$clients, $split, $check] as $socket) {
    fclose($socket);
}
```

**Перевод комментариев:**
- `enc()` (PHPDoc): кодирует одну команду так, как сервер ожидает её на проводе
  (массив RESP).
- `readFor()` (PHPDoc): читает всё, что пришло за `$wait`, затем
  останавливается.
- `// A command split across two TCP writes, the way a network can split it.` —
  команда, разбитая на две TCP-записи, как это может сделать сеть.
- `// The value really did land.` — значение действительно записалось.

Выполните внутри контейнера:

```bash
docker compose exec php php /tmp/connect.php
```

Реальный вывод:

```text
three open sockets -> connected_clients:3
sent 18 of 37 bytes -> reply now: (none)
sent the remaining 19 bytes -> reply now: +OK
GET user:42 -> $5
Tanat
```

Здесь видно всё, ради чего написана глава:

1. **Три соединения — три записи.** `INFO` вернул `connected_clients:3`: сервер
   держит каждое соединение отдельно, хотя поток один.
2. **Половина команды не даёт ответа.** Первые 18 из 37 байт легли в
   `ReadBuffer`; парсер вернул `null`, ответа нет — и это правильное поведение,
   а не «зависание». Обратите внимание: `(none)`, а не ошибка.
3. **Оставшиеся 19 байт собирают команду.** Теперь парсер видит целое значение,
   команда выполняется, приходит `+OK`.
4. **Значение действительно записано:** `GET user:42` возвращает `$5\r\nTanat`.
   Буферизация не «проглатывает» половину команды — она её **ждёт**.

### Обратное давление: медленный клиент

Второй эксперимент — про пишущий буфер. `examples/slow-client.php` открывает
клиент, который пайплайнит 60 `GET` большого значения и **никогда** не читает
ответы, а вторым, обычным соединением шлёт `PING` и замеряет каждый:

```bash
docker compose exec php php examples/slow-client.php
```

```text
[slow client] pipelined 60 GETs of a 409,600-byte value (24,576,000 total), and will never read a single reply

[healthy client] pinging 10 times while the slow client sits there...
[healthy client] PING 1 answered in 0.15 ms
[healthy client] PING 2 answered in 0.15 ms
[healthy client] PING 3 answered in 0.31 ms
[healthy client] PING 4 answered in 0.30 ms
[healthy client] PING 5 answered in 0.90 ms
[healthy client] PING 6 answered in 0.38 ms
[healthy client] PING 7 answered in 0.37 ms
[healthy client] PING 8 answered in 0.62 ms
[healthy client] PING 9 answered in 0.49 ms
[healthy client] PING 10 answered in 0.49 ms
```

60 × 409 600 = **24 576 000 байт** ответов, которые никто не читает — это больше
верхней ватерлинии (16 МиБ). Сервер накапливает их в `WriteBuffer` медленного
соединения, доходит до порога и снимает с него чтение. Второе соединение всё это
время получает `PONG` за доли миллисекунды: один невоспитанный клиент **не**
влияет на остальных. Это и есть ответ на вопрос «что будет, если клиент не
читает» — не падение и не деградация для всех, а пауза для него одного.

## 5. Отказы

| Что ломается                            | Чем обнаруживается                             | Что происходит                                                        |
| --------------------------------------- | ---------------------------------------------- | --------------------------------------------------------------------- |
| Команда пришла половинкой               | парсер вернул `null`                           | байты остаются в ReadBuffer, ответа нет, соединение живёт             |
| В один `read()` пришло несколько команд | цикл парсера                                   | все выполняются; ответы копятся в пачке и уходят вместе               |
| `fwrite()` принял не всё                | `$written < длины`                             | остаток в WriteBuffer, сокет взводится на writable                    |
| Клиент не читает ответы                 | WriteBuffer > `maxWriteBufferBytes`            | чтение с соединения снимается (пауза)                                 |
| Пауза «залипла»                         | `lowWriteBufferBytes`                          | чтение возвращается; невыполненный хвост перезапускается таймером 0.0 |
| Подписчик не читает                     | WriteBuffer > `hardSubscriberWriteBufferBytes` | соединение **рвётся** — PUBLISH производит не оно                     |
| Поток байт рассинхронизирован           | `ProtocolException`                            | предыдущие команды применены, затем `ERR Protocol error` и разрыв     |
| Абсурдно много аргументов               | `maxArgumentsPerCommand`                       | `ERR too many arguments`, соединение **остаётся**                     |
| ReadBuffer перерос лимит                | `maxReadBufferBytes`                           | `ERR Protocol error: too big buffer` и разрыв                         |
| Стрим закрыт посреди прохода            | `is_resource()`/`forgetClosedStreams()`      | обработчик не вызывается; закрытые стримы выбывают из `select`        |
| Номер ресурса переиспользован           | `id()` = `get_resource_id`                     | реестр, забывший запись, отдаст её чужому соединению                  |
| Клиент оборвал связь                    | `fread` вернул `''` и `feof()`                 | `disconnectClient()` чистит подписки, транзакции, паузу               |
| Соединений больше `maxConnections`      | счётчик в `acceptClient()`                     | `ERR max number of clients reached`, сокет закрыт сразу               |

Три строки здесь — самые поучительные. Строка про залипшую паузу показывает,
зачем нужны **две** границы, а не одна. Строка про подписчика показывает, что
обратное давление работает только против того, что соединение **запросило
себе**, — и для производимого извне трафика нужен отдельный, жёсткий предел. А
строка про `get_resource_id` объясняет, почему `disconnectClient()` чистит _все_
реестры: номер освободится и будет выдан заново, и «забытая» запись превратится
в чужую.

## 6. Как это проверено

Тема разложена на три набора тестов, которые читаются как оглавление главы.

**Буферы и соединение** — `tests/Connection/`: `ReadBufferTest`
(`testAppendAccumulatesFragmentedWrites`, `testConsumeRemovesAPrefix`),
`WriteBufferTest` (`testConsumeDropsTheWrittenPrefixAfterAPartialWrite`),
`ConnectionManagerTest` (`testAddAndCount`, `testRemove`,
`testCloseAllClosesEveryConnectionAndEmptiesTheManager`), `ClientConnectionTest`
(`testNewConnectionStartsInNewState`, `testReadAndWriteBuffersAccumulateBytes`,
`testCloseMarksConnectionAsClosed`).

**Цикл** — `tests/EventLoop/SelectLoopTest.php`:
`testOnReadableFiresWhenDataArrives`, `testOnWritableFiresForAWritableStream`,
`testTickReturnsZeroOnTimeout`.

**Сервер целиком** — `tests/Server/CacheServerTest.php` (47 тестов). Самые
близкие к главе:

- `testItAcceptsAConnectingClient`, `testAcceptedSocketIsNonBlocking`,
  `testAcceptTimesOutWithoutAClient` — принятие соединения и
  `stream_set_blocking(false)`;
- `testPartialCommandsAccumulateInTheReadBufferAcrossTicks` — та самая команда
  из двух кусков;
- `testProcessesMultiplePipelinedCommandsFromOneRead` и
  `testMultipleCommandsPlusATrailingPartialOneAreHandledCorrectly` — несколько
  команд в одном `read()` плюс неполный хвост;
- `testAPartialWriteIsQueuedInTheWriteBufferInsteadOfBlocking` — частичная
  запись;
- `testAPipelineIsAnsweredInBatchesRatherThanBuiltWhole` — пачки ответов;
- `testASlowReaderIsPausedThenResumedOnceItsWriteBufferDrains` и
  `testASlowReaderResumesWhenItsWriteBufferHitsTheLowWatermark` — верхняя и
  нижняя ватерлинии;
- `testACommandLeftOverFromAPausedPipelineRunsOnceReadingResumes` — хвост
  пайплайна, перезапущенный после снятия паузы;
- `testAnOversizedReadBufferGetsARespErrorAndIsDisconnected` и
  `testAValueTooBigForTheReadBufferIsRejectedOnItsDeclaredLength` — пределы
  чтения;
- `testAnUnknownCommandNameWithALineBreakCannotForgeASecondReply` — граница
  фрейминга;
- `testClientDisconnectIsDetectedAndCleanedUp` — чистка при обрыве;
- `testAConnectionOverTheLimitIsRejectedWithARespErrorAndClosed` — предел
  соединений;
- `testShutdownClosingConnectionsMidPassDoesNotCrashTheLoop` — тот самый
  закрытый стрим посреди прохода.

Запускаем:

```text
$ vendor/bin/phpunit tests/Connection tests/EventLoop/SelectLoopTest.php
OK (26 tests, 74 assertions)

$ vendor/bin/phpunit tests/Server/CacheServerTest.php
OK (47 tests, 1237 assertions)
```

Что здесь важно: тесты
`testASlowReaderIsPausedThenResumedOnceItsWriteBufferDrains` и
`testACommandLeftOverFromAPausedPipelineRunsOnceReadingResumes` проверяют не
«счастливый путь», а именно механизм обратного давления — и его последний байт.
Первый из них ловит ошибку «пауза снимается по верхнему порогу и дрожит»; второй
— «после снятия паузы хвост пайплайна потерян навсегда».
`testShutdownClosingConnectionsMidPassDoesNotCrashTheLoop` — прямой тест на
`forgetClosedStreams()` и `is_resource()`-guard: без них цикл падал бы
TypeError'ом ровно так, как падал `php-worker-pool` в главе 7.

## 7. Цифры

Замеры живых прогонов этой главы (Docker, PHP 8.5.11):

| Что                                  | Значение                               |
| ------------------------------------ | -------------------------------------- |
| Три открытых сокета → `INFO`         | `connected_clients:3`                  |
| Команда из двух TCP-записей          | 18 / 37 байт → ответа нет, затем `+OK` |
| Медленный клиент: невычитанный хвост | 60 × 409 600 = 24 576 000 байт         |
| `PING` рядом с медленным клиентом    | 0.15 – 0.90 мс                         |
| Тесты буферов и цикла                | 26 тестов, 74 утверждения              |
| Тесты сервера                        | 47 тестов, 1237 утверждений            |
| Весь набор проекта                   | 261 тест, 1759 утверждений             |

Константы, которые задают поведение (из `CacheServer`):

| Параметр                           | По умолчанию | Смысл                                         |
| ---------------------------------- | -----------: | --------------------------------------------- |
| `READ_CHUNK_SIZE`                  |       64 КиБ | сколько максимум берёт один `fread()`         |
| `REPLY_BATCH_BYTES`                |      256 КиБ | когда накопленные ответы сбрасываются в буфер |
| `WRITE_CHUNK_BYTES`                |      256 КиБ | сколько максимум берёт один `fwrite()`        |
| `maxReadBufferBytes`               |      512 КиБ | потолок ReadBuffer (и размер значения)        |
| `maxWriteBufferBytes`              |       16 МиБ | верхняя ватерлиния: пауза на чтение           |
| `lowWriteBufferBytes`              |        4 МиБ | нижняя ватерлиния: возврат чтения             |
| `hardSubscriberWriteBufferBytes`   |       64 МиБ | предел, после которого подписчик рвётся       |
| `COMPACTION_THRESHOLD_BYTES`       |       64 КиБ | когда WriteBuffer пересобирает строку         |
| `maxConnections`                   |          512 | почему не 1024 — см. ниже                     |
| `FD_SETSIZE` (для `stream_select`) |         1024 | жёсткий предел дескрипторов                   |

Последние две строки связаны. `stream_select()` не умеет следить за дескриптором
с номером ≥ `FD_SETSIZE` (1024 в стандартной сборке PHP) и **валит весь
`select`**, а не только этот стрим. Предел в 512 оставляет запас для слушающего
сокета, сигнального пайпа и файлов снапшотов — тест
`testConnectionsAreCappedByDefaultWellShortOfFdSetsize` держит это число
честным. Это цена, которую сервер на `select()` платит за свою простоту; `epoll`
в этой реализации не используется.

## 8. Упражнения

1. **Объясните.** Почему `processBufferedCommands()` сбрасывает пачку ответов в
   `WriteBuffer` **посреди** разбора, а не после того, как разберёт весь
   ReadBuffer? Что сломается (какая именно память), если убрать проверку
   `strlen($replies) < self::REPLY_BATCH_BYTES`?
2. **Найдите ошибку.** В `watchForIncomingData()` ветки `$chunk === false` и
   `($chunk === '' && feof(...))` приводят к разрыву, а «просто пустая строка» —
   к `return`. Что произойдёт, если заменить `feof()`-проверку на «пустая строка
   — это всегда конец соединения»? На каком сценарии это проявится?
3. **Спроектируйте.** Пауза на чтение защищает от того, что соединение запросило
   само. Придумайте, как защитить от избыточного трафика, который соединению
   **не принадлежит** (PUBLISH-рассылка), не прибегая к жёсткому пределу с
   разрывом. Почему проект всё-таки выбрал разрыв?
4. **Посчитайте.** `WriteBuffer::consume()` двигает смещение, пока мёртвый
   префикс не дорастёт до 64 КиБ. Сколько копирований выполнит строка при сливе
   хвоста в 16 МиБ кусками по 256 КиБ при пороге 64 КиБ? А если порог сделать 1
   МиБ? Что произойдёт с памятью в худшем случае?
5. **Разберите аудит.** `disconnectClient()` чистит `ConnectionManager`,
   подписки, транзакции и набор пауз. Представьте, что в проект добавят новый
   реестр (например, «соединения, ждущие `WATCH`»). Как сделать так, чтобы автор
   нового реестра не мог забыть чистку? Назовите два разных подхода.
6. **Сравните.** Прочитайте `src/EventLoop/SelectLoop.php` целиком. Чем
   обработка «ложного пробуждения» в `waitForReadiness()` отличается от
   обработки пустого `fread()` в сервере? Почему защита от закрытых стримов
   нужна в **двух** местах — до `select` и во время обхода?

## Что дальше

- **Глава 11** — протокол и pipelining на примере RESP: как из байтового потока
  получаются команды и как ответы на них уходят пачками (та самая
  `REPLY_BATCH_BYTES`).
- **Глава 12** — TTL, Pub/Sub и снапшоты: активное и ленивое истечение ключей и
  та самая асимметрия подписчиков, у которых есть жёсткий предел.
- **Глава 4** — цикл событий и неблокирующий ввод-вывод: `select()` как
  фундамент, на котором стоит вся эта глава.
