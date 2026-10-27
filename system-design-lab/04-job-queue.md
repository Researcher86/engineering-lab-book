# Глава 4. Job Queue: lease, retry и честный dead letter

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: notification service (глава 3). Следующая глава: distributed
> cache (глава 5).

Очередь кажется простой структурой: положить задачу, взять задачу, выполнить.
Сложность начинается, когда worker падает между «взял» и «выполнил». Очередь не
знает, выполнилась ли работа: ответ мог потеряться, процесс мог умереть, сеть
могла разорваться после side effect. Единственное, что можно сделать, — выдать
задачу ещё раз и потребовать от получателя идемпотентности. Это at-least-once, а
не exactly-once.

В лаборатории очередь — это три heap-а, карта lease и набор счётчиков. Ready
heap хранит задачи, готовые к выполнению, по приоритету и FIFO. Delayed heap —
задачи, у которых ещё не наступило время. Arrival heap — для возраста самой
старой задачи. Lease выдаёт временное право на выполнение; истечение lease
возвращает задачу в ready. Retry budget и dead-letter list завершают состояние.

---

## 1. Проблема

Пусть worker взял задачу, отправил письмо и умер до `complete()`. Если очередь
считает задачу выполненной, письмо потеряно. Если очередь сразу возвращает
задачу, два worker-а могут отправить два письма. Без знания о side effect нельзя
выбрать «правильно». Можно лишь выбрать, что именно гарантировать.

Лаборатория гарантирует at-least-once: задача будет выдана ещё раз, пока не
истечёт бюджет попыток. Получатель обязан сделать side effect идемпотентным по
стабильному ключу. Очередь со своей стороны хранит завершённые id в TTL, чтобы
повторный enqueue не запускал работу снова. Это две половины одной защиты, и ни
одна из них не даёт exactly-once.

Вторая проблема — порядок. Приоритетная очередь должна выдавать более важные
задачи раньше. Но если высокоприоритетный поток не кончается, низкоприоритетные
задачи голодают. Heap корректен, а очередь несправедлива. Эксперимент с пятью
уровнями показывает и нулевые инверсии, и цену ожидания хвоста.

Третья проблема — backlog. Очередь не «поглощает» нагрузку: она превращает её в
задержку. Если producer стабильно быстрее consumer, глубина растёт, а возраст
самой старой задачи увеличивается. Смотреть только на глубину недостаточно: две
тысячи задач могут быть свежими или очень старыми.

---

## 2. Устройство

### 2.1. Состояние задачи

Задача хранит попытки, lease, время завершения и ошибку. `attempts` считает
выдачи, а не неудачи: worker, который исчез, всё равно потратил попытку.

```php
/**
 * One unit of work and its scheduling state.
 *
 * The queue owns the heaps; the job owns whether it is pending, leased,
 * completed or dead-lettered. `attempts` counts leases, not failures: a
 * worker that disappears still consumed one delivery. A completion or failure
 * from an expired lease must be rejected by the queue, not trusted.
 */
final class Job
{
    private int $availableAtMs;

    private int $attempts = 0;

    private ?string $leaseOwner = null;

    private ?int $leaseExpiresAtMs = null;

    private ?int $completedAtMs = null;

    private ?int $deadLetteredAtMs = null;

    private ?string $lastError = null;

    public function __construct(
        public readonly string $id,
        public readonly string $type,
        public readonly int $priority,
        public readonly int $createdAtMs,
        public readonly int $maxAttempts = 5,
        ?int $availableAtMs = null,
    ) {
        if (trim($this->id) === '') {
            throw new InvalidArgumentException('Job id cannot be empty');
        }

        if (trim($this->type) === '') {
            throw new InvalidArgumentException('Job type cannot be empty');
        }

        if ($this->createdAtMs < 0) {
            throw new InvalidArgumentException('Job creation timestamp cannot be negative');
        }

        if ($this->maxAttempts < 1) {
            throw new InvalidArgumentException('Job max attempts must be at least 1');
        }

        $availableAtMs ??= $this->createdAtMs;

        if ($availableAtMs < $this->createdAtMs) {
            throw new InvalidArgumentException('A job cannot become available before it was created');
        }

        $this->availableAtMs = $availableAtMs;
    }
```

[`src/Queue/Job.php` L10-L65](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/Job.php#L10-L65)

`lease()` увеличивает `attempts` и записывает владельца. Проверки не дают взять
задачу дважды или взять её раньше `availableAtMs`.

```php
    public function lease(string $workerId, int $nowMs, int $visibilityTimeoutMs): void
    {
        if (trim($workerId) === '') {
            throw new InvalidArgumentException('Worker id cannot be empty');
        }

        if ($nowMs < 0) {
            throw new InvalidArgumentException('Timestamp cannot be negative');
        }

        if ($visibilityTimeoutMs < 1) {
            throw new InvalidArgumentException('Visibility timeout must be at least 1 ms');
        }

        if (!$this->isPending()) {
            throw new LogicException(sprintf('Job %s is no longer pending', $this->id));
        }

        if ($this->leaseOwner !== null) {
            throw new LogicException(sprintf('Job %s is already leased', $this->id));
        }

        if ($nowMs < $this->availableAtMs) {
            throw new LogicException(sprintf('Job %s is not available yet', $this->id));
        }

        ++$this->attempts;
        $this->leaseOwner = $workerId;
        $this->leaseExpiresAtMs = $nowMs + $visibilityTimeoutMs;
    }
```

[`src/Queue/Job.php` L119-L148](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/Job.php#L119-L148)

### 2.2. Три heap-а и карта lease

`JobQueue` разделяет ready и delayed. Это принципиально: высокоприоритетная
задача, которая ещё не наступила, не должна блокировать низкоприоритетную,
готовую прямо сейчас.

```php
/**
 * A deterministic in-process job queue with delayed and ready heaps.
 *
 * A delayed job is not in the ready heap: a high-priority job that is not due
 * yet must not block a lower-priority job that is due now. `lease()` first
 * releases expired leases, then promotes due delayed jobs, then extracts the
 * highest-priority ready job. The two heaps and the lease map are the whole
 * state machine; the counters are the evidence that it behaved.
 *
 * Delivery is at-least-once. An expired lease makes the job available again;
 * a worker that returns after its lease expired is told that its result is
 * stale. The queue does not claim exactly-once.
 */
final class JobQueue
{
    /** @var SplPriorityQueue<array{int, int}, Job> */
    private readonly SplPriorityQueue $ready;

    /** @var SplPriorityQueue<array{int, int}, Job> */
    private readonly SplPriorityQueue $delayed;

    /** @var SplPriorityQueue<array{int, int}, Job> */
    private readonly SplPriorityQueue $arrivals;

    /** @var array<string, Lease> */
    private array $leases = [];

    /** @var array<string, true> */
    private array $active = [];

    /** @var list<Job> */
    private array $deadLetters = [];

    private readonly ProcessedJobStore $processed;

    private readonly Counters $counters;

    private int $sequence = 0;
```

[`src/Queue/JobQueue.php` L12-L49](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L12-L49)

### 2.3. Постановка и выдача

`enqueue()` проверяет, что задача ещё pending и её id не активен и не завершён.
Только после этого она попадает в ready или delayed. `lease()` сначала
освобождает просроченные lease, затем продвигает созревшие delayed, затем
извлекает ready.

```php
    public function enqueue(Job $job, int $nowMs): bool
    {
        $this->assertTimestamp($nowMs);

        if (!$job->isPending()) {
            throw new InvalidArgumentException(sprintf('Job %s is already finished', $job->id));
        }

        if (isset($this->active[$job->id]) || $this->processed->isProcessed($job->id, $nowMs)) {
            $this->counters->add('duplicateEnqueues');

            return false;
        }

        $this->active[$job->id] = true;
        $this->arrivals->insert($job, [-$job->createdAtMs, -$this->nextSequence()]);
        $this->counters->add('enqueued');

        if ($job->availableAtMs() <= $nowMs) {
            $this->insertReady($job);
        } else {
            $this->insertDelayed($job);
        }

        return true;
    }
```

[`src/Queue/JobQueue.php` L66-L91](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L66-L91)

Приоритет хранится как первый элемент пары `[priority, -sequence]`. `sequence`
растёт при каждой вставке, поэтому при равном приоритете первым выходит тот, кто
пришёл раньше. Это FIFO внутри уровня.

```php
    public function lease(string $workerId, int $nowMs, int $visibilityTimeoutMs = 30_000): ?Lease
    {
        if (trim($workerId) === '') {
            throw new InvalidArgumentException('Worker id cannot be empty');
        }

        $this->assertTimestamp($nowMs);

        if ($visibilityTimeoutMs < 1) {
            throw new InvalidArgumentException('Visibility timeout must be at least 1 ms');
        }

        $this->releaseExpiredLeases($nowMs);
        $this->promoteDue($nowMs);

        if ($this->ready->isEmpty()) {
            return null;
        }

        $job = $this->ready->extract();

        if (!$job instanceof Job) {
            throw new LogicException('The ready heap returned a non-job value');
        }

        if (!$job->isPending() || $job->isLeased()) {
            throw new LogicException(sprintf('Job %s is not leaseable', $job->id));
        }

        $job->lease($workerId, $nowMs, $visibilityTimeoutMs);
        $lease = new Lease($job, $workerId, $nowMs + $visibilityTimeoutMs);
        $this->leases[$job->id] = $lease;
        $this->counters->add('leased');

        return $lease;
    }
```

[`src/Queue/JobQueue.php` L93-L128](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L93-L128)

### 2.4. Завершение и провал

`complete()` и `fail()` сверяют lease с авторитетной копией в карте. Если lease
просрочен или это другая копия, результат отклоняется как stale. Это защищает от
worker-а, который вернулся после истечения visibility timeout и пытается
завершить уже переданную другому работу.

```php
    public function complete(Lease $lease, int $nowMs): bool
    {
        $this->assertTimestamp($nowMs);
        $current = $this->leases[$lease->job->id] ?? null;

        if ($current !== $lease || $lease->expiredAt($nowMs)) {
            $this->counters->add('staleCompletions');

            return false;
        }

        unset($this->leases[$lease->job->id], $this->active[$lease->job->id]);
        $lease->job->complete($nowMs);
        $this->processed->mark($lease->job->id, $nowMs);
        $this->counters->add('completed');

        return true;
    }

    public function fail(Lease $lease, int $nowMs, string $error = 'job failed'): FailureOutcome
    {
        $this->assertTimestamp($nowMs);
        $current = $this->leases[$lease->job->id] ?? null;

        if ($current !== $lease || $lease->expiredAt($nowMs)) {
            $this->counters->add('staleFailures');

            return FailureOutcome::stale();
        }

        unset($this->leases[$lease->job->id]);
        $this->counters->add('failed');
        $job = $lease->job;

        if ($job->attempts() >= $job->maxAttempts) {
            $job->deadLetter($nowMs, $error);
            unset($this->active[$job->id]);
            $this->deadLetters[] = $job;
            $this->counters->add('deadLettered');

            return FailureOutcome::deadLettered();
        }

        $delay = $this->backoff->delayMs($job->attempts());
        $job->scheduleRetry($nowMs, $delay, $error);

        if ($delay === 0) {
            $this->insertReady($job);
        } else {
            $this->insertDelayed($job);
        }

        $this->counters->add('retried');

        return FailureOutcome::retried($delay);
    }
```

[`src/Queue/JobQueue.php` L130-L185](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L130-L185)

Провал на последней попытке уходит в dead-letter list, а не в бесконечный retry.
В этой лаборатории dead letter — это список в памяти; replay из него не
реализован, и книга этого не обещает.

### 2.5. Истечение lease

Просроченный lease — это redelivery, а не сообщённый провал. Задача сразу
возвращается в ready, но попытка уже потрачена. Если попыток больше нет, она
становится dead letter.

```php
    public function releaseExpiredLeases(int $nowMs): int
    {
        $this->assertTimestamp($nowMs);
        $expired = [];

        foreach ($this->leases as $lease) {
            if ($lease->expiresAtMs <= $nowMs) {
                $expired[] = $lease;
            }
        }

        foreach ($expired as $lease) {
            unset($this->leases[$lease->jobId()]);
            $this->counters->add('expiredLeases');
            $job = $lease->job;

            if ($job->attempts() >= $job->maxAttempts) {
                $job->deadLetter($nowMs, 'lease expired');
                unset($this->active[$job->id]);
                $this->deadLetters[] = $job;
                $this->counters->add('deadLettered');

                continue;
            }

            $job->scheduleRetry($nowMs, 0, 'lease expired');
            $this->insertReady($job);
            $this->counters->add('retried');
        }

        return count($expired);
    }
```

[`src/Queue/JobQueue.php` L194-L225](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L194-L225)

### 2.6. Продвижение delayed

Delayed heap упорядочен по `availableAtMs`, а не по приоритету. `promoteDue()`
забирает верхушку, пока она созрела, и переносит задачи в ready. После переноса
ready heap сам выберет приоритет.

```php
    public function promoteDue(int $nowMs): int
    {
        $this->assertTimestamp($nowMs);
        $promoted = 0;

        while (!$this->delayed->isEmpty()) {
            $job = $this->delayed->top();

            if (!$job instanceof Job) {
                throw new LogicException('The delayed heap returned a non-job value');
            }

            if ($job->availableAtMs() > $nowMs) {
                break;
            }

            $this->delayed->extract();
            $this->insertReady($job);
            ++$promoted;
        }

        if ($promoted > 0) {
            $this->counters->add('promoted', $promoted);
        }

        return $promoted;
    }
```

[`src/Queue/JobQueue.php` L227-L253](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/JobQueue.php#L227-L253)

### 2.7. Backoff и processed ids

Задержка растёт от базы и ограничивается сверху. Jitter по умолчанию нулевой,
поэтому эксперимент воспроизводим; в production jitter нужен, чтобы retry не
собирались в одну волну.

```php
    public function delayMs(int $attempt): int
    {
        if ($attempt < 1) {
            throw new InvalidArgumentException('Backoff attempt must be at least 1');
        }

        $raw = $this->baseMs * ($this->multiplier ** ($attempt - 1));
        $delay = $raw >= $this->maxMs ? $this->maxMs : (int) round($raw);

        if ($this->jitterMs > 0) {
            $delay = min($this->maxMs, $delay + $this->random->intBetween(0, $this->jitterMs));
        }

        return $delay;
    }
```

[`src/Queue/ExponentialBackoff.php` L48-L62](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Queue/ExponentialBackoff.php#L48-L62)

`ProcessedJobStore` хранит завершённые id в TTL. Повторный enqueue того же id
отклоняется, пока запись жива. Это защита от replay, а не от повторного side
effect: worker, завершившийся после истечения lease, мог успеть выполнить
работу, и очередь об этом не узнает.

---

## 3. Эксперимент

### 3.1. Retry budget

`queue:retry` создаёт 1000 задач с бюджетом три попытки. Каждая пятая падает
всегда, остальные — детерминированное число раз, затем завершаются.

```bash
make experiment ARGS="queue:retry"
```

| метрика                         |   значение |
| ------------------------------- | ---------: |
| задач                           |       1000 |
| завершено                       |        800 |
| dead letter                     |        200 |
| выдач lease                     |      2 199 |
| провалов                        |      1 399 |
| retry                           |      1 199 |
| суммарная задержка              | 166 500 мс |
| replay завершённого id отклонён |         да |

800 завершённых и 200 dead letter дают 1000 задач, но 2 199 lease. Разница — это
повторные выдачи, а не новые задачи. `failed = retried + deadLettered`: 1 399 =
1 199 + 200. Такая арифметика сохранения — главный инвариант очереди.

Задержка удваивается от 100 мс и ограничивается 10 000 мс. 166 500 мс — это
сумма всех запланированных задержек, а не время работы эксперимента. Симуляция
продвигает виртуальные часы.

### 3.2. Падающие worker-ы

`queue:leases` запускает 1000 задач, четырёх worker-ов и visibility timeout 500
мс. Каждая третья выдача «падает»: worker не вызывает ни `complete()`, ни
`fail()`.

```bash
make experiment ARGS="queue:leases"
```

| метрика                |  значение |
| ---------------------- | --------: |
| завершено              |       963 |
| dead letter            |        37 |
| выдач lease            |     1 444 |
| истёкших lease         |       481 |
| redelivery             |       444 |
| пиковая конкурентность |         4 |
| симулированное время   | 90 625 мс |

481 истечение и 444 redelivery показывают, что очередь заметила исчезновение
worker-ов. 37 задач дошли до dead letter, потому что потратили все попытки.
Пиковая конкурентность осталась на четырёх: lease ограничивает число
одновременных исполнителей, но не гарантирует, что каждый из них жив.

### 3.3. Приоритет и хвост

`queue:priority` ставит 1000 задач по пять уровней, по 200 на уровень, и выдаёт
их одним worker-ом по 10 мс на задачу.

```bash
make experiment ARGS="queue:priority"
```

| метрика                         | значение |
| ------------------------------- | -------: |
| завершено                       |     1000 |
| инверсий приоритета             |        0 |
| среднее ожидание высшего уровня |   995 мс |
| среднее ожидание низшего уровня | 8 995 мс |

Heap выдал все задачи строго по убыванию приоритета. Низший уровень ждал в
среднем 9 секунд — ровно позади всех более важных задач. Это корректно, но не
справедливо. При непрерывном потоке высокого приоритета низкий может голодать
бесконечно; статический батч этого не показывает.

### 3.4. Backlog

`queue:backlog` производит 100 задач за тик и завершает 80 за тик. Тик — 1000
мс, всего 100 тиков.

```bash
make experiment ARGS="queue:backlog"
```

| метрика                     |  значение |
| --------------------------- | --------: |
| произведено                 |    10 000 |
| завершено                   |     8 000 |
| финальная глубина           |     2 000 |
| максимальная глубина        |     2 000 |
| возраст самой старой задачи | 19 000 мс |
| рост backlog за тик         |        20 |

Глубина 2000 — это 20 задач на тик × 100 тиков. Возраст 19 000 мс показывает,
что очередь не просто «держит» работу, а откладывает её почти на двадцать
секунд. Если смотреть только на глубину, можно не заметить, что хвост уже
устарел.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="job-queue --repetitions=3"
```

| операция               | итерации | медиана, мс |   ops/sec |
| ---------------------- | -------: | ----------: | --------: |
| enqueue ready          |   10 000 |      11.381 |   878 667 |
| enqueue delayed        |   10 000 |       7.605 | 1 314 953 |
| enqueue lease complete |   20 000 |      37.224 |   537 288 |
| enqueue lease fail     |   10 000 |      16.815 |   594 692 |
| promote due            |   10 000 |      11.676 |   856 458 |
| oldest pending         |  100 000 |      13.373 | 7 477 521 |

Delayed enqueue дешевле ready, потому что не конкурирует с продвижением и не
попадает в ready heap. Round trip «enqueue → lease → complete» стоит 37.2 мс на
20 000 операций: это три операции, две вставки/извлечения и запись processed id.
`oldestPendingAtMs()` дешёвый в этом прогоне, потому что верхушка arrival heap
обычно уже указывает на живую задачу; ленивая очистка не превращается в скан.

---

## 5. Что ломается

**Lease — не блокировка.** Он не мешает второму worker-у получить задачу после
истечения срока. Это visibility timeout, а не mutual exclusion навсегда. Долгая
задача обязана продлевать lease; в лаборатории продления нет.

**Истечение стоит попытку.** Worker, который упал, не сообщил о провале, но
попытка уже потрачена. Это осознанно: иначе падение было бы бесплатным retry и
бюджет ничего бы не ограничивал.

**Stale completion.** Worker, вернувшийся после истечения, может попытаться
завершить задачу. Очередь отклоняет результат, но side effect уже мог произойти.
Именно поэтому получатель должен быть идемпотентным.

**Delayed и ready вместе.** Если держать отложенную задачу в ready heap, она
блокирует готовые задачи с более низким приоритетом. Разделение heap-ов — не
оптимизация, а условие корректного порядка.

**Голодание.** Приоритет без aging или fair queueing может навсегда оставить
низкий уровень без выполнения. Эксперимент с статическим батчем показывает
порядок, но не проверяет бесконечный поток.

**Backlog.** Очередь не бесконечна по полезности. Producer быстрее consumer-а
увеличивает глубину и возраст; в какой-то момент работа становится бесполезной.
Нужны лимиты, back-pressure и метрика возраста, а не только depth.

**Processed TTL.** После истечения TTL повторный enqueue того же id снова
принимается. Это окно, а не вечная гарантия. Размер TTL — компромисс между
памятью и защитой от replay.

**Exactly-once.** Очередь её не даёт. At-least-once плюс идемпотентный
получатель — единственная честная формулировка. Если в книге где-то написано
«ровно один раз», это ошибка.

**Один процесс.** Все heap-ы и карта lease живут в памяти PHP. Нет брокера, нет
репликации, нет восстановления после падения процесса. Лаборатория моделирует
state machine, а не production-кластер.

---

## 6. Как это проверено

- `tests/Queue/JobTest.php` проверяет переходы pending → leased → completed /
  dead-lettered, счётчик попыток и запрет неверных переходов.
- `tests/Queue/JobQueueTest.php` покрывает приоритет, FIFO, delayed jobs,
  истечение lease, stale completion/failure, duplicate enqueue и dead letters.
- `tests/Queue/ExponentialBackoffTest.php` проверяет рост, cap и jitter.
- `tests/Queue/ProcessedJobStoreTest.php` проверяет TTL, очистку и повторный
  enqueue.
- Эксперименты `queue:retry`, `queue:leases`, `queue:priority` и `queue:backlog`
  дают независимые счётчики для каждой гарантии.

К моменту завершения главы набор содержал 220 тестов; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` включает PHPUnit, PHPStan level 8 и
PHP-CS-Fixer.

---

## 7. Что запомнить

1. Очередь даёт at-least-once. Exactly-once — это свойство всей системы, а не
   одной структуры.
2. Lease — временное право, а не вечная блокировка. Истечение возвращает задачу
   и тратит попытку.
3. Stale completion нужно отклонять, но side effect всё равно может произойти.
   Получатель должен быть идемпотентным.
4. Delayed задачи живут отдельно от ready, иначе они блокируют готовую работу.
5. Приоритетный heap корректен, но не справедлив. Без aging хвост может
   голодать.
6. Backlog измеряется глубиной и возрастом. Одно без другого врёт.
7. Retry budget и dead-letter list ограничивают бесконечные попытки.

---

## 8. Упражнения

1. Запустите `queue:retry` с `--max-attempts=5`. Как изменится число lease и
   dead letters? Сойдётся ли `failed = retried + dead`?
2. Увеличьте visibility timeout до 2000 мс в `queue:leases`. Уменьшится ли число
   истечений? Что произойдёт с временем drain?
3. Добавьте aging: повышайте приоритет задачи на единицу каждые 5000 мс
   ожидания. Как изменится среднее ожидание низшего уровня?
4. Запустите `queue:backlog` с consumer rate 100. Почему очередь перестанет
   расти?
5. Реализуйте dead-letter replay с новым id. Какие гарантии изменились бы?
6. Предложите, как продлевать lease для долгой задачи, не теряя защиту от stale
   completion.

---

Дальше — Distributed Cache. Очередь умеет повторять работу; следующая глава
показывает, как читать данные из нескольких узлов, что происходит при падении
одного из них и почему consistent hashing не делает кэш бесплатным.
