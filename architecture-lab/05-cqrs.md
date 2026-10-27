# Глава 5. CQRS: две модели, две стоимости и окно устаревания

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: модульный монолит. Дальше: событийная доставка (глава 6).

CQRS расшифровывается как Command Query Responsibility Segregation: команды
изменяют состояние, запросы читают, и у них могут быть разные модели. В
лаборатории это не два сервиса и не две базы. Это два пути в одном процессе:
write-side загружает агрегаты и пишет документы, read-side читает проекции,
построенные из событий.

Главный вопрос главы не «быстрее ли читать проекцию». Главный вопрос — что
происходит между записью и чтением. Read model обновляется не в момент команды,
а когда доставлено событие. Пока outbox не разобран, проекция отстаёт. Это окно
существует по построению, и сценарий специально его наблюдает. CQRS без честного
разговора об eventual consistency — это просто два названия для одного запроса.

---

## 1. Проблема

В предыдущих главах чтение шло по write-модели. `EnrollmentQueryService::all()`
возвращает все зачисления, `forStudent()` читает всю коллекцию и фильтрует в
PHP. Для диагностики это нормально; для экрана «мои курсы» — плохо. Чем больше
данных, тем дороже каждый запрос, и тем сильнее read-требования влияют на форму
агрегатов.

Классический ответ — разделить модели:

- **write model** отвечает за инварианты: нельзя занять место сверх capacity,
  нельзя подтвердить отменённое зачисление, нельзя списать дважды;
- **read model** отвечает на вопросы: сколько мест осталось, какие курсы у
  студента, сколько зачислений подтверждено.

У моделей разные потребности. Write-модели нужны агрегаты и транзакции.
Read-модели нужны денормализованные документы и быстрый доступ по ключу запроса.
CQRS разрешает им быть разными.

Цена — согласованность. Если read model строится из событий, она не обязана
быть актуальной в момент ответа. Нужно ответить: какое отставание допустимо,
как его измерять и что видит пользователь. В лаборатории ответ такой: отставание
наблюдается до `pump()`, после доставки проекция сходится с write-моделью, и
сценарий проверяет это инвариантом.

---

## 2. Устройство

### 2.1. Две шины

`CommandBus` хранит один обработчик на класс команды. Он не ищет обработчик по
строке, не сканирует контейнер и не делает магию: регистрация явная.

```php
/**
 * One handler per command class.
 *
 * The bus is intentionally tiny: its job is to make the write/read split
 * explicit and to keep delivery code from constructing application services.
 */
final class CommandBus
{
    /** @var array<class-string, Closure(object): mixed> */
    private array $handlers = [];

    /**
     * @param class-string $command
     * @param callable(object): mixed $handler
     */
    public function register(string $command, callable $handler): void
    {
        $this->handlers[$command] = Closure::fromCallable($handler);
    }

    public function dispatch(object $command): mixed
    {
        $class = $command::class;
        $handler = $this->handlers[$class] ?? null;

        if ($handler === null) {
            throw new LogicException(sprintf('No handler registered for %s', $class));
        }

        return $handler($command);
    }
}
```

[`src/Cqrs/CommandBus.php` L10-L41](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Cqrs/CommandBus.php#L10-L41)

`QueryBus` устроен так же, но метод называется `ask()`. Это не косметика: имя
подчёркивает, что запрос не меняет состояние и не обязан возвращать агрегат.

```php
/** The read-side counterpart of CommandBus. */
final class QueryBus
{
    /** @var array<class-string, Closure(object): mixed> */
    private array $handlers = [];

    /**
     * @param class-string $query
     * @param callable(object): mixed $handler
     */
    public function register(string $query, callable $handler): void
    {
        $this->handlers[$query] = Closure::fromCallable($handler);
    }

    public function ask(object $query): mixed
    {
        $class = $query::class;
        $handler = $this->handlers[$class] ?? null;

        if ($handler === null) {
            throw new LogicException(sprintf('No handler registered for %s', $class));
        }

        return $handler($query);
    }
}
```

[`src/Cqrs/QueryBus.php` L10-L36](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Cqrs/QueryBus.php#L10-L36)

Шины не являются event bus. Они не доставляют сообщения подписчикам и не
хранят историю. Их задача — отделить delivery-код от конкретных обработчиков и
сделать направление сообщения явным.

### 2.2. Проекция ёмкости

`OfferingCapacityProjection` слушает шесть событий и поддерживает один документ
на курс: capacity, reserved, pending, confirmed, cancelled.

```php
    public function handle(EventEnvelope $event): void
    {
        $offeringId = (string) ($event->payload['offering_id'] ?? '');

        if ($offeringId === '') {
            return;
        }

        $document = $this->store->get(self::COLLECTION, $offeringId) ?? [
            'offering_id' => $offeringId,
            'title' => '',
            'capacity' => 0,
            'reserved_seats' => 0,
            'pending' => 0,
            'confirmed' => 0,
            'cancelled' => 0,
        ];

        match ($event->name) {
            EventNames::COURSE_OFFERING_PUBLISHED => $this->published($document, $event),
            EventNames::SEAT_RESERVED => $document['reserved_seats'] = (int) $document['reserved_seats'] + 1,
            EventNames::SEAT_RELEASED => $document['reserved_seats'] = max(0, (int) $document['reserved_seats'] - 1),
            EventNames::ENROLLMENT_REQUESTED => $document['pending'] = (int) $document['pending'] + 1,
            EventNames::ENROLLMENT_CONFIRMED => $this->confirmed($document),
            EventNames::ENROLLMENT_CANCELLED => $this->cancelled($document),
            default => null,
        };

        if (!in_array($event->name, [
            EventNames::COURSE_OFFERING_PUBLISHED,
            EventNames::SEAT_RESERVED,
            EventNames::SEAT_RELEASED,
            EventNames::ENROLLMENT_REQUESTED,
            EventNames::ENROLLMENT_CONFIRMED,
            EventNames::ENROLLMENT_CANCELLED,
        ], true)) {
            return;
        }

        $this->store->put(self::COLLECTION, $offeringId, $document);
        ++$this->counters->projectionUpdates;
    }
```

[`src/Reporting/Projection/OfferingCapacityProjection.php` L31-L72](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Reporting/Projection/OfferingCapacityProjection.php#L31-L72)

Здесь важно не то, что проекция «считает», а **как** она считает. События
содержат не абсолютное число мест, а факт изменения: `seat_reserved` —
инкремент, `seat_released` — декремент. Если событие доставлено поздно или
повторно, инкремент всё равно останется инкрементом; абсолютное `seats_taken`
могло бы перезаписать более новое состояние.

Переходы между статусами вынесены в методы:

```php
    private function confirmed(array &$document): void
    {
        $document['pending'] = max(0, (int) $document['pending'] - 1);
        $document['confirmed'] = (int) $document['confirmed'] + 1;
    }

    /**
     * @param array<string, mixed> $document
     */
    private function cancelled(array &$document): void
    {
        $document['pending'] = max(0, (int) $document['pending'] - 1);
        $document['cancelled'] = (int) $document['cancelled'] + 1;
    }
```

[`src/Reporting/Projection/OfferingCapacityProjection.php` L86-L99](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Reporting/Projection/OfferingCapacityProjection.php#L86-L99)

`max(0, ...)` защищает от отрицательного pending при повторной доставке. Но это
защита от конкретной ошибки, а не полноценная идемпотентность: повторный
`enrollment.requested` увеличит pending дважды. Именно поэтому в главе 6
слушатели оборачиваются в `IdempotentListener`, а не полагаются на арифметику.

### 2.3. Проекция расписания

`StudentScheduleProjection` хранит один документ на студента и карту зачислений.
Ключ карты — `enrollment_id`, поэтому повторное событие перезаписывает статус, а
не добавляет дубль.

```php
    public function handle(EventEnvelope $event): void
    {
        if (!in_array($event->name, [
            EventNames::ENROLLMENT_REQUESTED,
            EventNames::ENROLLMENT_CONFIRMED,
            EventNames::ENROLLMENT_CANCELLED,
        ], true)) {
            return;
        }

        $studentId = (string) $event->payload['student_id'];
        $enrollmentId = (string) $event->payload['enrollment_id'];
        $document = $this->store->get(self::COLLECTION, $studentId) ?? [
            'student_id' => $studentId,
            'entries' => [],
        ];
        /** @var array<string, array<string, string>> $entries */
        $entries = is_array($document['entries'] ?? null) ? $document['entries'] : [];
        $statuses = [
            EventNames::ENROLLMENT_REQUESTED => 'pending_payment',
            EventNames::ENROLLMENT_CONFIRMED => 'confirmed',
            EventNames::ENROLLMENT_CANCELLED => 'cancelled',
        ];
        $status = $statuses[$event->name];
        $entries[$enrollmentId] = [
            'enrollment_id' => $enrollmentId,
            'offering_id' => (string) $event->payload['offering_id'],
            'course_title' => (string) $event->payload['course_title'],
            'status' => $status,
        ];
        $document['entries'] = $entries;
        $this->store->put(self::COLLECTION, $studentId, $document);
        ++$this->counters->projectionUpdates;
    }
```

[`src/Reporting/Projection/StudentScheduleProjection.php` L24-L57](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Reporting/Projection/StudentScheduleProjection.php#L24-L57)

Документ — это map, а не список. Это осознанный выбор: статус зачисления — это
последнее известное состояние, а не журнал переходов. Если нужна история, нужен
другой projection с массивом событий, и его стоимость будет другой.

### 2.4. Query handler читает один документ

Запрос ёмкости не загружает агрегат и не сканирует зачисления. Он читает один
документ по ключу и превращает его в DTO.

```php
/** Reads one document and returns a view, never an aggregate. */
final readonly class OfferingCapacityQueryHandler
{
    public function __construct(
        private DocumentStore $store,
    ) {
    }

    public function handle(OfferingCapacityQuery $query): OfferingCapacityView
    {
        $document = $this->store->get(OfferingCapacityProjection::COLLECTION, $query->offeringId) ?? [];

        return new OfferingCapacityView(
            offeringId: $query->offeringId,
            title: (string) ($document['title'] ?? ''),
            capacity: (int) ($document['capacity'] ?? 0),
            reservedSeats: (int) ($document['reserved_seats'] ?? 0),
            pending: (int) ($document['pending'] ?? 0),
            confirmed: (int) ($document['confirmed'] ?? 0),
            cancelled: (int) ($document['cancelled'] ?? 0),
        );
    }
}
```

[`src/Reporting/Application/OfferingCapacityQueryHandler.php` L12-L34](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Reporting/Application/OfferingCapacityQueryHandler.php#L12-L34)

`?? []` и значения по умолчанию означают: неизвестный курс возвращает нулевую
view, а не исключение. Это решение read-side: пустой экран лучше 500-й ошибки,
если данные ещё не доехали. Но оно же скрывает опечатку в id; в production
границу между «нет данных» и «нет такого курса» нужно выбирать осознанно.

### 2.5. Где находится окно устаревания

В событийном режиме команда зачисления заканчивается на `enrollment.requested` в
outbox. Проекции обновляются только после `pump()`. Эксперимент специально
снимает состояние до и после:

```php
        $pendingBefore = $runtime->pendingEvents();
        $stale = $runtime->offeringCapacity('arch-101');
        $runtime->pump();

        $runtime->counters()->reset();
        $readStart = hrtime(true);
        $readEntries = 0;

        for ($round = 0; $round < 5; ++$round) {
            for ($student = 1; $student <= $students; ++$student) {
                $readEntries += count($runtime->studentSchedule(sprintf('cqrs-%03d', $student))->entries);
            }

            $runtime->offeringCapacity('arch-101');
        }

        $readElapsed = (hrtime(true) - $readStart) / 1e9;
```

[`experiments/05-cqrs/queries.php` L26-L42](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/experiments/05-cqrs/queries.php#L26-L42)

Это и есть CQRS в одном абзаце: запись уже произошла, чтение ещё не обязано
увидеть её. Окно измеряется числом pending events и содержимым проекции.

---

## 3. Эксперимент

Эксперимент зачисляет 100 студентов, снимает проекцию до `pump()`, затем
сравнивает чтение проекции с чтением write-модели.

```bash
make experiment ARGS="cqrs:queries --students=100"
```

Вывод одного прогона:

```text
Experiment: CQRS read models
One projection lookup versus scanning the write side.

students: 100, capacity: 100
before delivery: 501 pending events, projection confirmed=0
after delivery: pending=0, projection confirmed=100, write confirmed=100
read model:  2.841 ms, 505 store reads, 500 entries
write model: 74.785 ms, 505 store reads, 500 entries
speedup: 26.33x
```

До доставки в outbox 501 событие, а проекция подтверждённых равна нулю. Это не
ошибка: события ещё не разобраны. После `pump()` pending = 0, проекция и
write-модель совпадают по confirmed (100). Сценарий с 12 студентами проверяет то
же самое инвариантом `stale-before-pump`.

Почему у обеих сторон 505 чтений? Пять раундов по 100 студентов плюс один
запрос ёмкости: `5 × (100 + 1) = 505`. Read model читает один документ на
студента, write model вызывает `forStudent()`, который делает `all()` и
фильтрует. Счётчик `storeReads` для `all()` добавляет по одному чтению на
документ, поэтому числа совпадают. Разница — в работе после чтения: projection
читает один документ и мапит записи, write-side декодирует и гидратирует все
зачисления, а затем отбрасывает чужие.

Бенчмарк на 500 студентах:

| Benchmark         | Итерации | Медиана  | p95      | max      | ops/sec | PHP delta | peak delta |
| ----------------- | -------: | -------: | -------: | -------: | ------: | --------: | ---------: |
| `cqrs.read_model` |      100 |  0.501ms |  0.604ms |  0.604ms | 199 667 |     +592B |         0B |
| `cqrs.write_scan` |      100 | 70.899ms | 71.480ms | 71.480ms |   1 410 |     +592B |    +648B |

Это разница между просмотром индекса и сканом в терминах документного store.
В реальной SQL-базе она выглядела бы как `WHERE student_id = ?` с индексом
против `SELECT *` и фильтрации в приложении. Числа зависят от fixture и не
являются обещанием «CQRS ускоряет в 140 раз».

---

## 4. Что ломается

**Eventual consistency — это свойство, а не баг.** Пользователь может сразу
после зачисления увидеть «pending», а через секунду — «confirmed». Если UI
обещает мгновенную согласованность, CQRS ему не подходит без дополнительного
чтения write-модели или синхронного обновления проекции.

**Проекция может отставать навсегда.** Если dispatcher упал, outbox остался
pending, а `pump()` никто не вызвал, read model не обновится. В лаборатории
`pump()` вызывает сценарий; в production нужен планировщик, мониторинг лага и
алерты. Их здесь нет.

**Нет пересборки проекции.** Если проекция испорчена или изменилась её схема,
нет команды `rebuild`. В реальной системе нужен replay из event log или
пересборка из write-модели. В лаборатории проекция строится только из текущих
событий, и это ограничение важно.

**Нет checkpoint.** `IdempotentListener` хранит ключи `(listener, event id)`, но
не хранит позицию в потоке. Повторная доставка не создаёт эффект, однако
пропущенное событие не будет обнаружено. Exactly-once в этой схеме нет; есть
at-least-once с эффективно однократным эффектом.

**Две модели нужно согласовывать.** Если изменить форму события, projection и
write-side могут разойтись. `EventEnvelope` имеет `version`, но в лаборатории
нет реестра схем и миграций. Версия пока не используется; это задел, а не
работающий механизм.

**Read model не является источником истины.** По ней нельзя принимать решения о
списании денег или выдаче места. Она может быть устаревшей. Все команды идут
через write-side и проверяют инварианты там.

**Одна база — не «две базы».** Проекции лежат в той же SQLite, что и write-
модель. Это упрощает сценарий, но не моделирует отдельный read-store. Нельзя
делать вывод о сетевой задержке, репликации или консистентности между базами.

**`forStudent()` остаётся.** Write-side inspection API не удалён: он нужен
тестам и диагностике. Если оставить его как основной путь чтения, CQRS
превратится в дополнительную проекцию, которую никто не использует. В
лаборатории бенчмарк специально сравнивает оба пути, чтобы цена скана была
видна.

---

## 5. Как это проверено

- `testCapacityProjectionFoldsTheEventStream` — проекция собирает счётчики из
  последовательности событий;
- `testReleaseAndCancellationAreCounted` — decrement и cancelled не теряются;
- `testProjectionIgnoresEventsWithoutAnOffering` — событие без offering id не
  создаёт мусорный документ;
- `testUnknownEventsAreIgnored` — неизвестное имя не обновляет проекцию;
- `testEmptyCapacityQueryReturnsZeroes` — query handler отдаёт нулевую view;
- `testStudentScheduleKeepsTheLatestStatusPerEnrollment` — повторное событие
  перезаписывает статус, а не добавляет запись;
- `testScheduleIsSortedByEnrollmentIdAndEmptyForUnknownStudent` — порядок
  стабилен, неизвестный студент даёт пустой список;
- `testScheduleIgnoresOtherEventNames` — projection не реагирует на чужие
  события;
- `testEventDrivenModeIsStaleUntilTheOutboxIsPumped` — окно устаревания
  действительно существует;
- `testSyncModeReturnsATerminalReceipt` — в синхронном режиме ответ терминален,
  потому что Billing вызывается до возврата.

Сценарий главы 7 добавляет интеграционные инварианты: `stale-before-pump`,
`read-model-confirmed`, `read-model-cancelled`, `capacity-conserved`. Они
проверяют не отдельный projection, а согласованность двух моделей после
доставки.

---

## 6. Что запомнить

- CQRS — это разделение моделей и путей, а не обязательное разделение баз.
- Команда меняет состояние; запрос читает projection и не загружает агрегат.
- Read model из событий всегда имеет окно устаревания; его нужно измерять.
- Проекция должна считать изменения (инкременты/декременты), а не перезаписывать
  абсолютные значения из старых событий.
- Идемпотентность projection — отдельная задача; `max(0, ...)` её не решает.
- Read model не является источником истины и не заменяет инварианты write-side.
- Стоимость чтения зависит от модели данных: один документ против скана — это
  разница в работе после чтения, а не только в числе запросов.

---

## 7. Упражнения

1. Добавьте projection `CourseRoster`, которая хранит список студентов курса.
   Как она переживёт повторное `enrollment.requested`?
2. Сделайте `rebuild` проекции из outbox. Какие события нужно проиграть и в
   каком порядке?
3. Измерьте `cqrs:queries` с 10, 100 и 500 студентами. Как меняется отношение
   read/write?
4. Придумайте запрос, для которого projection будет медленнее write-модели.
   Почему это возможно?
5. Что произойдёт, если `enrollment.confirmed` придёт раньше
   `enrollment.requested`? Какие поля projection станут отрицательными?
6. Добавьте в query handler различие между «нет данных» и «нет курса». Какой
   контракт вы выберете для публичного API?

---

## 8. Что дальше

Проекции уже строятся из событий, но доставка пока выглядит как вызов
`pump()` в тесте. В главе 6 outbox становится центральной темой: запись
агрегата и запись события в одной транзакции, at-least-once доставка, повторные
попытки, dead letter и идемпотентные слушатели. Там же станет видно, где
заканчивается «эффективно однократный» эффект и начинается честная цена
распределённых систем.
