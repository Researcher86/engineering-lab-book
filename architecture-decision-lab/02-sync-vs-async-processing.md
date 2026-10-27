# Глава 2. Sync и async: 202 Accepted — это обещание, а не результат

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: форма развёртывания. Следующая: SQL vs document storage
> (глава 3).

Асинхронная обработка часто продаётся как «быстрее»: клиент получает ответ
сразу, работа продолжается в фоне. В лаборатории это утверждение проверяется
буквально. `submit` действительно дешевле полного бронирования, потому что он
делает меньше. Но как только вы хотите узнать, чем закончилась поездка, в
уравнение возвращается очередь, worker, повторная запись статуса и ожидание.

Глава разделяет три вещи, которые легко смешать:

- **latency принятия** — сколько клиент ждёт ответа «запрос принят»;
- **latency завершения** — сколько проходит до финального статуса;
- **throughput и back-pressure** — что происходит, когда очередь заполнена.

Главный вопрос не «sync или async», а «какой результат нужен клиенту и что
система обещает, когда ответила `202`».

---

## 1. Проблема

Синхронное бронирование возвращает финальный статус. Клиент знает, подтверждена
поездка или отменена, и может показать результат. Плата — время: запрос
держится, пока не выполнены резервации, платёж и запись события.

Асинхронное бронирование возвращает `pending` и `booking_id`. Работа уходит в
очередь. Это уменьшает время ответа, но создаёт новый контракт:

- нужно где-то хранить `pending`-бронирование;
- нужен worker, который её разберёт;
- нужен способ сообщить клиенту финальный статус;
- нужна политика на случай, если очередь переполнена;
- нужно понимать, что бизнес-ошибка — это не сбой инфраструктуры и её нельзя
  ретраить бесконечно.

Если хотя бы один пункт не решён, асинхронность не ускоряет систему. Она просто
переносит ожидание в место, где его труднее наблюдать.

В лаборатории очередь ограничена. Это принципиально: неограниченная очередь
превращает отказ во «всё в порядке, просто подождите», а память процесса — в
скрытый буфер, который однажды закончится. Ограниченная очередь либо принимает
работу, либо честно отказывает.

---

## 2. Устройство

### 2.1. Точка ветвления

`Lab::book()` — единственное место, где режим обработки меняет путь. Синхронный
режим создаёт бронирование и сразу выполняет workflow. Асинхронный — передаёт
запрос процессору, который только принимает его в очередь.

```php
    public function book(TripRequest $request, int $index = 0): BookingResult
    {
        if ($this->config->processing === ProcessingMode::Async) {
            return $this->async->submit($request, $index);
        }

        $booking = $this->workflow->submit($request);

        return $this->workflow->execute($booking, $index);
    }

    /**
     * @return list<BookingResult>
     */
    public function drain(int $workers = 1, int $batchSize = 10): array
    {
        return $this->async->drain($workers, $batchSize);
    }
```

[`src/Integration/Lab.php` L82-L99](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/Lab.php#L82-L99)

Домен и `BookingWorkflow` не знают о режиме. Это важно: асинхронность — не
отдельная бизнес-логика, а другая точка входа в ту же логику. Worker позже
вызовет тот же `execute()`.

### 2.2. Ограниченная очередь

`JobQueue` хранит `BookingJob` в массиве и проверяет лимит **до** добавления.
Если очередь полна, `enqueue()` возвращает `false`, а счётчик фиксирует отказ.

```php
    public function enqueue(BookingJob $job): bool
    {
        if (count($this->jobs) >= $this->limit) {
            $this->counters->increment('queueRejections');
            $this->counters->set('queueDepthGauge', count($this->jobs));

            return false;
        }

        $this->jobs[] = $job;
        $this->counters->increment('jobsEnqueued');
        $this->counters->set('queueDepthGauge', count($this->jobs));

        return true;
    }

    public function pop(): ?BookingJob
    {
        $job = array_shift($this->jobs);
        $this->counters->set('queueDepthGauge', count($this->jobs));

        return $job;
    }
```

[`src/Processing/JobQueue.php` L27-L49](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Processing/JobQueue.php#L27-L49)

`array_shift()` — осознанно простой, но не бесплатный выбор. Он переиндексирует
массив, поэтому извлечение из очереди стоит `O(n)`, а разбор `n` задач —
`O(n²)`. В главе 6 это упоминается как ограничение учебной реализации, а не как
свойство правильной очереди. Здесь важен сам контракт: лимит существует, отказ
видим, память не растёт бесконечно.

### 2.3. Submit не создаёт бронь, если очередь уже полна

Порядок проверок в `submit()` — самая важная деталь главы. Сначала проверяется
очередь. Только потом создаётся `BookingRecord`.

```php
    public function submit(TripRequest $request, int $index = 0): BookingResult
    {
        $startedAt = $this->clock->now();

        if ($this->queue->isFull()) {
            $this->counters->increment('queueRejections');
            $this->counters->increment('bookingsRejectedByQueue');
            $this->counters->set('queueDepthGauge', $this->queue->size());

            return new BookingResult(
                '',
                BookingStatus::Failed,
                false,
                $this->clock->now() - $startedAt,
                0,
                0,
                'queue is full',
            );
        }

        $booking = $this->workflow->submit($request);
        $job = new BookingJob($booking->id, $index, $startedAt, 0);

        if (!$this->queue->enqueue($job)) {
            $this->counters->increment('bookingsRejectedByQueue');
            $this->store->save($booking->withStatus(BookingStatus::Failed, $this->clock->now()));

            return new BookingResult(
                $booking->id,
                BookingStatus::Failed,
                false,
                $this->clock->now() - $startedAt,
                0,
                0,
                'queue is full',
            );
        }

        return new BookingResult(
            $booking->id,
            BookingStatus::Pending,
            true,
            $this->clock->now() - $startedAt,
            0,
            0,
            'accepted',
        );
    }
```

[`src/Processing/AsyncBookingProcessor.php` L36-L83](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Processing/AsyncBookingProcessor.php#L36-L83)

Первая ветка возвращает пустой `booking_id`: бронь не создавалась, значит, и
показывать клиенту нечего. Это лучше, чем создать `pending`-строку, которую
никто никогда не обработает. Вторая ветка — защита от гонки между `isFull()` и
`enqueue()`. В текущей однопроцессной модели гонки нет, но код не полагается на
это: если `enqueue()` всё-таки отказал, бронь переводится в `failed`.

Пустой `booking_id` — часть контракта, а не случайность. Тест на ограниченную
очередь проверяет, что после отказов в базе остаются только принятые
`pending`-строки.

### 2.4. Worker разбирает очередь детерминированно

`drain()` — не настоящий пул потоков. Это цикл по workers и batch size. Он
извлекает задачу, загружает бронь и вызывает тот же
`BookingWorkflow::execute()`.

```php
    public function drain(int $workers = 1, int $batchSize = 10): array
    {
        $results = [];
        $this->counters->increment('workerTicks');

        for ($worker = 0; $worker < $workers; ++$worker) {
            for ($item = 0; $item < $batchSize; ++$item) {
                $job = $this->queue->pop();

                if ($job === null) {
                    break 2;
                }

                $this->counters->increment('jobsProcessed');
                $booking = $this->store->get($job->bookingId);

                if ($booking === null) {
                    $this->counters->increment('jobsFailed');
                    continue;
                }

                try {
                    $result = $this->workflow->execute($booking, $job->index);
                } catch (\Throwable $throwable) {
                    if ($job->attempts + 1 < $this->maxAttempts) {
                        $this->counters->increment('jobsRetried');
                        $this->queue->enqueue($job->retry());
                        continue;
                    }

                    $this->counters->increment('jobsFailed');
                    $this->store->save($booking->withStatus(BookingStatus::Failed, $this->clock->now()));
                    $results[] = new BookingResult(
                        $booking->id,
                        BookingStatus::Failed,
                        false,
                        $this->clock->now() - $job->submittedAt,
                        $this->clock->now() - $job->submittedAt,
                        0,
                        $throwable->getMessage(),
                    );
                    continue;
                }

                $results[] = $result->withLatencies(
                    $result->clientLatencyMs,
                    $this->clock->now() - $job->submittedAt,
                );
            }
        }

        $this->counters->set('queueDepthGauge', $this->queue->size());

        return $results;
    }
```

[`src/Processing/AsyncBookingProcessor.php` L88-L142](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Processing/AsyncBookingProcessor.php#L88-L142)

Здесь есть важное различие между **исключением** и **бизнес-отказом**.
`BookingWorkflow::execute()` обрабатывает отказ платежа и возвращает
`BookingResult` со статусом `cancelled`; он не бросает исключение наружу.
Поэтому worker не ретраит такой результат. Ретрай в `catch` предназначен для
инфраструктурных сбоев, которые действительно выбросили исключение. В
лаборатории таких сбоев почти нет, и это честно: политика ретраев не
симулируется как полноценная.

Когда worker забирает задачу, он вычисляет `completionLatencyMs` как разницу
между текущим логическим временем и `submittedAt`. Это время включает ожидание в
очереди, а не только выполнение. В синхронном режиме обе latency совпадают,
потому что клиент ждёт весь путь.

---

## 3. Эксперимент

```bash
make experiment ARGS="processing:sync-async"
```

По умолчанию: 50 бронирований, capacity 60, очередь 50, два worker, batch 50.
Вот реальный вывод:

```text
Experiment: Sync vs async
bookings=50 capacity=60 queue_limit=50 workers=2 batch=50

case            accepted rejected confirmed  client ms completion ms   queue max      sql
sync                  50        0       50       0.00          0.00           0     1680
async                 50        0       50       1.00         25.50          50     1780
async-bounded         10       40       10       1.00          5.50          10      380
```

Три строки — три разных вопроса.

**`sync`.** Все 50 приняты и подтверждены. Логическое время клиента и завершения
равно нулю: в синхронном пути нет искусственной задержки, а реальное время
измеряется бенчмарком, не этим экспериментом. SQL = 1 680 — это весь прогон,
включая схему и seed.

**`async`.** Все 50 приняты, но клиент ждёт только 1,00 мс — стоимость submit.
Очередь доходит до 50, а завершение занимает в среднем 25,50 мс: работа
дождалась worker и прошла тот же путь. Это не ускорение бронирования. Это
перемещение ожидания из клиентского запроса в очередь.

**`async-bounded`.** Лимит очереди 10. Принято 10, отклонено 40. Completion 5,50
мс ниже, чем у неограниченного async, но не потому, что bounded-очередь быстрее.
Просто в очередь попало всего 10 задач, и ждать пришлось меньше. 40 отклонённых
запросов не создали ни одной `pending`-строки.

Обратите внимание на `sql = 380` в bounded-случае. Это не «в 4,4 раза
эффективнее». Это просто меньше принятой работы. Сравнивать SQL-запросы между
строками с разным числом принятых броней бессмысленно.

---

## 4. Бенчмарк

```bash
make benchmark ARGS="02-processing"
```

500 операций на измерение. `async_submit` только отправляет работу и оставляет
её в очереди; `async_roundtrip` отправляет и сразу разбирает одну задачу.

| benchmark                    | итераций | медиана |     p95 | ops/sec | медиана на операцию |
| ---------------------------- | -------: | ------: | ------: | ------: | ------------------: |
| `processing.sync_booking`    |      500 | 594.522 | 867.059 |     841 |            1 189 µs |
| `processing.async_submit`    |      500 |  95.479 | 103.817 |   5 237 |              191 µs |
| `processing.async_roundtrip` |      500 | 666.326 | 723.606 |     750 |            1 333 µs |

Submit в 6,2 раза дешевле полного синхронного бронирования. Это ожидаемо: он
создаёт `pending`-строку и кладёт задачу в очередь, но не резервирует ресурсы и
не проводит платёж. За эту дешевизну платит память: за пять измеренных
повторений в очереди накопилось 2 500 задач, а прирост PHP allocator составил
около 1,1 MiB. Это состояние очереди, а не бесплатный ресурс.

Roundtrip, который дожидается выполнения, на 12,1% медленнее синхронного пути.
Причина не в «асинхронности как таковой», а в дополнительных шагах: запись
`pending`, постановка в очередь, извлечение, повторная загрузка брони и
обновление статуса. Если клиенту нужен финальный результат, асинхронная схема
добавляет стоимость к тому же пути.

Этот бенчмарк не измеряет throughput очереди под конкурентной нагрузкой. В нём
один процесс, один worker в roundtrip-случае и отсутствие блокировок. Число
`ops/sec` здесь — производная от медианы, а не пропускная способность
production-брокера.

---

## 5. Что ломается и что не измерено

**Submit не является идемпотентным.** Повторная отправка того же `TripRequest`
создаст новую бронь и новую задачу. В лаборатории нет idempotency key на входе.
Это ограничение, а не скрытая функция: клиент должен сам решать, как не
отправить заказ дважды.

**Очередь живёт в памяти процесса.** Она исчезнет при падении процесса. Нет
persistent queue, нет lease, нет visibility timeout. `BookingJob` хранит
`attempts`, но retry-политика минимальна: только для исключений, без backoff.

**Бизнес-отказ не ретраится.** Это правильно: если платёж отклонил операцию,
повтор через секунду не сделает её успешной. Но в лаборатории нет отдельного
класса инфраструктурных ошибок, поэтому граница между «повторить» и «не
повторять» задана кодом workflow, а не типом исключения.

**Нет способа доставить финальный статус клиенту.** Эксперимент вызывает
`drain()` и читает результаты напрямую. В реальной системе нужен polling,
webhook, WebSocket или отдельный notification-сервис. Лаборатория не выбирает за
вас этот канал.

**Нет настоящей конкурентности.** Два worker в эксперименте — это два
последовательных цикла, а не два потока. Поэтому нет гонок за одну задачу, нет
двойной обработки и нет необходимости в распределённой блокировке.

**`array_shift()` — не production-очередь.** Извлечение из массива
переиндексирует его. Для 50 задач это незаметно, для миллиона — нет. Бенчмарк
roundtrip разбирает по одной задаче и не показывает эту стоимость.

---

## 6. Как это проверено

Тест на ограниченную очередь фиксирует не только число отказов, но и отсутствие
фантомных `pending`-строк:

```php
    public function testBoundedQueueRejectsWorkInsteadOfGrowingWithoutLimit(): void
    {
        $lab = $this->lab($this->config(
            processing: ProcessingMode::Async,
            queueLimit: 2,
        ));
        $lab->seed('f1', 'h1', 'c1', capacity: 10);

        $accepted = 0;
        $rejected = 0;

        for ($index = 0; $index < 4; ++$index) {
            $result = $lab->book(new TripRequest('t' . $index, 'f1', 'h1', 'c1'), $index);

            if ($result->accepted) {
                ++$accepted;
            } else {
                ++$rejected;
            }
        }

        self::assertSame(2, $accepted);
        self::assertSame(2, $rejected);
        self::assertSame(2, $lab->queue->size());
        self::assertSame(2, $lab->counters->get('queueRejections'));
        self::assertSame(2, $lab->store->countByStatus()['pending'] ?? 0);
    }
```

[`tests/Integration/AsyncProcessingTest.php` L14-L40](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/AsyncProcessingTest.php#L14-L40)

Второй тест отделяет бизнес-отказ от инфраструктурного сбоя:

```php
    public function testABusinessFailureIsCancelledRatherThanRetried(): void
    {
        $counters = new \ArchitectureDecisionLab\Instrumentation\Counters();
        $faults = new \ArchitectureDecisionLab\Foundation\FaultInjector(
            step: 'payment.authorize',
            counters: $counters,
        );
        $lab = $this->lab(
            $this->config(
                processing: ProcessingMode::Async,
                queueLimit: 10,
                transaction: \ArchitectureDecisionLab\Domain\TransactionMode::Saga,
            ),
            $faults,
            $counters,
        );
        $lab->seed('f1', 'h1', 'c1', capacity: 10);
        $lab->book(new TripRequest('t1', 'f1', 'h1', 'c1'), 0);

        $results = $lab->drain(workers: 1, batchSize: 10);

        self::assertCount(1, $results);
        self::assertSame(BookingStatus::Cancelled, $results[0]->status);
        self::assertSame(0, $lab->counters->get('jobsRetried'));
        self::assertSame(0, $lab->counters->get('jobsFailed'));
    }
```

[`tests/Integration/AsyncProcessingTest.php` L65-L90](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Integration/AsyncProcessingTest.php#L65-L90)

Тест утверждает три вещи одновременно: результат отменён, ретраев нет, счётчик
инфраструктурных падений равен нулю. Если бы workflow начал бросать исключение
на отказ платежа, тест поймал бы изменение контракта.

---

## 7. Что запомнить

- Асинхронность уменьшает latency **принятия**, а не latency выполнения.
- `202 Accepted` — обещание, что работа будет выполнена или отменена. Это
  обещание нужно где-то хранить и уметь проверять.
- Ограниченная очередь с явным отказом лучше неограниченной, которая маскирует
  перегрузку ростом памяти.
- Проверка очереди до создания брони предотвращает `pending`-строки, которые
  никто не обработает.
- Бизнес-отказ и инфраструктурный сбой — разные вещи. Ретраить
  `payment declined` бессмысленно.
- Submit-бенчмарк не равен end-to-end-бенчмарку. Сравнивайте операции, которые
  делают одинаковую работу.
- `array_shift()` и очередь в памяти — учебные ограничения, а не рецепт для
  production.

---

## 8. Упражнения

1. Запустите `processing:sync-async --bookings=100 --queue-limit=10` и
   объясните, почему completion latency падает, хотя нагрузка выросла.
2. Добавьте в `AsyncBookingProcessor` счётчик отклонённых запросов по причине
   «очередь полна» и сравните его с числом созданных `pending`-строк. Что должно
   совпасть?
3. Предложите idempotency key для submit. Где его хранить и как отличить
   повторную отправку от нового заказа?
4. Измерьте, как меняется `async_submit` при `queue-limit=100` и при
   неограниченной очереди. Что именно вы измеряете в каждом случае?
5. Объясните, почему `completionLatencyMs` в синхронном режиме равен
   `clientLatencyMs`, а в асинхронном — нет. Где находится граница наблюдения?

---

Дальше — глава 3. Она спрашивает, что происходит, когда меняется не момент
ответа, а форма хранения: нормализованные таблицы против документа.
