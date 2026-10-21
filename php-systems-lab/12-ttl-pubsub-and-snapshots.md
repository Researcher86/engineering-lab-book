# Глава 12. TTL, Pub/Sub и снапшоты

> Проект: [`php-mini-cache`](https://github.com/Researcher86/php-mini-cache),
> релиз `v1.1.0`, коммит `5838655`. Нужно знать: соединения и буферы (→
> глава 10) и протокол (→ глава 11). Связанное, уже пройденное: разделяемая
> память и семафоры (→ глава 3) и `fork`/copy-on-write (→ глава 2).

В главах 10 и 11 мы разобрали, как сервер принимает соединения, превращает байты
в команды и отвечает. Всё это было про **один** ключ и **одно** соединение. Эта
глава — про три вещи, которые ломают эту простую картину и заставляют сервер
помнить о времени, о других соединениях и о собственном перезапуске:

- **TTL** — ключ, который должен исчезнуть сам, без чьей-либо команды;
- **Pub/Sub** — одного отправителя слушают много получателей;
- **снапшоты** — данные должны пережить выключение процесса.

Все три живут в `php-mini-cache`, и все три опираются на уже знакомые механизмы:
цикл событий (таймеры), буферы записи (обратное давление) и `fork` (снапшот в
отдельном процессе).

## 1. Проблема

### Ключ должен уметь умереть сам

Обычный ключ живёт, пока его не удалят `DEL`. Но у большинства реальных ключей
есть **срок годности**: сессия живёт час, кэш-запись — минуту, код подтверждения
— пять минут. Хранить их вечно нельзя — память кончится, — а просить клиента «не
забудь удалить» бессмысленно: он как раз затем и пишет в кэш, чтобы не помнить.

Значит, сервер должен уметь: «этот ключ существует до момента T, а потом его как
будто не было». И тут же возникает вопрос: **когда** его удалять?

- Если удалять **при чтении** (lazy expiration) — это дёшево, но ключ, к
  которому больше не обращаются, останется в памяти навсегда. А именно такие
  ключи и составляют мусор: истёкшие и забытые.
- Если удалять **по таймеру** (active expiration) — мусор убирается сам, но это
  регулярная работа: проход по хранилищу должен быть дешёвым.

Проект держит оба механизма одновременно, и главный вопрос здесь — как сделать
активный проход дешёвым. Наивный ответ — «раз в секунду пройти по всем ключам» —
это O(всего хранилища) на каждый тик, то есть ровно та работа, которую кэш и
должен экономить.

### Одно сообщение — многим

Публикация/подписка нужна, когда одно событие важно нескольким клиентам:
обновление конфигурации, сигнал «данные устарели», чат. `PUBLISH news hello`
должен разойтись всем, кто сейчас подписан на `news`.

Здесь есть ловушка, которую легко не заметить. В главе 10 мы выяснили: если
клиент не читает, сервер **приостанавливает чтение** с его соединения — это
обратное давление. Ему это подходит, потому что байты для соединения производит
**само** соединение: перестал читать его запросы — перестал производить ответы.

Pub/Sub это предположение ломает. Байты для подписчика производит **другой**
клиент — тот, кто публикует. Пауза чтения не останавливает публикацию ни на
байт. Подписчик, который подписался и ушёл в себя, будет накапливать сообщения в
своём буфере записи, пока сервер не кончится, — и при этом обратное давление
будет бодро рапортовать, что работает. Нужна вторая, жёсткая граница.

### Память теряется при перезапуске

`InMemoryStore` держит всё в PHP-массиве. Перезапуск процесса — деплой,
аварийное завершение, `SIGKILL` — стирает всё. Чтобы данные переживали
перезапуск, нужна **персистентность**.

Самый простой её вид — **снапшот**: сохранить всё текущее состояние в файл, а на
старте прочитать обратно. Он не спасает записи, сделанные после последнего
снапшота (это не журнал с опережающей записью, WAL), но закрывает главную
задачу: «сервер поднялся, а данные на месте».

И у снапшота есть свои три обязательных вопроса: что делать, если процесс умрёт
**посреди** записи; что делать, если сохранять надо, не останавливая
обслуживание клиентов; и можно ли доверять файлу на диске.

## 2. Устройство

### TTL: значение плюс момент смерти

Ключевое решение TTL в проекте — хранить **абсолютный момент** истечения, а не
оставшееся число секунд. `StoredValue` — это пара «значение + необязательный
`expiresAt`», и проверка истечения — одно сравнение:

```text
StoredValue { value, expiresAt: ?float }
isExpired(now) = expiresAt !== null && now >= expiresAt
```

Абсолютный момент удобен тем, что его можно **сравнивать**: «та ли это запись,
что была поставлена в очередь» — это `entry->expiresAt === $expiresAt`.
Оставшиеся секунды пришлось бы пересчитывать при каждом чтении, и они бы
«плыли».

Дальше — два независимых механизма удаления.

**Ленивое истечение** встроено в единственную точку чтения. Все операции,
которые смотрят на ключ (`get`, `has`, `delete`), идут через `entryOrNull()`:
если запись есть и её срок прошёл — она тут же удаляется и считается
отсутствующей. Никакой таймер для этого не нужен: факт, что ключ спросили, и
есть момент, когда его дешевле всего проверить.

**Активное истечение** — это таймер в цикле событий плюс структура, которая
позволяет не сканировать всё хранилище. Структура — **min-heap** (`SplMinHeap`)
из пар `[expiresAt, key]`. На её вершине всегда самая ранняя запись, поэтому
проход такой:

```text
пока heap не пуст:
    [expiresAt, key] = вершина
    если expiresAt > now: стоп     ← всё, что дальше, ещё позже
    извлечь из heap
    если у ключа сейчас тот же expiresAt:
        удалить ключ
```

Это **O(сколько истекло)**, а не O(сколько хранится): как только вершина
оказалась в будущем, все записи за ней тоже, и проход останавливается.

### Отжившие записи heap: почему их не удаляют

Heap фактически **append-only**. Когда ключ перезаписывают, старую запись из
heap никто не достаёт: `put()` просто кладёт новую. И когда ключ удаляют — тоже.
Запись, «повисшая» в heap после перезаписи или удаления, называется **отжившей**
(stale).

Их специально оставляют. Альтернатива — второй индекс «ключ → позиция в heap» и
поддержание его при каждом просеивании — это настоящая структура данных, чтобы
сэкономить память в сценарии (горячий ключ с TTL, перезаписываемый в тесном
цикле), который сервер не обязан оптимизировать. Что их ограничивает: у каждой
отжившей записи есть срок, и проход выбрасывает её, когда срок наступит, **не
трогая хранилище**.

Как тогда отличить отжившую запись от настоящей? Сравнением:

```text
entry = data[key] ?? null
if entry !== null && entry->expiresAt === expiresAt: удалить
```

Три случая решаются одной проверкой. Ключ **удалили** — `entry` равен `null`,
запись игнорируется. Ключ **перезаписали с новым TTL** — `entry->expiresAt`
другой, запись игнорируется. Ключ **перезаписали без TTL** — `expiresAt` в entry
равен `null`, снова не совпадает. Совпадение бывает ровно тогда, когда ключ всё
ещё живёт именно тем сроком, который только что наступил.

### `INCR` не должен оживлять ключ

Тонкий, но важный случай. `INCR` переписывает значение, но ничего не знает о
сроке. Если бы он писал через обычный `set($key, $value)` без TTL, то
`SET hits 1 EX 60` + один `INCR` превратили бы счётчик в **вечный** — незаметная
потеря TTL. Поэтому `Store` обзавёлся отдельным методом `setKeepingTtl()`:
значение меняется, срок остаётся прежним. Он возвращает `false` для
отсутствующего ключа, и тогда `INCR` пишет его как новый — без срока, как и
положено.

Обратите внимание: это не «мелочь ради аккуратности». Альтернатива — прочитать
оставшиеся секунды и передать их обратно в `set()` — переводила бы абсолютный
момент в целые секунды и обратно, и TTL «плыл» бы на каждой инкрементации, а
инвариант переехал бы в вызывающий код, где про него забыл бы следующий метод.

### Pub/Sub: реестр каналов

`ChannelRegistry` хранит соответствие «канал → подписчики». Ключевая деталь —
подписчики индексируются по `ClientConnection::id()` (id ресурса сокета), а не
по объекту. Тогда список подписчиков канала — обычный массив, а отписка «ото
всех каналов» — один проход без поиска по идентичности объектов.

Цена такого решения — целая страница в docblock: **идентификаторы
переиспользуются**. Если вовремя не удалить запись, она не просто «утечёт» — она
со временем будет указывать на **другое** соединение, занявшее тот же id.
Поэтому отписка отовсюду — обязательный шаг при отключении, и
`disconnectClient()` его делает.

`SUBSCRIBE` возвращает массив из трёх элементов: `subscribe`, имя канала и
**общее** число подписок этого соединения (не только на этот канал). `PUBLISH`
кодирует сообщение **один раз** — и рассылает один и тот же готовый буфер всем
подписчикам, возвращая их число.

### Pub/Sub и обратное давление

Доставка идёт через тот же `WriteBuffer`, что и обычные ответы, — то есть
подчиняется правилам обратного давления. Но, как мы выяснили, пауза чтения
подписчика **не** останавливает публикацию. Поэтому у доставки есть вторая,
жёсткая граница: если буфер записи подписчика превысил
`hardSubscriberWriteBufferBytes` (по умолчанию вчетверо больше обычного предела
— 64 МиБ), подписчик **отключается**, а не ставится в очередь. Это и есть
разделение, которое проводит и эталонная реализация: «сколько можно накопить»
для обычного клиента ограничено тем, что он сам запросил, а для подписчика —
ничем, и потому у него есть отдельный потолок.

Естественные следствия, которые важно назвать прямо: сообщение, отправленное при
нуле подписчиков, теряется безвозвратно (нет истории канала и повтора); доставка
— **at-most-once**; возвращённое `PUBLISH` число подписчиков — единственная
запись о том, что событие вообще было.

### Снапшоты: точка во времени и `fork`

`InMemoryStore` умеет отдать `snapshot()` — массив «ключ → значение +
`expiresAt`» — и `restore()`, который **заменяет** всё содержимое. Здесь два
решения.

Первое: снапшот хранит TTL. Восстановленный ключ не становится вечным. Больше
того, `restore()` **отбрасывает** записи, которые успели истечь к моменту
восстановления: то же ленивое правило, что и везде.

Второе: сохранение — это `serialize()` массива в файл. И тут два приёма, каждый
из которых закрывает конкретный отказ.

**Запись через временный файл и `rename`.** Сначала данные пишутся в
`<path>.<pid>.tmp`, и только потом `rename()` ставит файл на место. `rename` в
пределах одной файловой системы атомарен: читатель увидит либо старый снапшот,
либо новый, но никогда — наполовину записанный. Имя временного файла включает
**pid**, потому что снапшоты пишутся из форкнутых детей, и два ребёнка с общим
временным путём ломали бы саму гарантию `rename`: тот, кто переименует первым,
отдал бы живой путь **чужому** открытому дескриптору, продолжающему в него
писать.

**Чтение с `allowed_classes: false`.** Файл на диске — это пересечение границы
доверия. Обычный `unserialize()` инстанцирует встреченные имена классов и
вызывает их `__wakeup()`/`__destruct()` — классический путь от «записываемого
файла» к «выполнению кода». Снапшот содержит только массивы, строки и числа,
поэтому отказ от классов ничего не стоит, а дыру закрывает.

### Сохранение не должно останавливать сервер

`serialize()` большого хранилища — заметная работа. Делать её в цикле событий
значило бы заморозить обслуживание клиентов. Поэтому снапшот пишется в
**форкнутом ребёнке** (`ForkingSnapshotWorker`): в момент `fork()` copy-on-write
даёт ребёнку согласованный снимок состояния **бесплатно**, а родитель сразу
возвращается в цикл. Ребёнок получается через `SIGCHLD`.

Три правила делают это корректным:

- **не более одного фонового снапшота**. Интервал между снапшотами задаётся, а
  время записи — нет. Хранилище, которое пишется дольше своего интервала, иначе
  форкало бы нового ребёнка на каждом тике, и они бы наперегонки переименовывали
  свои копии: последним на диск лёг бы не самый новый снапшот. Поэтому если
  ребёнок ещё пишет — новый тик **пропускается**;
- **`pcntl_waitpid(..., WNOHANG)`, а не `posix_kill($pid, 0)`** для проверки,
  жив ли ребёнок. Завершившийся, но не «пожнутый» ребёнок — зомби, который всё
  ещё отвечает на сигнал `0`, и сервер без обработчика `SIGCHLD` решил бы, что
  снапшот пишется вечно;
- **`fork()` может провалиться**. Тогда выполняется синхронное сохранение —
  правильно, просто с блокировкой цикла на время записи.

Наконец, при **корректном завершении** (`SIGTERM`/`SIGINT` или `stop()`) сервер
пишет **финальный** снапшот — чтобы запланированный перезапуск не потерял записи
с момента последнего планового. Но перед этим он **дожидается** ребёнка, если
тот ещё пишет, — и убивает его, если тот не уложился в отведённый бюджет:
ребёнок форкнулся **до** последних записей и переименует в тот же путь, поэтому,
дав ему закончить после, мы положили бы на диск **старый** снапшот.

## 3. Код

### Модель значения с моментом истечения

```php
/**
 * A stored value plus optional expiration metadata.
 */
final readonly class StoredValue
{
    public function __construct(
        public mixed $value,
        public ?float $expiresAt = null,
    ) {
    }

    public function isExpired(float $now): bool
    {
        return $this->expiresAt !== null && $now >= $this->expiresAt;
    }
}
```

[`src/Storage/StoredValue.php#L7-L22`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Storage/StoredValue.php#L7-L22)

**Перевод комментариев:**
- `StoredValue` (PHPDoc): сохранённое значение плюс необязательные метаданные
  срока истечения.

Значение и его срок живут в одном объекте. `readonly` здесь важен: проверка
`entry->expiresAt === $expiresAt` в проходе сравнивает именно тот срок, с
которым запись была поставлена, — если бы `StoredValue` можно было менять на
месте, это сравнение перестало бы быть надёжным.

### Активное истечение: проход по heap

```php
public function sweepExpired(): int
{
    $now = $this->clock->now();
    $removed = 0;

    while (!$this->expirations->isEmpty()) {
        [$expiresAt, $key] = $this->expirations->top();

        // The top of the heap is the earliest due entry, so once it is
        // in the future every entry behind it is too - that is what
        // makes this O(what expired) instead of O(everything stored).
        if ($expiresAt > $now) {
            break;
        }

        $this->expirations->extract();

        $entry = $this->data[$key] ?? null;

        // The key may have been deleted, overwritten with a new TTL, or
        // overwritten without one since this entry was queued, leaving
        // the entry stale. Comparing the entry's due time against the
        // key's current one settles all three: they match only when the
        // key is still living by exactly the expiry that just came due.
        if ($entry !== null && $entry->expiresAt === $expiresAt) {
            unset($this->data[$key]);
            $removed++;
        }
    }

    return $removed;
}
```

[`src/Storage/InMemoryStore.php#L78-L109`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Storage/InMemoryStore.php#L78-L109)

**Перевод комментариев:**
- `// The top of the heap is the earliest due entry, so once it is` — вершина
  heap — самая ранняя запись по сроку, поэтому как только она в будущем, все
  записи за ней тоже, и именно это делает проход O(истёкших), а не O(всего
  хранимого).
- `// The key may have been deleted, overwritten with a new TTL, or` — ключ
  могли удалить, перезаписать с новым TTL или перезаписать без него с момента
  постановки этой записи в очередь, и запись стала отжившей. Сравнение срока
  записи с текущим сроком ключа решает все три случая: они совпадают только
  когда ключ всё ещё живёт ровно тем сроком, который только что наступил.

Это сердце активного истечения. Заметьте, что heap — только **подсказка о
порядке**: решение выдаётся после сверки с текущим состоянием ключа. Именно
поэтому отжившие записи не нужно вычищать при перезаписи — они безвредны, пока
проход умеет их распознать. Возвращаемое число идёт в метрику `expired_keys`.

### Ленивое истечение и постановка в heap

```php
/** Stores an entry and, if it expires, queues it for the sweep. */
private function put(string $key, mixed $value, ?float $expiresAt): void
{
    $this->data[$key] = new StoredValue($value, $expiresAt);

    if ($expiresAt !== null) {
        $this->expirations->insert([$expiresAt, $key]);
    }
}

/**
 * Reads the entry for $key, lazily removing and treating it as absent
 * if its TTL has expired.
 */
private function entryOrNull(string $key): ?StoredValue
{
    $entry = $this->data[$key] ?? null;

    if ($entry === null) {
        return null;
    }

    if ($entry->isExpired($this->clock->now())) {
        unset($this->data[$key]);

        return null;
    }

    return $entry;
}
```

[`src/Storage/InMemoryStore.php#L150-L179`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Storage/InMemoryStore.php#L150-L179)

**Перевод комментариев:**
- `put()` (PHPDoc): сохраняет запись и, если у неё есть срок, ставит её в
  очередь на проход.
- `entryOrNull()` (PHPDoc): читает запись по `$key`, лениво удаляя её и считая
  отсутствующей, если её TTL истёк.

`put()` — единственный писатель `data` и `expirations`, поэтому он же —
единственное место, где ключ с TTL попадает в heap. `entryOrNull()` —
единственный читатель, поэтому именно он отвечает за ленивое удаление. Все
публичные операции (`get`, `has`, `delete`) построены поверх него, и ни одна не
может «увидеть» истёкший ключ.

### Снапшот и восстановление

```php
/**
 * A serializable copy of every entry, TTL included, for persistence.
 *
 * @return array<string, array{value: mixed, expiresAt: float|null}>
 */
public function snapshot(): array
{
    $entries = [];

    foreach ($this->data as $key => $entry) {
        $entries[$key] = ['value' => $entry->value, 'expiresAt' => $entry->expiresAt];
    }

    return $entries;
}

/**
 * Replaces the current contents with a previously taken snapshot().
 * Entries already expired by the time this runs are simply not
 * restored - the same lazy-expiration rule applies as everywhere else.
 *
 * @param array<string, array{value: mixed, expiresAt: float|null}> $entries
 */
public function restore(array $entries): void
{
    $this->data = [];
    $this->expirations = new SplMinHeap();

    foreach ($entries as $key => $entry) {
        $expiresAt = $entry['expiresAt'];

        if ($expiresAt !== null && $expiresAt <= $this->clock->now()) {
            continue;
        }

        $this->put($key, $entry['value'], $expiresAt);
    }
}
```

[`src/Storage/InMemoryStore.php#L111-L148`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Storage/InMemoryStore.php#L111-L148)

**Перевод комментариев:**
- `snapshot()` (PHPDoc): сериализуемая копия каждой записи, включая TTL, для
  персистентности. Возвращает
  `array<string, array{value: mixed, expiresAt: float|null}>`.
- `restore()` (PHPDoc): заменяет текущее содержимое ранее снятым `snapshot()`.
  Записи, уже истёкшие к моменту запуска, просто не восстанавливаются —
  действует то же правило ленивого истечения, что и везде. Принимает
  `array<string, array{value: mixed, expiresAt: float|null}> $entries`.

`snapshot()` копирует `expiresAt` **как есть** — восстановленный ключ сохраняет
свой абсолютный срок. `restore()` сначала полностью очищает хранилище и heap, а
затем пропускает всё, что уже истекло: снапшот мог пролежать на диске дольше,
чем жил какой-то из ключей. Обратите внимание, что `restore()` кладёт данные
через тот же `put()`, так что heap перестраивается согласованно.

### Реестр каналов

```php
/**
 * Tracks which connections are subscribed to which channels.
 *
 * Keyed by `ClientConnection::id()` - the socket's resource id - rather
 * than by the object, so a channel's subscribers are a plain array and a
 * connection can be removed from every channel without a scan for object
 * identity. The price is that entries must be dropped explicitly when a
 * connection goes (`CacheServer::disconnectClient()` does it): those ids
 * are reused by later connections, so a leftover entry does not merely
 * leak, it eventually points at someone else.
 */
final class ChannelRegistry
{
    /** @var array<string, array<int, ClientConnection>> */
    private array $channels = [];

    public function subscribe(string $channel, ClientConnection $connection): void
    {
        $this->channels[$channel][$connection->id()] = $connection;
    }

    /**
     * Removes $connection from every channel, e.g. when it disconnects.
     */
    public function unsubscribeAll(ClientConnection $connection): void
    {
        foreach (array_keys($this->channels) as $channel) {
            unset($this->channels[$channel][$connection->id()]);

            if ($this->channels[$channel] === []) {
                unset($this->channels[$channel]);
            }
        }
    }

    /** @return list<ClientConnection> */
    public function subscribers(string $channel): array
    {
        return array_values($this->channels[$channel] ?? []);
    }

    public function subscriptionCount(ClientConnection $connection): int
    {
        $count = 0;

        foreach ($this->channels as $subscribers) {
            if (isset($subscribers[$connection->id()])) {
                $count++;
            }
        }

        return $count;
    }
}
```

[`src/PubSub/ChannelRegistry.php#L9-L62`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/PubSub/ChannelRegistry.php#L9-L62)

**Перевод комментариев:**
- `ChannelRegistry` (PHPDoc): отслеживает, какие соединения подписаны на какие
  каналы. Ключ — `ClientConnection::id()`, id ресурса сокета, а не объект,
  поэтому подписчики канала — обычный массив, а соединение можно убрать из всех
  каналов без поиска по идентичности объектов. Цена — записи нужно удалять явно,
  когда соединение уходит (это делает `CacheServer::disconnectClient()`): эти id
  переиспользуются следующими соединениями, поэтому оставшаяся запись не просто
  течёт, а со временем указывает на кого-то другого.
- `$channels` (`@var`): канал → id соединения → соединение.
- `unsubscribeAll()` (PHPDoc): убирает `$connection` из всех каналов, например
  когда оно отключается.
- `subscribers()` (PHPDoc): возвращает `list<ClientConnection>` — подписчиков
  канала (пустой список для неизвестного канала).

Docblock здесь — не украшение, а предупреждение о реальном риске. Ключ — id
сокета, а id переиспользуются операционной системой. Поэтому `unsubscribeAll()`
обязателен при отключении, и он же подчищает пустые каналы, чтобы реестр не рос.

### Рассылка: закодировать один раз

```php
public function handle(Command $command, Store $store, ClientConnection $connection): RespValue
{
    if (count($command->arguments) !== 2) {
        return RespValue::error("ERR wrong number of arguments for 'publish' command");
    }

    [$channel, $message] = $command->arguments;
    $subscribers = $this->channels->subscribers($channel);

    $payload = $this->encoder->encode(RespValue::array([
        RespValue::bulkString('message'),
        RespValue::bulkString($channel),
        RespValue::bulkString($message),
    ]));

    foreach ($subscribers as $subscriber) {
        ($this->deliver)($subscriber, $payload);
    }

    return RespValue::integer(count($subscribers));
}
```

[`src/Command/Handler/PublishCommand.php#L27-L47`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Command/Handler/PublishCommand.php#L27-L47)

Сообщение кодируется **один раз** в `$payload` и рассылается как готовые байты.
Полезная нагрузка — обычный RESP-массив `["message", channel, body]`, то есть
подписчик читает ответ тем же парсером из главы 11. Доставка вынесена в
замыкание `$deliver`: обработчик не знает ни о буферах, ни о соединениях — он
лишь просит сервер доставить байты. Это и позволяет серверу навесить на доставку
жёсткий предел.

### Жёсткий предел для подписчика

```php
/**
 * Pub/Sub delivery is not like answering a command: the bytes are
 * produced by whoever publishes, not by the connection receiving them,
 * so pausing that connection's reads throttles nothing at all. A
 * subscriber that never reads would queue messages until the server
 * ran out of memory - with backpressure "working" the whole time,
 * since its own reads have been paused since the first megabyte.
 *
 * Past the hard limit it is dropped instead. The same line is drawn
 * for the same reason (the reference `client-output-buffer-limit pubsub`
 * setting), while leaving ordinary clients
 * unlimited: what those queue is bounded by what they asked for.
 */
private function deliverToSubscriber(ClientConnection $subscriber, string $payload): void
{
    $this->queueForWrite($subscriber, $payload);

    if (
        $subscriber->state() === ConnectionState::Closed
        || $subscriber->writeBuffer()->length() <= $this->hardSubscriberWriteBufferBytes
    ) {
        return;
    }

    $this->metrics->recordError();
    $this->disconnectClient($subscriber);
}
```

[`src/Server/CacheServer.php#L689-L715`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L689-L715)

**Перевод комментариев:**
- `deliverToSubscriber()` (PHPDoc): доставка Pub/Sub не похожа на ответ на
  команду: байты производит тот, кто публикует, а не соединение, которое их
  получает, поэтому пауза чтения этого соединения ничего не сдерживает.
  Подписчик, который никогда не читает, копил бы сообщения, пока у сервера не
  кончится память, — и всё это время обратное давление «работало» бы, ведь его
  собственное чтение на паузе с первого мегабайта. За жёстким пределом он
  отключается. Та же граница проведена по той же причине (как настройка
  `client-output-buffer-limit pubsub` в эталоне), но обычные клиенты остаются
  без предела: их очередь ограничена тем, что они запросили.

Доставка сначала идёт обычным путём (`queueForWrite` — тот же буфер и то же
обратное давление, что и у ответов), а затем проверяется жёсткий предел.
Проверка на `Closed` нужна потому, что `queueForWrite` сам мог только что
отключить подписчика при неудачной записи, — писать в закрытый сокет нельзя.
Порог — свойство **подписчика**, а не публикующего: наказывать публикацию за
одного медленного получателя означало бы наказать всех остальных.

### Таймеры цикла: истечение, простой, снапшоты

```php
// Active expiration: expired keys are also removed on a timer,
// instead of only being noticed lazily the next time they are read.
$this->eventLoop->every($expirationSweepIntervalSeconds, function (): void {
    $this->metrics->recordExpiredKeys($this->store->sweepExpired());
});

if ($this->idleTimeoutSeconds !== null) {
    $this->eventLoop->every($this->idleTimeoutSeconds, $this->closeIdleConnections(...));
}

if ($this->snapshots !== null && $snapshotIntervalSeconds !== null) {
    $this->eventLoop->every($snapshotIntervalSeconds, $this->saveSnapshot(...));
}
```

[`src/Server/CacheServer.php#L207-L219`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Server/CacheServer.php#L207-L219)

**Перевод комментариев:**
- `// Active expiration: expired keys are also removed on a timer,` — активное
  истечение: истёкшие ключи также удаляются по таймеру, а не только замечаются
  лениво при следующем чтении.

Три независимых таймера, и все — «best effort»: цикл может задержать обратный
вызов, занятый событиями сокетов, но не выполнит его раньше срока. Период
прохода по истёкшим ключам по умолчанию — 1 секунда. Заметьте, что таймер
простоя (`idleTimeoutSeconds`) служит и порогом, и периодом проверки — об этом
решении ниже.

### Сохранение снапшота: временный файл и `rename`

```php
public function save(InMemoryStore $store): void
{
    $tmpPath = sprintf('%s.%d.tmp', $this->path, getmypid());

    $bytes = @file_put_contents($tmpPath, serialize($store->snapshot()));

    if ($bytes === false) {
        throw new RuntimeException(sprintf('Failed to write snapshot to %s.', $tmpPath));
    }

    if (!@rename($tmpPath, $this->path)) {
        @unlink($tmpPath);

        throw new RuntimeException(sprintf('Failed to move snapshot into place at %s.', $this->path));
    }
}
```

[`src/Persistence/SnapshotStore.php#L39-L54`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Persistence/SnapshotStore.php#L39-L54)

Четыре строки, три гарантии. Временный путь уникален по pid, поэтому
параллельные писатели не мешают друг другу. `rename()` атомарен, поэтому
читатель никогда не увидит полуфайл. Ошибка **бросается**, а не глотается:
потерянный снапшот должен быть заметен, а не молча отсутствовать.

### Загрузка снапшота: файл как недоверенный ввод

```php
/**
 * Loads the store's contents from a previously saved snapshot, if one
 * exists and is readable. A missing or unreadable file is not an
 * error - it just means there is nothing to restore yet.
 */
public function load(InMemoryStore $store): void
{
    if (!is_file($this->path)) {
        return;
    }

    $contents = file_get_contents($this->path);

    if ($contents === false || $contents === '') {
        return;
    }

    // No classes, whatever the file says. unserialize() otherwise
    // instantiates whatever class names it finds and runs their
    // __wakeup()/__destruct() - the classic way a writable file on disk
    // turns into code execution. A snapshot only ever holds arrays,
    // strings and numbers, so nothing is given up by refusing the rest.
    $entries = @unserialize($contents, ['allowed_classes' => false]);

    if (!is_array($entries)) {
        return;
    }

    /** @var array<string, array{value: mixed, expiresAt: float|null}> $entries */
    $store->restore($entries);
}
```

[`src/Persistence/SnapshotStore.php#L56-L86`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Persistence/SnapshotStore.php#L56-L86)

**Перевод комментариев:**
- `load()` (PHPDoc): загружает содержимое хранилища из ранее сохранённого
  снапшота, если он существует и читаем. Отсутствующий или нечитаемый файл не
  ошибка — это просто значит, что восстанавливать пока нечего.
- `// No classes, whatever the file says. unserialize() otherwise` — никаких
  классов, что бы ни говорил файл. Иначе `unserialize()` создаёт экземпляры всех
  найденных имён классов и запускает их `__wakeup()` /`__destruct()` —
  классический путь от записываемого файла на диске к выполнению кода. Снапшот
  всегда содержит только массивы, строки и числа, поэтому отказ от остального
  ничего не теряет.
- `$entries` (`@var`): загруженные записи в форме
  `array<string, array{value: mixed, expiresAt: float|null}>`.

Каждый «плохой» вход здесь — не ошибка, а **no-op**: файла нет, он пуст, он
нечитаем, `unserialize` вернул не массив — всё это значит «восстанавливать
нечего». Это осознанная асимметрия с `save()`: не суметь сохранить — проблема,
которую надо сообщить; не суметь загрузить — нормальный первый запуск.

### Снапшот в форкнутом ребёнке

```php
public function save(InMemoryStore $store): bool
{
    if ($this->isChildStillWriting()) {
        return false;
    }

    $pid = pcntl_fork();

    if ($pid === -1) {
        $this->snapshots->save($store);

        return true;
    }

    if ($pid > 0) {
        // Parent: the child writes the snapshot; it is reaped through the
        // SIGCHLD handler the server installs, or by the check above.
        $this->childPid = $pid;

        return true;
    }

    // Child: write the snapshot and exit immediately, never returning to
    // the caller (which would keep running the parent's event loop). A
    // failed write exits non-zero rather than reporting the success it
    // did not have - the exit status is the only thing the child can
    // still say about it.
    try {
        $this->snapshots->save($store);
    } catch (Throwable $exception) {
        $this->logger->error(sprintf('Snapshot failed: %s', $exception->getMessage()));

        exit(1);
    }

    exit(0);
}
```

[`src/Persistence/ForkingSnapshotWorker.php#L58-L94`](https://github.com/Researcher86/php-mini-cache/blob/58386550902da7bf9d65c491b9886b37981b55a2/src/Persistence/ForkingSnapshotWorker.php#L58-L94)

**Перевод комментариев:**
- `// Parent: the child writes the snapshot; it is reaped through the` —
  родитель: снимок пишет ребёнок; его «пожинают» через обработчик `SIGCHLD`,
  который ставит сервер, или через проверку выше.
- `// Child: write the snapshot and exit immediately, never returning to` —
  ребёнок: пишет снимок и сразу завершается, никогда не возвращаясь к
  вызывающему (иначе он продолжил бы выполнять цикл событий родителя). Провал
  записи завершается с ненулевым кодом, а не сообщает об успехе, которого не
  было, — код выхода — единственное, чем ребёнок ещё может об этом сказать.

`isChildStillWriting()` — это и «пропустить тик, если снапшот уже пишется», и
«пожать закончившегося ребёнка». Ребёнок **обязательно** завершается через
`exit()`: если бы он вернулся из `save()`, он продолжил бы выполнять цикл
событий родителя — а это уже два сервера на одном порту. Провал записи приводит
к ненулевому коду выхода: единственный канал, по которому ребёнок может сообщить
о неудаче, — его статус.

## 4. Эксперимент

Поднимаем контейнер и сервер (как в главе 10):

```bash
make build install
make run-server
```

### TTL: ключ исчезает без чтения

```bash
docker compose exec php php examples/ttl.php
```

```text
SET session abc EX 2
-> OK

GET session (immediately)
-> abc

Waiting 3 seconds for the TTL to pass...

GET session (after the TTL)
-> (nil)
```

Ключ создан с `EX 2`, прочитан сразу (ещё жив), затем сценарий спит 3 секунды и
читает снова. Что здесь важно: между двумя `GET` ключ **никто не читал**, и всё
равно он исчез — это работает активное истечение по таймеру, а не ленивая
проверка при чтении. Ленивое истечение тоже включается (второй `GET` бы удалил
ключ, даже если бы таймер не сработал), но в этом сценарии его достаточно, чтобы
увидеть именно таймер.

### Pub/Sub: сообщение доходит до другого соединения

```bash
docker compose exec php php examples/pubsub.php
```

```text
[subscriber] subscribed, now watching 1 channel(s)
[publisher] PUBLISH news "first message"
[publisher] -> delivered to 1 subscriber(s)
[subscriber] message on news: first message
[publisher] PUBLISH news "second message"
[subscriber] message on news: second message
[publisher] -> delivered to 1 subscriber(s)
```

Скрипт форкает ребёнка-подписчика и публикует из родителя — на одном соединении
двух ролей не совместить. Обратите внимание на порядок: `PUBLISH` возвращает
число подписчиков (`1`) **до** того, как ребёнок напечатает сообщение. Это не
гонка, а следствие устройства: родитель видит свой ответ сразу после рассылки, а
ребёнок получает данные, когда дочитает свой сокет. Ноль подписчиков вернул бы
`0` — и сообщение исчезло бы навсегда.

### Сколько стоит веерная рассылка

```bash
docker compose exec php php benchmarks/pubsub-fanout.php
```

```text
Subscribers:  20
Messages:     50
Fan-out total 1000 deliveries in 0.023s
Deliveries/s: 44312
```

50 сообщений × 20 подписчиков = 1 000 доставок. Основная стоимость — не
кодирование сообщения (оно одно на всех), а обход 20 сокетов на каждый
`PUBLISH`; отсюда и формулировка «насколько быстро один однопоточный цикл
касается 20 сокетов».

### Снапшот: данные переживают перезапуск

Здесь нужен отдельный сервер со снапшотом (чтобы не трогать основной на 6380).
Три файла: два клиента и сценарий-оркестратор.

`/tmp/snapshot-put.php` — пишет три ключа:

```php
<?php

declare(strict_types=1);

use PhpMiniCache\Sdk\CacheClient;

require '/app/vendor/autoload.php';

$client = new CacheClient('127.0.0.1', 6381);

$client->set('permanent', 'Tanat');
$client->set('session', 'abc', ttlSeconds: 60);
$client->set('shortlived', 'poof', ttlSeconds: 1);

$client->close();

echo "SET permanent Tanat\n";
echo "SET session abc EX 60\n";
echo "SET shortlived poof EX 1\n";
```

`/tmp/snapshot-get.php` — читает их обратно:

```php
<?php

declare(strict_types=1);

use PhpMiniCache\Sdk\CacheClient;

require '/app/vendor/autoload.php';

$client = new CacheClient('127.0.0.1', 6381);

foreach (['permanent', 'session', 'shortlived'] as $key) {
    printf("GET %-11s -> %s\n", $key, $client->get($key) ?? '(nil)');
}

$client->close();
```

`/tmp/snapshot-demo.sh` — поднимает сервер, пишет, ждёт, гасит по `SIGTERM`
(финальный снапшот), поднимает заново и читает:

```bash
#!/usr/bin/env bash
set -euo pipefail

SNAP=/tmp/mini-cache.snapshot
export CACHE_PORT=6381
export CACHE_SNAPSHOT_PATH=$SNAP

rm -f "$SNAP" "$SNAP".*.tmp

echo "== 1. start a server with snapshots configured =="
php bin/server.php >/tmp/snapshot-server-1.log 2>&1 &
PID=$!
sleep 1

echo "== 2. write three keys, one of them short-lived =="
php /tmp/snapshot-put.php

echo "== 3. wait past the 1s TTL, then SIGTERM for the final snapshot =="
sleep 2
kill -TERM $PID
wait $PID
echo "on disk now:"
ls -l "$SNAP"

echo "== 4. start a fresh server and read everything back =="
php bin/server.php >/tmp/snapshot-server-2.log 2>&1 &
PID=$!
sleep 1
php /tmp/snapshot-get.php
kill -TERM $PID
wait $PID

rm -f "$SNAP"
```

Скопируйте файлы в контейнер и запустите:

```bash
docker compose cp /tmp/snapshot-put.php php:/tmp/snapshot-put.php
docker compose cp /tmp/snapshot-get.php php:/tmp/snapshot-get.php
docker compose cp /tmp/snapshot-demo.sh php:/tmp/snapshot-demo.sh
docker compose exec php bash /tmp/snapshot-demo.sh
```

```text
== 1. start a server with snapshots configured ==
== 2. write three keys, one of them short-lived ==
SET permanent Tanat
SET session abc EX 60
SET shortlived poof EX 1
== 3. wait past the 1s TTL, then SIGTERM for the final snapshot ==
on disk now:
-rw-r--r-- 1 root root 148 Oct 16 07:34 /tmp/mini-cache.snapshot
== 4. start a fresh server and read everything back ==
GET permanent   -> Tanat
GET session     -> abc
GET shortlived  -> (nil)
```

Разбор:

1. **Первый сервер настроен на снапшот** через `CACHE_SNAPSHOT_PATH`; в этот
   момент файла нет — загрузка это просто no-op.
2. **Три ключа:** вечный (`permanent`), с часовым TTL (`session`) и с
   одномоментным (`shortlived`, `EX 1`).
3. **После `sleep 2` однократный ключ истёк**, и активный проход его убрал.
   Затем `SIGTERM` — сервер корректно завершается и пишет **финальный** снапшот
   (148 байт).
4. **Новый процесс** поднимается, загружает снапшот и отдаёт: `permanent` и
   `session` на месте (TTL `session` пережил перезапуск), `shortlived` —
   `(nil)`. Даже если бы запись не успел убрать проход, `restore()` отбросил бы
   её: срок уже прошёл.

## 5. Отказы

| Что ломается                                         | Чем обнаруживается                                      | Что происходит                                                     |
| ---------------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------ |
| Истёкший ключ читают                                 | `entryOrNull()`                                         | ленивое удаление; `get` → `(nil)`                                  |
| Истёкший ключ никто не читает                        | таймер `every(1.0)` → `sweepExpired()`                  | удаляется проходом; счётчик `expired_keys`                         |
| Ключ перезаписан с новым TTL, старая запись в heap   | `entry->expiresAt === $expiresAt`                       | запись распознана как отжившая и игнорируется                      |
| Горячий ключ перезаписывается с TTL в цикле          | —                                                       | heap растёт O(TTL-перезаписей); ограничен их сроками               |
| `INCR` по ключу с TTL                                | `setKeepingTtl()`                                       | TTL сохраняется, а не сбрасывается                                 |
| `INCR` по отсутствующему ключу                       | `setKeepingTtl()` → `false`                             | пишется как новый, без TTL                                         |
| Подписчик не читает и копит сообщения                | `hardSubscriberWriteBufferBytes`                        | при превышении (64 МиБ) подписчик **отключается**                  |
| Публикация в уже закрытое соединение                 | `queueForWrite` видит `Closed`                          | no-op, а не падение на `fwrite()`                                  |
| Подписчик отключился                                 | `disconnectClient` → `unsubscribeAll`                   | id не останется «смотрящим» на чужое соединение                    |
| Ноль подписчиков                                     | `count($subscribers) === 0`                             | `PUBLISH` → `0`; сообщение потеряно (нет истории)                  |
| Процесс умер посреди записи снапшота                 | `tmp` + `rename`                                        | на месте остаётся прежний снапшот; полуфайла нет                   |
| Два ребёнка делят временный путь                     | pid в имени `*.tmp`                                     | не мешают друг другу у `rename`                                    |
| Снапшот подменён/повреждён                           | `unserialize(..., allowed_classes: false)` + `is_array` | игнорируется; код из файла не исполняется                          |
| Предыдущий снапшот ещё пишется                       | `isChildStillWriting()`                                 | новый тик **пропускается**                                         |
| Ребёнок-писатель не уложился в бюджет при выключении | `awaitCurrentSnapshot(5.0)`                             | `SIGKILL` ребёнку, чтобы не перезаписать новое старым              |
| `fork()` не удался                                   | `pcntl_fork() === -1`                                   | синхронное сохранение; корректно, но блокирует цикл                |
| `SIGKILL` / пропажа питания                          | —                                                       | финального снапшота нет; записи после последнего снапшота потеряны |

Отдельно стоит подчеркнуть две границы, а не строки таблицы.

**TTL — это не событие цикла.** Истечение не ставит в очередь «событие
истечения»; оно либо лениво замечается при обращении, либо попадает в следующий
проход. Значит, между фактическим сроком и удалением может пройти до одного
интервала. Это записано в модели отказов проекта: таймеры «best-effort», не
«точно в срок».

**Снапшот — это точка во времени, а не журнал.** Он не спасает записи, сделанные
после последнего сохранения. Восстановление — это «столько, сколько было на
момент снапшота», и ничего больше.

## 6. Как это проверено

TTL — `tests/Storage/InMemoryStoreTest.php`:
`testAValueWithATtlIsAvailableBeforeItExpires`,
`testAValueWithATtlIsGoneOnceItExpires`,
`testDeleteReturnsFalseForAnExpiredKey`, `testSetWithoutATtlNeverExpires`,
`testOverwritingAKeyReplacesItsPreviousTtl`,
`testSweepExpiredRemovesOnlyExpiredEntriesAndReportsHowMany`. Отжившие записи
heap — отдельные тесты: `testSweepSkipsAStaleHeapEntryAfterTheKeyWasDeleted`,
`testOverwritingAnExpiringKeyDoesNotLetItsOldTtlKillTheNewValue`,
`testAKeyWrittenAgainAfterDeletionSurvivesItsPreviousTtl`,
`testKeysThatCameAndWentLeaveNoBookkeepingBehind`. И поведение `INCR`:
`testSetKeepingTtlRewritesTheValueOnTheSameExpiration`,
`testSetKeepingTtlReportsAKeyThatIsNotThereAndWritesNothing`. Плюс
`tests/Storage/StoredValueTest.php` — два теста на саму проверку истечения.

Pub/Sub — `tests/PubSub/ChannelRegistryTest.php`:
`testSubscribersReturnsEveryoneSubscribedToAChannel`,
`testSubscribersReturnsAnEmptyListForAnUnknownChannel`,
`testSubscribingTwiceToTheSameChannelDoesNotDuplicateTheSubscriber`,
`testSubscriptionCountTracksHowManyChannelsAConnectionJoined`,
`testUnsubscribeAllRemovesAConnectionFromEveryChannel`. И на уровне сервера:
`CacheServerTest::testASubscriberReceivesAPublishedMessage`,
`testDisconnectingASubscriberRemovesItFromItsChannels`,
`testPublishingToAConnectionThatIsAlreadyClosedIsANoOp`,
`testASubscriberThatNeverReadsIsDroppedInsteadOfQueuedForever`,
`testAnIdleTimedOutSubscriberIsUnsubscribedFromItsChannels`.

Снапшоты — `tests/Persistence/SnapshotStoreTest.php`:
`testSavedValuesAreRestoredIntoAnotherStore`, `testATtlSurvivesTheRoundTrip`,
`testSaveOverwritesAPreviousSnapshot`,
`testSavingIgnoresATemporaryFileLeftBehindByAnotherWriter`,
`testACorruptSnapshotFileIsIgnoredInsteadOfCrashing`,
`testLoadIntoAFreshStoreIsANoOpWhenNoSnapshotExists`. Форк —
`tests/Persistence/ForkingSnapshotWorkerTest.php`:
`testTheForkedChildWritesALoadableSnapshot`,
`testASecondSaveIsSkippedWhileTheFirstChildIsStillWriting`,
`testAChildThatCannotWriteTheSnapshotExitsNonZeroAndSaysWhy`. И на уровне
сервера: `CacheServerTest::testDataSavedByOneServerIsLoadedByTheNext`,
`testPeriodicSnapshotsSaveWithoutBeingAskedExplicitly`,
`testAGracefulShutdownSnapshotsWhatWasWrittenSinceTheLastOne`,
`testTheFinalSnapshotIsNotOvertakenByOneAlreadyBeingWritten`.

Персистентность, истечение и подписки на живом сервере — это `examples/ttl.php`,
`examples/pubsub.php` и эксперимент со снапшотом выше. Обратите внимание на
`testKeysThatCameAndWentLeaveNoBookkeepingBehind`: он проверяет не значение
ключа, а **отсутствие мусора** в служебных структурах после жизненного цикла
ключей. Именно на такое — «структура выросла и не уменьшилась» — обычные тесты
на значения не реагируют.

Запускаем:

```text
$ vendor/bin/phpunit tests/Storage tests/PubSub tests/Persistence
OK (36 tests, 85 assertions)

$ vendor/bin/phpunit tests/Server
OK (47 tests, 1237 assertions)

$ vendor/bin/phpunit
OK (261 tests, 1759 assertions)
```

## 7. Цифры

Живые прогоны этой главы (Docker, PHP 8.5.11):

| Что                                    | Значение                                                 |
| -------------------------------------- | -------------------------------------------------------- |
| Ключ `EX 2` через 3 секунды без чтения | `(nil)` — исчез по таймеру                               |
| `PUBLISH` при одном подписчике         | доставлено 1, сообщение напечатано подписчиком           |
| 20 подписчиков × 50 сообщений          | 1 000 доставок за 0.023 с                                |
| Скорость веерной рассылки              | 44 312 доставок/с                                        |
| Размер снапшота в эксперименте         | 148 байт (три ключа)                                     |
| После перезапуска                      | `permanent` и `session` на месте, `shortlived` → `(nil)` |
| Тесты TTL/PubSub/снапшотов             | 36 тестов, 85 утверждений                                |
| Тесты сервера                          | 47 тестов, 1237 утверждений                              |
| Весь набор проекта                     | 261 тест, 1759 утверждений                               |

Числа, которые проект приводит в `docs/BENCHMARKS.md` для веерной рассылки: 20
подписчиков × 50 сообщений → 1 000 доставок, 33 583 доставки/с. Живой прогон дал
44 312 — на этом масштабе разница объясняется в основном шумом и тем, что разные
контейнерные нагрузки измерялись в разное время; порядок величины совпадает. Обе
цифры полезны: документация показывает, каким проект измерил механизм, а живой
прогон — что его можно воспроизвести.

Несколько чисел, задающих «цену честности» трёх механизмов:

- **период активного прохода** — 1 секунда по умолчанию; истёкший ключ живёт «в
  среднем ещё полсекунды», а не исчезает в момент срока;
- **порог отключения подписчика** — 64 МиБ буфера записи (обычный клиент
  ставится на паузу уже на 16 МиБ, а отключается лишь при неудачной записи);
- **бюджет ожидания финального снапшота** — 5 секунд; не уложившийся ребёнок
  убивается, чтобы новое состояние не было перезаписано старым.

## 8. Упражнения

1. **Объясните.** `sweepExpired()` сперва смотрит на вершину heap и сравнивает
   `$entry->expiresAt === $expiresAt`. Почему этого одного сравнения достаточно,
   чтобы отличить живую запись от отжившей после (а) удаления ключа, (б)
   перезаписи с новым TTL, (в) перезаписи без TTL? Разберите каждый случай
   отдельно.
2. **Найдите ошибку.** В `entryOrNull()` убрали проверку `isExpired()` и просто
   вернули запись. Какой тест из `InMemoryStoreTest` упадёт первым и что именно
   он проверяет? А что сломается в сценарии `examples/ttl.php` — ключ всё равно
   исчезнет?
3. **Спроектируйте.** Что произойдёт, если убрать сравнение
   `entry->expiresAt === $expiresAt` и удалять ключ при первом же истечении
   любой записи heap? Приведите последовательность команд, после которой живой
   ключ будет удалён раньше срока.
4. **Посчитайте.** `PUBLISH` кодирует сообщение один раз и рассылает готовый
   буфер. Сколько раз сообщение было бы закодировано в наивной реализации,
   которая зовёт обработчик для каждого подписчика? Как это масштабируется при
   20 подписчиках и при 1 000?
5. **Сравните.** `deliverToSubscriber()` отключает подписчика при переполнении,
   тогда как обычный медленный клиент лишь ставится на паузу. Объясните, почему
   пауза **не** спасает подписчика, и почему нельзя вместо отключения просто
   замедлить публикующего.
6. **Разберите аудит.** `SnapshotStore::load()` читает файл через
   `unserialize(..., allowed_classes: false)` и молча игнорирует любой сбой.
   Почему «не суметь прочитать» здесь не ошибка, а «не суметь записать» —
   ошибка? И от какого конкретного класса атак защищает
   `allowed_classes: false`?
7. **Спроектируйте отказ.** Снапшот берётся раз в 60 секунд, а процесс убит
   через 30 секунд после последнего. Какие записи потеряны и почему это
   принципиально неустранимо без журнала с опережающей записью (WAL)? Что именно
   должен был бы хранить WAL, чтобы этого избежать?

## Что дальше

- **Глава 10** — соединения и буферы: `WriteBuffer`, на который опирается и
  обратное давление обычных ответов, и жёсткий предел подписчиков.
- **Глава 2** — `fork` и copy-on-write: почему ребёнок-писатель снапшота
  получает снимок состояния бесплатно.
- **Глава 13** — HTTP: инкрементальный разбор и фрейминг в
  `php-mini-http-server` и `php-mini-search` — та же задача «байты → сообщение»,
  но для другого протокола, решённая дважды и по-разному.
- **Глава 18** — WAL, `fsync` и восстановление после сбоя: полноценный ответ на
  вопрос, который снапшот оставляет открытым.
