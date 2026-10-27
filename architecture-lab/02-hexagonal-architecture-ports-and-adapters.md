# Глава 2. Hexagonal Architecture: порты, адаптеры и цена замены

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: слоистый baseline. Дальше: Clean Architecture (глава 3).

В главе 1 приложение знало `PDO`, а контроллер — репозиторий. Hexagonal
architecture предлагает другой ход: приложение формулирует, что ему нужно от
внешнего мира, в виде портов, а внешний мир подключается адаптерами. Направление
зависимости меняется. Не приложение импортирует базу, а адаптер базы реализует
интерфейс приложения.

В лаборатории этот переход можно измерить. Один и тот же сценарий зачисления
запускается на двух наборах адаптеров: in-memory и SQLite. Приложение не
меняется; меняется только композиция. Эксперимент показывает, что решение
одинаковое, а стоимость и число SQL-запросов — разные. Это и есть главный
результат главы: порт покупает заменяемость, но не бесплатно и не автоматически.

---

## 1. Проблема

Слоистая архитектура уже разделила код на папки, но зависимость осталась
направленной наружу: сервис знает репозиторий, репозиторий знает `PDO`. Чтобы
протестировать сценарий, нужна база. Чтобы заменить базу, нужно править сервис.

Порты решают эту задачу не магией, а инверсией зависимости. Приложение объявляет
интерфейс в своём пространстве имён:

- `DocumentStore` — «мне нужно положить, достать и перебрать документы»;
- `UnitOfWork` — «мне нужно выполнить работу атомарно»;
- `SeatAllocator` — «мне нужно зарезервировать место»;
- `PaymentProcessor` — «мне нужно списать деньги»;
- `Clock` и `IdGenerator` — «мне нужны время и идентификаторы».

Адаптеры живут снаружи и реализуют эти интерфейсы. `SqliteDocumentStore` знает
SQL, `InMemoryDocumentStore` знает массивы, `CatalogSeatAllocator` знает
`CatalogApi`. Приложение видит только интерфейс. Композиционный корень
(`LabRuntime`) — единственное место, которое знает и порты, и адаптеры.

У этого есть цена. Появляются интерфейсы, DTO, маппинг, больше классов и больше
косвенности. Появляется соблазн сделать порт «на всякий случай» или, наоборот,
протащить в порт детали конкретной базы. Глава показывает, где проходит граница
в этом проекте и что она не обещает.

---

## 2. Устройство

### 2.1. Порт хранилища

`DocumentStore` намеренно узкий: три операции и транзакция. В нём нет `WHERE`,
`JOIN`, имени таблицы или типа базы.

```php
interface DocumentStore extends UnitOfWork
{
    /**
     * @param array<string, mixed> $document
     */
    public function put(string $collection, string $id, array $document): void;

    /**
     * @return array<string, mixed>|null
     */
    public function get(string $collection, string $id): ?array;

    /**
     * @return list<array<string, mixed>>
     */
    public function all(string $collection): array;
}
```

[`src/Shared/Port/DocumentStore.php` L13-L29](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Shared/Port/DocumentStore.php#L13-L29)

Транзакция вынесена в отдельный интерфейс, потому что не каждому адаптеру есть
что откатывать:

```php
interface UnitOfWork
{
    public function transactional(callable $work): mixed;
}
```

[`src/Shared/Port/UnitOfWork.php` L13-L16](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Shared/Port/UnitOfWork.php#L13-L16)

`DocumentStore extends UnitOfWork` означает: адаптер хранилища умеет и хранить,
и выполнять работу атомарно. Это не обязательно свойство любой базы, но в этом
проекте SQLite-адаптер действительно владеет соединением и может открыть
транзакцию. In-memory адаптер имитирует её снимком массива.

Важно, что порт оперирует массивами, а не агрегатами. Маппинг агрегата в
документ — ответственность репозитория-адаптера, а не домена. Домен не знает,
что документ сериализуется в JSON.

### 2.2. Приложение видит только порты

Конструктор обработчика зачисления принимает `EnrollmentRepository`,
`SeatAllocator`, `UnitOfWork`, `Outbox`, `EventRecorder`, `IdGenerator`,
`Clock` и необязательный `PaymentProcessor`. Ни одного `PDO`.

```php
final readonly class RequestEnrollmentHandler
{
    public function __construct(
        private EnrollmentRepository $enrollments,
        private SeatAllocator $seats,
        private UnitOfWork $unitOfWork,
        private Outbox $outbox,
        private EventRecorder $recorder,
        private IdGenerator $ids,
        private Clock $clock,
        private ?PaymentProcessor $payment = null,
    ) {
    }
```

[`src/Enrollment/Application/Command/RequestEnrollmentHandler.php` L35-L47](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Enrollment/Application/Command/RequestEnrollmentHandler.php#L35-L47)

Сценарий тоже не знает адаптеров: он работает внутри `transactional()` и
оперирует доменными объектами и событиями.

```php
            $enrollment = $this->unitOfWork->transactional(function () use ($command): Enrollment {
                $reservation = $this->seats->reserve(new OfferingId($command->offeringId));
                $enrollment = new Enrollment(
                    id: new EnrollmentId($this->ids->next('enr')),
                    studentId: new StudentId($command->studentId),
                    offeringId: $reservation->offeringId,
                    courseTitle: $reservation->courseTitle,
                    price: $reservation->price,
                    requestedAt: $this->clock->now(),
                );
                $this->enrollments->save($enrollment);
                $this->outbox->record($this->recorder->record(
                    EventNames::ENROLLMENT_REQUESTED,
                    $enrollment->id()->value,
                    [
                        'enrollment_id' => $enrollment->id()->value,
                        'student_id' => $enrollment->studentId()->value,
                        'offering_id' => $enrollment->offeringId()->value,
                        'course_title' => $enrollment->courseTitle(),
                        'price_cents' => $enrollment->price()->cents,
                        'currency' => $enrollment->price()->currency,
                        'requested_at' => $enrollment->requestedAt()->format(DATE_ATOM),
                    ],
                ));

                return $enrollment;
            });
```

[`src/Enrollment/Application/Command/RequestEnrollmentHandler.php` L52-L78](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Enrollment/Application/Command/RequestEnrollmentHandler.php#L52-L78)

Обратите внимание: здесь нет ни `beginTransaction()`, ни `commit()`, ни имени
таблицы. Есть `UnitOfWork`, который обещает атомарность, и `Outbox`, который
обещает сохранить событие. Как именно это сделано — вопрос адаптера.

### 2.3. In-memory адаптер

Простейший адаптер хранит документы в трёхмерном массиве: коллекция → id →
документ. Транзакция — снимок массива и восстановление при исключении.

```php
final class InMemoryDocumentStore implements DocumentStore
{
    /** @var array<string, array<string, array<string, mixed>>> */
    private array $collections = [];

    public function __construct(
        private readonly Counters $counters,
    ) {
    }

    public function put(string $collection, string $id, array $document): void
    {
        $this->collections[$collection][$id] = $document;
        ++$this->counters->storeWrites;
    }

    public function get(string $collection, string $id): ?array
    {
        ++$this->counters->storeReads;

        return $this->collections[$collection][$id] ?? null;
    }

    public function all(string $collection): array
    {
        $documents = array_values($this->collections[$collection] ?? []);
        $this->counters->storeReads += count($documents) + 1;

        return $documents;
    }

    public function transactional(callable $work): mixed
    {
        $snapshot = $this->collections;

        try {
            return $work();
        } catch (\Throwable $exception) {
            $this->collections = $snapshot;

            throw $exception;
        }
    }
}
```

[`src/Persistence/InMemoryDocumentStore.php` L17-L60](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Persistence/InMemoryDocumentStore.php#L17-L60)

Этот адаптер — не «фейковая база для красоты». Он доказывает, что сценарий не
требует `PDO`: тесты и эксперимент действительно проходят без SQL. Но у него
есть граница: снимок относится только к `collections`. Outbox и
`ProcessedEventStore` — отдельные объекты, поэтому их изменения не откатываются
вместе с документами. In-memory композиция не атомарна между агрегатом и
событием; это честно указано в документации и повторяется в главе 6.

### 2.4. SQLite-адаптер

SQLite-адаптер хранит документы как JSON в одной таблице. Запись — upsert:

```php
    public function put(string $collection, string $id, array $document): void
    {
        $statement = $this->pdo->prepare(
            'INSERT INTO documents (collection, id, payload) VALUES (:collection, :id, :payload)
             ON CONFLICT (collection, id) DO UPDATE SET payload = excluded.payload',
        );

        $statement->execute([
            'collection' => $collection,
            'id' => $id,
            'payload' => json_encode($document, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES),
        ]);
        ++$this->counters->storeWrites;
        ++$this->counters->sqlQueries;
    }
```

[`src/Persistence/SqliteDocumentStore.php` L36-L50](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Persistence/SqliteDocumentStore.php#L36-L50)

Транзакция использует savepoint, если внешняя транзакция уже открыта. Это
позволяет обработчику комбинировать две единицы работы, не теряя внешнюю:

```php
    public function transactional(callable $work): mixed
    {
        if ($this->pdo->inTransaction()) {
            $savepoint = 'sp_' . bin2hex(random_bytes(4));
            $this->pdo->exec('SAVEPOINT ' . $savepoint);

            try {
                $result = $work();
                $this->pdo->exec('RELEASE SAVEPOINT ' . $savepoint);

                return $result;
            } catch (\Throwable $exception) {
                $this->pdo->exec('ROLLBACK TO SAVEPOINT ' . $savepoint);
                $this->pdo->exec('RELEASE SAVEPOINT ' . $savepoint);

                throw $exception;
            }
        }

        $this->pdo->beginTransaction();

        try {
            $result = $work();
            $this->pdo->commit();

            return $result;
        } catch (\Throwable $exception) {
            $this->pdo->rollBack();

            throw $exception;
        }
    }
```

[`src/Persistence/SqliteDocumentStore.php` L102-L133](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Persistence/SqliteDocumentStore.php#L102-L133)

Это реальная транзакция SQLite, а не имитация. В главе 6 та же транзакция
охватит запись агрегата и строку outbox. Здесь важно, что приложение об этом не
знает: оно видит только `UnitOfWork::transactional()`.

### 2.5. Репозиторий как переводчик

Репозиторий — ещё один адаптер, но уже между агрегатом и портом хранилища.
`DocumentCourseOfferingRepository::save()` переводит агрегат в документ:

```php
    public function save(CourseOffering $offering): void
    {
        $this->store->put(self::COLLECTION, $offering->id()->value, [
            'id' => $offering->id()->value,
            'title' => $offering->title(),
            'price_cents' => $offering->price()->cents,
            'currency' => $offering->price()->currency,
            'starts_at' => $offering->startsAt()->format(DATE_ATOM),
            'capacity' => $offering->capacity(),
            'reserved_seats' => $offering->reservedSeats(),
        ]);
        ++$this->counters->repositoryCalls;
    }
```

[`src/Catalog/Infrastructure/DocumentCourseOfferingRepository.php` L31-L43](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Catalog/Infrastructure/DocumentCourseOfferingRepository.php#L31-L43)

При чтении документ снова становится агрегатом, а инвариант восстанавливается
через публичные методы:

```php
    private function hydrate(array $document): CourseOffering
    {
        $offering = new CourseOffering(
            new OfferingId((string) $document['id']),
            (string) $document['title'],
            new Money((int) $document['price_cents'], (string) $document['currency']),
            new DateTimeImmutable((string) $document['starts_at']),
            (int) $document['capacity'],
        );

        for ($seat = 0; $seat < (int) $document['reserved_seats']; ++$seat) {
            $offering->reserveSeat();
        }

        return $offering;
    }
```

[`src/Catalog/Infrastructure/DocumentCourseOfferingRepository.php` L69-L84](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Catalog/Infrastructure/DocumentCourseOfferingRepository.php#L69-L84)

Восстановление через `reserveSeat()` выглядит медленнее, чем присвоение поля, но
оно проходит через инвариант: если в базе окажется `reserved_seats > capacity`,
гидратация упадёт, а не создаст невозможный агрегат. Это цена честного маппинга:
`O(reserved_seats)` вместо `O(1)`, зато одно правило и один путь проверки.

### 2.6. Композиционный корень

`LabRuntime` — единственное место, где выбирается адаптер. Сравните два
статических конструктора:

```php
    public static function sqlite(string $path = ':memory:', string $mode = 'event-driven', int $failEvery = 0): self
    {
        $counters = new Counters();
        $pdo = new PDO('sqlite:' . $path);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);

        return new self(
            mode: $mode,
            counters: $counters,
            store: new SqliteDocumentStore($pdo, $counters),
            outbox: new SqliteOutbox($pdo, $counters),
            processed: new SqliteProcessedEventStore($pdo, $counters),
            clock: new FrozenClock(),
            ids: new SequentialIdGenerator(),
            failEvery: $failEvery,
        );
    }
```

[`src/Runtime/LabRuntime.php` L218-L234](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Runtime/LabRuntime.php#L218-L234)

В `inMemory()` те же роли играют `InMemoryDocumentStore`, `InMemoryOutbox` и
`InMemoryProcessedEventStore`. Приложение и сценарий не меняются: они получают
те же интерфейсы. Разница только в том, что SQLite-версия разделяет один `PDO`
между документами, outbox и processed events, а in-memory-версия — нет.

---

## 3. Эксперимент

Эксперимент запускает 100 синхронных зачислений сначала на in-memory, затем на
SQLite. Оба runtime получают одинаковый курс, capacity и `failEvery = 0`.

```bash
make experiment ARGS="hexagonal:adapter-swap --students=100"
```

Вывод одного прогона:

```text
Experiment: Ports and adapters
The application sees the same port; only the adapter changes.

students: 100, capacity: 100
in-memory: 4.356 ms, SQL statements 0, confirmed 100
SQLite:    16.625 ms, SQL statements 4121, confirmed 100
same decisions: yes
ports: 14, adapter classes: 15
```

`same decisions: yes` здесь означает совпадение числа подтверждённых зачислений,
а не полное сравнение всех решений. Дифференциальные тесты идут дальше: они
сравнивают статусы и последствия на двух адаптерах. Для эксперимента важно, что
оба пути прошли один и тот же сценарий и получили 100 подтверждений.

4 121 SQL-запрос — это не «41 запрос на зачисление». Счётчик включает создание
схем, записи документов, outbox, processed events, чтения проекций и работу
диспетчера. Он показывает порядок величины и рост, а не норму на один бизнес-
шаг. In-memory путь не делает SQL вообще, потому что SQL там неоткуда взяться.

Бенчмарк снимает ту же пару на 200 итерациях:

| Benchmark             | Итерации |   Медиана |       p95 |       max | ops/sec | PHP delta | peak delta |
| --------------------- | -------: | --------: | --------: | --------: | ------: | --------: | ---------: |
| `hexagonal.in_memory` |      200 |  19.776ms |  29.701ms |  29.701ms |  10 113 |  +9.57 MB |   +9.62 MB |
| `hexagonal.sqlite`    |      200 | 226.374ms | 349.116ms | 349.116ms |     883 |     +592B |   +1.78 MB |

Разница во времени ожидаема: SQLite делает реальные запросы и сериализует JSON.
Разница в памяти объясняется тем, что in-memory адаптер держит все документы в
PHP-массивах, а SQLite-адаптер — в базе, поэтому PHP delta остаётся почти
плоской, а пик растёт за счёт буферов. Это разные единицы: `php_delta_bytes` —
аллокатор PHP, а не RSS.

---

## 4. Что ломается

**Порт не гарантирует атомарность.** In-memory документы, outbox и processed
events — три объекта. `transactional()` откатывает только документы. Если
обработчик запишет документ, а затем упадёт на записи в outbox, документ
останется. SQLite-композиция решает это общим `PDO`, но это свойство конкретной
композиции, а не интерфейса.

**Порт может протечь.** Если добавить в `DocumentStore` метод
`whereStatus(string $status)`, приложение начнёт зависеть от модели запросов.
Порт должен описывать потребность приложения, а не возможности базы. В этом
проекте `all()` — сознательно неэффективный, но достаточный минимум; цена
фильтрации в PHP видна в главе 5.

**Адаптеры расходятся.** Два адаптера легко могут принимать разные решения:
in-memory транзакция не видит outbox, SQLite видит. Дифференциальные тесты
сравнивают результаты, но не доказывают эквивалентность при сбоях. Для
production-кода нужен contract test suite, который прогоняет один набор
сценариев против каждого адаптера.

**«Same decisions» — слабое утверждение.** Эксперимент сравнивает только
количество подтверждений. Он не проверяет порядок событий, идентификаторы или
откаты. Поэтому в книге это число приводится с оговоркой, а не как доказательство
полной эквивалентности.

**Порты добавляют косвенность.** Появляются интерфейсы, которые в одном
приложении имеют одну реализацию. Это нормальная цена, если граница нужна для
тестов, замены или изоляции. Если замены не будет, порт может остаться
формальностью; архитектура должна отвечать на вопрос «что мы покупаем», а не
«сколько интерфейсов мы можем создать».

**SQLite — не production-база.** Одна таблица с JSON, без индексов под запросы,
без конкурентной нагрузки и без сетевых сбоев. Она даёт реальные транзакции, но
не моделирует PostgreSQL. Числа главы — про этот адаптер и этот fixture.

---

## 5. Как это проверено

- `testDocumentStoreRoundTripsJson` — документ сохраняется и читается обратно;
- `testPutUpsertsTheSameDocument` — повторная запись не создаёт дубль;
- `testTransactionRollsBackOnFailure` — исключение откатывает изменения;
- `testNestedTransactionUsesASavepointAndKeepsTheOuterWork` — вложенная
  транзакция не ломает внешнюю;
- `testInMemoryAndSqliteAdaptersMakeTheSameDecision` — дифференциальный тест
  runtime: одинаковый сценарий на двух адаптерах даёт одинаковый результат;
- `testSyncSuccessConfirmsTheEnrollment` и
  `testSyncFailureCancelsAndReleasesTheSeat` — порты `PaymentProcessor` и
  `SeatAllocator` работают в обе стороны.

Плюс архитектурные тесты требуют, чтобы application layer не импортировал
`Persistence` и собственный `Infrastructure`. Это правило и делает порт
настоящим: без него адаптер можно обойти импортом.

---

## 6. Что запомнить

- Порт — это интерфейс, принадлежащий приложению, а не базе.
- Адаптер переводит между языком порта и языком технологии.
- Композиционный корень — единственное место, которое знает конкретные
  адаптеры.
- Заменяемость стоит денег: интерфейсы, маппинг, косвенность и риск расхождения
  адаптеров.
- In-memory адаптер доказывает отсутствие `PDO` в ядре, но не доказывает
  атомарность.
- Числа SQLite и in-memory нельзя сравнивать как «быстрее/медленнее» без
  указания, что именно измеряется.

---

## 7. Упражнения

1. Добавьте в `DocumentStore` метод `delete()` и реализуйте его в обоих
   адаптерах. Какие тесты нужно добавить, чтобы адаптеры не разошлись?
2. Сделайте `InMemoryOutbox` частью той же транзакции, что и документы. Какой
   интерфейс придётся изменить?
3. Замените SQLite на PostgreSQL, не меняя application layer. Какие SQL-
   утверждения из `SqliteDocumentStore` придётся переписать?
4. Придумайте порт, который был бы слишком широким для этого приложения.
   Объясните, какую зависимость он протащил бы в ядро.
5. Измерьте `hexagonal:adapter-swap` с `--fail-every=3`. Совпадут ли решения
   адаптеров? Какие счётчики изменятся?
6. Почему `hydrate()` восстанавливает `reserved_seats` циклом, а не присваивает
   поле? Найдите цену этого решения в бенчмарке.

---

## 8. Что дальше

Порты отделили приложение от технологий, но не ответили на вопрос, какие
зависимости вообще разрешены внутри кода. В главе 3 появляется checker:
сканер читает `use`-импорты, строит граф, проверяет направление зависимостей,
ищет циклы и считает метрики модулей. Clean Architecture здесь — не диаграмма
кругов, а исполняемое правило и отчёт о том, где оно нарушено.
