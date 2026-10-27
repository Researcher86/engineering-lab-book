# Глава 1. Monolith, modular monolith и microservices: где проходит граница

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Следующая глава: sync vs async processing (глава 2).

Первый архитектурный вопрос обычно звучит так: «нам уже пора на микросервисы?» В
лаборатории на него нельзя ответить одним сравнением секундомера. Три
конфигурации — монолит, модульный монолит и микросервисы — отличаются не только
формой развёртывания. Вместе с границей меняются владение базой, модель
транзакции и способ обработки потерянного ответа. Поэтому эксперимент честно
сравнивает **референсные конфигурации**, а не одну изолированную переменную.

Главный результат главы не в том, что «микросервисы медленнее на 24,6%». Главный
результат — в том, что при потере ответа монолит может откатить транзакцию, а
микросервисный вариант обязан компенсировать уже совершённые эффекты. Цена
границы — это не только лишний вызов, но и новая ответственность за состояние,
которое уже стало чужим.

---

## 1. Проблема

В монолите бронирование поездки — это одна операция: зарезервировать рейс, отель
и машину, авторизовать платёж, подтвердить поездку. Все записи идут в одну базу,
и одна транзакция может покрыть их целиком. Если запись в машину упала,
транзакция откатывает и рейс, и отель, и платёж. Пользователь видит отказ, но
система не оставляет половину брони.

У микросервисов та же бизнес-операция состоит из независимых шагов. Каждый шаг
принадлежит своему сервису и своей базе. Общей транзакции нет. Остаётся сага:
шаги коммитятся по отдельности, а при ошибке координатор вызывает компенсации.
Это не «распределённая транзакция» и не атомарность. Это договор о том, как
система будет возвращаться к согласованному состоянию после частичного успеха.

Самый неприятный отказ — не «сервис недоступен», а «сервис выполнил работу, но
ответ потерялся». Вызывающая сторона получила исключение и не знает, существует
ли эффект. Локальная транзакция в такой ситуации может откатить всё, потому что
эффект ещё не закоммичен. Сага обязана компенсировать **и упавший шаг тоже**:
его эффект мог сохраниться.

Именно этот сценарий и моделирует лаборатория. `FaultInjector` умеет падать до
эффекта и после него:

```php
    public function before(string $step): void
    {
        $this->maybeFail($step, false);
    }

    public function after(string $step): void
    {
        $this->maybeFail($step, true);
    }
```

[`src/Foundation/FaultInjector.php` L42-L50](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Foundation/FaultInjector.php#L42-L50)

В эксперименте по умолчанию используется `car.write` с `failure-after=1`: запись
в машину уже произошла, но вызывающий получил исключение. Это и есть потерянный
ответ, который отделяет локальный rollback от компенсации.

---

## 2. Устройство

### 2.1. Три формы развёртывания

`DeploymentShape` — маленький enum, но он определяет, какой транспорт получит
домен и какие границы будут считаться настоящими.

```php
enum DeploymentShape: string
{
    case Monolith = 'monolith';
    case ModularMonolith = 'modular-monolith';
    case Microservices = 'microservices';

    public function isSingleProcess(): bool
    {
        return $this !== self::Microservices;
    }
}
```

[`src/Domain/DeploymentShape.php` L7-L17](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Domain/DeploymentShape.php#L7-L17)

`isSingleProcess()` — не косметический helper. Монолит и модульный монолит живут
в одном процессе, поэтому их вызовы не проходят через сеть. Микросервисы
получают `RemoteTransport`, который считает вызовы, байты и логическую задержку.
Но даже этот транспорт не является сетью: это детерминированная модель внутри
одного PHP-процесса.

### 2.2. Семь решений в одной конфигурации

Все эксперименты используют один домен и один `LabConfig`. Семь полей — это семь
решений книги; глава меняет только те, которые относятся к её вопросу.

```php
final readonly class LabConfig
{
    public function __construct(
        public DeploymentShape $shape = DeploymentShape::ModularMonolith,
        public ProcessingMode $processing = ProcessingMode::Sync,
        public StorageMode $storage = StorageMode::Sql,
        public DatabaseMode $database = DatabaseMode::Shared,
        public DeliveryMode $delivery = DeliveryMode::Outbox,
        public TransactionMode $transaction = TransactionMode::Local,
        public ConsistencyMode $consistency = ConsistencyMode::Strong,
        public string $seed = 'book',
        public int $projectionLagMs = 50,
        public int $remoteLatencyMs = 2,
        public int $queueLimit = 1000,
        public string $databaseDirectory = 'var/databases',
    ) {
        if ($this->projectionLagMs < 0) {
            throw new \InvalidArgumentException('projection lag cannot be negative');
        }

        if ($this->remoteLatencyMs < 0) {
            throw new \InvalidArgumentException('remote latency cannot be negative');
        }

        if ($this->queueLimit < 1) {
            throw new \InvalidArgumentException('queue limit must be at least 1');
        }

        if ($this->transaction === TransactionMode::Local && $this->database === DatabaseMode::PerService) {
            throw new \InvalidArgumentException(
                'A local transaction cannot be atomic across databases; use saga or two-phase-commit',
            );
        }
    }
```

[`src/Integration/LabConfig.php` L20-L53](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabConfig.php#L20-L53)

Последняя проверка — принципиальная. Нельзя поставить `transaction: local` и
`database: per-service`, потому что одна SQLite-транзакция не может быть
атомарной сразу в нескольких файлах. Лаборатория не притворяется, что может: она
отказывается собирать такую конфигурацию. Это лучше, чем отчёт, в котором
«локальная транзакция» на самом деле не покрывает половину записи.

### 2.3. Одна база или шесть

`LabFactory` — единственная точка сборки. Для shared-режима все сервисы получают
один объект `SqliteDatabase`; для per-service создаются отдельные файлы и
отдельные схемы.

```php
        if ($config->database === DatabaseMode::Shared) {
            $bookingDb = new SqliteDatabase($directory . '/shared.sqlite', $counters);
            $reportingDb = $bookingDb;
            $flightDb = $bookingDb;
            $hotelDb = $bookingDb;
            $carDb = $bookingDb;
            $paymentDb = $bookingDb;
            Schema::all($bookingDb);
        } else {
            $bookingDb = new SqliteDatabase($directory . '/booking.sqlite', $counters);
            $reportingDb = new SqliteDatabase($directory . '/reporting.sqlite', $counters);
            $flightDb = new SqliteDatabase($directory . '/flight.sqlite', $counters);
            $hotelDb = new SqliteDatabase($directory . '/hotel.sqlite', $counters);
            $carDb = new SqliteDatabase($directory . '/car.sqlite', $counters);
            $paymentDb = new SqliteDatabase($directory . '/payment.sqlite', $counters);
            Schema::booking($bookingDb);
            Schema::reporting($reportingDb);
            Schema::inventory($flightDb, ResourceType::Flight);
            Schema::inventory($hotelDb, ResourceType::Hotel);
            Schema::inventory($carDb, ResourceType::Car);
            Schema::payment($paymentDb);
        }

        $transport = self::transport($config, $counters, $faults, $clock);
        $outbox = new Outbox($bookingDb, $counters, $clock);
        $store = $config->storage === StorageMode::Document
            ? new DocumentBookingStore($bookingDb, $counters, $outbox)
            : new SqlBookingStore($bookingDb, $counters, $outbox);
```

[`src/Integration/LabFactory.php` L61-L88](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabFactory.php#L61-L88)

В shared-режиме `reportingDb` — тот же объект, что `bookingDb`. Это важно для
главы 4: отчёт может быть одним join, потому что все таблицы физически лежат
рядом. В per-service режиме даже projection живёт в отдельной
`reporting.sqlite`, а не является view над базой бронирований.

### 2.4. Транспорт как модель границы

Транспорт выбирается по форме развёртывания:

```php
    private static function transport(
        LabConfig $config,
        Counters $counters,
        FaultInjector $faults,
        VirtualClock $clock,
    ): Transport {
        return match ($config->shape) {
            DeploymentShape::Monolith => new DirectTransport($counters, $faults),
            DeploymentShape::ModularMonolith => new ModuleTransport($counters, $faults),
            DeploymentShape::Microservices => new RemoteTransport(
                $counters,
                $faults,
                $clock,
                $config->remoteLatencyMs,
            ),
        };
    }
```

[`src/Integration/LabFactory.php` L189-L205](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabFactory.php#L189-L205)

`DirectTransport` и `ModuleTransport` отличаются счётчиком вызовов, но не
добавляют ни задержки, ни байтов. `RemoteTransport` добавляет всё три:

```php
final readonly class RemoteTransport implements Transport
{
    public function __construct(
        private Counters $counters,
        private FaultInjector $faults,
        private VirtualClock $clock,
        private int $latencyMs = 2,
        private int $requestBytes = 64,
        private int $responseBytes = 64,
    ) {
        if ($latencyMs < 0) {
            throw new \InvalidArgumentException('latency cannot be negative');
        }
    }

    public function call(string $service, string $operation, \Closure $work): mixed
    {
        $this->counters->increment('remoteCalls');
        $this->counters->add('networkRequestBytes', $this->requestBytes);
        $this->clock->advance($this->latencyMs);
        $this->counters->add('virtualLatencyMs', $this->latencyMs);
        $step = $service . '.' . $operation;
        $this->faults->before($step);
        $result = $work();
        $this->counters->add('networkResponseBytes', $this->responseBytes);
        $this->faults->after($step);

        return $result;
    }

    public function name(): string
    {
        return 'remote';
    }
}
```

[`src/Services/RemoteTransport.php` L17-L51](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Services/RemoteTransport.php#L17-L51)

Здесь нет `sleep()`. Логические 2 мс — это маркер стоимости вызова, а не прогноз
сетевой задержки. `after($step)` вызывается после closure: если он бросает
исключение, эффект уже существует, но вызывающий об этом не знает. Именно этот
порядок делает возможной ситуацию, в которой сага нужна, а локальный rollback
недостаточен.

### 2.5. Сага компенсирует упавший шаг первым

`SagaCoordinator` не пытается притвориться транзакцией. Он выполняет шаги по
очереди, запоминает завершённые и при первом исключении собирает список
компенсаций: сначала упавший шаг, затем завершённые в обратном порядке.

```php
    public function run(array $steps): SagaResult
    {
        $completed = [];

        foreach ($steps as $step) {
            try {
                ($step->action)();
                $completed[] = $step;
                $this->counters->increment('sagaSteps');
            } catch (\Throwable $throwable) {
                $this->counters->increment('sagaFailures');

                return $this->compensate($step, $completed, $throwable->getMessage());
            }
        }

        $this->counters->increment('sagaCompleted');

        return new SagaResult(true, '', array_map(static fn (SagaStep $step): string => $step->name, $completed), 0, 0);
    }

    /**
     * @param list<SagaStep> $completed
     */
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

[`src/Saga/SagaCoordinator.php` L28-L85](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Saga/SagaCoordinator.php#L28-L85)

Компенсация упавшего шага — не перестраховка. При потере ответа эффект мог
сохраниться, поэтому «упавший» шаг тоже входит в список кандидатов. Все
компенсации в лаборатории идемпотентны: повторный release уже освобождённой
резервации или void уже отменённого платежа не ломают состояние. Об этом
подробнее в главе 6.

---

## 3. Эксперимент

Команда:

```bash
make experiment ARGS="topology:deployment"
```

По умолчанию запускаются 30 бронирований, capacity 40, отказ `car.write` после
эффекта. Вот реальный вывод:

```text
Experiment: Topology
bookings=30 capacity=40 failure=car.write(after) seed=topology

shape                   ok cancelled     comp   remote      sql    avg ms     avail
monolith                29        1        0        0     1012      0.00        11
modular-monolith        29        1        0        0     1012      0.00        11
microservices           29        1        3      122      963      8.13        11
```

Все три конфигурации подтверждают 29 бронирований из 30 и оставляют 11 свободных
мест: одна поездка не состоялась, её ресурсы освобождены. Разница в том, **как**
система пришла к этому результату.

- Монолит и модульный монолит откатывают локальную транзакцию. Компенсаций нет:
  откат вернул рейс, отель и машину. Платёж не вызывался, потому что отказ
  произошёл на шаге машины.
- Микросервисы уже закоммитили рейс, отель и машину, когда потерялся ответ на
  записи машины. Сага выполнила 3 компенсации: машину, отель и рейс. Шаг платежа
  вообще не выполнялся, поэтому в список компенсаций он не попал.
- 122 удалённых вызова — это цена пересечения границы. Средние 8,13 мс —
  логическое время, а не измеренная сеть: примерно четыре вызова по 2 мс на
  бронирование, включая неудачное и его компенсации.
- `sql` — это все SQL-запросы прогона, включая создание схемы, seed и 30
  бронирований. Число 963 у микросервисов ниже не потому, что путь стал дешевле,
  а потому, что per-service схема создаёт меньше объектов в каждом файле.
  Сравнивать эти числа как «запросы на бронирование» нельзя.

Последняя колонка `avail` — доступность рейса после прогона. Она одинакова во
всех трёх случаях: 11. Это важнее, чем 8,13 мс. Разные механизмы пришли к одному
наблюдаемому результату, но заплатили разную цену за промежуточные состояния и
восстановление.

---

## 4. Бенчмарк

Эксперимент показывает счётчики и логическое время, но не отвечает на вопрос,
сколько стоит сама операция в контейнере. Для этого есть отдельный suite:

```bash
make benchmark ARGS="01-topology"
```

Он делает 500 полных бронирований в каждой конфигурации. Монолит и модульный
монолит используют общую базу и локальную транзакцию; микросервисы — базу на
сервис и сагу. Вот один реальный прогон с commit `87bc2f0`, пять повторений и
один прогрев:

| benchmark                        | итераций | медиана |     p95 | ops/sec | медиана на бронь |
| -------------------------------- | -------: | ------: | ------: | ------: | ---------------: |
| `topology.monolith_booking`      |      500 | 645.366 | 668.035 |     775 |         1 291 µs |
| `topology.modular_booking`       |      500 | 621.101 | 691.936 |     805 |         1 242 µs |
| `topology.microservices_booking` |      500 | 804.615 | 847.261 |     621 |         1 609 µs |

Микросервисная конфигурация медленнее монолита на 24,6% и модульного монолита на
29,6% в этом прогоне. Но это **не «стоимость сети»**: транспорт всё ещё внутри
процесса, а логическая задержка не спит. Разница складывается из пяти
SQLite-файлов, раздельной схемы, саги и дополнительных запросов, которые нужны
для коммита каждого шага отдельно.

Модульный монолит оказался на 3,8% быстрее монолита. У них одна база и одна
транзакция; разница ниже шума контейнера. Не читайте это как «модульность
ускоряет код». Это тот же класс различий, который в главе 5 заставил назвать
разницу outbox и dual write «примерно одинаковой».

Полное окружение и оговорки:

```text
PHP 8.5.11 on Linux 7.0.14-linuxkit, aarch64 x 12, memory_limit 512M, cgroup unlimited
OPcache off, JIT off, Xdebug off, allocator Zend MM
git 87bc2f017532cd2576addc48b32cc5d3b158410f
```

Это Docker Desktop на macOS, виртуализированный Linux и общий CPU. Абсолютные
миллисекунды здесь — свойство этой машины, а не константа архитектуры.

---

## 5. Что ломается и что не измерено

**Граница здесь не сетевая.** `RemoteTransport` не открывает сокет, не ставит
TCP-соединение и не сериализует HTTP. Он считает байты и двигает виртуальные
часы. Поэтому эксперимент отвечает на вопрос «сколько логических шагов и
переходов появляется», а не «сколько будет p99 в Kubernetes».

**Микросервисная конфигурация меняет сразу три вещи.** Форма развёртывания,
владение базой и модель транзакции связаны: per-service база требует саги или
2PC, а shared-база позволяет локальную транзакцию. Это не ошибка эксперимента, а
реальный выбор команды. Но нельзя приписать всю разницу 24,6% только
«микросервисам». Вклад границы, разделения базы и саги здесь не разложен на
независимые слагаемые; глава 6 даёт более узкий взгляд на стоимость транзакции.

**Сага не делает систему атомарной.** Она оставляет видимые промежуточные
состояния: рейс уже зарезервирован, отель ещё нет. Другой читатель может увидеть
эту половину. Компенсация возвращает состояние, но не отменяет факт, что оно
было видимым. В лаборатории нет изоляции между конкурентными читателями, потому
что нет конкурентности.

**Отказ — исключение в названной точке, а не убитый процесс.** `FaultInjector`
бросает `InjectedFailure` до или после closure. Это позволяет детерминированно
воспроизвести потерю ответа, но не моделирует падение процесса, таймаут,
переподключение или частично записанный ответ.

**Нет реальной сети, брокера и ретраев.** В модели нет packet loss, DNS,
connection pool, backoff и dead-letter queue. Всё, что зависит от этих
механизмов, остаётся за границей главы.

---

## 6. Как это проверено

Тесты фиксируют именно различие между rollback и компенсацией. Первый тест ловит
потерянный ответ в локальной транзакции:

```php
    public function testLocalTransactionRollsBackAReservationWhoseResponseWasLost(): void
    {
        $counters = new Counters();
        $faults = new FaultInjector(step: 'car.write', after: true, counters: $counters);
        $lab = $this->lab($this->config(transaction: TransactionMode::Local), $faults, $counters);
        $lab->seed('f1', 'h1', 'c1', capacity: 4);

        $result = $lab->book($this->trip());

        self::assertFalse($result->accepted);
        self::assertSame(BookingStatus::Cancelled, $result->status);
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Flight, 'f1'));
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Hotel, 'h1'));
        self::assertSame(4, $lab->sourceAvailability(ResourceType::Car, 'c1'));
        self::assertCount(0, $lab->flight->reservations());
        self::assertSame(BookingStatus::Cancelled, $lab->store->get($result->bookingId)?->status);
    }
```

[`tests/Integration/BookingWorkflowTest.php` L35-L51](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/BookingWorkflowTest.php#L35-L51)

Второй тест показывает, что сага действительно освобождает все три ресурса и
удаляет платёж, а не только меняет статус брони:

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

Полный прогон лаборатории на момент тега: 74 теста, 3 468 assertions, PHPStan
level 8 без ошибок, PHP-CS-Fixer без изменений. Эти тесты не доказывают, что
микросервисы лучше или хуже. Они доказывают, что обе модели доводят систему до
согласованного состояния после одного и того же отказа.

---

## 7. Что запомнить

- Граница — это не только сетевой вызов. Вместе с ней меняются владение данными,
  транзакция и способ восстановления.
- Локальная транзакция умеет откатывать ещё не закоммиченные эффекты. Сага умеет
  только компенсировать уже закоммиченные.
- Потерянный ответ (`after`) — главный тест для саги. Исключение не означает
  «ничего не произошло».
- Сравнивайте референсные конфигурации, а не одну переменную, если команда
  реально выбирает между ними. Но явно перечисляйте, что именно входит в
  разницу.
- 24,6% в этом контейнере — не приговор микросервисам. Это стоимость конкретного
  набора решений в конкретном прогоне.
- Одинаковый финальный `avail` не означает одинаковую стоимость пути. Счётчики
  компенсаций и вызовов показывают то, что скрывает итоговая таблица.

---

## 8. Упражнения

1. Запустите эксперимент с `failure-after=0` и объясните, почему число
   компенсаций у микросервисов меняется. Какие шаги успели закоммититься?
2. Запустите `topology:deployment --bookings=100 --capacity=40` и посмотрите,
   как отказ распределяется между бронированиями. Почему `failEvery` меняет
   картину?
3. Соберите конфигурацию `shape: microservices`, `database: shared`,
   `transaction: local`. Объясните, почему `LabConfig` её принимает, а
   `BookingWorkflow` всё равно требует `localTransactionIsAtomic`.
4. Предложите, как измерить вклад одной границы отдельно от саги. Какие
   конфигурации для этого понадобятся и почему они могут быть нереалистичны?
5. Найдите в `SagaCoordinator` место, где ошибка компенсации не прерывает
   остальные компенсации. Почему это правильное решение для саги и что оно
   скрывает от вызывающего?

---

Дальше — глава 2. Она задаёт следующий вопрос: если бронирование можно принять
быстрее, чем выполнить, что именно возвращается клиенту и где заканчивается
очередь.
