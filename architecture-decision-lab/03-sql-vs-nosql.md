# Глава 3. SQL и document: форма запроса важнее ярлыка

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: sync и async. Следующая: database per service (глава 4).

«SQL или NoSQL» — плохой первый вопрос. Он смешивает три независимых решения:
как хранить агрегат, какие запросы к нему нужны и какие гарантии транзакции
требуются. В лаборатории эти решения разведены. Обе реализации — SQL и document
— работают на SQLite. Document-адаптер хранит весь агрегат одной JSON-строкой в
таблице `documents`. Это не MongoDB и не попытка изобразить MongoDB. Это способ
показать, как форма агрегата меняет цену конкретных запросов.

Результат главы симметричен: документ выигрывает на точечном чтении агрегата и
проигрывает на запросе по чужому полю. Вопрос не «какая база быстрее», а «какие
запросы вы собираетесь делать и какие индексы готовы поддерживать».

---

## 1. Проблема

Бронирование поездки — это агрегат: заголовок брони, путешественник, три позиции
(рейс, отель, машина), суммы и статусы. В нормализованной модели заголовок лежит
в `bookings`, позиции — в `booking_lines`. В документной модели всё это лежит в
одном JSON-документе.

У двух форм разные сильные стороны:

- **SQL.** Один join возвращает все брони путешественника. Индекс по
  `traveler_id` позволяет не читать чужие строки. Но точечное чтение агрегата
  требует двух запросов: заголовок и позиции.
- **Document.** Точечное чтение — один запрос и один JSON decode. Но запрос «все
  брони путешественника» не имеет индекса по полю внутри JSON: адаптер читает
  всю коллекцию и фильтрует в PHP.

Это не моральный выбор между «старой» и «новой» базой. Это выбор того, какой
запрос будет частым и какую денормализацию вы готовы оплачивать.

В лаборатории оба хранилища реализуют один интерфейс `BookingStore`, поэтому
workflow, outbox и тесты работают с любой формой. Различие остаётся только в
адаптере и в схеме.

---

## 2. Устройство

### 2.1. Общий контракт

Интерфейс требует не только CRUD, но и транзакцию, и запись outbox-события
вместе с бронью. Это важно: document-хранилище не освобождается от
транзакционных обязанностей.

```php
interface BookingStore
{
    public function create(BookingRecord $booking): void;

    /**
     * Writes the booking and, when delivery is transactional, the outbox row in
     * the same database transaction.
     */
    public function save(BookingRecord $booking, ?EventEnvelope $event = null): void;

    public function get(string $id): ?BookingRecord;

    /**
     * @return list<BookingRecord>
     */
    public function byTraveler(string $travelerId): array;

    /**
     * @return array<string, int>
     */
    public function countByStatus(): array;

    /**
     * @template T
     *
     * @param callable(): T $work
     *
     * @return T
     */
    public function transaction(callable $work): mixed;

    public function kind(): string;

    /**
     * @return list<\ArchitectureDecisionLab\Domain\BookingLine>
     */
    public function lines(string $bookingId): array;
}
```

[`src/Storage/BookingStore.php` L10-L47](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/BookingStore.php#L10-L47)

`kind()` возвращает `sql` или `document`. Эксперимент печатает это значение,
чтобы таблица не осталась без подписи. `transaction()` позволяет workflow
обернуть несколько операций, даже если конкретный адаптер хранит агрегат иначе.

### 2.2. Нормализованная запись

`SqlBookingStore::save()` обновляет заголовок, удаляет старые позиции и
вставляет новые. Всё это — в одной транзакции. Если передан event, outbox-строка
пишется туда же.

```php
    public function save(BookingRecord $booking, ?EventEnvelope $event = null): void
    {
        $this->db->transaction(function () use ($booking, $event): void {
            $this->writeBooking($booking);
            $this->db->execute(
                'DELETE FROM booking_lines WHERE booking_id = :booking_id',
                ['booking_id' => $booking->id],
            );

            foreach ($booking->lines as $line) {
                $this->db->execute(
                    'INSERT INTO booking_lines
                        (booking_id, resource_type, resource_id, units, price_cents, state)
                     VALUES (:booking_id, :resource_type, :resource_id, :units, :price_cents, :state)',
                    [
                        'booking_id' => $booking->id,
                        'resource_type' => $line->resourceType->value,
                        'resource_id' => $line->resourceId,
                        'units' => $line->units,
                        'price_cents' => $line->priceCents,
                        'state' => $line->state,
                    ],
                );
                $this->counters->increment('bookingLinesWritten');
            }

            if ($event !== null) {
                $this->outbox->append($event);
            }

            $this->counters->increment('bookingWrites');
        });
    }
```

[`src/Storage/SqlBookingStore.php` L35-L67](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/SqlBookingStore.php#L35-L67)

Удаление и повторная вставка позиций — простой способ синхронизировать агрегат с
таблицей. Он не самый эффективный для больших агрегатов, зато делает состояние
детерминированным: после `save()` в `booking_lines` ровно те строки, которые
сейчас в объекте. В лаборатории у брони всегда три позиции, поэтому цена
операции предсказуема.

Индекс по путешественнику создаётся явно:

```php
        $db->execute(
            'CREATE INDEX IF NOT EXISTS bookings_traveler
             ON bookings (traveler_id, status)',
        );

        $db->execute(
            'CREATE TABLE IF NOT EXISTS booking_lines (
                booking_id TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                units INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                state TEXT NOT NULL,
                PRIMARY KEY (booking_id, resource_type)
            )',
        );
```

[`src/Storage/Schema.php` L88-L103](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/Schema.php#L88-L103)

`bookings_traveler` позволяет найти брони путешественника, не сканируя всю
таблицу. Дальше join к `booking_lines` собирает агрегат. Именно этот индекс
отсутствует у document-адаптера по полю `traveler_id`.

### 2.3. Точечное чтение и запрос путешественника в SQL

Точечное чтение делает два запроса: заголовок и позиции. Запрос путешественника
— один join.

```php
    public function byTraveler(string $travelerId): array
    {
        $rows = $this->db->select(
            'SELECT b.id, b.traveler_id, b.flight_id, b.hotel_id, b.car_id, b.status,
                    b.amount_cents, b.created_at, b.updated_at,
                    l.resource_type, l.resource_id, l.units, l.price_cents, l.state
             FROM bookings b
             LEFT JOIN booking_lines l ON l.booking_id = b.id
             WHERE b.traveler_id = :traveler_id
             ORDER BY b.id,
                CASE l.resource_type WHEN \'flight\' THEN 0 WHEN \'hotel\' THEN 1 ELSE 2 END',
            ['traveler_id' => $travelerId],
        );

        return $this->hydrateJoined($rows);
    }
```

[`src/Storage/SqlBookingStore.php` L92-L107](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/SqlBookingStore.php#L92-L107)

`hydrateJoined()` группирует строки по `id` и собирает `BookingLine`. Один
SQL-запрос возвращает `1 + 3B` строк для `B` броней (заголовок и три позиции),
но приложению не нужно делать N+1 запросов. Это форма, которую глава 4
сравнивает с обращением к трём сервисам.

### 2.4. Документ: один агрегат — одна строка

Document-адаптер хранит агрегат в `documents`:

```php
        $db->execute(
            'CREATE TABLE IF NOT EXISTS documents (
                collection TEXT NOT NULL,
                id TEXT NOT NULL,
                body TEXT NOT NULL,
                version INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY (collection, id)
            )',
        );
```

[`src/Storage/Schema.php` L123-L132](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/Schema.php#L123-L132)

Точечное чтение — один `SELECT` по первичному ключу и один `json_decode`:

```php
    public function get(string $id): ?BookingRecord
    {
        $rows = $this->db->select(
            'SELECT body FROM documents WHERE collection = :collection AND id = :id',
            ['collection' => self::COLLECTION, 'id' => $id],
        );

        if ($rows === []) {
            return null;
        }

        $this->counters->increment('documentsRead');

        return $this->decode($this->string($rows[0]['body']));
    }
```

[`src/Storage/DocumentBookingStore.php` L48-L62](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/DocumentBookingStore.php#L48-L62)

Запрос путешественника выглядит иначе. В таблице нет индексируемого столбца
`traveler_id`, поэтому адаптер читает всю коллекцию и фильтрует результат после
decode:

```php
    public function byTraveler(string $travelerId): array
    {
        $rows = $this->db->select(
            'SELECT body FROM documents WHERE collection = :collection ORDER BY id',
            ['collection' => self::COLLECTION],
        );

        $this->counters->add('documentsScanned', count($rows));
        $bookings = [];

        foreach ($rows as $row) {
            $booking = $this->decode($this->string($row['body']));

            if ($booking->travelerId === $travelerId) {
                $bookings[] = $booking;
            }
        }

        $this->counters->add('documentsRead', count($bookings));

        return $bookings;
    }
```

[`src/Storage/DocumentBookingStore.php` L64-L85](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Storage/DocumentBookingStore.php#L64-L85)

Именно `ORDER BY id` и последующий PHP-фильтр делают запрос `O(total documents)`
по чтению и decode. В реальной документной базе можно было бы создать индекс по
`traveler_id`. В этой лаборатории такого индекса нет — и это часть модели, а не
случайная забывчивость. Мы измеряем форму «агрегат вместе» без вторичных
индексов.

Запись тоже проста: один `INSERT ... ON CONFLICT` с JSON-телом и инкрементом
версии. В отличие от SQL, нет отдельных строк для позиций, поэтому нет delete +
insert на каждую позицию. Зато любое изменение любой позиции перезаписывает весь
документ.

### 2.5. Подсчёт по статусу

`countByStatus()` в SQL — один `GROUP BY` по индексу/таблице. В document-версии
— чтение всех документов и подсчёт после decode. Это ещё одна форма запроса,
которая не является точечной и поэтому платит за размер коллекции. В таблице
эксперимента она показывает разницу между `O(1)` строкой результата и
`O(total documents)` сканированием.

---

## 3. Эксперимент

```bash
make experiment ARGS="storage:sql-document"
```

По умолчанию: 100 броней, 10 путешественников, capacity 110. Оба хранилища
создаются отдельно, заполняются одинаковыми данными, counters сбрасываются перед
каждой операцией. Вот реальный вывод:

```text
Experiment: SQL vs document
bookings=100 travelers=10 capacity=110

operation          store       queries     rows      bytes    scanned     read
point_read         sql               2        4        478          0        0
traveler_query     sql              10      300      83356          0        0
status_count       sql               1        1         36          0        0
point_read         document          1        1        524          0        1
traveler_query     document         10     1000     524830       1000      100
status_count       document          1      100      52483        100        0
```

Читаем таблицу по операциям.

**`point_read`.** SQL: 2 запроса, 4 строки (заголовок + три позиции), 478 байт.
Document: 1 запрос, 1 строка, 524 байта, один decode. Документ быстрее по числу
запросов и строк, но тело документа больше суммы нормализованных строк. В
бенчмарке это даёт 1,9-кратный выигрыш по медиане.

**`traveler_query`.** Эксперимент обходит 10 путешественников, поэтому SQL
делает 10 запросов и читает 300 строк (по три позиции на бронь), а document
делает 10 запросов, но каждый читает всю коллекцию: 1 000 документов, из которых
100 совпадают. `scanned = 1000`, `read = 100`. Это и есть полное сканирование
коллекции.

**`status_count`.** SQL: 1 запрос, 1 строка результата, 36 байт. Document: 1
запрос, 100 строк, 52 483 байта, 100 сканирований. Даже если запрос один, объём
прочитанного и decode растёт с числом документов.

Байты в таблице — это размер JSON/строк, которые адаптер действительно прочитал,
а не страницы SQLite и не сетевой трафик. Не превращайте их в «стоимость диска».

---

## 4. Бенчмарк

```bash
make benchmark ARGS="03-storage"
```

Каждое хранилище заполняется 100 бронями и 10 путешественниками. Точечное чтение
— 5 000 итераций, запрос путешественника — 1 000.

| benchmark                         | итераций | медиана |     p95 | ops/sec | медиана на операцию |
| --------------------------------- | -------: | ------: | ------: | ------: | ------------------: |
| `storage.sql_point_read`          |    5 000 |  88.984 |  90.210 |  56 190 |             17.8 µs |
| `storage.document_point_read`     |    5 000 |  46.674 |  47.337 | 107 126 |              9.3 µs |
| `storage.sql_traveler_query`      |    1 000 |  84.343 |  90.853 |  11 856 |             84.3 µs |
| `storage.document_traveler_query` |    1 000 | 452.050 | 461.615 |   2 212 |            452.1 µs |

Точечное чтение документа в 1,9 раза быстрее: одна строка и один decode против
двух запросов и четырёх строк. Запрос путешественника в 5,4 раза медленнее:
полное сканирование и decode всей коллекции против join по индексу.

Это результат формы запроса, а не приговор MongoDB. У document-адаптера нет
вторичного индекса, статистики планировщика и сжатия. Настоящая документная база
может добавить индекс по `traveler_id` и изменить эту таблицу. Меняется не
только скорость, но и цена записи: индекс нужно поддерживать. Лаборатория
измеряет один конкретный компромисс, а не все возможные документные базы.

---

## 5. Что ломается и что не измерено

**Document-адаптер — это SQLite с JSON-колонкой.** Он не моделирует replication,
sharding, read concern, write concern, eventual consistency самой базы или
поведение MongoDB под нагрузкой. Все гарантии транзакции здесь — гарантии
SQLite, а не документной базы.

**Нет вторичных индексов по полям документа.** Поэтому запрос по `traveler_id` —
полное сканирование. Это сделано намеренно, чтобы показать стоимость
кросс-агрегатного запроса. Если добавить индекс, запрос ускорится, но появится
стоимость поддержки индекса при каждой записи; лаборатория этого не измеряет.

**Точечное чтение агрегата — не то же самое, что произвольный запрос.** Документ
хорош, когда вы всегда читаете агрегат целиком по ключу. Он плох, когда нужно
часто фильтровать по полям, которые не входят в ключ.

**Байты — не страницы.** `sqlBytesRead` и `documentsRead` считают то, что
адаптер вернул в PHP. Они не показывают, сколько страниц SQLite прочитала с
диска, сколько было в page cache и сколько заняла запись WAL.

**Нет конкурентности.** Один процесс, одно соединение на базу. Поведение под
параллельными записями, блокировками и конфликтами версий не моделируется.

**Нет миграций.** Схема создаётся с нуля при каждом запуске. Эволюция схемы и
backfill в лаборатории отсутствуют.

---

## 6. Как это проверено

Тест на запрос путешественника фиксирует не только результат, но и форму работы:
SQL делает один запрос, document сканирует всю коллекцию.

```php
    public function testTravelerQueryScansEveryDocumentButUsesOneJoinInSql(): void
    {
        $sql = $this->lab($this->config(storage: StorageMode::Sql));
        $document = $this->lab($this->config(storage: StorageMode::Document));
        $sql->seed('f1', 'h1', 'c1', capacity: 20);
        $document->seed('f1', 'h1', 'c1', capacity: 20);

        foreach (['t1', 't1', 't1', 't2', 't2'] as $traveler) {
            $sql->book(new TripRequest($traveler, 'f1', 'h1', 'c1'));
            $document->book(new TripRequest($traveler, 'f1', 'h1', 'c1'));
        }

        $sql->resetCounters();
        $document->resetCounters();

        $sqlBookings = $sql->store->byTraveler('t1');
        $documentBookings = $document->store->byTraveler('t1');

        self::assertCount(3, $sqlBookings);
        self::assertCount(3, $documentBookings);
        self::assertSame(1, $sql->counters->get('sqlQueries'));
        self::assertSame(5, $document->counters->get('documentsScanned'));
        self::assertSame(3, $document->counters->get('documentsRead'));
    }
```

[`tests/Integration/StorageModesTest.php` L37-L60](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/StorageModesTest.php#L37-L60)

Тест на точечное чтение проверяет другой компромисс: SQL делает два запроса,
document — один.

Есть и дифференциальный тест: оба хранилища возвращают одинаковый агрегат — тот
же amount, те же три позиции, те же значения. Это важно, потому что сравнение
производительности имеет смысл только при одинаковой семантике.

---

## 7. Что запомнить

- «SQL vs NoSQL» — это не про ярлык базы, а про форму агрегата и запросов.
- Хранить агрегат вместе выгодно для точечного чтения по ключу.
- Запрос по полю, которого нет в ключе, требует индекса или сканирования.
- Нормализация выигрывает на join и агрегатах; документ — на чтении целого
  агрегата.
- Сравнивайте одинаковые операции и одинаковые данные. Иначе цифры ничего не
  значат.
- Document-адаптер на SQLite не является MongoDB. Его ограничения нужно называть
  явно.
- Байты адаптера — не сеть и не страницы диска. Это объём данных, который
  прочитал PHP-код.

---

## 8. Упражнения

1. Добавьте в `documents` индекс по `traveler_id` (например, отдельную таблицу
   `document_travelers`). Как изменится запрос? Какую цену вы заплатите на
   записи?
2. Запустите `storage:sql-document --bookings=1000 --travelers=100` и объясните,
   как меняется отношение SQL/document для запроса путешественника.
3. Почему статус-подсчёт в документном хранилище сканирует все документы, хотя
   возвращает только несколько чисел? Что нужно изменить в модели?
4. Сравните размер JSON-документа и сумму строк SQL для одной брони. Почему
   документ больше и когда это перестаёт быть проблемой?
5. Предложите запрос, на котором документная модель выиграет даже без индекса.
   Объясните, почему он не входит в эксперимент.

---

Дальше — глава 4. Она берёт тот же агрегат и разделяет его между сервисами: что
произойдёт с отчётом, когда join станет вызовом?
