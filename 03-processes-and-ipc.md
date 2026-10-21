# Глава 3. Процессы и IPC: пайпы, сокеты, разделяемая память, семафоры

> Проекты: [`php-memory-lab`](https://github.com/Researcher86/php-memory-lab)
> (коммит `7c8d365`) и
> [`php-concurrency`](https://github.com/Researcher86/php-concurrency) (коммит
> `8d5006b`). Нужно знать: `fork` и copy-on-write (→ глава 2). Пригодится
> дальше: пул воркеров на процессах (→ глава 6) и очередь задач (→ глава 8).

Два процесса, созданные `fork`, делят память только **до первой записи**. Как
только один из них что-то меняет, страница приватизируется, и дальше у каждого
своя копия. Значит, чтобы передать данные, нужен транспорт: ядро или участок
памяти, куда смотрят оба.

Способов ровно столько, сколько в этой главе: **пайп/сокет** (поток байт),
**очередь сообщений** (сообщения в ядре), **разделяемая память** (общие байты
без правил) и **семафор** (правила к ним). У каждого своя цена, и интуиция здесь
подводит: разделяемая память кажется быстрее всего, потому что не копирует
данные, — но измерение в этом проекте даёт обратный ответ.

---

## 1. Проблема

Передать значение из одного процесса в другой — значит ответить сразу на четыре
вопроса, а не на один:

1. **Как читатель узнаёт, где кончается сообщение?** Поток — это байты, а не
   сообщения.
2. **Что делает быстрый писатель, когда читатель медленный?** Копит в памяти или
   ждёт?
3. **Кто может писать в общий кусок?** Без правил — никто и все.
4. **Кто убирает ресурс и что будет, если процесс умрёт?**

Разные механизмы отвечают на них по-разному, и «быстрее/медленнее» — не первый
вопрос. В проекте это вынесено в одну таблицу (`docs/ipc-comparison.md`): сокет
копирует дважды, но даёт границы сообщений, порядок, backpressure и блокирующее
ожидание бесплатно; разделяемая память не копирует вообще, но не даёт **ничего**
из этого.

Итог замера обескураживает:

```text
$ make experiment ARGS="ring:throughput"
     64 B | ring + lock   |  1116.4 ms |  17,915 msg/s |  1.09 MiB/s | 0.81 CPU s
     64 B | unix socket   |    47.8 ms | 418,802 msg/s | 25.56 MiB/s | 0.06 CPU s
     64 B | ring, no lock |    31.1 ms | 642,573 msg/s | 39.22 MiB/s | 0.06 CPU s
```

Разделяемая память **действительно** быстрее всех: строка «ring, no lock» — та
же память, тот же формат, но без семафора — обгоняет сокет в полтора раза и не
копирует payload. И всё же «ring + lock», то есть разделяемая память **в
безопасном виде**, в **23 раза медленнее** сокета. Разбор этой строки — и есть
содержание главы: не память медленная, а **блокировка**.

## 2. Устройство

### Четыре механизма и что они делают

|                            | Unix-сокет                      | SysV очередь         | SysV shared memory              | `mmap` (`MAP_SHARED`) |
| -------------------------- | ------------------------------- | -------------------- | ------------------------------- | --------------------- |
| Единица                    | поток байт                      | сообщение            | регион байт                     | регион байт           |
| Границы                    | ваши (length-prefix)            | ядро хранит          | нет                             | нет                   |
| Копий на сообщение         | две (в ядро и из ядра)          | две                  | нет, но `serialize` в оба конца | нет                   |
| Синхронизация              | ядро само                       | ядро                 | ваша (семафор)                  | ваша (семафор)        |
| Backpressure               | встроен: полный буфер блокирует | встроен              | нет: быстрый писатель затирает  | нет                   |
| Блокирующее ожидание       | `recv()` спит до данных         | `msgrcv()` умеет     | опрос                           | опрос                 |
| Писатель погиб             | отправленное не потеряно        | так же               | половина записи остаётся        | так же                |
| Очистка                    | закрыть дескриптор              | `ipcrm`, иначе течёт | `shm_remove()`, иначе течёт     | `unlink()` файла      |
| Видно в                    | `lsof`                          | `ipcs -q`            | `ipcs -m`, `RssShmem`           | `ls`, `RssFile`       |
| `memory_get_usage()` видит | payload как строку              | payload              | **нет**                         | **нет**               |

SysV очередь в лаборатории не реализована намеренно: это семантика сокета
(кадрированные сообщения, порядок, блокирующий приём, backpressure) с числовым
ключом вместо дескриптора. В `php-concurrency` она есть как урок
(`03_ipc/msg_queue.php`), но новой задачи не решает.

### Пайп — это не сокет-пара, и наоборот

В PHP встроенной `pipe()` нет. То, что обычно называют «пайпом», делают через
`stream_socket_pair()` — и это **двунаправленная** пара, а не классический
однонаправленный пайп с read- и write-концом:

```php
[$parent, $child] = stream_socket_pair(STREAM_PF_UNIX, STREAM_SOCK_STREAM, STREAM_IPPROTO_IP);

$pid = pcntl_fork();

if ($pid === -1) {
    die('fork failed');
}

if ($pid === 0) {
    // Child: закрываем родительский конец
    fclose($parent);
```

[`php-concurrency/03_ipc/pipe.php#L23-L33`](https://github.com/Researcher86/php-concurrency/blob/8d5006b2a34d20931af584eac51897247959e799/03_ipc/pipe.php#L23-L33)

Ключевое здесь — `fclose($parent)` в ребёнке (и парный `fclose($child)` в
родителе). Это не уборка, а часть протокола: пока процесс держит чужой конец,
EOF на противоположном не наступит, и читатель будет ждать вечно. Мы уже видели
ту же ловушку в `Rendezvous` (→ глава 2).

### Поток — это байты, не сообщения

`send()` может принять три сообщения за раз, `recv()` — вернуть полтора. Чтобы
читать сообщения, а не байты, поток нужно **кадрировать**: положить в начало
каждого сообщения его длину. Сокет в проекте это и делает, добавляя ровно четыре
байта:

```php
public static function encode(string $payload): string
{
    $length = strlen($payload);

    if ($length > self::MAX_MESSAGE_LENGTH) {
        throw new LengthException(sprintf(
            'Payload of %d bytes exceeds the %d-byte frame limit',
            $length,
            self::MAX_MESSAGE_LENGTH,
        ));
    }

    return pack('N', $length) . $payload;
}
```

[`src/Ipc/MessageFramer.php#L36-L49`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Ipc/MessageFramer.php#L36-L49)

`pack('N', ...)` — четырёхбайтовое число big-endian, то есть длина, одинаковая
на любой платформе. Верхняя граница `MAX_MESSAGE_LENGTH` здесь принципиальна:
четыре байта заголовка позволяют запросить 4 ГиБ, и без предела испорченный
заголовок превращается в аллокацию, которую процесс не переживёт.

### Backpressure: очередь, которой никто не проектировал

Между `send()` и `receive()` всегда стоит ядро — два буфера, send и receive.
Писатель идёт на полной скорости, пока они не заполнятся, а потом **вынужден
ждать читателя**, потому что `send()` блокируется до появления места. Это и есть
backpressure: писатель сам выравнивается по скорости читателя, без
договорённостей между ними. Именно поэтому неограниченная очередь в памяти перед
сокетом — плохая идея: она превращает ограниченную задачу в неограниченную.

### Разделяемая память: байты и ничего больше

SysV-сегмент в PHP — не «общий буфер байт», а **API сериализации с общим
буфером**:

```php
/**
 * A System V shared-memory segment, addressed by a numeric key.
 *
 * Two things about it are easy to get wrong and both are the reason this
 * wrapper exists rather than bare shm_* calls.
 *
 * It is not a shared byte buffer. PHP stores each variable through
 * serialize(), so put() writes a serialized copy and get() builds a fresh
 * PHP value from it. Two processes never share a zval, only bytes that
 * happen to describe one - which means a read-modify-write is three
 * operations with a gap in the middle, and the gap is where the lost updates
 * of experiments/07-shared-memory/race-condition.php come from.
 *
 * It outlives every process that touches it. detach() drops this process's
 * handle; the segment stays in the kernel, visible in /proc/sysvipc/shm and
 * `ipcs -m`, until someone calls destroy() or the machine reboots. A crashed
 * process leaves its segment behind, and the next run attaching the same key
 * inherits whatever was in it.
 */
```

[`src/Ipc/SharedMemorySegment.php#L10-L28`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Ipc/SharedMemorySegment.php#L10-L28)

**Перевод комментариев:**
- `SharedMemorySegment` (PHPDoc): сегмент разделяемой памяти System V,
  адресуемый числовым ключом. Здесь легко ошибиться в двух вещах, и обе —
  причина, по которой существует эта обёртка, а не голые вызовы `shm_*`. Это не
  общий буфер байт: PHP хранит каждую переменную через `serialize()`, поэтому
  `put()` записывает сериализованную копию, а `get()` собирает из неё новое
  значение PHP. Два процесса никогда не делят zval — только байты, которые
  случайно его описывают. Значит, чтение — изменение — запись — это три операции
  с зазором посередине, и из этого зазора берутся потерянные обновления в
  `experiments/07-shared-memory/race-condition.php`. Сегмент переживает каждый
  процесс, который его касался: `detach()` отпускает дескриптор этого процесса,
  а сегмент остаётся в ядре, виден в `/proc/sysvipc/shm` и `ipcs -m`, пока
  кто-нибудь не вызовет `destroy()` или машина не перезагрузится. Упавший
  процесс оставляет сегмент после себя, и следующий запуск с тем же ключом
  наследует то, что в нём было.

Отсюда два свойства, которых у сокета нет. Первое: **чтение-изменение-запись не
атомарны** — `get()` строит новое значение, `put()` сериализует обратно, и между
ними есть окно. Второе: **сегмент переживает все процессы**, которые его
трогали. `detach()` отпускает только этот процесс; пока кто-то не вызовет
`destroy()`, сегмент останется в ядре — и следующий запуск с тем же ключом молча
унаследует прошлое содержимое.

### Семафор: единственная блокировка, которую ядро убирает само

Без блокировки счётчик в общей памяти — это гонка. Семафор закрывает окно, но
важен не только `acquire`:

```php
public function synchronized(callable $critical): mixed
{
    $this->acquire();

    try {
        return $critical();
    } finally {
        $this->release();
    }
}
```

[`src/Ipc/Semaphore.php#L93-L102`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Ipc/Semaphore.php#L93-L102)

`finally` — это весь смысл метода: блокировка, которая отпускается только на
успешном пути, перестаёт отпускаться при первой же ошибке. И есть второй,
невидимый слой защиты: PHP передаёт `SEM_UNDO` при каждом `sem_acquire()`, и
ядро **само снимает** блокировку с процесса, который умер, — даже под `SIGKILL`.
Самодельный флаг «занято» в сегменте такого владельца не имеет, и ядру нечего
снимать.

### Кольцевой буфер: сколько стоит пересобрать сокет руками

Фаза 7 строит поверх сырого сегмента то, что сокет даёт из коробки: границы
сообщений, порядок, понятие «полон», backpressure и **обнаружение погибшего
писателя**. Формат — 32-байтовый заголовок и слоты:

```text
/**
 * A fixed-size ring buffer in raw shared memory, for one producer and one
 * consumer.
 *
 * This is the phase where the socket has to be rebuilt by hand. A segment
 * gives bytes and nothing else: no message boundaries, no ordering, no notion
 * of "full", no backpressure, and no way to know that a writer has died. The
 * layout below supplies each of those, and how much of it there is, is the
 * point of the exercise.
 *
 *   header (32 B)                        data area (capacity slots)
 *   ┌───────┬─────────┬─────...─┬──────┐ ┌────────────┬────────────┬───...
 *   │ magic │ version │         │ busy │ │   slot 0   │   slot 1   │
 *   └───────┴─────────┴─────...─┴──────┘ └────────────┴────────────┴───...
 *    0       4                   28       └ 4-byte length, then that many bytes
 *
 * Every header field is a 32-bit big-endian unsigned integer, so the segment
 * means the same thing to any process that opens it - the same reason a
 * network protocol fixes its byte order.
```

[`src/Ipc/RingBuffer.php#L11-L29`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Ipc/RingBuffer.php#L11-L29)

Ядро снимает семафор умершего процесса, поэтому следующий писатель получает
**чистую блокировку над грязными данными**. Отличить здоровый буфер от
брошенного на полпути можно только по `busyPid` в заголовке:

```php
private function transaction(callable $operation): mixed
{
    return $this->semaphore->synchronized(function () use ($operation): mixed {
        $header = $this->readHeader();

        if ($header['busyPid'] !== self::READY) {
            throw new RingBufferCorruptedException(sprintf(
                'Ring buffer 0x%x was left mid-update by pid %d',
                $this->key,
                $header['busyPid'],
            ));
        }
```

[`src/Ipc/RingBuffer.php#L316-L327`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/src/Ipc/RingBuffer.php#L316-L327)

Полузаписанный слот **не восстанавливается** — он не может быть восстановлен,
потому что данных для этого нет. Буфер объявляется испорченным: лучше отказать,
чем отдать потребителю сообщение, которого никто не отправлял.

## 3. Эксперимент

### `ipc:socket` — сколько стоит круг

```text
$ make experiment ARGS="ipc:socket"
payload  |  trips |       mean        min        max |   throughput
0 B      |   2000 |    0.010 ms    0.009 ms    0.066 ms |       0 B/s
64 B     |   2000 |    0.010 ms    0.008 ms    0.047 ms | 12.79 MiB/s
1 KiB    |   2000 |    0.009 ms    0.008 ms    0.051 ms | 216.67 MiB/s
64 KiB   |    500 |    0.043 ms    0.036 ms    0.596 ms |  2.83 GiB/s
1 MiB    |     50 |    0.601 ms    0.528 ms    0.765 ms |  3.25 GiB/s
10 MiB   |     10 |   12.436 ms   11.647 ms   13.307 ms |  1.57 GiB/s
```

Малые payload'ы измеряют стоимость системного вызова и переключения контекста
(~10 микросекунд на круг), большие — копирование через буфер сокета. Крупные
сообщения перестают ускоряться в байтах в секунду — вот где окупается
кадрирование.

### `ipc:backpressure` — буферы копят, а не процесс

```text
$ make experiment ARGS="ipc:backpressure"
Producer sends 40 x 64.00 KiB as fast as it can; the consumer sleeps 20 ms per message.
Kernel send buffer: 208.00 KiB

Accepted without waiting: 3 messages (192.00 KiB) - the buffers absorbed that much before the producer felt anything.
Blocked inside send():   828.0 ms of 828.8 ms total (100% of the producer's life).
Effective rate:          48.3 messages/s, against a consumer capable of 50.0/s.

Delta producer, across the whole blocked run:
  PHP usage: +6.07 KiB
  RSS:       +12.00 KiB

Producer SIGKILLed after 5 sends: consumer read 5 of them, then saw "Peer closed the channel".
```

Писатель заполнил 208-килобайтный буфер за три сообщения и дальше **весь остаток
жизни спал внутри `send()`** — 828 мс из 828.8. При этом её память не росла:
сообщение, ждущее в буфере ядра, относится к ядру, а не к процессу. И главное:
убей писателя после пяти `send()` — потребитель прочитает все пять. Байты уже
принадлежат ядру и переживают процесс; меняется только EOF.

### `ipc:serialization` — сколько стоит данные превратить в байты

```text
$ make experiment ARGS="ipc:serialization"
format     |     bytes |    encode    decode  round-trip | peak alloc | equal?
json       | 112.37 KiB |   0.28 ms   0.89 ms     1.17 ms |   1.10 MiB | yes
serialize  | 184.04 KiB |   0.23 ms   0.36 ms     0.59 ms |   1.20 MiB | yes
csv        |  52.31 KiB |   0.41 ms   0.57 ms     0.98 ms | 931.48 KiB | yes
binary     |  52.52 KiB |   1.09 ms   5.77 ms     6.87 ms | 835.02 KiB | yes
raw string |  97.66 KiB |    n/a       n/a        0.00 ms |      288 B | yes
```

Здесь интуиция проигрывает дважды. `serialize()` — **и** самый крупный payload,
**и** самый быстрый round trip. Бинарный формат вдвое меньше JSON, но **в шесть
раз медленнее** его разбирает: `json_decode()` — один вызов C на весь payload, а
распаковка по полям идёт в userland. Меньше байт выгодно, только когда узкое
место — провод, а не процессор.

### `shm:lifecycle` — сериализация, размер, `RssShmem`

```text
$ make experiment ARGS="shm:lifecycle"
Stored counter=1, read it back, set the copy to 999, stored it separately.
  index 1 is still 1 - modifying what get() returned changed nothing shared.
  an object round-trips by value too: same class stdClass, same id 7, different instance.

A 5,000-row array (316.31 KiB serialized):
  put()    0.354 ms
  get()    0.566 ms
  a plain PHP copy of the same array: 0.000037 ms (refcount++, nothing moves)

A 4.00 KiB segment refused a 4.00 KiB payload; the largest it took was 3.75 KiB.

Writing and reading 512.00 KiB through the segment:
  PHP usage +192 B   RSS +520.00 KiB   RssShmem +512.00 KiB

After detach(): the segment is still there, now with 0 process(es) attached.
```

Три факта из одной строки: объект по общей памяти путешествует **по значению**
(разные экземпляры), доступ стоит двух сериализаций (0.35 + 0.57 мс против
0.00004 мс обычной PHP-копии), а лимит — это размер сериализованного значения
плюс служебные данные, а не PHP-размер. И плата за это — 512 KiB в `RssShmem`:
часть RSS, и **ни одного байта** в `memory_get_usage()`.

### `shm:race` — счётчик, которого нет

```text
$ make experiment ARGS="shm:race"
children | expected |      unsynchronized | semaphore-protected
       1 |      500 |    500 (  0.0% lost) |    500 (   5.1 ms)
       2 |     1000 |    883 ( 11.7% lost) |   1000 (  22.9 ms)
       4 |     2000 |   counter destroyed |   2000 (  53.3 ms)
       8 |     4000 |   counter destroyed |   4000 ( 101.8 ms)
      16 |     8000 |   counter destroyed |   8000 ( 205.1 ms)
```

`get()`, `+1`, `put()` — это чтение, сложение и запись с двумя окнами:

```php
$increment = static function () use ($ownSegment, $counterIndex): void {
    try {
        // The read-modify-write. Three operations, two windows.
        $current = (int) $ownSegment->get($counterIndex);
        $ownSegment->put($counterIndex, $current + 1);
    } catch (SharedMemoryException) {
        // Unsynchronized only, and worse than a lost update:
        // shm_put_var() replaces a variable by removing it and
        // inserting it again, so for a moment the counter does
        // not exist at all. A reader landing in that window finds
        // nothing to read. The increment is dropped and the loop
        // continues, which is the most a process can do about a
        // race it cannot see.
    }
};
```

[`experiments/07-shared-memory/race-condition.php#L57-L71`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/experiments/07-shared-memory/race-condition.php#L57-L71)

**Перевод комментариев:**
- `// The read-modify-write. Three operations, two windows.` — чтение, изменение
  и запись: три операции, два окна.
- `// Unsynchronized only, and worse than a lost update:` — только без
  синхронизации, и это хуже потерянного обновления: `shm_put_var()` заменяет
  переменную, удаляя её и вставляя заново, поэтому на мгновение счётчика не
  существует вовсе. Читатель, попавший в это окно, не находит ничего для чтения.
  Инкремент теряется, а цикл продолжается — больше процесс ничего не может
  сделать с гонкой, которую не видит.

Три вывода важнее процентов потерь:

- **Один ребёнок всегда прав.** Соперников нет — гонки нет, и код проходит
  тесты. Именно так этот класс ошибок и доживает до продакшена: он проявляется
  только при масштабировании.
- **Потери молчаливые.** Ни ошибки, ни предупреждения, ни исключения — просто
  сумма, которая не сходится. Нечего повторять, потому что никто не сообщил о
  провале.
- **На четырёх писателях счётчик перестаёт существовать.** PHP хранит переменные
  внутри сегмента как каталог и переписывает запись, удаляя и вставляя её
  заново; параллельные писатели портят не значение, а каталог. Разделяемая
  память деградирует не плавно — она перестаёт быть структурой данных.

### `shm:counter` — что покупает замок

```text
$ make experiment ARGS="shm:counter"
Counter: 4000, expected 4000 - exact
Wall clock: 83.8 ms for 4000 protected updates (47746 updates/s).

 child |        total |      waiting | share spent waiting
--------------------------------------------------------
     0 |      70.4 ms |      64.0 ms | 91.0%
     1 |      75.2 ms |      67.9 ms | 90.3%

Holder SIGKILLed while holding two locks at once:
  SysV semaphore:       acquirable again - the kernel undid the acquisition
  flag in shared memory: still says "held by 295" - every waiter would wait forever
```

Итог точный по построению: **4000 = 4000**. Но каждый ребёнок провёл ~91 % жизни
в ожидании остальных: четыре процесса не ускорились вчетверо, а выстроились в
очередь. И последний пункт — тот самый аргумент за семафор ядра против
самодельного флага: `SEM_UNDO` снял блокировку за убитого держателя, а флаг в
памяти остался врать навсегда.

### `ring:throughput` — та же память, но по-настоящему в деле

```text
$ make experiment ARGS="ring:throughput"
     64 B | ring + lock   |  1116.4 ms |  17,915 msg/s |  1.09 MiB/s | 0.81
     64 B | unix socket   |    47.8 ms | 418,802 msg/s | 25.56 MiB/s | 0.06
     64 B | ring, no lock |    31.1 ms | 642,573 msg/s | 39.22 MiB/s | 0.06
 1.00 KiB | ring + lock   |  1120.1 ms |  17,855 msg/s | 17.44 MiB/s | 0.79
 1.00 KiB | unix socket   |    38.5 ms | 520,035 msg/s | 507.85 MiB/s | 0.07
 1.00 KiB | ring, no lock |    29.3 ms | 681,482 msg/s | 665.51 MiB/s | 0.06
16.00 KiB | ring + lock   |  1030.8 ms |  19,402 msg/s | 303.15 MiB/s | 0.79
16.00 KiB | unix socket   |    96.4 ms | 207,383 msg/s |  3.16 GiB/s | 0.15
16.00 KiB | ring, no lock |    48.0 ms | 416,578 msg/s |  6.36 GiB/s | 0.09
```

Третья строка каждой тройки — контроль: тот же сегмент, тот же заголовок, без
семафора. Это **самый быстрый транспорт таблицы**. Значит, память не медленная —
медленный **замок**: один `acquire` и один `release` на сообщение, между двумя
процессами, которые хотят его одновременно, означает, что большинство `acquire`
застаёт его занятым, а ожидание — это сон и пробуждение, десятки микросекунд
против ~8 мкс самой операции. Сокет выигрывает не хитростью, а тем, что ядро
синхронизирует как побочный эффект перемещения байтов и берёт плату один раз за
оба.

### `ring:pacing` — опрос как ручка без хорошего положения

Когда в буфере пусто, `pop()` возвращает `null`, ждать не на чем, и потребитель
вынужден спрашивать снова:

```text
$ make experiment ARGS="ring:pacing"
    poll gap |       wall |   mean latency | consumer CPU
spin, no gap |   485.8 ms |        50.5 us |     476.3 ms
      100 us |   456.3 ms |       126.9 us |      35.4 ms
     1000 us |   479.3 ms |       651.0 us |      13.8 ms
    10000 us |   453.3 ms |      5216.2 us |       3.2 ms
```

Вращение даёт задержку 50 мкс ценой целого ядра. Пауза в 10 мс возвращает ядро и
стоит в 100 раз больше задержки. У сокета такой ручки нет: читатель спит в
`recv()`, и ядро будит его ровно тогда, когда есть данные. Полный буфер при этом
возвращает `false` — писателя никто не затирает, и он идёт в темпе потребителя,
как на сокете, но **построено это из восьми слотов и сравнения**.

### Пайп и сокет-сервер в `php-concurrency`

Курс даёт те же примитивы на минимальных примерах. Пара сокетов:

```text
$ docker compose exec -T php php /app/03_ipc/main.php pipe
=== pipe.php — Unix socket pair (stream_socket_pair) ===
child 15: got task: 21
parent 14: got result: 42
```

И AF_UNIX сокет по файлу в `/tmp`, к которому подключаются два форкнутых
клиента:

```text
$ docker compose exec -T php php /app/03_ipc/main.php unix_socket
server: accepted: hello from client 1 (24)
server: accepted: hello from client 2 (25)
client 1: got response: pong
client 2: got response: pong
```

`stream_socket_server('unix://'.$path)` — это сокет с именем в файловой системе,
а не анонимная пара; именно так веб-серверы разных типов договариваются на одном
хосте. Все шесть демо урока (`pipe`, `unix_socket`, `sysvshm`, `semaphore`,
`msg_queue`, `signals`) устроены по одной схеме: создать, передать, почистить.

## 4. Отказы

| Что ломается                                            | Чем это проявится                            | Что мешает                                                 |
| ------------------------------------------------------- | -------------------------------------------- | ---------------------------------------------------------- |
| Читатель ждёт EOF, а писатель не закрыл свой конец пары | вечное ожидание                              | закрывать чужой конец сразу после `fork`                   |
| Поток приняли за сообщения                              | склеенные/разрезанные кадры                  | length-prefix, единый `MessageFramer`                      |
| Заголовок длины без предела                             | аллокация в 4 ГиБ по испорченным 4 байтам    | `MAX_MESSAGE_LENGTH` при кодировании и разборе             |
| Быстрый писатель в сокет                                | блокируется, и это правильно                 | встроенный backpressure; не ставить неограниченную очередь |
| Общий счётчик без семафора                              | молчаливые потери, затем разрушение каталога | `synchronized()` на каждое изменение                       |
| `release()` только на успешном пути                     | блокировка навсегда                          | `try/finally` в `synchronized()`                           |
| Самодельный флаг «занято» в сегменте                    | висит после смерти держателя                 | семафор ядра + `SEM_UNDO`                                  |
| Писатель убит в середине обновления                     | грязный буфер под чистой блокировкой         | `busyPid` в заголовке: обнаружить, не восстанавливать      |
| Опрос пустого буфера                                    | либо сожжённое ядро, либо огромная задержка  | событие для пробуждения (eventfd/futex)                    |
| Забытая очистка                                         | сегменты/очереди копятся                     | чистить по ключу, а не по объекту                          |

## 5. Как это проверено

- [`tests/Ipc/SocketChannelTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Ipc/SocketChannelTest.php)
  проверяет кадрирование на обоих краях: три сообщения в одном `recv()` и одно
  сообщение, пришедшее несколькими кусками, должны выйти тремя и одним
  сообщением соответственно; при этом обрыв на середине кадра отличается от
  чистого закрытия.
- [`tests/Ipc/MessageFramerTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Ipc/MessageFramerTest.php)
  фиксирует формат заголовка и то, что payload сверх предела отвергается при
  кодировании, а не при отправке.
- [`tests/Ipc/SharedMemorySegmentTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Ipc/SharedMemorySegmentTest.php)
  ловит главную ловушку: объект по общей памяти возвращается **новым
  экземпляром**, а изменение возвращённого значения не меняет сегмент;
  `detach()` не уничтожает сегмент, `destroy()` — уничтожает.
- [`tests/Ipc/SemaphoreTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Ipc/SemaphoreTest.php)
  и
  [`tests/Ipc/RingBufferTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/Ipc/RingBufferTest.php)
  проверяют критическую секцию, полный/пустой буфер и отказ на чужом («не ring
  buffer») или испорченном сегменте.
- [`tests/DocumentationTest.php`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/tests/DocumentationTest.php)
  следит, чтобы все эти механизмы были связаны из документации и из README, а
  команды в главах называли существующие эксперименты.

`php-concurrency` — курс, а не библиотека: у него нет `docs/` и unit-тестов.
Проверка там другая — `make lint` (`php -l` по всем файлам) и `make smoke`
(несколько быстрых уроков целиком, каждый с таймаутом), плюс `docker compose` с
`pcntl`, `posix`, `sysvmsg`, `sysvsem`, `sysvshm`. Каждый урок — папка с
`README.md`, `diagram.txt` и запускаемым `main.php`; в уроке 03 это шесть
отдельных демо.

## 6. Цифры

Замеры одного прогона в контейнере проекта
([`docs/BENCHMARKS.md`](https://github.com/Researcher86/php-memory-lab/blob/7c8d365692bcc8756653695b88a62d3412128b88/docs/BENCHMARKS.md),
набор `ipc`; PHP 8.5.10, Linux 7.0.12-linuxkit, aarch64 × 12,
`memory_limit 128M`):

| Операция                        | итераций | median     | ops/sec   |
| ------------------------------- | -------- | ---------- | --------- |
| socket: 1 KiB send+receive      | 20 000   | 59.344 ms  | 337 018   |
| ring buffer: 1 KiB push+pop     | 20 000   | 200.867 ms | 99 568    |
| semaphore: acquire+release      | 20 000   | 22.603 ms  | 884 835   |
| shm: put+get 1 KiB              | 20 000   | 19.912 ms  | 1 004 409 |
| shm: put+get 100 строк          | 2 000    | 53.356 ms  | 37 484    |
| serialize: 100 строк round trip | 2 000    | 51.466 ms  | 38 860    |
| json: 100 строк round trip      | 2 000    | 105.909 ms | 18 884    |

Две строки здесь важнее рейтинга, и обе совпадают с двухпроцессными
экспериментами. `shm: put+get 100 строк` стоит почти столько же, сколько
`serialize` тех же строк: PHP-сегмент — это API сериализации с общим буфером, и
ничего кроме него он не делает. А кольцевой буфер в **одном** процессе, без
конкуренции, медленнее сокета ровно из-за двух семафорных операций на сообщение
— тот же вывод, что и в `ring:throughput`, только с другой стороны.

## 7. Упражнения

1. **Почему.** «ring, no lock» быстрее сокета, а «ring + lock» — в 23 раза
   медленнее. Что именно оплачивает `+ lock`? Ответ: один `acquire` и один
   `release` на сообщение, то есть сон и пробуждение при каждом захвате.
2. **Посчитайте.** Кадр — это 4 байта заголовка плюс payload. Сколько байт
   придёт на провод при 1 000 сообщений по 1 KiB? Ответ: 1 000 × (1024 + 4) = 1
   028 000 B = 1.028 МБ (0.980 MiB).
3. **Предскажите.** Производителя убили после пяти `send()` на сокет. Сколько
   сообщений прочитает потребитель и что он увидит потом?
4. **Проектирование.** Восемь воркеров пишут в общий счётчик. Опишите, что
   случится с 1, 2, 4 и 8 писателями без блокировки и почему тест на одном
   процессе это не поймает.
5. **Найдите ошибку.** В `shm:lifecycle` объект после `get()` совпал по классу и
   `id`, но оказался другим экземпляром. Почему это не баг, а неизбежность?
6. **Сравните.** Потребитель опрашивает пустой буфер раз в 10 мс. Что лучше —
   опрос каждые 100 мкс или организация события для пробуждения, и какой ценой?

## Что дальше

- **Глава 6** — пул воркеров: `fork`, сокет-пара или очередь как транспорт между
  супервизором и воркерами, и как всё это переживает падение одного воркера.
- **Глава 8** — очередь задач: надёжность поверх транспорта — подтверждения,
  повторная доставка и что делать с сообщением, обработка которого упала.
- **Глава 18** — WAL и `fsync`: почему «отправлено в сокет» и «записано на диск»
  — две разные долговечности.
