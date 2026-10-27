# Глава 5. Outbox: событие, которое переживает коммит

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: database per service. Следующая: saga и 2PC (глава 6).

Двойная запись выглядит безобидно: сначала сохранить бронь, потом опубликовать
событие. Проблема в том, что между этими двумя операциями нет транзакции. Если
процесс упадёт после коммита брони и до публикации, в базе останется
подтверждённая поездка, о которой никто не узнает. Outbox убирает этот разрыв:
событие пишется в ту же транзакцию, что и состояние, а публикуется позже
отдельным диспетчером.

Цена — доставка **at-least-once**. Событие может быть доставлено дважды, если
сбой случился после публикации, но до отметки `published`. Поэтому outbox — это
не «ровно один раз». Это «не потерять» плюс «научиться терпеть дубликат».

---

## 1. Проблема

Есть два независимых факта:

1. Бронь подтверждена и закоммичена.
2. Событие `booking.confirmed` опубликовано.

В dual-write они записываются разными операциями. Между ними возможен сбой:
процесс упал, сеть отвалилась, брокер не ответил. Если упасть после первого
факта, второй потерян навсегда. Проекция не узнает о поездке, отчёт останется
неполным, а повторная публикация «вручную» потребует искать расхождения.

Outbox меняет порядок:

1. В одной транзакции записываются бронь и строка `outbox(status = pending)`.
2. Диспетчер читает pending-строки и публикует их.
3. После успешной публикации строка помечается `published`.

Если сбой до публикации — строка остаётся pending и будет повторена. Если сбой
после публикации, но до `markPublished()` — событие будет опубликовано снова.
Второй случай неизбежен без распределённой транзакции между базой и брокером.
Именно поэтому потребитель обязан быть идемпотентным.

---

## 2. Устройство

### 2.1. Событие пишется вместе с бронью

`BookingWorkflow::finalize()` создаёт событие и выбирает режим доставки. В
outbox-режиме он передаёт событие в `save()`:

```php
    private function finalize(BookingRecord $booking, BookingStatus $status, array $lines, string $reason = ''): bool
    {
        $now = $this->clock->now();
        $record = $booking->withStatus($status, $now)->withLines($lines, $now);

        if ($status !== BookingStatus::Confirmed && $status !== BookingStatus::Cancelled) {
            $this->store->save($record);

            if ($status === BookingStatus::Failed) {
                $this->counters->increment('bookingsFailed');
            }

            return false;
        }

        $event = new EventEnvelope(
            $this->ids->next('evt'),
            $status === BookingStatus::Confirmed ? 'booking.confirmed' : 'booking.cancelled',
            [
                'booking_id' => $record->id,
                'traveler_id' => $record->travelerId,
                'amount_cents' => $record->amountCents,
                'lines' => array_map(static fn (BookingLine $line): array => $line->toArray(), $lines),
                'reason' => $reason,
            ],
            $now,
        );

        if ($this->delivery === DeliveryMode::Outbox) {
            $this->store->save($record, $event);
            $this->counters->increment($status === BookingStatus::Confirmed ? 'bookingsConfirmed' : 'bookingsCancelled');

            return true;
        }

        $this->store->save($record);
        $delivered = false;

        try {
            $this->faults->before('dualwrite.publish');
            $this->bus->publish($event);
            $delivered = true;
            $this->faults->after('dualwrite.publish');
        } catch (\Throwable) {
            $this->counters->increment('eventPublishFailures');
        }

        $this->counters->increment($status === BookingStatus::Confirmed ? 'bookingsConfirmed' : 'bookingsCancelled');

        return $delivered;
    }
```

[`src/Services/BookingWorkflow.php` L469-L519](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Services/BookingWorkflow.php#L469-L519)

Обратите внимание на возвращаемое значение. В outbox-режиме метод возвращает
`true` сразу после записи строки, ещё до публикации: событие **надёжно
записано**, но не обязательно доставлено. В dual-write он возвращает `true`
только если `publish()` завершился успешно. Это разные обещания, и
`BookingResult::$eventPublished` отражает именно их.

`SqlBookingStore::save()` оборачивает запись брони и `outbox->append()` в одну
транзакцию. Если запись события упадёт, откатится и бронь. Если упадёт бронь, не
появится событие. Это и есть транзакционная граница outbox.

### 2.2. Append — обычная строка

```php
    public function append(EventEnvelope $event): void
    {
        $this->db->execute(
            'INSERT INTO outbox (event_id, type, payload, status, attempts, occurred_at, published_at)
             VALUES (:event_id, :type, :payload, :status, 0, :occurred_at, NULL)',
            [
                'event_id' => $event->id,
                'type' => $event->type,
                'payload' => $event->toJson(),
                'status' => 'pending',
                'occurred_at' => $event->occurredAt,
            ],
        );

        $this->counters->increment('outboxAppends');
        $this->counters->set('outboxPendingGauge', $this->countPending());
    }
```

[`src/Consistency/Outbox.php` L25-L41](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/Outbox.php#L25-L41)

Строка содержит `event_id` с уникальным ограничением, тип, JSON-payload, статус,
число попыток и время. Уникальность `event_id` не даёт вставить одно и то же
событие дважды на уровне базы. Это не делает всю доставку exactly-once, но
защищает журнал от дублирующей записи.

### 2.3. Диспетчер публикует и отмечает

`OutboxDispatcher` читает pending-строки, увеличивает `attempts`, публикует
событие в шину, затем помечает строку опубликованной. При исключении он
останавливает текущий проход и возвращает отчёт.

```php
    public function dispatch(int $limit = 100): DispatchReport
    {
        $messages = $this->outbox->pending($limit);
        $delivered = 0;
        $failed = 0;
        $lastError = '';

        foreach ($messages as $message) {
            $this->outbox->registerAttempt($message->event);

            try {
                $this->faults->before('outbox.publish');
                $this->bus->publish($message->event);
                $this->faults->after('outbox.publish');
                $this->outbox->markPublished($message->event);
                ++$delivered;
            } catch (\Throwable $throwable) {
                ++$failed;
                $lastError = $throwable->getMessage();
                $this->counters->increment('outboxFailures');

                break;
            }
        }

        return new DispatchReport(
            attempted: count($messages),
            delivered: $delivered,
            failed: $failed,
            pending: $this->outbox->countPending(),
            lastError: $lastError,
        );
    }
```

[`src/Consistency/OutboxDispatcher.php` L25-L57](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/OutboxDispatcher.php#L25-L57)

Порядок операций — источник обоих сценариев:

- сбой в `before('outbox.publish')`: событие не опубликовано, строка pending,
  следующий dispatch повторит;
- сбой в `after('outbox.publish')`: событие уже в шине, но `markPublished()` не
  выполнился; следующий dispatch опубликует его снова;
- сбой в `markPublished()`: то же самое — событие доставлено, отметка потеряна.

`break` при ошибке останавливает проход, чтобы не публиковать следующие события
поверх сбойного. Это простая политика: один сбой — один остановленный батч.
Retry с backoff и dead-letter здесь нет; это ограничение, а не скрытая функция.

### 2.4. Идемпотентный потребитель

Проекция не может предполагать, что событие придёт один раз.
`IdempotentProjector` сначала проверяет `(consumer, event_id)`, затем применяет
событие и записывает ключ в одной транзакции.

```php
    public function handle(EventEnvelope $event): void
    {
        if ($this->processed->isProcessed($this->consumer, $event->id)) {
            $this->counters->increment('eventHandlerSkips');
            $this->counters->increment('duplicateEvents');

            return;
        }

        $this->db->transaction(function () use ($event): void {
            $this->projection->apply($event);
            $this->processed->markProcessed($this->consumer, $event->id);
        });
    }
```

[`src/Consistency/IdempotentProjector.php` L26-L39](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/IdempotentProjector.php#L26-L39)

Ключ хранится отдельно от проекции:

```php
    public function markProcessed(string $consumer, string $eventId): void
    {
        $this->db->execute(
            'INSERT OR IGNORE INTO processed_events (consumer, event_id, processed_at)
             VALUES (:consumer, :event_id, :processed_at)',
            [
                'consumer' => $consumer,
                'event_id' => $eventId,
                'processed_at' => $this->clock->now(),
            ],
        );
        $this->counters->increment('processedEvents');
    }
```

[`src/Consistency/ProcessedEventStore.php` L34-L46](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/ProcessedEventStore.php#L34-L46)

Применение проекции и запись ключа находятся в одной транзакции. Если процесс
упадёт между ними, откатятся оба действия; повторная доставка снова применит
событие ровно один раз. Это важнее, чем `INSERT OR IGNORE` сам по себе: без
общей транзакции можно было бы применить проекцию и потерять ключ, а затем
применить её повторно.

В модели один процесс и нет конкурентных потребителей. В реальной системе
проверка `isProcessed()` и `markProcessed()` должны быть защищены от гонки
(например, уникальным ключом и обработкой конфликта). Здесь `INSERT OR IGNORE`
есть, но проверка стоит до транзакции; конкурентного сценария лаборатория не
моделирует.

---

## 3. Эксперимент

```bash
make experiment ARGS="outbox:delivery"
```

20 броней, capacity 30. Пять случаев: обычный dual write, обычный outbox, dual
write со сбоем до публикации, outbox со сбоем до публикации и outbox со сбоем
после публикации. Вот реальный вывод:

```text
Experiment: Outbox
bookings=20 capacity=30

case                         ok   events    trips     lost  pending      dup   failed
dual-write                   20       20       20        0        0        0        0
outbox                       20       20       20        0        0        0        0
dual-write-crash              1        0        0        1        0        0        1
outbox-retry                  1        1        1        0        0        0        1
outbox-duplicate              1        1        1        0        0        1        1
```

`events` здесь — число результатов с `eventPublished = true`, а не число строк в
outbox. В обычных случаях обе схемы публикуют 20 событий и дают 20 проекций.

Различие появляется при сбое:

- **`dual-write-crash`.** Бронь подтверждена, но событие не опубликовано.
  `lost = 1`, `trips = 0`, `failed = 1`. Строка outbox не создавалась, поэтому
  восстановить событие нечем.
- **`outbox-retry`.** Первый dispatch упал до публикации. Строка осталась
  pending; второй dispatch доставил её. `lost = 0`, `trips = 1`, `failed = 1`.
- **`outbox-duplicate`.** Первый dispatch упал после публикации, но до отметки.
  Второй dispatch повторил событие; проекция увидела дубликат и поглотила его.
  `dup = 1`, `lost = 0`, `trips = 1`.

`failed = 1` — это зафиксированный сбой попытки, а не потеря. В outbox-случаях
он сосуществует с `lost = 0`. Это главный результат главы: сбой доставки и
потеря события — разные вещи.

---

## 4. Бенчмарк

```bash
make benchmark ARGS="05-outbox"
```

300 броней на измерение. `dual_write_booking` сохраняет бронь и публикует
событие сразу; `outbox_booking` сохраняет бронь и outbox-строку;
`booking_and_dispatch` дополнительно разбирает outbox и проекцию.

| benchmark                     | итераций | медиана |     p95 | ops/sec | медиана на бронь |
| ----------------------------- | -------: | ------: | ------: | ------: | ---------------: |
| `outbox.dual_write_booking`   |      300 | 373.385 | 527.780 |     803 |         1 245 µs |
| `outbox.outbox_booking`       |      300 | 345.188 | 481.659 |     869 |         1 151 µs |
| `outbox.booking_and_dispatch` |      300 | 485.556 | 634.100 |     618 |         1 619 µs |

Outbox-запись оказалась на 7,6% **быстрее** dual write в этом прогоне. Это не
повод утверждать, что outbox бесплатен. Обе операции делают разную работу: dual
write публикует событие синхронно и применяет проекцию в том же вызове, а outbox
только добавляет строку и откладывает доставку. Разница ниже шума контейнера, и
в более раннем прогоне знак был противоположным. Правильное чтение — «примерно
одинаково».

Настоящая цена видна в третьей строке: бронь + dispatch + drain на 40,7% дороже
outbox-записи и на 30,0% дороже dual write. Если вам нужна не только durability,
но и доставка, платить придётся за dispatcher, чтение outbox, публикацию и
работу проекции. Outbox не убирает эту работу; он делает её отложенной и
восстановимой.

---

## 5. Что ломается и что не измерено

**At-least-once, не exactly-once.** Outbox гарантирует, что событие не
потеряется между коммитом и публикацией, но не гарантирует единственную
доставку. Дубликат — штатный сценарий, а не ошибка. Потребитель обязан быть
идемпотентным.

**Нет exactly-once между базой и брокером.** `markPublished()` — отдельная
операция. Между публикацией и отметкой всегда есть окно. Лаборатория не пытается
его убрать; она делает окно видимым через duplicate-тест.

**Нет реального брокера.** `InMemoryEventBus` вызывает слушателей в том же
процессе. Нет partition, offset, consumer group, retry topic, dead-letter queue.
Всё, что связано с Kafka/RabbitMQ, остаётся за границей.

**Нет фонового процесса.** `dispatch()` вызывается вручную из эксперимента или
сценария. В production нужен scheduler, lease или конкурентный worker, чтобы
несколько dispatcher'ов не разбирали одни и те же строки. Лаборатория
однопроцессная.

**Нет очистки outbox.** Опубликованные строки остаются в таблице. Нет retention,
архивации и партиционирования. Для 20 событий это не важно, для миллионов —
важно.

**Нет backoff и poison-message.** При сбое dispatcher делает `break` и
возвращает отчёт. Следующий вызов снова попробует ту же строку. Постоянно
падающее событие будет блокировать очередь, пока его не разберут вручную.

**Нет конкурентности.** Два dispatcher'а в одном процессе не моделируются,
поэтому нет гонки за одну pending-строку и нет необходимости в `SKIP LOCKED`.

---

## 6. Как это проверено

Тест на сбой до публикации проверяет, что событие остаётся pending и успешно
доставляется вторым проходом:

```php
    public function testCrashBeforePublishLeavesTheEventPendingForTheNextDispatch(): void
    {
        $counters = new Counters();
        $faults = new FaultInjector(step: 'outbox.publish', counters: $counters);
        $lab = $this->lab($this->config(), $faults, $counters);
        $lab->seed('f1', 'h1', 'c1', capacity: 4);
        $lab->book($this->trip());

        $first = $lab->dispatchOutbox();

        self::assertSame(1, $first->failed);
        self::assertSame(1, $first->pending);
        self::assertSame(0, $lab->projection->tripCount());

        $second = $lab->dispatchOutbox();

        self::assertSame(1, $second->delivered);
        self::assertSame(0, $second->pending);
        self::assertSame(1, $lab->projection->tripCount());
    }
```

[`tests/Consistency/OutboxAndProjectionTest.php` L51-L70](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Consistency/OutboxAndProjectionTest.php#L51-L70)

Тест на сбой после публикации проверяет дубликат и его поглощение:

```php
    public function testCrashAfterPublishProducesADuplicateThatTheConsumerIgnores(): void
    {
        $counters = new Counters();
        $faults = new FaultInjector(step: 'outbox.publish', after: true, counters: $counters);
        $lab = $this->lab($this->config(), $faults, $counters);
        $lab->seed('f1', 'h1', 'c1', capacity: 4);
        $lab->book($this->trip());

        $first = $lab->dispatchOutbox();

        self::assertSame(1, $first->failed);
        self::assertSame(1, $first->pending);
        self::assertSame(1, $lab->projection->tripCount());

        $second = $lab->dispatchOutbox();

        self::assertSame(1, $second->delivered);
        self::assertSame(0, $second->pending);
        self::assertSame(1, $lab->projection->tripCount());
        self::assertSame(1, $lab->counters->get('duplicateEvents'));
        self::assertSame(3, $lab->projectedAvailability(ResourceType::Flight, 'f1'));
    }
```

[`tests/Consistency/OutboxAndProjectionTest.php` L72-L93](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Consistency/OutboxAndProjectionTest.php#L72-L93)

Оба теста проверяют не только финальное состояние, но и промежуточное: `failed`,
`pending`, `tripCount`, `duplicateEvents`. Без промежуточных утверждений тест
мог бы пройти на реализации, которая просто повторяет всё всегда и не считает
дубликаты.

---

## 7. Что запомнить

- Dual write теряет событие в окне между коммитом состояния и публикацией.
- Outbox пишет событие в той же транзакции, что и состояние, и доставляет позже.
- Outbox — at-least-once. Дубликат неизбежен; потребитель должен его поглощать.
- Идемпотентность — это не «проверить флаг», а «применить эффект и записать ключ
  в одной транзакции».
- `eventPublished` в outbox-режиме означает «записано», а не «доставлено».
  Называйте это явно.
- Бенчмарк outbox-записи не включает доставку. Стоимость появляется в
  `booking_and_dispatch`.
- Outbox не заменяет брокер и не даёт exactly-once. Он даёт восстановимость.

---

## 8. Упражнения

1. Запустите `outbox:delivery --bookings=100` и объясните, почему при
   `outbox-duplicate` число `dup` может быть больше одного при нескольких сбоях.
2. Добавьте в `OutboxDispatcher` backoff и лимит попыток. Как отличить временный
   сбой от poison message?
3. Предложите, как очищать опубликованные строки, не теряя возможность аудита.
   Какие индексы понадобятся?
4. Почему `markPublished()` не может быть частью той же транзакции, что и
   публикация в реальный брокер? Что изменится, если брокер поддерживает
   транзакции?
5. Сравните `eventPublished` в outbox и dual-write режимах. Какое обещание
   получает вызывающий в каждом случае?

---

Дальше — глава 6. Она сравнивает два способа справиться с отсутствием общей
транзакции: сагу с компенсациями и двухфазный коммит с удержанием ресурсов.
