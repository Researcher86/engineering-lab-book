# Глава 6. Event-Driven Architecture: outbox, повторы и граница идемпотентности

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: CQRS. Дальше: DDD и bounded contexts (глава 7).

Событийная архитектура начинается не с брокера. Она начинается с вопроса: что
произойдёт, если после записи зачисления процесс упадёт до отправки события?
Если ответ «зачисление есть, а Billing никогда не узнает», то событийная
архитектура построена на честном слове. Outbox решает именно эту проблему:
запись агрегата и запись события происходят в одной транзакции, а доставка —
отдельный шаг, который можно повторить.

В этой главе outbox, диспетчер, повторные попытки, dead letter и
идемпотентные слушатели рассматриваются вместе. По отдельности каждый механизм
выглядит простым; вместе они дают at-least-once доставку и **эффективно**
однократный side effect — но только при определённых условиях. Глава явно
показывает, где эта гарантия заканчивается.

---

## 1. Проблема

Представим наивную событийную реализацию: обработчик сохраняет зачисление, затем
вызывает `eventBus->publish()`. Если публикация упала, зачисление осталось, а
событие потеряно. Если обработчик сохранил зачисление, опубликовал событие и
упал до ответа клиенту, клиент повторит команду и создаст второе зачисление.
Если событие доставлено дважды, Billing спишет деньги дважды.

Есть три отдельные проблемы:

1. **Атомарность записи и события.** Агрегат и сообщение должны стать
   видимыми вместе или не стать вовсе.
2. **Доставка.** После коммита событие нужно отдать слушателям, даже если
   процесс перезапустился.
3. **Повторный эффект.** At-least-once доставка означает, что слушатель может
   увидеть одно событие больше одного раза.

Outbox отвечает на первую и вторую, idempotent listener — на третью. Вместе они
не превращают систему в exactly-once. Они дают воспроизводимый компромисс,
который можно проверить.

---

## 2. Устройство

### 2.1. Конверт события

Событие — это immutable value object с id, именем, aggregate id, payload,
временем и версией. Payload — массив скаляров, а не объект домена.

```php
/**
 * The immutable message that crosses a boundary.
 *
 * The payload is an array of scalar values on purpose: an integration event is
 * a published language, not a reference to a domain object. A subscriber in
 * another bounded context must not be able to mutate the producer's aggregate.
 */
final readonly class EventEnvelope
{
    /**
     * @param array<string, int|string|bool|null> $payload
     */
    public function __construct(
        public string $id,
        public string $name,
        public string $aggregateId,
        public array $payload,
        public string $occurredAt,
        public int $version = 1,
    ) {
        if (trim($this->id) === '') {
            throw new InvalidArgumentException('Event id cannot be empty');
        }

        if (trim($this->name) === '') {
            throw new InvalidArgumentException('Event name cannot be empty');
        }

        if ($this->version < 1) {
            throw new InvalidArgumentException('Event version must be positive');
        }
    }
```

[`src/Eventing/EventEnvelope.php` L9-L40](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Eventing/EventEnvelope.php#L9-L40)

Скалярный payload — это граница опубликованного языка. Подписчик не получает
ссылку на агрегат и не может его изменить. Он также не получает объект
`Money` или `Enrollment`: эти типы принадлежат контексту-источнику. Если
подписчику нужны деньги, он читает `price_cents` и `currency` и создаёт своё
значение.

### 2.2. Outbox как порт

Порт outbox — пять операций: записать, получить pending, отметить доставленным,
отметить неудачу, посчитать pending и dead letters.

```php
/** A durable (or in-memory) queue between an aggregate write and delivery. */
interface Outbox
{
    public function record(EventEnvelope $event): void;

    /**
     * @return list<OutboxMessage>
     */
    public function pending(int $limit): array;

    public function markDelivered(string $eventId): void;

    public function markFailed(string $eventId, string $error, bool $deadLetter): void;

    public function countPending(): int;

    public function countDeadLettered(): int;
}
```

[`src/Eventing/Outbox.php` L7-L24](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Eventing/Outbox.php#L7-L24)

SQLite-реализация создаёт реальную таблицу с автоинкрементной позицией. Позиция
задаёт порядок, в котором сообщения выбираются:

```php
/**
 * A real outbox table in the same database as the document store.
 *
 * The outbox row is written inside the same transaction as the aggregate, so
 * a crash cannot leave one without the other. Delivery is a separate step and
 * intentionally at-least-once.
 */
final class SqliteOutbox implements Outbox
{
    public function __construct(
        private readonly PDO $pdo,
        private readonly Counters $counters,
    ) {
        $this->pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $this->pdo->exec(
            'CREATE TABLE IF NOT EXISTS outbox (
                position INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                payload TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT \'pending\',
                last_error TEXT NULL
            )',
        );
```

[`src/Persistence/SqliteOutbox.php` L13-L39](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Persistence/SqliteOutbox.php#L13-L39)

Запись события — обычный `INSERT`; транзакцию открывает вызывающий код.

```php
    public function record(EventEnvelope $event): void
    {
        $statement = $this->pdo->prepare(
            'INSERT INTO outbox (id, name, aggregate_id, payload, occurred_at)
             VALUES (:id, :name, :aggregate_id, :payload, :occurred_at)',
        );
        $statement->execute([
            'id' => $event->id,
            'name' => $event->name,
            'aggregate_id' => $event->aggregateId,
            'payload' => json_encode($event->payload, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
            'occurred_at' => $event->occurredAt,
        ]);
        ++$this->counters->sqlQueries;
    }
```

[`src/Persistence/SqliteOutbox.php` L42-L56](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Persistence/SqliteOutbox.php#L42-L56)

В основном сценарии `record()` вызывается внутри `UnitOfWork::transactional()`.
`CatalogService::publish()` открывает транзакцию явно; `reserveSeat()` и
`releaseSeat()` полагаются на транзакцию вызывающего обработчика. Это важная
деталь: outbox не открывает транзакцию сам. Если вызвать `reserveSeat()` вне
юнита работы, запись агрегата и запись события могут стать отдельными
операциями. В сценарии зачисления внешний `RequestEnrollmentHandler` как раз
создаёт эту границу.

### 2.3. Диспетчер: доставка и повторы

`OutboxDispatcher::dispatch()` берёт пачку pending-сообщений и отдаёт каждое в
шину. Событие считается доставленным только после того, как все слушатели
вернулись без исключения. При исключении увеличивается число попыток; после
`maxAttempts` сообщение становится dead.

```php
/**
 * Drains the outbox once, one event at a time.
 *
 * Delivery is at-least-once. An event is marked delivered only after every
 * listener returned; a failure increments the attempt counter and either
 * leaves the event pending or moves it to the dead-letter state.
 */
final readonly class OutboxDispatcher
{
    public function __construct(
        private Outbox $outbox,
        private EventBus $bus,
        private int $maxAttempts = 3,
    ) {
        if ($this->maxAttempts < 1) {
            throw new \InvalidArgumentException('maxAttempts must be at least 1');
        }
    }

    public function dispatch(int $limit = 100): DispatchReport
    {
        $report = new DispatchReport();

        foreach ($this->outbox->pending($limit) as $message) {
            try {
                $report->handled += $this->bus->publish($message->event);
                $this->outbox->markDelivered($message->id);
                ++$report->delivered;
            } catch (Throwable $exception) {
                $attempts = $message->attempts + 1;
                $deadLetter = $attempts >= $this->maxAttempts;
                $this->outbox->markFailed($message->id, $exception->getMessage(), $deadLetter);
                ++$report->failed;

                if ($deadLetter) {
                    ++$report->deadLettered;
                }
            }
        }

        return $report;
    }
}
```

[`src/Eventing/OutboxDispatcher.php` L9-L51](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Eventing/OutboxDispatcher.php#L9-L51)

Здесь есть тонкость: `pending($limit)` возвращает снимок на начало прохода.
События, созданные слушателями во время этого прохода, попадут в следующий
вызов. Поэтому `LabRuntime::pump()` вызывает диспетчер в цикле, пока outbox не
опустеет.

```php
    public function pump(int $limit = 100, int $maxRounds = 100): \ArchitectureLab\Eventing\DispatchReport
    {
        $total = new \ArchitectureLab\Eventing\DispatchReport();

        for ($round = 0; $round < $maxRounds; ++$round) {
            if ($this->outbox->countPending() === 0) {
                break;
            }

            $report = $this->dispatcher->dispatch($limit);
            $total->add($report);

            if ($report->delivered === 0 && $report->failed === 0) {
                break;
            }
        }

        return $total;
    }
```

[`src/Runtime/LabRuntime.php` L304-L322](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Runtime/LabRuntime.php#L304-L322)

`maxRounds = 100` — предохранитель от бесконечного цикла, а не часть бизнес-
логики. Если событие продолжает порождать события, сценарий остановится и
вернёт отчёт, а не зависнет.

### 2.4. Идемпотентный слушатель

`IdempotentListener` оборачивает внутренний listener. Перед вызовом он
проверяет ключ `(listener, event id)`, после успеха — записывает его.

```php
/**
 * At-least-once delivery becomes effectively-once side effects here.
 *
 * The wrapper checks a key before executing the inner listener and records the
 * key after success. If a later listener in the same bus fails, the dispatcher
 * retries the whole event; already-successful listeners are skipped.
 */
final class IdempotentListener implements EventListener
{
    public function __construct(
        private readonly string $name,
        private readonly EventListener $inner,
        private readonly ProcessedEventStore $processed,
        private readonly Counters $counters,
    ) {
    }

    public function handle(EventEnvelope $event): void
    {
        if ($this->processed->has($this->name, $event->id)) {
            ++$this->counters->handlerSkips;

            return;
        }

        $this->inner->handle($event);
        $this->processed->mark($this->name, $event->id);
        ++$this->counters->eventHandlers;
    }
}
```

[`src/Eventing/IdempotentListener.php` L9-L38](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Eventing/IdempotentListener.php#L9-L38)

Ключ включает имя слушателя, поэтому два разных слушателя могут обработать одно
событие независимо. Если третий слушатель в той же шине падает, диспетчер
повторит всё событие; первые два уже записали ключи и будут пропущены. Это
работает, пока side effect и `mark()` не разъедутся.

**Честное ограничение:** `mark()` вызывается после `inner->handle()`. Если
процесс упадёт между побочным эффектом и записью ключа, при повторе эффект
случится снова. Это не exactly-once. Чтобы закрыть окно, side effect и отметка
должны быть одной транзакцией (например, запись в ту же базу), либо сам эффект
должен быть идемпотентным по внешнему ключу. В лаборатории это окно не закрыто,
и книга не делает вид, что закрыто.

### 2.5. Шина синхронная

`InMemoryEventBus` выполняет слушателей в процессе вызывающего. Он не является
брокером и не притворяется им.

```php
/**
 * A synchronous in-process bus.
 *
 * The bus is deliberately not an async runtime. It executes handlers in the
 * caller's process and propagates their exceptions; the outbox dispatcher is
 * responsible for retrying a failed delivery later.
 */
final class InMemoryEventBus implements EventBus
{
    /** @var array<string, list<EventListener>> */
    private array $listeners = [];

    public function __construct(
        private readonly Counters $counters,
    ) {
    }

    public function subscribe(string $eventName, EventListener $listener): void
    {
        $this->listeners[$eventName][] = $listener;
    }

    public function publish(EventEnvelope $event): int
    {
        $this->counters->eventsPublished++;
        $listeners = $this->listeners[$event->name] ?? [];

        foreach ($listeners as $listener) {
            $listener->handle($event);
        }

        return count($listeners);
    }
}
```

[`src/Eventing/InMemoryEventBus.php` L9-L42](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Eventing/InMemoryEventBus.php#L9-L42)

Синхронность означает: медленный слушатель задерживает диспетчер, а исключение
одного слушателя отменяет доставку остальным в этом проходе. Диспетчер поймает
исключение и повторит событие; уже успешные слушатели будут пропущены по ключу.
Это простая и проверяемая модель, но не модель распределённой системы.

### 2.6. Цепочка реакции

В событийном режиме Billing реагирует на `enrollment.requested`, а Enrollment —
на результат платежа. `PaymentResultListener` подтверждает или отменяет
зачисление и освобождает место при отказе:

```php
    public function handle(EventEnvelope $event): void
    {
        if ($event->name !== EventNames::PAYMENT_SUCCEEDED && $event->name !== EventNames::PAYMENT_FAILED) {
            return;
        }

        $this->unitOfWork->transactional(function () use ($event): void {
            $enrollment = $this->enrollments->get(new EnrollmentId((string) $event->payload['enrollment_id']));

            if ($enrollment === null || $enrollment->status() !== EnrollmentStatus::PendingPayment) {
                return;
            }

            if ($event->name === EventNames::PAYMENT_SUCCEEDED) {
                $enrollment->confirm($this->clock->now());
                $this->enrollments->save($enrollment);
                $this->record($enrollment, EventNames::ENROLLMENT_CONFIRMED, null);

                return;
            }

            $reason = (string) ($event->payload['reason'] ?? 'payment_failed');
            $enrollment->cancel($reason, $this->clock->now());
            $this->enrollments->save($enrollment);
            $this->seats->release($enrollment->offeringId());
            $this->record($enrollment, EventNames::ENROLLMENT_CANCELLED, $reason);
        });
    }
```

[`src/Enrollment/Application/Event/PaymentResultListener.php` L38-L65](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Enrollment/Application/Event/PaymentResultListener.php#L38-L65)

Слушатель идемпотентен не только обёрткой: он проверяет, что зачисление всё ещё
`PendingPayment`. Если событие пришло повторно после подтверждения, переход не
повторится. Это вторая линия защиты, но она не закрывает окно между side effect
и `mark()`.

---

## 3. Эксперимент

Эксперимент проверяет три сценария: временный сбой, повторную публикацию и
постоянный сбой с dead letter. Затем запускает полную цепочку runtime.

```bash
make experiment ARGS="events:delivery --events=20 --students=6 --capacity=6"
```

Вывод одного прогона:

```text
Experiment: Event delivery
A retry is not a duplicate side effect when the listener is idempotent.

flaky delivery: attempts=22 effects=20 delivered=20 failed=2 pending=0
duplicate publish: effects=1 skips=1
permanent failure: dead=1 pending=0
runtime chain: pending before=13, after=0, notifications=6
```

Разберём числа.

**Flaky delivery.** 20 событий, слушатель падает на первых двух попытках, затем
работает. Первое событие требует трёх попыток, остальные — по одной. Итого
22 попытки, 20 эффектов, 2 неудачных прохода, 0 pending. Слушатель не
идемпотентен сам по себе; эффект не дублируется потому, что обёртка записывает
ключ после успеха.

**Duplicate publish.** Одно и то же событие публикуется дважды. Внутренний
listener вызывается один раз, второй раз срабатывает `handlerSkips`. Это
проверка того, что ключ действительно сохраняется между вызовами.

**Permanent failure.** Слушатель всегда падает, `maxAttempts = 2`. После двух
проходов сообщение становится dead, pending = 0. Это не «потеря»: сообщение
сохранилось со статусом `dead`, но механизма replay в лаборатории нет.

**Runtime chain.** 6 студентов, capacity 6, `failEvery = 4`. До pump 13
событий, после pump 0, notifications 6. Здесь видно, что цепочка
`enrollment.requested → billing → payment result → enrollment decision`
действительно доходит до конца.

Бенчмарк:

| Benchmark                 | Итерации | Медиана |     p95 |     max |   ops/sec | PHP delta | peak delta |
| ------------------------- | -------: | ------: | ------: | ------: | --------: | --------: | ---------: |
| `events.outbox_roundtrip` |       50 | 3.536ms | 4.647ms | 4.647ms |    14 140 |  +5.70 MB |   +5.67 MB |
| `events.duplicate_skip`   |    1 000 | 0.173ms | 0.184ms | 0.184ms | 5 767 844 |     +592B |         0B |

`outbox_roundtrip` пишет 20 событий и прогоняет их через одного идемпотентного
слушателя. Память растёт, потому что in-memory outbox хранит все доставленные
сообщения. `duplicate_skip` после первой доставки только проверяет ключ, поэтому
время и аллокации почти не растут.

В полном сценарии на 12 студентов и 2 курса (capacity 4, `failEvery = 4`)
событийный режим даёт:

```text
requests: accepted 8, rejected by capacity 4
write model: confirmed 6, cancelled 2, pending 0
outbox: pending before pump 16, delivered 42, failed 0, dead-lettered 0, pending after 0
notifications: 8, stale entries before delivery: 0
```

`pending before pump = 16` — это 8 `catalog.seat_reserved` и 8
`enrollment.requested`. Два события `catalog.course_offering_published` были
доставлены раньше, сразу после публикации курсов, поэтому в снимок не попали.
Второй pump доставил 42 события: по пять фактов на каждое принятое зачисление
(`seat_reserved`, `requested`, `invoice_issued`, результат платежа, решение
Enrollment) плюс два `seat_released` для двух отменённых. Всего за сценарий
опубликовано 44 события: 2 публикации курсов + 42. Счётчик `eventsPublished` в
JSON-артефакте равен 44.

Синхронный режим даёт 10 принятых, 2 отменённых и 52 доставленных события:
10 × 5 + 2 `seat_released`. Публикации курсов, как и в событийном режиме, были
доставлены первым pump сразу после публикации; поэтому `pending before pump`
равен 52, а всего за сценарий опубликовано 54 события. Сравнивать 42 и 52 как
«больше/меньше событий на зачисление» нельзя: в sync-режиме больше зачислений
успевает пройти, потому что отказ платежа сразу освобождает место.

---

## 4. Что ломается

**Окно между side effect и `mark()`.** Это главное ограничение. Если процесс
упадёт после записи проекции, но до `mark()`, при повторе проекция обновится
ещё раз. В лаборатории это не воспроизводится тестом с падением процесса, но
следует из кода. Настоящее решение — транзакционная запись ключа вместе с
эффектом или идемпотентный внешний ключ.

**In-memory outbox не атомарен с документом.** В in-memory композиции
`InMemoryDocumentStore`, `InMemoryOutbox` и `InMemoryProcessedEventStore` — три
объекта. `transactional()` откатывает только документы. SQLite-композиция
разделяет один `PDO` и поэтому атомарна; in-memory — нет. Это не мелочь: именно
на такой разнице ломаются тесты, которые «прошли на быстром адаптере».

**Exactly-once нет.** At-least-once + идемпотентность дают эффективно
однократный эффект только в пределах доступного `ProcessedEventStore` и только
если side effect не происходит до записи ключа. Слово «exactly-once» в книге не
используется как обещание.

**Нет брокера, сети и многопроцессности.** Шина синхронная, всё в одном
процессе. Нет конкуренции двух диспетчеров за одну строку outbox, нет
распределённой блокировки, нет сетевых таймаутов. SQLite-транзакция и один
процесс — сознательное упрощение.

**Нет backoff и планировщика.** Повтор происходит в следующем проходе
`pump()`, без задержки. Постоянно падающее событие быстро уходит в dead letter
или крутится до `maxRounds`. В production нужен экспоненциальный backoff,
лимиты и алерты.

**Dead letter не воспроизводится.** Сообщение со статусом `dead` остаётся в
таблице, но команды replay нет. Это витрина механизма, а не операционный
процесс.

**Событие без слушателей считается доставленным.** `publish()` возвращает 0,
`markDelivered()` всё равно вызывается. Это нормально для шины, которая не
знает о подписчиках, но означает, что «delivered» не равно «кто-то обработал».
Счётчик `handled` в отчёте показывает число вызовов слушателей и помогает это
увидеть.

**Версия события не используется.** `version` есть в конверте, но нет реестра
схем и миграций. Изменение payload может сломать старого подписчика; в
лаборатории все подписчики обновляются вместе с продюсером.

**Порядок только по позиции outbox.** SQLite выбирает `ORDER BY position`, но
гарантия порядка действует в пределах одного dispatcher и одной таблицы. При
параллельной доставке или нескольких очередях её пришлось бы проектировать
отдельно.

---

## 5. Как это проверено

- `testDeliversEveryPendingMessage` — все pending доставляются, outbox пуст;
- `testRetriesATemporaryFailureUntilItSucceeds` — временный сбой приводит к
  повтору, эффект не теряется;
- `testDeadLettersAPermanentFailureAfterMaxAttempts` — после лимита сообщение
  становится dead;
- `testLimitCapsOnePass` — `limit` ограничивает один проход;
- `testIdempotentListenerSuppressesRedelivery` — повторная публикация не даёт
  второго эффекта;
- `testFailedListenerIsNotMarkedAsProcessed` — упавший слушатель не записывает
  ключ, поэтому повтор возможен;
- `testOutboxLifecycle` и `testOutboxRoundTripsTheEventPayload` — SQLite-outbox
  сохраняет payload и статусы;
- `testProcessedEventStoreIsDurableAndListenerScoped` — ключи переживают
  пересоздание store и разделены по listener;
- `testPumpIsIdempotentWhenNothingIsPending` — повторный pump на пустом outbox
  безопасен;
- сценарий проверяет `no-pending-events`, `no-dead-letters` и
  `notifications-match`.

Тест `testFailedListenerIsNotMarkedAsProcessed` важен для понимания границы: он
доказывает, что после исключения ключ не записан. Но он не моделирует падение
процесса между эффектом и `mark()`. Это ограничение тестового сценария, а не
доказательство отсутствия окна.

---

## 6. Что запомнить

- Outbox связывает запись агрегата и запись события в одной транзакции.
- Доставка отделена от записи и является at-least-once.
- `markDelivered()` вызывается только после успешной работы всех слушателей.
- Повтор после сбоя не должен создавать второй side effect; для этого нужен
  идемпотентный слушатель.
- `mark()` после side effect оставляет окно дублирования при падении процесса.
- Dead letter — это сохранённое сообщение, а не автоматически решённая проблема.
- Синхронная in-process шина — удобная модель для лаборатории, но не брокер.
- «Delivered» не всегда значит «обработано»: считайте `handled`, а не только
  `delivered`.

---

## 7. Упражнения

1. Сделайте `ProcessedEventStore::mark()` частью той же транзакции, что и
   обновление проекции. Какие интерфейсы придётся изменить?
2. Добавьте exponential backoff в `OutboxDispatcher`. Как хранить время
   следующей попытки в SQLite?
3. Реализуйте replay dead-letter сообщений. Что должно произойти с ключами
   `processed_events`?
4. Придумайте side effect, который нельзя сделать идемпотентным по event id.
   Какой внешний ключ вы бы использовали?
5. Проверьте, что будет, если событие вообще не имеет слушателей. Почему
   `delivered` увеличивается, а `handled` — нет?
6. Добавьте второй dispatcher, работающий параллельно. Какие блокировки нужны,
   чтобы одно сообщение не ушло дважды?

---

## 8. Что дальше

Outbox и проекции уже работают, но пять контекстов всё ещё говорят на общих
типах и живут в одном пространстве имён. В главе 7 они становятся bounded
contexts: у каждого свой агрегат, свой язык и свой публичный API. Catalog
владеет местами, Billing — счетами, Enrollment — состоянием зачисления.
Антикоррупционные адаптеры переводят между языками, а сценарий проверяет, что
контексты сходятся к одному состоянию, не деля общую модель.
