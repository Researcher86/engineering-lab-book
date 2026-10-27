# Глава 4. Database per service: когда join становится вызовом

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: SQL и document. Следующая: outbox (глава 5).

Разделить базу по сервисам легко на схеме. Труднее принять, что вместе с базой
вы теряете join, локальную транзакцию и мгновенный снимок состояния. Отчёт
«покажи мои поездки и доступность ресурсов» был одним SQL-запросом. Теперь это
запрос к сервису бронирований и по три вызова к сервисам рейсов, отелей и машин
на каждую поездку.

В лаборатории это не абстрактный N+1, а измеренный N+1: 20 броней дают 61
SQL-запрос и 60 удалённых вызовов вместо одного join. Глава показывает, откуда
берётся эта форма, что она даёт и чем за неё платят.

---

## 1. Проблема

В shared-базе отчёт путешественника — это один `SELECT` с тремя join. Все
таблицы лежат в одном файле, и база сама решает, как собрать строки. Приложение
получает готовые пары «бронь — доступность рейса, отеля, машины».

Когда у каждого сервиса своя база, таблицы `flights`, `hotels` и `cars` больше
не видны сервису бронирований. Остаются два варианта:

1. Дублировать данные в отчётное хранилище и поддерживать проекцию.
2. Сходить в каждый сервис за актуальным значением.

Лаборатория выбирает второй вариант, потому что он показывает цену границы
напрямую. `ServiceTripReport` читает список броней из `BookingStore`, а затем
для каждой брони вызывает `available()` у трёх inventory-сервисов. Это
классический N+1: один запрос за списком и N наборов вызовов за деталями.

У этого решения есть не только стоимость вызовов. Между чтением списка и чтением
доступности проходит время. В per-service конфигурации нет общей транзакции,
которая дала бы отчёту единый снимок. Разные строки отчёта могут относиться к
разным моментам. В shared-базе один SQL-запрос даёт снимок на момент выполнения.

---

## 2. Устройство

### 2.1. Одна база или отдельные файлы

Решение принимает `LabFactory`: для `DatabaseMode::Shared` все сервисы получают
один `SqliteDatabase`, для `DatabaseMode::PerService` — шесть отдельных файлов.
Это не деталь реализации, а источник всех дальнейших ограничений.

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
```

[`src/Integration/LabFactory.php` L61-L84](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabFactory.php#L61-L84)

В shared-режиме `reportingDb` — тот же объект. В per-service это отдельный файл:
projection не является view над базой бронирований, у неё своя схема и своя
таблица `processed_events`.

`LabConfig` не разрешает локальную транзакцию при `database: per-service`.
Поэтому per-service эксперимент обязан использовать сагу или 2PC. Это не
удобство, а признание факта: одна SQLite-транзакция не может быть атомарной
сразу в шести файлах.

### 2.2. Отчёт, который умеет только shared-база

`SqlTripReport` делает ровно один join. Его существование возможно только
потому, что таблицы рейсов, отелей и машин лежат рядом с бронями.

```php
    public function forTraveler(string $travelerId): array
    {
        $rows = $this->db->select(
            'SELECT b.id, b.status,
                    f.available AS flight_available,
                    h.available AS hotel_available,
                    c.available AS car_available
             FROM bookings b
             JOIN flights f ON f.id = b.flight_id
             JOIN hotels h ON h.id = b.hotel_id
             JOIN cars c ON c.id = b.car_id
             WHERE b.traveler_id = :traveler_id
             ORDER BY b.id',
            ['traveler_id' => $travelerId],
        );

        $summaries = [];

        foreach ($rows as $row) {
            $summaries[] = new TripSummary(
                $this->string($row['id']),
                $this->string($row['status']),
                (int) $row['flight_available'],
                (int) $row['hotel_available'],
                (int) $row['car_available'],
            );
        }

        return $summaries;
    }
```

[`src/Reporting/SqlTripReport.php` L21-L50](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Reporting/SqlTripReport.php#L21-L50)

`JOIN` — не только синтаксис. Он означает, что база может использовать индексы,
планировщик и один снимок данных. Здесь нет сети, сериализации и частичных
отказов. Цена — связность: отчёт знает о таблицах трёх сервисов.

### 2.3. Отчёт, который навязывает граница

`ServiceTripReport` не знает о таблицах. Он знает только интерфейсы: список
броней и три inventory-сервиса.

```php
    public function forTraveler(string $travelerId): array
    {
        $summaries = [];

        foreach ($this->store->byTraveler($travelerId) as $booking) {
            $summaries[] = new TripSummary(
                $booking->id,
                $booking->status->value,
                $this->flight->available($booking->flightId),
                $this->hotel->available($booking->hotelId),
                $this->car->available($booking->carId),
            );
        }

        return $summaries;
    }
```

[`src/Reporting/ServiceTripReport.php` L25-L40](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Reporting/ServiceTripReport.php#L25-L40)

Для `N` броней это `1` запрос за списком и `3N` вызовов `available()`. Каждый
вызов проходит через транспорт, а в per-service конфигурации — через
`RemoteTransport`: он считает удалённые вызовы, байты и логическую задержку.
Внутри каждого сервиса `available()` делает ещё один SQL-запрос к своей базе.
Отсюда `1 + 3N` SQL-запросов в отчёте.

Выбор реализации — явный:

```php
    private static function tripReport(
        LabConfig $config,
        SqliteDatabase $bookingDb,
        BookingStore $store,
        InventoryService $flight,
        InventoryService $hotel,
        InventoryService $car,
    ): TripReport {
        if ($config->database === DatabaseMode::Shared && $config->storage === StorageMode::Sql) {
            return new SqlTripReport($bookingDb);
        }

        return new ServiceTripReport($store, $flight, $hotel, $car);
    }
```

[`src/Integration/LabFactory.php` L174-L187](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabFactory.php#L174-L187)

Условие включает не только `database`, но и `storage`. Если база общая, но брони
хранятся документом, SQL-join к `booking_lines` невозможен. Тогда используется
service-отчёт, даже если все данные физически в одном файле. Это ещё один пример
того, что решения взаимодействуют.

### 2.4. Что происходит с транзакцией

В shared-конфигурации `BookingWorkflow` может обернуть резервации, платёж, бронь
и outbox в одну локальную транзакцию. Ошибка откатывает всё.

В per-service конфигурации такой возможности нет. BookingWorkflow использует
сагу: три резервации и платёж коммитятся по отдельности. Если платёж падает,
сага компенсирует три завершённых шага и сам платёж. В эксперименте это видно по
колонке `comp`: у shared-local 0, у per-service-saga 4 на одном отказе.

Это не отчётная проблема, а следствие того же разделения. Нельзя одновременно
получить независимые базы и одну атомарную транзакцию на все записи. Глава 6
разбирает, чем именно платят сага и 2PC.

---

## 3. Эксперимент

```bash
make experiment ARGS="database:shared-per-service"
```

По умолчанию: 20 броней, capacity 30, отказ `payment.authorize` до эффекта,
каждый десятый запрос. Вот реальный вывод:

```text
Experiment: Database per service
bookings=20 capacity=30 failure=payment.authorize every 10

case               confirmed cancelled    comp   report report sql report net    avg ms     avail
shared-local             19        1       0       20          1          0      8.00        11
per-service-saga         19        1       4       20         61       3840      8.40        11
```

Сначала о том, что **не** является главным результатом. Обе конфигурации
подтверждают 19 броней, отменяют одну и оставляют 11 свободных мест. Бизнес-
результат одинаков. Разница в цене пути:

- shared-local использует одну транзакцию; компенсаций нет;
- per-service-saga компенсирует 4 шага на отменённой брони (три резервации и
  платёж);
- отчёт shared-local — 1 SQL-запрос и 20 строк;
- отчёт per-service — 61 SQL-запрос, 60 удалённых вызовов и 3 840 байт запросов.

`report net = 3840` — это 60 вызовов по 64 байта. `report sql = 61` — один
запрос за списком плюс по одному запросу внутри каждого из 60 вызовов
`available()`. Именно это и есть N+1 в его честной форме.

Колонка `avg ms` (8.00 и 8.40) — **средняя latency бронирования**, а не latency
отчёта. В shared-local 20 броней дают 80 удалённых вызовов (четыре на бронь), а в
per-service — 84: 19 успешных по четыре и одна неудачная с четырьмя действиями и
четырьмя компенсациями. При 2 мс на вызов это ровно 8.00 и 8.40 мс. Отчёт
добавляет ещё 60 вызовов, но эксперимент печатает не отдельную latency отчёта, а
среднее по броням. Не читайте 8.40 как «отчёт занимает 8,4 мс».

---

## 4. Бенчмарк

```bash
make benchmark ARGS="04-database"
```

Отдельный suite изолирует отчёт: 30 броней для одного путешественника, затем 1
000 отчётов в каждой конфигурации.

| benchmark                     | итераций | медиана |     p95 | ops/sec | медиана на отчёт |
| ----------------------------- | -------: | ------: | ------: | ------: | ---------------: |
| `database.shared_report`      |    1 000 |  40.018 |  40.492 |  24 988 |          40.0 µs |
| `database.per_service_report` |    1 000 | 563.023 | 593.677 |   1 776 |         563.0 µs |

Per-service отчёт в 14,1 раза медленнее. Это не «сеть медленная»: транспорт
снова внутри процесса, а логическая задержка не спит. Разница — 20 SQL-запросов
за списком и 60 вызовов, каждый из которых проходит через транспорт, загружает
бронь, обращается к сервису и делает SQL-запрос к его базе.

У бенчмарка есть важная оговорка: shared-случай использует модульный монолит и
локальную транзакцию, а per-service — микросервисы и сагу. Это не чистое
сравнение «одна база против шести»: вместе с владением базой меняются форма
развёртывания и транзакционная модель. Именно такую конфигурацию выбрала бы
команда, но приписывать все 14,1 раза только базе нельзя.

Число 40,0 µs для shared-отчёта — медиана одного join по 30 строкам. 563,0 µs
для per-service — медиана полного N+1. Это отчётная latency, в отличие от
колонки `avg ms` в эксперименте.

---

## 5. Что ломается и что не измерено

**Отчёт не является снимком в per-service случае.** `ServiceTripReport` читает
список броней, затем по очереди обращается к трём сервисам для каждой брони.
Между вызовами состояние может измениться. В лаборатории один процесс и нет
конкурентных писателей, поэтому окно не наблюдается, но архитектурно оно есть. В
shared-случае один SQL-запрос даёт согласованный снимок.

**N+1 можно уменьшить, но не отменить границей.** Можно кэшировать доступность,
делать batch-запрос «дай доступность для списка ресурсов» или строить
read-model. Лаборатория не реализует ни один из этих вариантов: она измеряет
наивный, но честный отчёт, который появляется сразу после разделения баз.

**Нет частичных отказов.** Если один inventory-сервис недоступен, реальный отчёт
должен решить: вернуть ошибку, показать частичные данные или использовать кэш. В
модели `available()` либо возвращает число, либо бросает исключение; нет
timeout, retry и fallback.

**Нет распределённой транзакции для отчёта.** Даже если бы она была, удержание
блокировок на время чтения из трёх сервисов — плохая идея для отчёта.
Лаборатория не пытается сделать отчёт атомарным.

**`report sql` считает и запросы внутри сервисов.** Это не «61 запрос к одной
базе». Это сумма запросов, выполненных каждым адаптером в своих файлах. В
shared-случае все они идут в один файл, в per-service — в разные.

**Бенчмарк не включает реальную сеть и сериализацию.** 64 байта запроса и 2 мс
логической задержки — модель, а не HTTP. Настоящий p99 зависит от DNS,
connection pool, TLS, retries и нагрузки, которых здесь нет.

---

## 6. Как это проверено

Тест фиксирует обе формы отчёта и их счётчики:

```php
    public function testSharedDatabaseReportIsOneJoin(): void
    {
        $lab = $this->lab($this->config(
            shape: DeploymentShape::Microservices,
            database: DatabaseMode::Shared,
        ));
        $lab->seed('f1', 'h1', 'c1', capacity: 10);
        $lab->book(new TripRequest('t1', 'f1', 'h1', 'c1'));

        $lab->resetCounters();
        $summaries = $lab->tripReport->forTraveler('t1');

        self::assertSame('sql-join', $lab->tripReport->kind());
        self::assertCount(1, $summaries);
        self::assertSame(1, $lab->counters->get('sqlQueries'));
        self::assertSame(9, $summaries[0]->flightAvailable);
    }

    public function testPerServiceReportCallsEveryInventoryService(): void
    {
        $lab = $this->lab($this->config(
            shape: DeploymentShape::Microservices,
            database: DatabaseMode::PerService,
            transaction: \ArchitectureDecisionLab\Domain\TransactionMode::Saga,
        ));
        $lab->seed('f1', 'h1', 'c1', capacity: 10);
        $lab->book(new TripRequest('t1', 'f1', 'h1', 'c1'));

        $lab->resetCounters();
        $summaries = $lab->tripReport->forTraveler('t1');

        self::assertSame('service-calls', $lab->tripReport->kind());
        self::assertCount(1, $summaries);
        self::assertSame(3, $lab->counters->get('remoteCalls'));
        self::assertSame(9, $summaries[0]->flightAvailable);
    }
```

[`tests/Integration/TripReportTest.php` L14-L49](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/TripReportTest.php#L14-L49)

Оба теста проверяют не только число строк, но и `kind()` и счётчики. Если
`LabFactory` выберет не ту реализацию, тест упадёт. Если service-отчёт
перестанет вызывать сервисы и начнёт читать общую таблицу, `remoteCalls` станет
нулём и тест тоже упадёт.

---

## 7. Что запомнить

- Database per service убирает join не потому, что join плох, а потому, что
  данные принадлежат разным владельцам.
- Отчёт по `N` броням превращается в `1 + 3N` запросов и вызовов. Это N+1,
  измеренный в счётчиках.
- Вместе с базой вы теряете общий снимок и локальную транзакцию. Это не побочный
  эффект, а та же граница.
- Кэш, batch-запрос или read-model могут уменьшить N+1, но это отдельные
  решения, которые нужно проектировать и измерять.
- Сравнение с shared-базой включает не только владение, но и форму
  развёртывания/транзакции. Называйте это явно.
- 14,1 раза в этом контейнере — не приговор. Это цена наивного отчёта при
  выбранной границе.

---

## 8. Упражнения

1. Добавьте в `ServiceTripReport` batch-метод `availableFor(array $ids)` для
   каждого сервиса. Сколько вызовов останется для 20 броней? Как изменится
   контракт сервисов?
2. Постройте read-model, которая хранит доступность вместе с бронью. Какие
   события её обновляют и как быстро она сходится?
3. Объясните, почему shared-отчёт может быть одним SQL-запросом, а
   per-service-отчёт не может быть одной транзакцией даже при наличии 2PC.
4. Запустите `database:shared-per-service --bookings=100` и посмотрите, как
   меняется число компенсаций. Что происходит с capacity?
5. Предложите политику поведения отчёта, если один из трёх сервисов недоступен.
   Какие варианты честны, а какие скрывают проблему?

---

Дальше — глава 5. Она задаёт вопрос, который возникает сразу после разделения
записи и публикации: как событие переживает момент между коммитом и отправкой.
