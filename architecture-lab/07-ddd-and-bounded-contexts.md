# Глава 7. DDD и bounded contexts: один сценарий, пять языков

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: событийная доставка. Это последняя глава книги.

Domain-Driven Design начинается не с папки `Domain` и не с слова «агрегат».
Он начинается с признания, что у разных частей системы разные модели и разные
языки. В интернет-магазине «товар» для каталога — это описание и цена, для
склада — количество на полке, для доставки — габариты и адрес. Если свести всё
к одной сущности `Product`, каждая часть получит поля, которые ей не нужны, и
инварианты, которые она не может поддерживать.

В лаборатории тот же принцип на маленьком домене. `Catalog` владеет курсом и
местами. `Enrollment` — попыткой студента записаться. `Billing` — счётом и
платежом. `Reporting` — проекциями. `Notifications` — журналом уведомлений. Это
пять bounded contexts в одном PHP-процессе. У каждого свой агрегат, свой
публичный API и свой язык. Связь идёт через явные переводы, а не через общую
модель.

Глава собирает предыдущие шесть: слои, порты, правило зависимостей, модули,
CQRS и outbox. Здесь видно, зачем они были нужны — чтобы контексты могли
различаться, не разваливая сценарий.

---

## 1. Проблема

Соблазн общей модели велик. Один класс `Enrollment` с полями `studentId`,
`courseId`, `courseTitle`, `price`, `paid`, `status`, `invoiceId` кажется
простым. Но у него сразу несколько владельцев:

- `Catalog` отвечает за capacity и не должен зависеть от платежа;
- `Enrollment` отвечает за переход `pending → confirmed/cancelled` и не должен
  знать, как устроен счёт;
- `Billing` отвечает за идемпотентность списания и не должен зависеть от
  жизненного цикла зачисления;
- `Reporting` отвечает на вопросы и не должен менять write-модель;
- `Notifications` реагирует на факты и не должен знать, откуда они пришли.

Если все пять контекстов используют один класс, любое изменение в одном
задевает остальные. Общая модель становится «god object»: у неё появляются
поля, которые нужны только одному потребителю, и методы, которые нарушают
инварианты другого.

Bounded context — это граница, внутри которой язык имеет одно значение. Внутри
`Billing` слово `invoice` означает конкретный агрегат с статусами `open`,
`paid`, `failed`. Внутри `Enrollment` слово `enrollment` означает попытку
записаться с статусами `pending_payment`, `confirmed`, `cancelled`. На границе
эти языки переводятся явно.

---

## 2. Устройство

### 2.1. Контекстная карта

Пять контекстов и их роли:

| Контекст        | Агрегат          | Что означает                | Публичный API       |
| --------------- | ---------------- | --------------------------- | ------------------- |
| `Catalog`       | `CourseOffering` | курс, расписание, места     | `CatalogApi`        |
| `Enrollment`    | `Enrollment`     | попытка студента записаться | `EnrollmentApi`     |
| `Billing`       | `Invoice`        | счёт и решение платежа      | `BillingApi`        |
| `Reporting`     | проекции         | ответы на запросы           | query objects/views |
| `Notifications` | лог              | наблюдаемый side effect     | событие/лог         |

`Shared` содержит только то, что действительно общее: `Money`, типизированные
идентификаторы (`StudentId`, `OfferingId`, `EnrollmentId`, `InvoiceId`), `Clock`,
`IdGenerator` и порты хранилища. Общего `Course` или `Student` нет. Это
сознательно: каждый контекст хранит только те атрибуты, которые ему нужны.

### 2.2. Агрегаты и их инварианты

`CourseOffering` не знает о зачислениях и платежах. Он знает только, что
reserved seats не может превысить capacity, и что освобождать можно только
зарезервированное место.

```php
/**
 * Catalog's aggregate: one scheduled course and the seats it owns.
 *
 * The aggregate enforces the capacity invariant. No SQL, no event bus and no
 * other context appears here; the only collaborators are value objects.
 */
final class CourseOffering
{
    private int $reservedSeats = 0;

    public function __construct(
        private readonly OfferingId $id,
        private readonly string $title,
        private readonly Money $price,
        private readonly DateTimeImmutable $startsAt,
        private readonly int $capacity,
    ) {
        if (trim($this->title) === '') {
            throw new InvalidArgumentException('Course title cannot be empty');
        }

        if ($this->capacity < 1) {
            throw new InvalidArgumentException('A course offering needs at least one seat');
        }
    }

    public function reserveSeat(): void
    {
        if ($this->reservedSeats >= $this->capacity) {
            throw new SeatsExhausted(sprintf(
                'Offering %s has no seats left (%d/%d)',
                $this->id->value,
                $this->reservedSeats,
                $this->capacity,
            ));
        }

        ++$this->reservedSeats;
    }

    public function releaseSeat(): void
    {
        if ($this->reservedSeats === 0) {
            throw new LogicException(sprintf('Offering %s has no reserved seat to release', $this->id->value));
        }

        --$this->reservedSeats;
    }
```

[`src/Catalog/Domain/CourseOffering.php` L13-L60](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Catalog/Domain/CourseOffering.php#L13-L60)

`Invoice` знает зачисление только по id. Он не содержит объекта `Enrollment` и
не зависит от его формулировок.

```php
/**
 * Billing's aggregate.
 *
 * It knows an enrollment only by id. The invoice does not embed an Enrollment
 * object, so a change in Enrollment's wording cannot silently change Billing.
 */
final class Invoice
{
    private InvoiceStatus $status = InvoiceStatus::Open;

    private ?string $failureReason = null;

    private ?DateTimeImmutable $decidedAt = null;

    public function __construct(
        private readonly InvoiceId $id,
        private readonly EnrollmentId $enrollmentId,
        private readonly StudentId $studentId,
        private readonly OfferingId $offeringId,
        private readonly Money $amount,
        private readonly DateTimeImmutable $issuedAt,
    ) {
    }

    public function markPaid(DateTimeImmutable $at): void
    {
        if ($this->status !== InvoiceStatus::Open) {
            throw new DomainException(sprintf('Cannot pay invoice in status %s', $this->status->value));
        }

        $this->status = InvoiceStatus::Paid;
        $this->decidedAt = $at;
    }

    public function markFailed(string $reason, DateTimeImmutable $at): void
    {
        if ($this->status !== InvoiceStatus::Open) {
            throw new DomainException(sprintf('Cannot fail invoice in status %s', $this->status->value));
        }

        $this->status = InvoiceStatus::Failed;
        $this->failureReason = $reason;
        $this->decidedAt = $at;
    }
```

[`src/Billing/Domain/Invoice.php` L15-L58](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Billing/Domain/Invoice.php#L15-L58)

`Enrollment` владеет переходом состояния, но не местом и не платежом:

```php
/**
 * Enrollment's aggregate: one student's pending or finished course attempt.
 *
 * It owns the state transition, not the payment and not the seat. The seat is
 * reserved in Catalog through a port; the payment result arrives as an event
 * or from a synchronous adapter.
 */
final class Enrollment
{
    private EnrollmentStatus $status = EnrollmentStatus::PendingPayment;

    private ?string $cancellationReason = null;

    private ?DateTimeImmutable $decidedAt = null;

    public function __construct(
        private readonly EnrollmentId $id,
        private readonly StudentId $studentId,
        private readonly OfferingId $offeringId,
        private readonly string $courseTitle,
        private readonly Money $price,
        private readonly DateTimeImmutable $requestedAt,
    ) {
        if (trim($this->courseTitle) === '') {
            throw new InvalidArgumentException('Course title cannot be empty');
        }
    }

    public function confirm(DateTimeImmutable $at): void
    {
        if ($this->status !== EnrollmentStatus::PendingPayment) {
            throw new DomainException(sprintf('Cannot confirm enrollment in status %s', $this->status->value));
        }

        $this->status = EnrollmentStatus::Confirmed;
        $this->decidedAt = $at;
    }

    public function cancel(string $reason, DateTimeImmutable $at): void
    {
        if ($this->status !== EnrollmentStatus::PendingPayment) {
            throw new DomainException(sprintf('Cannot cancel enrollment in status %s', $this->status->value));
        }

        $this->status = EnrollmentStatus::Cancelled;
        $this->cancellationReason = $reason;
        $this->decidedAt = $at;
    }
```

[`src/Enrollment/Domain/Enrollment.php` L15-L62](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Enrollment/Domain/Enrollment.php#L15-L62)

Три агрегата не ссылаются друг на друга объектами. Они разделяют
идентификаторы. Это позволяет контекстам развиваться независимо, но означает,
что согласованность между ними — не свойство одного агрегата, а свойство
сценария и транзакции.

### 2.3. Перевод через порт: Catalog → Enrollment

В синхронном режиме Enrollment не импортирует `CatalogApi` в application layer.
Он объявляет порт `SeatAllocator`, а адаптер в `Infrastructure` переводит
исключения и DTO Catalog в локальный язык Enrollment.

```php
/**
 * Anti-corruption layer: Catalog's language becomes Enrollment's language.
 *
 * This adapter is the only Enrollment class allowed to import CatalogApi. The
 * application port contains no Catalog exception and no Catalog DTO.
 */
final readonly class CatalogSeatAllocator implements SeatAllocator
{
    public function __construct(
        private CatalogApi $catalog,
    ) {
    }

    public function reserve(OfferingId $offeringId): SeatReservation
    {
        try {
            $offering = $this->catalog->offering($offeringId);
            $this->catalog->reserveSeat($offeringId);
        } catch (NoSeatsAvailable $exception) {
            throw new EnrollmentNoSeatsAvailable($exception->getMessage(), 0, $exception);
        } catch (OfferingNotFound $exception) {
            throw new EnrollmentNoSeatsAvailable($exception->getMessage(), 0, $exception);
        }

        return new SeatReservation(
            offeringId: $offering->id,
            courseTitle: $offering->title,
            price: $offering->price,
            seatsLeft: $offering->seatsLeft() - 1,
        );
    }

    public function release(OfferingId $offeringId): void
    {
        $this->catalog->releaseSeat($offeringId);
    }
```

[`src/Enrollment/Infrastructure/CatalogSeatAllocator.php` L15-L50](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Enrollment/Infrastructure/CatalogSeatAllocator.php#L15-L50)

Здесь два разных исключения Catalog (`NoSeatsAvailable` и `OfferingNotFound`)
сводятся в одно исключение Enrollment (`EnrollmentNoSeatsAvailable`). Это
осознанная потеря информации: application layer Enrollment не должен знать,
почему Catalog не дал место. Если различие важно для UX, порт нужно расширить
отдельным типом, а не тащить Catalog в application.

### 2.4. Перевод через событие: Enrollment → Billing

В событийном режиме Billing не вызывает Enrollment и не импортирует его классы.
Он подписан на `enrollment.requested` и читает payload как опубликованный
контракт.

```php
/**
 * Billing's anti-corruption layer for the enrollment event.
 *
 * The payload is read as a published contract and translated into Billing's
 * local values. No Enrollment class is imported here.
 */
final readonly class EnrollmentRequestedListener implements EventListener
{
    public function __construct(
        private BillingApi $billing,
    ) {
    }

    public function handle(EventEnvelope $event): void
    {
        if ($event->name !== EventNames::ENROLLMENT_REQUESTED) {
            return;
        }

        $this->billing->chargeForEnrollment(
            new EnrollmentId((string) $event->payload['enrollment_id']),
            new StudentId((string) $event->payload['student_id']),
            new OfferingId((string) $event->payload['offering_id']),
            new Money(
                (int) $event->payload['price_cents'],
                (string) $event->payload['currency'],
            ),
        );
    }
```

[`src/Billing/Application/Event/EnrollmentRequestedListener.php` L16-L44](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Billing/Application/Event/EnrollmentRequestedListener.php#L16-L44)

Событие — это не объект `Enrollment`, а набор скаляров. Billing создаёт свои
`EnrollmentId`, `StudentId`, `OfferingId` и `Money`. Если Enrollment изменит
внутреннее поле, Billing не сломается, пока не изменится опубликованный
контракт.

### 2.5. Идемпотентность на границе контекста

Billing должен быть идемпотентен по `enrollment_id`: повторная доставка
`enrollment.requested` не должна списывать деньги второй раз. Поэтому перед
вызовом gateway он ищет счёт по зачислению и возвращает существующее решение,
если оно уже принято.

```php
        return $this->unitOfWork->transactional(function () use (
            $enrollmentId,
            $studentId,
            $offeringId,
            $amount,
        ): PaymentOutcome {
            $invoice = $this->invoices->findByEnrollment($enrollmentId);

            if ($invoice === null) {
                $invoice = new Invoice(
                    id: new InvoiceId($this->ids->next('inv')),
                    enrollmentId: $enrollmentId,
                    studentId: $studentId,
                    offeringId: $offeringId,
                    amount: $amount,
                    issuedAt: $this->clock->now(),
                );
                $this->invoices->save($invoice);
                $this->outbox->record($this->recorder->record(
                    EventNames::INVOICE_ISSUED,
                    $invoice->id()->value,
                    [
                        'invoice_id' => $invoice->id()->value,
                        'enrollment_id' => $enrollmentId->value,
                        'student_id' => $studentId->value,
                        'offering_id' => $offeringId->value,
                        'amount_cents' => $amount->cents,
                        'currency' => $amount->currency,
                    ],
                ));
            }

            if ($invoice->status() === InvoiceStatus::Paid) {
                return new PaymentOutcome(true, $invoice->id()->value);
            }

            if ($invoice->status() === InvoiceStatus::Failed) {
                return new PaymentOutcome(false, $invoice->id()->value, $invoice->failureReason());
            }

            $result = $this->gateway->charge($invoice);
```

[`src/Billing/Application/BillingService.php` L51-L91](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Billing/Application/BillingService.php#L51-L91)

Это идемпотентность на уровне контекста, а не слушателя. Даже если
`IdempotentListener` пропустит событие, Billing сам не спишет дважды. Две
защиты дополняют друг друга: одна работает по `(listener, event id)`, другая —
по `enrollment id`.

### 2.6. Композиция контекстов

`LabRuntime` собирает все пять контекстов и решает, какие адаптеры включить.
Вот фрагмент, где создаются Catalog, Enrollment, Billing и адаптеры:

```php
        $catalogRepository = new DocumentCourseOfferingRepository($store, $counters);
        $catalog = new CatalogService($catalogRepository, $store, $outbox, $recorder);
        $enrollmentRepository = new DocumentEnrollmentRepository($store, $counters);
        $invoiceRepository = new DocumentInvoiceRepository($store, $counters);
        $billing = new BillingService(
            $invoiceRepository,
            new DeterministicPaymentGateway($failEvery),
            $store,
            $outbox,
            $recorder,
            $ids,
            $clock,
        );
        $seats = new CatalogSeatAllocator($catalog);
        $payment = $mode === 'sync' ? new BillingPaymentProcessor($billing) : null;
        $requestEnrollment = new RequestEnrollmentHandler(
            $enrollmentRepository,
            $seats,
            $store,
            $outbox,
            $recorder,
            $ids,
            $clock,
            $payment,
        );
```

[`src/Runtime/LabRuntime.php` L91-L115](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Runtime/LabRuntime.php#L91-L115)

В sync-режиме `$payment` — это `BillingPaymentProcessor`, и зачисление получает
терминальный результат до возврата команды. В event-driven режиме `$payment`
равен `null`, и Billing подключается слушателем. Один и тот же домен, два
способа координации.

### 2.7. Сценарий как проверка контекстов

`CourseEnrollmentScenario::run()` публикует два курса, делает 12 запросов,
снимает устаревшую проекцию, затем запускает доставку и проверяет инварианты.

```php
    public function run(ScenarioOptions $options): ScenarioReport
    {
        $offerings = $this->publish($options);
        $this->runtime->pump();

        $accepted = 0;
        $rejected = 0;

        for ($student = 1; $student <= $options->students; ++$student) {
            $studentId = sprintf('s-%02d', $student);
            $offeringId = $offerings[($student - 1) % count($offerings)];

            try {
                $this->runtime->commands()->dispatch(new RequestEnrollment($studentId, $offeringId));
                ++$accepted;
            } catch (EnrollmentRejected) {
                ++$rejected;
            }
        }

        $pendingBefore = $this->runtime->pendingEvents();
        $stale = $this->snapshot($offerings, $options);

        $delivery = $this->runtime->pump();
        $final = $this->snapshot($offerings, $options);
        $write = $this->writeCounts();

        $checks = $this->checks($stale, $final, $write, $accepted, $rejected);

        return new ScenarioReport(
            mode: $options->mode,
            seed: $options->seed,
            numbers: [
                'students' => $options->students,
                'offerings' => $options->offerings,
                'capacity_per_offering' => $options->capacity,
                'fail_every' => $options->failEvery,
                'accepted' => $accepted,
                'rejected_by_capacity' => $rejected,
                'write_confirmed' => $write['confirmed'],
                'write_cancelled' => $write['cancelled'],
                'write_pending' => $write['pending'],
                'pending_events_before_pump' => $pendingBefore,
                'events_delivered' => $delivery->delivered,
                'events_failed' => $delivery->failed,
                'events_dead_lettered' => $delivery->deadLettered,
                'pending_events_after_pump' => $this->runtime->pendingEvents(),
                'stale_projection_entries' => $stale['projection_entries'],
                'notifications' => $this->runtime->notifications()->count(),
                'read_model' => $final,
                'counters' => $this->runtime->counters()->toArray(),
            ],
            checks: $checks,
        );
    }
```

[`src/Integration/CourseEnrollmentScenario.php` L26-L80](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Integration/CourseEnrollmentScenario.php#L26-L80)

Проверки — это не «код запустился», а согласованность пяти языков:

```php
        return [
            'stale-before-pump' => [
                'ok' => $stale['projection_entries'] === 0,
                'detail' => sprintf('read model had %d entries before delivery', $stale['projection_entries']),
            ],
            'no-pending-events' => [
                'ok' => $this->runtime->pendingEvents() === 0,
                'detail' => sprintf('%d events are still pending', $this->runtime->pendingEvents()),
            ],
            'no-dead-letters' => [
                'ok' => $this->runtime->deadLettered() === 0,
                'detail' => sprintf('%d events are dead-lettered', $this->runtime->deadLettered()),
            ],
            'write-converged' => [
                'ok' => $write['pending'] === 0,
                'detail' => sprintf('%d enrollments are still pending payment', $write['pending']),
            ],
            'read-model-confirmed' => [
                'ok' => $final['confirmed'] === $write['confirmed'],
                'detail' => sprintf('read model %d vs write model %d', $final['confirmed'], $write['confirmed']),
            ],
            'read-model-cancelled' => [
                'ok' => $final['cancelled'] === $write['cancelled'],
                'detail' => sprintf('read model %d vs write model %d', $final['cancelled'], $write['cancelled']),
            ],
            'capacity-conserved' => [
                'ok' => $final['reserved_seats'] === $write['confirmed'] + $write['pending'],
                'detail' => sprintf('read model reserved %d vs write reserved %d', $final['reserved_seats'], $write['confirmed'] + $write['pending']),
            ],
            'catalog-conserved' => [
                'ok' => $catalogReserved === $write['confirmed'] + $write['pending'],
                'detail' => sprintf('catalog reserved %d vs write reserved %d', $catalogReserved, $write['confirmed'] + $write['pending']),
            ],
            'no-oversubscription' => [
                'ok' => $final['reserved_seats'] <= count($this->runtime->catalog()->offerings()) * $this->capacityOfFirstOffering(),
                'detail' => sprintf('%d seats reserved, total capacity %d', $final['reserved_seats'], count($this->runtime->catalog()->offerings()) * $this->capacityOfFirstOffering()),
            ],
            'notifications-match' => [
                'ok' => $this->runtime->notifications()->count() === $write['confirmed'] + $write['cancelled'],
                'detail' => sprintf('notifications %d vs terminal enrollments %d', $this->runtime->notifications()->count(), $write['confirmed'] + $write['cancelled']),
            ],
            'requests-accounted' => [
                'ok' => $accepted === count($this->runtime->enrollments()->all()),
                'detail' => sprintf('%d accepted vs %d stored enrollments; %d rejected', $accepted, count($this->runtime->enrollments()->all()), $rejected),
            ],
        ];
```

[`src/Integration/CourseEnrollmentScenario.php` L171-L216](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Integration/CourseEnrollmentScenario.php#L171-L216)

`catalog-conserved` сравнивает зарезервированные места в Catalog с суммой
подтверждённых и ожидающих зачислений. `capacity-conserved` делает то же для
read model. `notifications-match` требует, чтобы на каждое терминальное
зачисление было ровно одно уведомление. Если перевод между контекстами
потеряет идентификатор или событие, эти числа разойдутся.

---

## 3. Эксперимент

Сначала — карта контекстов и антикоррупционные адаптеры:

```bash
make experiment ARGS="ddd:contexts"
```

Вывод одного прогона:

```text
Experiment: Bounded contexts
Five models in one deployable unit, with explicit translations.

contexts: 5, public API files: 20, public API types: 20
anti-corruption adapters: 4, dependency violations: 0, cycles: 0

context        public API                                   classes     Ca
Billing        ArchitectureLab\Billing\PublicApi                 11      1
Catalog        ArchitectureLab\Catalog\PublicApi                 10      1
Enrollment     ArchitectureLab\Enrollment\PublicApi              20      0
Notifications  ArchitectureLab\Notifications                      1      0
Reporting      ArchitectureLab\Reporting\PublicApi                9      0

baseline shared model: 4 namespace-level layers, 2 layer violations
```

Пять контекстов, двадцать файлов публичного API, двадцать публичных типов,
четыре ACL-адаптера, ноль нарушений и циклов. Baseline с общей моделью даёт
четыре слоя и два нарушения. Это не значит, что любой shared model хуже
bounded contexts; это значит, что в этом коде общая модель baseline не
изолирована, и checker это показывает.

Теперь полный сценарий:

```bash
make scenario
```

Вывод по умолчанию (event-driven, 12 студентов, 2 курса, capacity 4,
`failEvery = 4`):

```text
Experiment: Course enrollment scenario
One deterministic flow through every chapter.

mode: event-driven, students: 12, offerings: 2, capacity each: 4, fail every: 4
requests: accepted 8, rejected by capacity 4
write model: confirmed 6, cancelled 2, pending 0
outbox: pending before pump 16, delivered 42, failed 0, dead-lettered 0, pending after 0
read model: reserved 6, confirmed 6, cancelled 2, pending 0, entries 8
notifications: 8, stale entries before delivery: 0

Invariants:
  stale-before-pump        ok  read model had 0 entries before delivery
  no-pending-events        ok  0 events are still pending
  no-dead-letters          ok  0 events are dead-lettered
  write-converged          ok  0 enrollments are still pending payment
  read-model-confirmed     ok  read model 6 vs write model 6
  read-model-cancelled     ok  read model 2 vs write model 2
  capacity-conserved       ok  read model reserved 6 vs write reserved 6
  catalog-conserved        ok  catalog reserved 6 vs write reserved 6
  no-oversubscription      ok  6 seats reserved, total capacity 8
  notifications-match      ok  notifications 8 vs terminal enrollments 8
  requests-accounted       ok  8 accepted vs 8 stored enrollments; 4 rejected
```

Почему приняты 8, а не 10? В event-driven режиме место резервируется сразу, а
отказ платежа освобождает его только после доставки события. Два студента,
которые попали на четвёртые места, получают отказ по capacity до того, как
оплата освободит места. В sync-режиме отказ платежа освобождает место сразу, и
успевают пройти 10 запросов.

Синхронный вариант:

```bash
make scenario ARGS="--mode=sync"
```

```text
mode: sync, students: 12, offerings: 2, capacity each: 4, fail every: 4
requests: accepted 10, rejected by capacity 2
write model: confirmed 8, cancelled 2, pending 0
outbox: pending before pump 52, delivered 52, failed 0, dead-lettered 0, pending after 0
read model: reserved 8, confirmed 8, cancelled 2, pending 0, entries 10
notifications: 10, stale entries before delivery: 0
```

Оба режима проходят 11 инвариантов. Разница в принятых запросах — это
архитектурное следствие, а не ошибка: в sync-режиме результат платежа известен
до возврата команды, поэтому место освобождается раньше.

Бенчмарк контекстов:

| Benchmark                   | Итерации |     Медиана |         p95 |         max | ops/sec | PHP delta | peak delta |
| --------------------------- | -------: | ----------: | ----------: | ----------: | ------: | --------: | ---------: |
| `ddd.sync_contexts`         |       20 | 5 695.327ms | 9 231.261ms | 9 231.261ms |       4 |  +2.60 MB |  +11.51 MB |
| `ddd.event_driven_contexts` |       20 | 5 948.853ms | 9 416.664ms | 9 416.664ms |       3 |  +2.60 MB |   +3.31 MB |

Каждая итерация — 50 зачислений, то есть 1 000 на измерение. Эти числа
доминируются SQLite-запросами и цепочкой outbox, а не «стоимостью границы
контекста». Медианы близки; нельзя утверждать, что один стиль быстрее. Можно
утверждать, что event-driven путь создаёт больше событий и больше работы
диспетчеру — это видно по счётчикам, а не только по времени.

---

## 4. Что ломается

**Один процесс и одна база.** Пять контекстов делят SQLite. Это удобно для
лаборатории, но не является моделью распределённой системы. Нет сетевых
таймаутов, ретраев между сервисами, отдельных схем и независимого деплоя.
Границы здесь — границы кода, а не операционные границы.

**Общая база соблазняет.** `Reporting` может прочитать коллекцию Enrollment
напрямую, и checker этого не увидит. Владение коллекциями — договорённость,
подкреплённая именами и ревью, а не правами базы. В production для этого нужны
отдельные схемы или как минимум политика доступа.

**Общий kernel может расти.** Сейчас `Shared` содержит деньги, id, часы и
порты. Если добавить туда `Course` или `Student`, контексты снова начнут
делить модель. Shared kernel должен оставаться маленьким и стабильным; это
самая дорогая часть для изменения.

**Идентификаторы общие.** `OfferingId` и `EnrollmentId` живут в `Shared`.
Это упрощает переводы и делает сценарий читаемым, но связывает контексты
семантикой идентификатора. Если один контекст захочет свою нумерацию, границу
придётся пересматривать.

**Агрегаты не записывают события сами.** Событие создаёт application service
через `EventRecorder`, а не агрегат через `pullEvents()`. Это упрощает
агрегаты и тесты, но означает, что «событие произошло» — решение приложения, а
не свойство модели. Можно забыть записать событие; тесты сценария помогают, но
не гарантируют.

**ACL не бесплатен.** Каждый перевод — это класс, DTO и обработка исключений.
Если контекстов много, количество адаптеров растёт. В лаборатории четыре
адаптера; в реальной системе их может быть десятки, и нужен контрактный тест
для каждого.

**Сценарий предполагает равные capacity.** `no-oversubscription` умножает число
курсов на capacity первого курса. Это верно для fixture с одинаковыми курсами,
но не является общим правилом. В production проверка должна суммировать
capacity каждого курса.

**Платёж детерминирован.** `DeterministicPaymentGateway` падает каждый N-й
вызов, без сети, задержек и реальных кодов ошибок. Он проверяет ветку отказа,
но не моделирует поведение провайдера. Возвраты, частичные платежи и
идемпотентность на стороне провайдера не реализованы.

**Нет авторизации и конкурентности.** Один студент может занять место, другой
не может; но проверка прав, сессии и одновременные запросы из разных процессов
не моделируются. SQLite в одном процессе не даёт ответа на вопрос, что будет
при двух конкурентных `reserveSeat()`.

---

## 5. Как это проверено

- `testReservesAndReleasesSeats`, `testReservingBeyondCapacityIsRefused`,
  `testReleasingAnUnreservedSeatIsRefused` — инварианты `CourseOffering`;
- `testIssueInvoiceAndChargeOnce`, `testRetryReturnsTheExistingDecisionWithoutChargingAgain`,
  `testFailedPaymentIsRecordedAndRetryDoesNotChargeAgain` — идемпотентность
  Billing по `enrollment id`;
- `testInvoiceDomainTransitions` — нельзя оплатить или провалить счёт дважды;
- `testAsyncModeStopsAtPendingPayment`,
  `testSyncSuccessConfirmsTheEnrollment`,
  `testSyncFailureCancelsAndReleasesTheSeat` — два режима координации;
- `testFullCourseBecomesAPublicRejectionWithoutStoredState` — отказ Catalog
  не оставляет мусорного зачисления;
- `testInMemoryAndSqliteAdaptersMakeTheSameDecision` — адаптеры не расходятся
  на сценарии;
- `testEventDrivenScenarioConvergesWithTheDocumentedNumbers` — сценарий
  сходится к задокументированным числам;
- `testSyncScenarioReusesSeatsReleasedByFailedPayments` — sync-режим
  действительно принимает больше запросов;
- `testScenarioWithNoPaymentFailures` — сценарий без отказов;
- `testZeroStudentsExplainsTheProblem` и родственные тесты — некорректные
  параметры дают понятную ошибку, а не тихий ноль.

Архитектурные тесты дополнительно требуют:

- `cross-context-public-api` для всех межконтекстных импортов;
- `application-no-foreign-context` — application не вызывает чужой контекст;
- `delivery-public-api` — сценарий видит только публичные API;
- ноль циклов между пятью модулями.

---

## 6. Что запомнить

- Bounded context — это граница языка и модели, а не граница деплоя.
- У каждого агрегата свои инварианты и свой жизненный цикл.
- Контексты не делят объекты; они делят идентификаторы и явные контракты.
- ACL переводит исключения, DTO и термины; он же локализует знание о соседе.
- Идемпотентность на границе (`enrollment id`) и идемпотентность доставки
  (`event id`) — разные механизмы, и нужны оба.
- Сценарий проверяет согласованность языков через числа, а не через «код
  скомпилировался».
- Sync и event-driven — разные компромиссы; event-driven не «лучше» и не
  «быстрее» по умолчанию.
- Один процесс и одна база — упрощение лаборатории; границы нужно проверять
  отдельно, если выносить контексты в сервисы.

---

## 7. Упражнения

1. Добавьте контекст `Reviews` с агрегатом `Review` и публичным API. Как он
   узнает о подтверждённом зачислении, не импортируя Enrollment?
2. Разделите `Shared` на «действительно общее» и «удобное». Что можно убрать
   без изменения сценария?
3. Сделайте `Reporting` независимым от общей базы: предложите отдельный
   read-store и способ его синхронизации.
4. Добавьте возврат платежа. Где находится его идемпотентность — в Billing, в
   провайдере или в Enrollment?
5. Запустите сценарий с `--fail-every=0` и `--mode=sync`. Объясните, почему
   принятых столько же, сколько мест.
6. Смоделируйте двух студентов, одновременно претендующих на последнее место.
   Какие транзакции и блокировки нужны в SQLite и в PostgreSQL?

---

## 8. Что дальше

Это последняя глава книги, но не конец лаборатории. Следующие шаги, которые
здесь сознательно не сделаны: HTTP API, реальный брокер, отдельные схемы для
контекстов, replay проекций, backoff и мониторинг outbox, конкурентные тесты,
аутентификация и авторизация. Каждый из них меняет архитектуру, а не только
код. Лаборатория останавливается на `v1.0.0` с работающим сценарием, измеренными
числами и честно названными ограничениями — чтобы следующий шаг можно было
обсуждать на фактах, а не на диаграммах.
