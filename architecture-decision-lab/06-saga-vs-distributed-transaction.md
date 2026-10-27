# Глава 6. Saga и двухфазный коммит: компенсация против удержания

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: outbox. Следующая: eventual consistency (глава 7).

Когда у сервисов нет общей транзакции, остаются два честных способа не оставить
половину брони: сага и двухфазный коммит. Они решают одну задачу, но платят
разными валютами. Сага коммитит каждый шаг сразу и при ошибке компенсирует уже
совершённые эффекты. Двухфазный коммит сначала удерживает ресурсы у всех
участников и только потом принимает решение.

Разница видна не в успешном сценарии, а в момент отказа. Сага оставляет
промежуточные состояния видимыми и чинит их после. 2PC прячет промежуточное
состояние за удержаниями, но если координатор падает после prepare, эти
удержания остаются висеть, пока кто-то явно не решит commit или abort.

Глава отвечает на вопрос: что именно система делает между отказом и
согласованным состоянием — и кто платит за это время.

---

## 1. Проблема

В главе 4 база была разделена по сервисам. В главе 5 появился outbox, чтобы
событие переживало коммит. Но остался вопрос о самой брони: рейс, отель, машина
и платёж лежат в разных базах. Одна SQLite-транзакция их не покрывает.

Есть два неприятных сценария:

1. **Бизнес-отказ.** Платёж отклонён. Резервации уже могли быть созданы.
2. **Потерянный ответ или падение.** Резервация выполнена, но вызывающий получил
   исключение. Эффект существует, а вызывающий об этом не знает.

Локальная транзакция умеет откатывать только то, что ещё не закоммичено. После
коммита остаётся компенсация. Сага делает компенсацию явной частью протокола:
каждый шаг имеет действие и компенсацию, а координатор вызывает их в нужном
порядке.

2PC идёт другим путём. Участник по команде prepare не фиксирует эффект
окончательно, а удерживает ресурс: уменьшает доступность и записывает резервацию
в состоянии `prepared`. Пока удержание живо, ресурсом нельзя воспользоваться.
Если все участники ответили «готов», координатор принимает решение commit. Если
кто-то не готов — abort. Сложность начинается после prepare: решение может
потеряться, а удержания — остаться.

---

## 2. Устройство

### 2.1. Сага коммитит шаг сразу

В саге четыре шага: рейс, отель, машина, платёж. Каждый шаг — это обычный
локальный вызов сервиса, который сразу коммитит своё состояние. Вот последний
шаг и запуск координатора:

```php
        $steps[] = new SagaStep(
            'payment.authorize',
            function () use ($booking, &$amount): void {
                $payment = $this->payment->authorize($booking->id, $amount);
                $this->requirePayment($payment);
            },
            function () use ($booking): void {
                $this->payment->void($booking->id);
            },
        );

        $saga = $this->saga()->run($steps);
```

[`src/Services/BookingWorkflow.php` L180-L191](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Services/BookingWorkflow.php#L180-L191)

Первые три шага устроены так же: `reserve()` и компенсация `release()`. Важно,
что `reserve()` не ждёт остальных шагов. После первого шага рейс уже занят, и
другой читатель может это увидеть. Сага не изолирует промежуточное состояние;
она обещает только довести систему до согласованного финала.

`SagaCoordinator` при ошибке собирает кандидатов на компенсацию: сначала упавший
шаг, затем завершённые в обратном порядке.

```php
    private function compensate(SagaStep $failed, array $completed, string $reason): SagaResult
    {
        $attempts = 0;
        $failures = 0;

        /** @var list<SagaStep> $candidates */
        $candidates = [$failed];

        foreach (array_reverse($completed) as $step) {
            $candidates[] = $step;
        }

        foreach ($candidates as $step) {
            ++$attempts;
            $this->counters->increment('compensationAttempts');

            try {
                ($step->compensation)();
                $this->counters->increment('compensations');
            } catch (\Throwable) {
                ++$failures;
                $this->counters->increment('compensationFailures');
            }
        }

        return new SagaResult(
            false,
            $failed->name,
            array_map(static fn (SagaStep $step): string => $step->name, $completed),
            $attempts,
            $failures,
            $reason,
        );
    }
```

[`src/Saga/SagaCoordinator.php` L52-L85](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Saga/SagaCoordinator.php#L52-L85)

Упавший шаг входит в список не из вежливости. Если ответ потерялся после записи,
эффект упавшего шага уже существует. Компенсация должна его снять. Именно
поэтому в эксперименте с потерей ответа на машине сага делает три компенсации:
машина (упавший шаг), отель и рейс. Платёж ещё не вызывался.

Обратите внимание на `compensationFailures`: ошибка компенсации не прерывает
остальные. Это разумно для саги — лучше освободить всё, что можно, — но
означает, что координатор не гарантирует восстановление. Он гарантирует только
попытку и счётчик.

### 2.2. 2PC сначала удерживает

Координатор 2PC выполняет prepare у всех участников. `$attempted` включает
участника до вызова: если prepare упал, его тоже нужно попытаться отменить.
`$prepared` включает только тех, кто ответил успешно.

```php
        try {
            foreach ($participants as $participant) {
                $attempted[] = $participant;
                $this->faults->before('2pc.prepare.' . $participant->name);
                ($participant->prepare)();
                $prepared[] = $participant;
                $this->counters->increment('twoPhasePrepared');
                $this->faults->after('2pc.prepare.' . $participant->name);
            }
        } catch (\Throwable $throwable) {
            $aborted = $this->abort($attempted);
            $this->counters->increment('twoPhaseAborted');

            return new TwoPhaseResult(
                'aborted',
                array_map(static fn (Participant $p): string => $p->name, $prepared),
                [],
                $aborted,
                0,
                $throwable->getMessage(),
            );
        }

        try {
            $this->faults->before('2pc.commit');
        } catch (\Throwable $throwable) {
            $this->counters->increment('twoPhaseCrashedAfterPrepare');
            $this->counters->set('twoPhaseHoldsGauge', count($prepared));

            return new TwoPhaseResult(
                'prepared',
                array_map(static fn (Participant $p): string => $p->name, $prepared),
                [],
                [],
                count($prepared),
                $throwable->getMessage(),
            );
        }
```

[`src/Saga/TwoPhaseCommitCoordinator.php` L36-L73](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Saga/TwoPhaseCommitCoordinator.php#L36-L73)

Здесь два разных отказа. Если prepare упал, координатор отменяет всех
попробованных участников и возвращает `aborted`. Если prepare прошёл у всех, но
упал вызов перед решением commit, координатор возвращает `prepared` и фиксирует
`twoPhaseHoldsGauge`. Это не «всё хорошо» и не «всё плохо». Это состояние, в
котором решение ещё не принято, а ресурсы уже заняты.

Prepare — не запись в локальный лог. Это реальное удержание. `InventoryService`
уменьшает `available` в той же транзакции, которая вставляет резервацию со
статусом `prepared`:

```php
        $price = $this->price($resourceId);

        if ($price === 0) {
            return ReservationResult::failed($this->type, $resourceId, $units, 'unknown resource');
        }

        $this->faults->before($this->type->value . '.write');

        $result = $this->db->transaction(function () use ($bookingId, $resourceId, $units, $price, $wanted): ?ReservationResult {
            $updated = $this->db->execute(
                sprintf(
                    'UPDATE %s SET available = available - :units
                     WHERE id = :id AND available >= :units',
                    $this->type->table(),
                ),
                ['units' => $units, 'id' => $resourceId],
            );

            if ($updated === 0) {
                return null;
            }

            $this->db->execute(
                'INSERT INTO reservations
                    (booking_id, resource_type, resource_id, units, status, created_at, updated_at)
                 VALUES (:booking_id, :type, :resource_id, :units, :status, :now, :now)',
                [
                    'booking_id' => $bookingId,
                    'type' => $this->type->value,
                    'resource_id' => $resourceId,
                    'units' => $units,
                    'status' => $wanted->value,
                    'now' => $this->clock->now(),
                ],
            );

            return new ReservationResult(true, $wanted, $resourceId, $this->type, $units, $price);
        });

        if ($result === null) {
            return ReservationResult::failed($this->type, $resourceId, $units, 'sold out');
        }

        $this->counters->increment('reservations');
        $this->counters->increment('inventoryWrites');
        $this->faults->after($this->type->value . '.write');

        return $result;
```

[`src/Services/InventoryService.php` L259-L306](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Services/InventoryService.php#L259-L306)

Для 2PC `$wanted` равен `Prepared`. Для саги — `Committed`. Разница только в
статусе и в том, что 2PC позже отдельно вызывает `commit()`. Но `available`
уменьшается в обоих случаях сразу. Поэтому удержание видно другим бронированиям:
в эксперименте после падения координатора доступность рейса равна 9, а не 10.

### 2.3. Решение и восстановление

Если prepare прошёл, координатор выполняет commit у всех участников. Если commit
падает на середине, часть участников уже зафиксирована, часть — нет. Координатор
возвращает `partial-commit` и оставляет удержания.

```php
    /**
     * Recovery is an explicit decision. A real coordinator would persist it;
     * this lab makes the choice visible in the report.
     *
     * @param list<Participant> $participants
     */
    public function recover(array $participants, string $decision): TwoPhaseResult
    {
        if (!in_array($decision, ['commit', 'abort'], true)) {
            throw new \InvalidArgumentException('decision must be commit or abort');
        }

        if ($decision === 'abort') {
            $aborted = $this->abort($participants);
            $this->counters->increment('twoPhaseRecovered');
            $this->counters->set('twoPhaseHoldsGauge', 0);

            return new TwoPhaseResult('recovered-abort', [], [], $aborted, 0);
        }

        $committed = [];

        foreach ($participants as $participant) {
            ($participant->commit)();
            $committed[] = $participant;
            $this->counters->increment('twoPhaseCommitted');
        }

        $this->counters->increment('twoPhaseRecovered');
        $this->counters->set('twoPhaseHoldsGauge', 0);

        return new TwoPhaseResult('recovered-commit', [], array_map(static fn (Participant $p): string => $p->name, $committed), [], 0);
    }
```

[`src/Saga/TwoPhaseCommitCoordinator.php` L108-L140](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Saga/TwoPhaseCommitCoordinator.php#L108-L140)

`recover()` — не автоматическое восстановление. Это явная команда: кто-то решает
commit или abort. В лаборатории решение передаётся аргументом, в реальной
системе его нужно где-то хранить. Если координатор потерял решение, участники не
должны решать за него: иначе один закоммитит, другой откатит, и получится худший
из возможных результатов.

`BookingWorkflow` после 2PC различает три исхода: `committed` подтверждает
бронь, `aborted` отменяет и освобождает, `prepared` оставляет бронь в `pending`
до восстановления.

```php
        $result = $coordinator->execute($participants);

        /** @var list<BookingLine> $lines */
        if ($result->status === 'committed') {
            $eventPublished = $this->finalize($booking, BookingStatus::Confirmed, $lines);

            return new BookingResult(
                $booking->id,
                BookingStatus::Confirmed,
                true,
                $this->clock->now() - $startedAt,
                $this->clock->now() - $startedAt,
                0,
                '',
                $lines,
                $eventPublished,
            );
        }

        if ($result->status === 'aborted') {
            $released = $this->released($lines);
            $eventPublished = $this->finalize($booking, BookingStatus::Cancelled, $released, $result->reason);

            return new BookingResult(
                $booking->id,
                BookingStatus::Cancelled,
                false,
                $this->clock->now() - $startedAt,
                $this->clock->now() - $startedAt,
                count($result->aborted),
                $result->reason,
                $released,
                $eventPublished,
            );
        }

        $this->finalize($booking, BookingStatus::Pending, $lines, $result->reason);

        return new BookingResult(
            $booking->id,
            BookingStatus::Pending,
            false,
            $this->clock->now() - $startedAt,
            $this->clock->now() - $startedAt,
            0,
            $result->reason,
            $lines,
            false,
        );
```

[`src/Services/BookingWorkflow.php` L268-L316](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Services/BookingWorkflow.php#L268-L316)

В состоянии `pending` нет ничего постыдного. Это честное признание, что решение
ещё не принято. Плохо, если `pending` считается успехом или теряется из виду. В
эксперименте `2pc-crash-commit` и `2pc-crash-abort` начинаются именно с такого
`pending`, а затем вызывают `recoverTwoPhase()`.

---

## 3. Эксперимент

```bash
make experiment ARGS="transaction:saga-2pc"
```

По умолчанию capacity 10, seed `transaction`. Пять случаев: два отката саги,
отказ prepare в 2PC и два падения 2PC после prepare — с восстановлением в commit
и в abort. Вот реальный вывод:

```text
Experiment: Saga vs two-phase commit
capacity=10 seed=transaction

case                   status       comp  holds  avail  elapsed payment     outbox
saga-lost-response     cancelled       3      0     10    12 ms none             0
saga-payment-failure   cancelled       4      0     10    16 ms none             0
2pc-prepare-abort      cancelled       3      0     10    10 ms none             0
2pc-crash-commit       confirmed       0      4      9     8 ms captured         0
2pc-crash-abort        cancelled       4      4     10     8 ms voided           0
```

Сначала о колонках, потому что они означают не одно и то же.

- `comp` у саги — число **попыток** компенсации, а не число успешных эффектов. В
  `saga-payment-failure` четыре попытки: платёж (no-op, потому что авторизации
  не было), машина, отель, рейс. Успешных освобождений три.
- `comp` у 2PC — число участников, которых координатор отменил. В
  `2pc-prepare-abort` это три попытанных участника: рейс, отель, машина. Платёж
  до prepare не дошёл.
- `holds` — снимок `twoPhaseHoldsGauge` сразу после первой попытки, до
  восстановления. В `2pc-crash-commit` удержания потом коммитятся, но снимок
  остаётся 4. Это не число удержаний в конце.
- `elapsed` в случаях с падением — latency **восстановления**, а не первой
  попытки. `recoverTwoPhase()` возвращает новый `BookingResult`, и его
  `clientLatencyMs` считается от начала recovery.

Теперь по строкам.

**`saga-lost-response`.** Отказ `car.write(after)`: запись машины уже произошла,
но ответ потерян. Сага не знает, существует ли эффект, поэтому компенсирует
упавший шаг первым: машину, затем отель и рейс. Три компенсации, доступность
возвращается к 10, платёж не создавался. Это тот же сценарий, что в главе 1, но
здесь важна цена: 12 мс логического времени и три удалённых вызова на
компенсации.

**`saga-payment-failure`.** Платёж падает до эффекта. Рейс, отель и машина уже
закоммичены. Сага пытается компенсировать платёж (no-op), затем машину, отель и
рейс. Четыре попытки, три реальных освобождения. 16 мс — потому что путь успел
дойти до платежа, а затем прошёл все четыре компенсации.

**`2pc-prepare-abort`.** Падает prepare машины. Рейс и отель уже удерживаются,
машина попробована, но не удержана. Координатор отменяет всех трёх попытанных,
платёж не трогает. Доступность 10, удержаний нет, бронь отменена. Это чистый
откат без компенсационных эффектов: `abort()` вызывает `release()`, но для
машины резервации нет, поэтому освобождать нечего.

**`2pc-crash-commit`.** Все четыре prepare прошли, но координатор падает перед
решением commit. Бронь остаётся `pending`, четыре удержания живы, доступность
рейса 9. Затем `recoverTwoPhase(..., 'commit')` фиксирует все четыре участника,
захватывает платёж и подтверждает бронь. `avail = 9` — это правильный финальный
результат: подтверждённая бронь действительно заняла единицу. `holds = 4` —
снимок до восстановления.

**`2pc-crash-abort`.** То же падение, но восстановление выбирает abort. Четыре
участника освобождаются, платёж отменяется (`voided`), доступность возвращается
к 10, бронь отменена. `comp = 4` — это четыре отменённых участника, а не четыре
успешные компенсации саги.

Главный результат не в том, что 2PC «надёжнее». Главный результат — в том, что
после падения координатор оставляет систему в состоянии, которое нельзя
игнорировать. Сага оставляет видимые промежуточные состояния и чинит их
компенсациями. 2PC оставляет удержания и ждёт явного решения. Оба варианта
требуют восстановления; разница в том, что именно восстанавливать.

---

## 4. Бенчмарк

```bash
make benchmark ARGS="06-transaction"
```

300 успешных бронирований на измерение. Локальный случай — одна база и одна
транзакция; сага и 2PC — база на сервис и соответствующий протокол.

| benchmark                       | итераций | медиана |     p95 | ops/sec | медиана на бронь |
| ------------------------------- | -------: | ------: | ------: | ------: | ---------------: |
| `transaction.local_booking`     |      300 | 370.805 | 453.184 |     809 |         1 236 µs |
| `transaction.saga_booking`      |      300 | 477.649 | 511.415 |     628 |         1 592 µs |
| `transaction.two_phase_booking` |      300 | 610.844 | 670.437 |     491 |         2 036 µs |

Сага на 28,8% медленнее локальной транзакции, 2PC — на 64,7% медленнее локальной
и на 27,9% медленнее саги. Это успешные брони, без отказов и восстановления.
Сага платит за четыре независимых коммита и четыре удалённых вызова. 2PC платит
за четыре prepare и четыре commit — вдвое больше переходов через границу.

Но это сравнение **референсных конфигураций**, а не одной переменной. Локальный
случай использует shared-базу и модульный монолит; сага и 2PC — per-service базу
и микросервисы. В разницу входят шесть SQLite-файлов, транспорт и схема, а не
только протокол транзакции. Нельзя приписать все 28,8% саге, а все 64,7% —
двухфазному коммиту.

Есть и вторая оговорка, ещё важнее. Сага оставляет платёж в состоянии
`authorized`: capture и settlement находятся за границей бронирования. 2PC в
commit вызывает `capture()` и реально меняет статус платежа на `captured`.
Значит, успешные 2PC-бронирования делают больше платёжной работы, чем успешные
саги. Бенчмарк этого не разделяет, поэтому его нельзя читать как чистое
сравнение «сага против 2PC при одинаковом платёжном эффекте».

И третья: бенчмарк не измеряет окно блокировки. Удержания живут в логическом
времени недолго, потому что все участники отвечают сразу. В эксперименте
удержание видно как `avail = 9` и `holds = 4`, но его длительность и влияние на
другие бронирования не измерены. В реальной системе именно это окно может съесть
capacity и превратить 2PC в узкое место.

---

## 5. Что ломается и что не измерено

### Сага

**Компенсация может не сработать.** `SagaCoordinator` ловит ошибку, увеличивает
`compensationFailures` и идёт дальше. Он не ретраит и не пишет компенсацию в
outbox. Если `release()` упал, ресурс останется занятым, а бронь — отменённой. В
лаборатории такого сценария нет, но код его допускает.

**Компенсация должна быть идемпотентной.** `release()` и `void()` в лаборатории
идемпотентны: повторный release уже освобождённой резервации и повторный void
отменённого платежа безопасны. В реальной системе возврат денег может быть не
идемпотентным, и тогда потерянный ответ на компенсацию становится отдельной
проблемой.

**Нет изоляции.** Между шагами саги рейс уже занят, а отель ещё нет. Другой
читатель увидит эту половину. Компенсация вернёт состояние, но не отменит факт,
что оно было видимым. В лаборатории нет конкурентных читателей, поэтому окно не
наблюдается, но архитектурно оно есть.

**Координатор не персистентен.** `SagaCoordinator` живёт в памяти и выполняет
шаги синхронно. Если процесс умрёт между шагами, никто не продолжит сагу. Нет
журнала шагов, нет таймаутов, нет фонового recovery. Это модель протокола, а не
production-оркестратор.

**Компенсация — не rollback.** `void()` отменяет авторизацию, но не возвращает
деньги, если capture уже произошёл. В этой лаборатории capture находится вне
границы бронирования, поэтому проблема не воспроизводится. В реальной системе её
нужно решать отдельно: refund, settlement, бухгалтерия.

### Двухфазный коммит

**Блокирующее окно.** Пока удержания живы, ресурс недоступен. В эксперименте
доступность рейса падает с 10 до 9 и остаётся такой до recovery. Если
координатор недоступен долго, удержания могут исчерпать capacity. Лаборатория не
моделирует очередь других бронирований, поэтому не показывает, как быстро это
происходит.

**Падение координатора после prepare.** Это главный сценарий 2PC. Участники не
знают решение и не имеют права решать сами. Нужен персистентный журнал
координатора и протокол восстановления. В лаборатории решение передаётся
вручную, а `TwoPhaseCommitCoordinator` не хранит его между вызовами.

**Частичный commit.** Если commit падает на середине, часть участников уже
зафиксирована. Координатор возвращает `partial-commit` и оставляет удержания, но
автоматического доразрешения нет. В реальной системе нужен идемпотентный повтор
commit для уже зафиксированных участников и явная политика для остальных.

**Эвристические решения.** Если координатор потерял решение, участники могут
самостоятельно выбрать commit или abort. Это может привести к расхождению: один
закоммитил, другой откатил. Лаборатория такие решения не моделирует.

**2PC не покрывает бронь и outbox.** Протокол атомарен для участников, но
`finalize()` — запись брони и outbox-события — выполняется после commit. Если
она упадёт, ресурсы уже зафиксированы, а бронь не подтверждена. В лаборатории
этот отказ не инжектируется, но граница видна по коду.

**Это не распределённая транзакция SQLite.** `TwoPhaseCommitCoordinator`
моделирует протокол и его блокирующее окно. Он не открывает транзакцию сразу в
шести файлах и не даёт атомарности на уровне СУБД. Удержания — это строки в
таблицах, а не блокировки, которые кто-то снимет при падении процесса.

---

## 6. Как это проверено

Тест на отказ платежа в саге фиксирует и число попыток, и реальное состояние
ресурсов:

```php
    public function testSagaCompensatesCompletedStepsWhenPaymentFails(): void
    {
        $counters = new Counters();
        $faults = new FaultInjector(step: 'payment.authorize', counters: $counters);
        $lab = $this->lab($this->config(transaction: TransactionMode::Saga), $faults, $counters);
        $lab->seed('f1', 'h1', 'c1', capacity: 4);

        $result = $lab->book($this->trip());

        self::assertFalse($result->accepted);
        self::assertSame(BookingStatus::Cancelled, $result->status);
        self::assertSame(4, $result->compensations);
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Flight, 'f1'));
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Hotel, 'h1'));
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Car, 'c1'));
        self::assertSame(ReservationStatus::Released, $lab->flight->reservationStatus($result->bookingId, 'f1'));
        self::assertNull($lab->payment->status($result->bookingId));
        self::assertSame(3, $lab->counters->get('reservationsReleased'));
        self::assertSame(1, $lab->outbox->countPending());
    }
```

[`tests/Integration/BookingWorkflowTest.php` L53-L72](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/BookingWorkflowTest.php#L53-L72)

`compensations = 4`, но `reservationsReleased = 3`: четвёртая попытка — no-op
void платежа, который не был авторизован. Тест специально проверяет оба числа,
чтобы не смешивать попытки с эффектами.

Тест на падение 2PC после prepare фиксирует удержания и восстановление в commit:

```php
    public function testTwoPhaseCommitCrashLeavesHoldsAndRecoveryCanCommitThem(): void
    {
        $counters = new Counters();
        $faults = new FaultInjector(step: '2pc.commit', counters: $counters);
        $lab = $this->lab($this->twoPhaseConfig(), $faults, $counters);
        $lab->seed('f1', 'h1', 'c1', capacity: 4);

        $result = $lab->book($this->trip());

        self::assertSame(BookingStatus::Pending, $result->status);
        self::assertSame(4, $lab->counters->get('twoPhaseHoldsGauge'));
        self::assertSame(ReservationStatus::Prepared, $lab->flight->reservationStatus($result->bookingId, 'f1'));
        self::assertSame(PaymentStatus::Prepared, $lab->payment->status($result->bookingId));
        self::assertSame(3, $lab->sourceAvailability(ResourceType::Flight, 'f1'));

        $booking = $lab->store->get($result->bookingId);
        self::assertNotNull($booking);
        $recovered = $lab->workflow->recoverTwoPhase($booking, 'commit');

        self::assertSame(BookingStatus::Confirmed, $recovered->status);
        self::assertSame(ReservationStatus::Committed, $lab->flight->reservationStatus($result->bookingId, 'f1'));
        self::assertSame(PaymentStatus::Captured, $lab->payment->status($result->bookingId));
        self::assertSame(3, $lab->sourceAvailability(ResourceType::Flight, 'f1'));
        self::assertSame(1, $lab->outbox->countPending());
    }
```

[`tests/Integration/BookingWorkflowTest.php` L138-L162](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/BookingWorkflowTest.php#L138-L162)

Тест утверждает промежуточное состояние (`pending`, четыре удержания,
`prepared`) и финальное (`confirmed`, `committed`, `captured`). Без
промежуточных утверждений тест прошёл бы и на реализации, которая сразу коммитит
всё и никогда не показывает окно.

Отдельный тест `testTwoPhaseCommitCrashCanBeRecoveredByAborting` делает тот же
сценарий с решением abort и проверяет `Released`, `Voided` и доступность 4. Тест
`testTwoPhaseCommitAbortsAllHoldsWhenACarPrepareFails` проверяет чистый откат
без удержаний.

Полный прогон на момент тега: 74 теста, 3 468 assertions, PHPStan level 8 без
ошибок, PHP-CS-Fixer без изменений.

---

## 7. Что запомнить

- Сага и 2PC решают одну проблему разными средствами: компенсация уже
  закоммиченного против удержания ещё не закоммиченного.
- Сага коммитит каждый шаг сразу и оставляет промежуточные состояния видимыми.
  2PC прячет их за удержаниями, но блокирует ресурсы.
- Упавший шаг саги тоже нужно компенсировать: потерянный ответ означает, что
  эффект мог сохраниться.
- `compensations` — это попытки, а не обязательно успешные эффекты. Читайте
  счётчики ресурсов вместе с ними.
- 2PC после падения координатора не «восстанавливается сам». Нужно явное решение
  commit или abort и персистентный журнал.
- Удержание — это реальная занятая capacity. В эксперименте `avail = 9` и
  `holds = 4`; это не абстрактное «заблокировано».
- 2PC не покрывает запись брони и outbox. Атомарность участников не равна
  атомарности всего workflow.
- Сравнение бенчмарков включает разные базы, формы развёртывания и даже разный
  платёжный эффект. Называйте эти различия до того, как называть проценты.
- Компенсация — не rollback и не refund. Это бизнес-операция, которую нужно
  проектировать и проверять отдельно.

---

## 8. Упражнения

1. Добавьте отказ `2pc.prepare.payment` после успешных prepare рейса, отеля и
   машины. Сколько участников отменит координатор и что покажет `holds`?
2. Смоделируйте падение `commit` после двух успешных участников. Какие строки
   останутся в состоянии `prepared`, а какие — в `committed`? Как должен
   выглядеть идемпотентный recovery?
3. Почему `2pc-crash-commit` показывает `avail = 9`, а `2pc-crash-abort` —
   `avail = 10`? Объясните через состояние резерваций, а не через итоговый
   статус брони.
4. Предложите, как хранить решение координатора между вызовами. Какие поля нужны
   в журнале и как пережить падение после записи решения, но до commit всех
   участников?
5. Сравните `void()` и `refund()` как компенсации. Что изменится, если платёж
   уже захвачен, а возврат не идемпотентен?
6. Постройте конфигурацию, в которой сага и 2PC делают одинаковый платёжный
   эффект. Какие ещё различия останутся в бенчмарке?

---

Дальше — глава 7. Она спрашивает, что видит читатель, пока событие ещё не дошло
до проекции, и сколько стоит окно устаревания.
