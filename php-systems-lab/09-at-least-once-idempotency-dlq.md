# Глава 9. At-least-once, идемпотентность и очередь мёртвых задач

> Проект: [`php-job-queue`](https://github.com/Researcher86/php-job-queue),
> релиз `v1.1.0`, коммит `b969ae4`. Нужно знать: аренды и ограждение (→ глава
> 8). Пригодится дальше: соединения и буферы (→ глава 10) и надёжность хранилища
> (→ глава 18).

В главе 8 мы построили аренды и ограждение: подтверждённая работа завершается
ровно по живому ответу, а неподтверждённая всегда возвращается. Там же
выяснилось главное, и это стоит повторить, потому что вся эта глава — его
следствие: очередь **не может** обещать ровно одно выполнение. Информации для
этого не существует. Когда воркер молчит, очередь не может отличить «он умер, не
начав» от «он сделал работу и умер, не сообщив». Ей остаётся допускать худшее —
и доставлять снова.

At-least-once — это не недостаток, который надо спрятать. Это контракт, из
которого напрямую вытекают две практические задачи:

1. **Что делать с задачей, которая не может завершиться?** Её нельзя повторять
   вечно и нельзя выбросить.
2. **Что делать с задачей, которая может выполниться дважды?** Это уже не
   проблема очереди, а проблема обработчика.

Первая задача решается **очередью мёртвых задач**, вторая —
**идемпотентностью**. Обычно их смешивают; здесь они разведены намеренно.

## 1. Проблема

### Задача, которая падает всегда

Представим задачу с полезной нагрузкой, в которой не хватает поля. Она
провалится с той же ошибкой на первой попытке, на второй и на сотой. Два
очевидных исхода — оба плохие:

- **Повторять вечно.** Воркеры заняты работой, которая не завершится никогда.
  Это не абстрактная трата: при достаточном потоке таких задач очередь просто
  перестаёт обрабатывать полезное.
- **Выбросить.** Вместе с задачей теряется факт, что она существовала и что
  что-то сломалось. «Тишина в мониторинге» будет означать и «всё хорошо», и «мы
  только что молча потеряли платёж».

Нужен третий исход: **перестать пытаться, но ничего не потерять и дождаться
человека**. Это и есть очередь мёртвых задач (DLQ) — не файл логов, а именно
очередь, потому что задачу оттуда можно вернуть в оборот.

### Задача, которая может выполниться дважды

Пусть обработчик списывает деньги. At-least-once доставит одну операцию дважды в
штатном режиме, без единой ошибки:

- воркер упал после списания, но до подтверждения (в главе 8 это
  `worker_crashes`);
- обработчик пережил свой visibility timeout, задача ушла второму воркеру (в
  главе 8 это `stale_acks`).

Обработчик не может опираться на число выдач — их не контролирует никто. Значит,
у него должен быть ключ, называющий **операцию**, а не доставку: «списать за
заказ 123», а не «выполнить выдачу 4». С этим ключом он сам себя дедуплицирует:
увидел ключ — ничего не делает.

## 2. Устройство

### Очередь мёртвых задач: третье окончание

DLQ хранит записи по ключу — идентификатору задачи, поэтому одна задача не может
попасть туда дважды. Запись держит всё, что нужно, чтобы понять произошедшее, не
заглядывая в логи:

```text
DeadLetterRecord
├── job         сама задача (её можно вернуть)
├── exception   ошибка, которая её доконала
├── attempts    сколько раз пытались
└── failedAt    когда сдались
```

`retry($jobId)` возвращает задачу в `READY` и удаляет запись. Именно поэтому это
очередь, а не лог-файл: у неё есть операция «вернуть в работу», которой у
журнала нет.

### Копия, а не ссылка

Тонкость, которая выглядит мелочью, но определяет, чему можно верить. Число
попыток в записи **копируется** в момент попадания в DLQ, а не читается из
задачи по запросу. Причина: задача мутабельна, и `retry()` возвращает её в
оборот. Запись обязана продолжать говорить «это падало три раза» и после того,
как началась четвёртая попытка. Если бы она ссылалась на живой счётчик задачи,
историю провала нельзя было бы прочитать — она бы «догоняла» настоящее.

### Идемпотентность: ключ называет операцию

Ключ идемпотентности — это часть полезной нагрузки, установленная
производителем, а не вычисленная обработчиком. Это критично: ключ обязан быть
**одной и той же строкой при каждой передоставке**. Ключ, выведенный из id
задачи или из номера попытки, был бы каждый раз другим и не дедуплицировал бы
ничего. Плохой ключ — «выполнить эту задачу». Хороший — «списать за заказ 123».

Классический обработчик выглядит так:

```text
key = job.getIdempotencyKey()          // "payment:order-7"
if guard.isProcessed(key):             // уже было?
    return success                     // да — ничего не делаем
charger(orderId, amount)               // нет — делаем побочный эффект
guard.markProcessed(key)               // и записываем, что сделали
```

Порядок здесь — весь смысл. Сначала проверка, потом эффект, потом запись. И
именно из этого порядка вырастает дефект, о котором дальше.

### Почему ключ должен жить дольше процесса

Рестарт — это ровно тот момент, когда задача, которая уже выполнялась,
возвращается: `PROCESSING`-задачи из лога идут обратно в `READY` вместе с их
побочным эффектом. Поэтому guard должен переживать процесс. Он пишет ключи в
хранилище — и, что важнее, **читает их оттуда на промахе** (см. ниже).

### Почему промах проверяется по хранилищу

Каждый форкнутый воркер держит свою копию guard. Ключи загружаются при создании
объекта — то есть один раз, до форка. После форка у каждого воркера свой снимок:
ключ, записанный воркером A, попадает в общий лог, но **не** в массив воркера B.
Если доверять только массиву, дедупликация работает между рестартами и **не**
работает между воркерами в одном прогоне: дубль, случайно попавший на другого
воркера, списывает деньги ещё раз. Поэтому на промах guard перечитывает
хранилище целиком. Эту ошибку проект реально допустил — тест
`testAKeyRecordedByOneWorkerIsSeenByAnotherInTheSameRun` закрывает её.

### Свой лог у guard

Guard нужен **отдельный** лог от очереди. Очередь при восстановлении
воспроизводит каждую запись в логе как задачу, а запись guard задачей не
является. `InMemoryQueue::restoreFromStorage()` отказывается загружать лог с
посторонними записями — вместо того чтобы угадывать. Это осознанный выбор:
перепутать журнал ключей с журналом задач и «восстановить» из него мусорные
задачи было бы намного хуже, чем не стартовать.

### Окно, которое нельзя закрыть из очереди

А теперь честная часть, ради которой всё это и написано. Между проверкой ключа и
его записью есть окно, и оно принципиально не закрывается средствами очереди:

```text
isProcessed()  →  false
charge         →  деньги ушли
💀              →  markProcessed() не выполнится
restart
isProcessed()  →  всё ещё false
charge         →  СПИСАНИЕ ПОВТОРЯЕТСЯ
```

Это не «проверка ключа плохая». Это два шага, которые ничто не связывает
атомарно. То же окно открывается без всякого краха: два воркера держат дубли
одной задачи **одновременно**, оба проверяют, оба промахиваются, оба списывают.
Ключ между проверкой и записью ничем не заперт.

Закрыть окно можно, но только там, где происходит сам побочный эффект: одна
транзакция в базе, которая и списывает, и сохраняет ключ (commit вместе); либо
собственный ключ идемпотентности платёжной системы, чтобы дедуплицировал второй
вызов. И то и другое — свойства **того, что списывают**, а не то, что может
выдать очередь. Что может очередь: доставлять не меньше одного раза, честно об
этом говорить и делать шов видимым. Именно для этого и существует
`IdempotencyGuard` — как **дедупликация**, а не как «ровно один раз».

## 3. Код

### Устройство очереди мёртвых задач

```php
/**
 * Where jobs go when retrying them has stopped being useful - README
 * § Dead letter queue.
 *
 * Without it, a job that can never succeed has two possible endings, and
 * both are bad: retry forever, burning workers on work that will not
 * complete, or drop it, and lose the fact that it existed. The DLQ is the
 * third: stop trying, keep everything, and wait for a human.
 *
 * So a record holds the job, the exception that finished it, the attempt
 * count and the time - enough to answer "what broke, and how often" without
 * going to the logs. And retry() puts a job back once whatever was broken
 * is fixed, which is why this is a queue and not a log file.
 *
 * Keyed by job id, so a job cannot be dead-lettered twice.
 */
final class DeadLetterQueue
{
    /** @var array<string, DeadLetterRecord> */
    private array $records = [];

    public function __construct(
        private readonly Clock $clock = new SystemClock(),
    ) {
    }

    public function add(Job $job, Throwable $exception): void
    {
        $this->records[$job->getId()->toString()] = new DeadLetterRecord(
            job: $job,
            exception: $exception,
            attempts: $job->getAttempts(),
            failedAt: $this->clock->now(),
        );
    }
```

[`src/DLQ/DeadLetterQueue.php#L13-L47`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/DLQ/DeadLetterQueue.php#L13-L47)

**Перевод комментариев:**
- `DeadLetterQueue` (PHPDoc): куда попадают задачи, когда повторять их стало
  бесполезно — README § Dead letter queue. Без неё у задачи, которая никогда не
  сможет завершиться, два возможных конца, и оба плохи: повторять вечно, сжигая
  воркеров на работе, которая не завершится, или выбросить её и потерять сам
  факт, что она существовала. DLQ — третий: перестать пытаться, сохранить всё и
  ждать человека. Поэтому запись держит задачу, исключение, которое её доконало,
  число попыток и время — достаточно, чтобы ответить «что сломалось и как
  часто», не заглядывая в логи. А `retry()` возвращает задачу в оборот, когда
  сломанное починили, — поэтому это очередь, а не файл логов. Ключ — id задачи,
  поэтому одну задачу нельзя отправить в мёртвые дважды.
- `$records` (`@var`): id задачи → запись очереди мёртвых задач.

`attempts: $job->getAttempts()` — это и есть копирование: значение берётся один
раз и кладётся в неизменяемую запись. «Keyed by job id» — ключ это идентификатор
задачи, поэтому повторное попадание перезаписывает запись, а не плодит дубли.

### Запись держит копию

```php
/**
 * One dead-lettered job, with everything needed to understand why.
 *
 * The attempt count is copied rather than read from the job on demand,
 * because the job is mutable and retry() puts it back into circulation: the
 * record has to keep saying "this failed three times" after the fourth
 * attempt starts.
 */
final readonly class DeadLetterRecord
{
    public function __construct(
        private Job $job,
        private Throwable $exception,
        private int $attempts,
        private float $failedAt,
    ) {
    }
```

[`src/DLQ/DeadLetterRecord.php#L10-L26`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/DLQ/DeadLetterRecord.php#L10-L26)

**Перевод комментариев:**
- `DeadLetterRecord` (PHPDoc): одна задача из очереди мёртвых со всем, что
  нужно, чтобы понять почему. Число попыток копируется, а не читается из задачи
  по запросу, потому что задача мутабельна и `retry()` возвращает её в оборот:
  запись обязана продолжать говорить «это падало три раза» и после начала
  четвёртой попытки.

`readonly` здесь не украшение, а часть доказательства: после создания поля
нельзя переписать, поэтому число попыток в записи действительно «заморожено».
Именно это мы увидим живьём в эксперименте — запись продолжает говорить
`attempts=3`, когда задача уже на попытке `4`.

### Возврат в оборот

```php
public function retry(string $jobId): ?Job
{
    $record = $this->records[$jobId] ?? null;
    if ($record === null) {
        return null;
    }

    $job = $record->getJob();
    if ($job->getState() !== JobState::FAILED) {
        return null;
    }

    unset($this->records[$jobId]);
    $job->markRequeued($this->clock->now());

    return $job;
}
```

[`src/DLQ/DeadLetterQueue.php#L75-L91`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/DLQ/DeadLetterQueue.php#L75-L91)

Две проверки до действия: записи нет — `null`; задача уже не `FAILED` (кто-то её
вернул раньше) — тоже `null`. `retry()` не «воскрешает» задачу, он использует
переход `requeue`, который по таблице из главы 8 разрешён **только из
`FAILED`**. Это тот самый второй путь в `READY`, о котором шла речь: не повтор
попытки, а человеческое решение пересмотреть провал.

### Окно, названное вслух

```php
* ## What this is not
*
* It is not exactly-once execution, and the gap is worth being precise
* about. Checking the key and performing the side effect are two steps,
* and so are the side effect and recording it:
*
*   isProcessed()  ->  false
*   charge         ->  the money has moved
*   💀              ->  markProcessed() never runs
*   restart        ->  isProcessed() is still false, so it charges AGAIN
*
* IdempotencyTest::testACrashBetweenTheChargeAndItsRecordChargesTwice
* does exactly that, with a real SIGKILL, and asserts the double charge.
*
* Two workers holding duplicates of one job AT THE SAME TIME fall through
* the same gap without any crash: both check, both miss, both charge.
* Nothing here locks the key between the check and the record.
*
* Closing the window needs the side effect and its record to COMMIT
* TOGETHER - one database transaction that both charges and stores the
* key, or the remote system's own idempotency key so the second call is
* the one that deduplicates. Both are properties of the thing being
* charged, not something a queue can hand you. What a queue can do is
* deliver at least once, say so, and make the seam visible - which is what
* this class is here for.
```

[`src/Idempotency/IdempotencyGuard.php#L42-L66`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Idempotency/IdempotencyGuard.php#L42-L66)

Это редкий, но правильный случай, когда ограничение задокументировано рядом с
механизмом, а не спрятано в примечании. Ошибочная формулировка была слабее —
будто «ровно один раз» можно «построить на другом конце из at-least-once и
проверки ключа, и это всё». Как видно из схемы, это не всё: между шагами есть
разрыв, и лучше показать его текстом и тестом, чем делать вид, что его нет.

### Промах не верится на слово

```php
/**
 * A miss is checked against the storage before it is believed.
 *
 * The keys loaded at construction are a snapshot, and after the fork
 * every worker holds its own copy of it: a key worker A records lands
 * in the shared log, but not in worker B's array. Trusting the array
 * alone made deduplication work across restarts and NOT across workers
 * in the same run - a duplicate delivery that happened to land on a
 * different worker charged again. A hit needs no re-read: keys are
 * never removed.
 *
 * The re-read is a full load() of the log, on every miss. That is the
 * price of a log-only store with no index, paid on the path that is
 * about to do the expensive, non-repeatable thing anyway.
 */
public function isProcessed(string $key): bool
{
    if (isset($this->processed[$key])) {
        return true;
    }

    $this->reload();

    return isset($this->processed[$key]);
}

public function markProcessed(string $key): void
{
    $this->processed[$key] = true;
    $this->storage?->store($key, ['kind' => self::RECORD_KIND]);
}
```

[`src/Idempotency/IdempotencyGuard.php#L81-L111`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Idempotency/IdempotencyGuard.php#L81-L111)

**Перевод комментариев:**
- `isProcessed()` (PHPDoc): промах проверяется по хранилищу, прежде чем ему
  поверить. Ключи, загруженные при создании, — это снимок, и после форка каждый
  воркер держит свою его копию: ключ, записанный воркером A, попадает в общий
  лог, но не в массив воркера B. Доверие только массиву делало дедупликацию
  работающей между рестартами и НЕ работающей между воркерами в одном прогоне:
  дубль доставки, попавший на другого воркера, списывал деньги ещё раз.
  Попаданию перечитывание не нужно: ключи никогда не удаляются. Перечитывание —
  это полный `load()` лога при каждом промахе. Такова цена хранилища только с
  логом, без индекса, и платится она на пути, который всё равно собирается
  сделать дорогое и необратимое действие.

Логика асимметрична: **попадание** доверяет массиву (ключи не удаляются, значит
массив не устаревает в сторону «забыл»), а **промах** перечитывает лог.
Комментарий объясняет, почему это не перестраховка: без перечитывания дедуп
работал между рестартами и не работал между воркерами в одном прогоне, и дубль
на «не том» воркере списывал деньги повторно. `markProcessed()` пишет в два
места: в свой массив и в общее хранилище — второе и делает ключ видимым для
остальных воркеров.

### Обработчик, который себя дедуплицирует

```php
/**
 * A handler that deduplicates its own side effect - the worked example for
 * README § At-least-once, and what it costs you.
 *
 * A payment may legally be charged once, and an at-least-once queue will
 * sometimes deliver the same operation twice: a worker that crashed after
 * charging, or a handler that outlived its visibility deadline. So the
 * handler cannot rely on the delivery count. It checks a key that names the
 * OPERATION - "payment:order-7" - and does nothing if that key has already
 * been seen.
 *
 * Note what makes the key usable: it comes from the job's payload-level
 * idempotencyKey, set by the producer, so it is the same string on every
 * redelivery. A key derived from the job id or the attempt number would be
 * different each time and would deduplicate nothing.
 *
 * This is deduplication, not exactly-once execution - see IdempotencyGuard
 * for the crash window that remains and what actually closes it.
 */
final readonly class ChargePaymentJob
{
    /**
     * @param Closure(string, float): void $charger executes the real charge
     */
    public function __construct(
        private IdempotencyGuard $guard,
        private Closure $charger,
    ) {
    }
```

[`src/Idempotency/ChargePaymentJob.php#L12-L40`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Idempotency/ChargePaymentJob.php#L12-L40)

**Перевод комментариев:**
- `ChargePaymentJob` (PHPDoc): обработчик, который сам дедуплицирует свой
  побочный эффект, — разобранный пример для README § At-least-once и того, чего
  он стоит. Платёж законно списать один раз, а at-least-once очередь иногда
  доставит одну операцию дважды: воркер, упавший после списания, или обработчик,
  переживший свой дедлайн видимости. Поэтому обработчик не может полагаться на
  число доставок. Он проверяет ключ, называющий ОПЕРАЦИЮ, — `payment:order-7` —
  и ничего не делает, если этот ключ уже видели. Обратите внимание, что делает
  ключ пригодным: он приходит из `idempotencyKey` на уровне полезной нагрузки
  задачи, установленного производителем, поэтому это одна и та же строка при
  каждой передоставке. Ключ, выведенный из id задачи или номера попытки, был бы
  каждый раз другим и не дедуплицировал бы ничего. Это дедупликация, а не
  ровно-однократное выполнение: см. `IdempotencyGuard` про оставшееся окно краха
  и то, что его действительно закрывает.
- `$charger` (`@param`): выполняет реальное списание.

Конструктор принимает **два** вклада: guard и сам заряд (`Closure`). Такое
разделение — это и есть разделение ответственности между дедупликацией и
побочным эффектом: guard умеет только «видел/не видел ключ», а что именно делать
один раз, решает переданный `charger`.

```php
public function __invoke(Job $job): JobResult
{
    $orderId = (string) ($job->getPayload()['order_id'] ?? '');
    $amount = (float) ($job->getPayload()['amount'] ?? 0.0);
    $key = $job->getIdempotencyKey();

    if ($key === null) {
        throw new RuntimeException(sprintf(
            'ChargePaymentJob requires an idempotency key, got none for job %s',
            $job->getId(),
        ));
    }

    if ($this->guard->isProcessed($key)) {
        return JobResult::success();
    }

    ($this->charger)($orderId, $amount);
    $this->guard->markProcessed($key);

    return JobResult::success();
}
```

[`src/Idempotency/ChargePaymentJob.php#L42-L63`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Idempotency/ChargePaymentJob.php#L42-L63)

Отсутствие ключа — это **исключение**, а не тихий проход: обработчик без ключа
не может быть корректным при at-least-once, и лучше упасть громко, чем списать
деньги дважды. А `return JobResult::success()` на уже виденном ключе — это не
«работа не сделана», а «работа сделана раньше»: задача заслуживает `COMPLETED`,
потому что её цель достигнута, просто не в этой выдаче.

## 4. Эксперимент

### Живой путь в мёртвую очередь

Задача, которая всегда падает, доходит до DLQ и возвращается оттуда:

```text
$ php examples/failed-job.php
  [worker] attempt 1 of charge_card
  ...backing off before attempt 2
  [worker] attempt 2 of charge_card
  ...backing off before attempt 3
  [worker] attempt 3 of charge_card

job is FAILED after 3 attempt(s)
dead letter queue holds 1 record(s)
  charge_card failed 3 time(s), last error: the payment gateway is down

after a manual retry: job is READY, DLQ holds 0
counters {"created":1,"retried":2,"failed":1,"dlq":1}
```

Здесь важно не число повторов (оно из главы 8), а две последние строки:
`failed: 1`, `dlq: 1` — провал состоялся один раз и попал в очередь мёртвых
ровно одной записью. И `after a manual retry` — задача вернулась в `READY`, а
DLQ опустела.

### Замороженная история

Скрипт показывает запись DLQ и то, что её счётчик попыток — копия:

```php
<?php

declare(strict_types=1);

use PhpJobQueue\DLQ\DeadLetterQueue;
use PhpJobQueue\Dispatcher\JobDispatcher;
use PhpJobQueue\Job\Job;
use PhpJobQueue\Metrics\MetricsCollector;
use PhpJobQueue\Producer\JobFactory;
use PhpJobQueue\Producer\Producer;
use PhpJobQueue\Queue\InMemoryQueue;
use PhpJobQueue\Retry\FixedDelayRetry;
use PhpJobQueue\Support\SystemClock;
use PhpJobQueue\Worker\WorkerPool;

require "/app/vendor/autoload.php";

$clock = new SystemClock();
$metrics = new MetricsCollector();
$queue = new InMemoryQueue($clock);
$dlq = new DeadLetterQueue($clock);
$producer = new Producer($queue, new JobFactory($clock, $metrics));

$job = $producer->dispatch("send_receipt", ["order" => 7], maxAttempts: 3);

$alwaysFails = static function (Job $received): void {
    throw new RuntimeException("smtp refused: 550");
};

$pool = new WorkerPool(1, $alwaysFails);
(new JobDispatcher($queue, $pool, new FixedDelayRetry(0), $clock, dlq: $dlq, metrics: $metrics))->drain();

$record = $dlq->find($job->getId()->toString());
printf("job:    state=%s attempts=%d\n", $job->getState()->name, $job->getAttempts());
printf("dlq:    size=%d\n", $dlq->size());
printf("record: type=%s attempts=%d error=%s\n",
    $record->getJob()->getType(), $record->getAttempts(), $record->getException()->getMessage());

$frozen = $record->getAttempts();
$retried = $dlq->retry($job->getId()->toString());
printf("after manual retry: state=%s, dlq size=%d\n", $retried->getState()->name, $dlq->size());

$queue->push($retried);
$pool2 = new WorkerPool(1, $alwaysFails);
$pool2->start();
(new JobDispatcher($queue, $pool2, new FixedDelayRetry(0), $clock, dlq: $dlq, metrics: $metrics))->drain();

printf("captured record still says attempts=%d; the job now says attempts=%d\n", $frozen, $job->getAttempts());
printf("counters %s\n", json_encode($metrics->getCounters()));
```

Output:

```text
$ php /tmp/dlq.php
job:    state=FAILED attempts=3
dlq:    size=1
record: type=send_receipt attempts=3 error=smtp refused: 550
after manual retry: state=READY, dlq size=0
captured record still says attempts=3; the job now says attempts=4
counters {"created":1,"retried":2,"failed":2,"dlq":2}
```

Разбор:

1. Задача дошла до `attempts=3` и попала в DLQ. Запись сохранила тип,
   `attempts=3` и текст ошибки — этого достаточно, чтобы понять «что сломалось и
   сколько раз» без логов.
2. `retry()` вернул задачу в `READY` и убрал запись из DLQ (`size=0`).
3. Задачу вернули в очередь и она провалилась ещё раз — уже на попытке `4`.
4. **Ключевая строка:**
   `captured record still says attempts=3; the job now says attempts=4`.
   Захваченная запись не поехала вслед за живой задачей — она осталась
   замороженной копией. Именно это и обещал `readonly` `DeadLetterRecord`, и
   именно поэтому история провала читается.

Обратите внимание и на `dlq:2` в counters: было две разных отправки в мёртвую
очередь (первоначальная и повторная), и каждая инкрементировала счётчик. Это не
ошибка — метрика считает события, а не уникальные задачи.

### Один ключ, две доставки; крах между списанием и записью

Этот скрипт показывает сразу обе стороны идемпотентности: где дедупликация
работает и где неизбежно проигрывает.

```php
<?php

declare(strict_types=1);

use PhpJobQueue\Idempotency\ChargePaymentJob;
use PhpJobQueue\Idempotency\IdempotencyGuard;
use PhpJobQueue\Job\Job;
use PhpJobQueue\Persistence\FileStorage;

require "/app/vendor/autoload.php";

// --- 1. Two deliveries of the SAME operation: charged once -----------
$guardLog = "/tmp/idem-guard.log";
@unlink($guardLog);

$charges = [];
$charge = new ChargePaymentJob(
    new IdempotencyGuard(new FileStorage($guardLog)),
    static function (string $orderId, float $amount) use (&$charges): void {
        $charges[] = $orderId . ":" . $amount;
    },
);

$job = Job::create(type: "charge_payment", payload: ["order_id" => "order-1", "amount" => 19.99], idempotencyKey: "payment:order-1");
$charge($job); // delivery 1  -> charges
$charge($job); // delivery 2  -> skipped
printf("deliveries=2  charges=%d  ->  %s\n", count($charges), implode(", ", $charges));

// --- 2. The crash window: charge, then die before recording it -------
$chargeLog = "/tmp/idem-crash-charge.log";
$crashGuard = "/tmp/idem-crash-guard.log";
@unlink($chargeLog);
@unlink($crashGuard);

$pid = pcntl_fork();
if ($pid === 0) {
    $handler = new ChargePaymentJob(
        new IdempotencyGuard(new FileStorage($crashGuard)),
        static function (string $orderId, float $amount) use ($chargeLog): void {
            file_put_contents($chargeLog, $orderId . "\n", FILE_APPEND);
            posix_kill(posix_getpid(), SIGKILL); // dies before markProcessed()
        },
    );
    $handler(Job::create(type: "charge_payment", payload: ["order_id" => "order-7", "amount" => 49.90], idempotencyKey: "payment:order-7"));
    exit(0);
}
pcntl_waitpid($pid, $status);
printf("child died by signal:  %s\n", pcntl_wifsignaled($status) ? "yes" : "no");

// Restart. The guard reads the same log and finds nothing.
$handler = new ChargePaymentJob(
    new IdempotencyGuard(new FileStorage($crashGuard)),
    static function (string $orderId, float $amount) use ($chargeLog): void {
        file_put_contents($chargeLog, $orderId . "\n", FILE_APPEND);
    },
);
$handler(Job::create(type: "charge_payment", payload: ["order_id" => "order-7", "amount" => 49.90], idempotencyKey: "payment:order-7"));

$lines = file($chargeLog, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES);
printf("charges logged:       %d  ->  %s\n", count($lines), implode(", ", $lines));
```

**Перевод комментариев:**
- `// --- 1. Two deliveries of the SAME operation: charged once -----------` —
  1. Две доставки ОДНОЙ И ТОЙ ЖЕ операции: списано один раз.
- `// delivery 1  -> charges` — доставка 1 → списывает.
- `// delivery 2  -> skipped` — доставка 2 → пропускается.
- `// --- 2. The crash window: charge, then die before recording it -------` —
  2. Окно краха: списать, затем умереть до записи этого.
- `// dies before markProcessed()` — умирает до `markProcessed()`.
- `// Restart. The guard reads the same log and finds nothing.` — рестарт. Guard
  читает тот же лог и ничего не находит.

Output:

```text
$ php /tmp/idem.php
deliveries=2  charges=1  ->  order-1:19.99
child died by signal:  yes
charges logged:       2  ->  order-7, order-7
```

Первая часть: две доставки одной операции → **одно** списание. Дедупликация
работает ровно так, как обещано.

Вторая часть: дочерний процесс действительно убит сигналом (`SIGKILL`, а не
имитация), списание уже случилось, а запись ключа — нет. После «рестарта» guard
читает тот же лог, ключа не находит и **списывает второй раз**. Это не дефект
кода — это граница самой модели, показанная живьём.

## 5. Отказы

| Что ломается                                  | Чем обнаруживается                            | Что с задачей                                                              |
| --------------------------------------------- | --------------------------------------------- | -------------------------------------------------------------------------- |
| Обработчик падает всегда                      | попытки кончились                             | `FAILED` + запись в DLQ                                                    |
| Обработчик не может принять решение о повторе | `shouldRetry`                                 | `FAILED` + DLQ немедленно, сколько бы попыток ни осталось                  |
| Задача «убивает» каждый воркер                | EOF / реап исчерпывает бюджет                 | `FAILED` + DLQ с `DeliveryLostException`                                   |
| Задача уже в DLQ, её снова туда шлют          | ключ — id задачи                              | запись перезаписывается, дублей нет                                        |
| `retry()` неизвестного id                     | нет записи                                    | `null`, ничего не происходит                                               |
| `retry()` задачи не в `FAILED`                | переход `requeue` разрешён только из `FAILED` | `null`, задача не воскрешается                                             |
| Промах guard на другом воркере                | перечитывание хранилища                       | ключ найден, дубль пропущен                                                |
| Крах между списанием и `markProcessed()`      | ничем — окно не закрывается                   | **повторное списание**; закрывается только транзакцией у платёжной системы |
| Два воркера держат дубли одновременно         | ничем — окно не закрывается                   | **повторное списание**, если оба успели до записи                          |
| Guard и очередь делят один лог                | `restoreFromStorage()` отказывает             | очередь не поднимается — лучше, чем мусорные задачи                        |

Две строки — про повторное списание — стоят здесь намеренно рядом с
«работающими» строками. Они показывают границу: DLQ и guard закрывают
**дедупликацию**, но не **атомарность побочного эффекта и его записи**. Всё, что
находится по эту сторону шва, очередь гарантирует; всё, что по ту, — свойство
того, что списывают.

## 6. Как это проверено

Тема покрыта двумя наборами тестов, которые читаются как оглавление главы:

- `tests/DLQ/DeadLetterQueueTest.php` — `testAddStoresRecord`,
  `testRecordPreservesFailureInformation` (та самая копия попыток),
  `testRetryRequeuesJobToReady`, `testRetryUnknownIdReturnsNull`,
  `testDeleteRemovesRecord`.
- `tests/Job/IdempotencyTest.php` — пятнадцать тестов, из них:
  `testChargesOnlyOncePerIdempotencyKey`,
  `testDifferentIdempotencyKeysChargeSeparately`,
  `testMissingIdempotencyKeyIsRejected`,
  `testDuplicateDeliveryChargesOnlyOnceEndToEnd` (сквозной: две доставки — одно
  списание), `testRedeliveryAfterRestartChargesOnce`,
  `testAKeyRecordedByOneWorkerIsSeenByAnotherInTheSameRun` (та самая ошибка с
  форком), `testSharingOneStorageBetweenGuardAndQueueIsRefusedClearlyOnRestore`,
  и — как центр тяжести — `testACrashBetweenTheChargeAndItsRecordChargesTwice`.

Оба набора проходят целиком:

```text
$ vendor/bin/phpunit tests/DLQ tests/Job/IdempotencyTest.php
PHPUnit 11.5.56 by Sebastian Bergmann and contributors.

Runtime:       PHP 8.5.11
Configuration: /app/phpunit.xml

.....................                                             21 / 21 (100%)

Time: 00:00.033, Memory: 10.00 MB

OK (21 tests, 42 assertions)
```

### Тест, который проверяет провал

Стоит отдельно остановиться на
`testACrashBetweenTheChargeAndItsRecordChargesTwice`, потому что это тест,
который **утверждает двойное списание**. Его docblock прямо говорит, что это не
баг, а «предел, сделанный наблюдаемым вместо оговорки». Внутри — настоящий
`pcntl_fork()` и настоящий `posix_kill(..., SIGKILL)` между списанием и записью:

```text
isProcessed()  ->  false
charge         ->  the money has moved
💀              ->  markProcessed() never runs
```

Такой тест делает больше, чем любой комментарий: он не даёт будущему изменению
тихо «решить», что окно закрыто. Аналогично живёт
`testAKeyRecordedByOneWorkerIsSeenByAnotherInTheSameRun` — тест на **реальный
исправленный баг**, а не на гипотезу.

## 7. Цифры

Замеры живых прогонов этой главы (Docker, PHP 8.5.11):

| Что                            | Значение                                       |
| ------------------------------ | ---------------------------------------------- |
| Провал → DLQ → ручной retry    | `{"created":1,"retried":2,"failed":1,"dlq":1}` |
| DLQ-скрипт: запись заморожена  | запись `attempts=3`, задача `attempts=4`       |
| Две доставки одного ключа      | `charges=1`                                    |
| Крах между списанием и записью | `charges=2` (ожидаемо)                         |
| Тесты DLQ                      | 6 тестов, 14 утверждений                       |
| Тесты идемпотентности          | 15 тестов, 28 утверждений                      |
| Вместе                         | 21 тест, 42 утверждения                        |

Почему идемпотентность **нельзя** сделать бесплатной, видно из стоимости
хранилища, на которое опирается guard. Каждый ключ — это запись в логе, и запись
стоит одинаково независимо от глубины:

| режим    | пропускная |    цена | на запись |                  лог |
| -------- | ---------: | ------: | --------: | -------------------: |
| `memory` |   23 600/с | базовый |    0.6 µs |                    — |
| `encode` |   22 800/с |   +8.4% |    1.2 µs |                    — |
| `file`   |   19 100/с |  +28.5% |    5.0 µs | 30 000 записей, 8 МБ |

То есть долговечность стоит примерно четверть пропускной способности, причём
цена **постоянна — 5 µs на запись** при любой глубине. Из этих 5 µs около 0.6 µs
— сериализация, а примерно 3.8 µs (три четверти) — это сама запись в файловую
систему. На миллионе тривиальных задач лог вырастает до **804 МБ**, а `load()`
проигрывает его целиком при старте — что и есть аргумент за периодические
снапшоты вместо бесконечно растущего журнала.

Guard добавляет по одной такой записи **на операцию** (не на доставку), и именно
поэтому эта плата оправдана: запись делается на пути, который всё равно
собирается выполнить дорогое и необратимое действие.

## 8. Упражнения

1. **Объясните.** Почему `attempts` в `DeadLetterRecord` копируется, а не
   читается из задачи при обращении? Что именно сломается в интерфейсе «покажи
   историю провала», если начать читать живое значение?
2. **Найдите ошибку.** `retry()` делает `unset($this->records[$jobId])` **до**
   `$job->markRequeued()`. Что произойдёт, если `markRequeued()` бросит
   исключение (задача уже не `FAILED` из-за гонки)? Запись потеряна навсегда или
   это допустимо?
3. **Спроектируйте.** Почему guard отказывается делить лог с очередью
   (`restoreFromStorage()` бросает)? Что произошло бы при «доверчивом»
   восстановлении, которое просто пропускало бы чужие записи?
4. **Посчитайте.** Пусть задача падает с вероятностью 0.4 и сделано 5 попыток.
   Сколько задач из 1000 в среднем попадут в DLQ? Как изменится число, если
   `shouldRetry` запретит повтор для неисправимых ошибок?
5. **Сравните.** Дедупликация через `isProcessed()` и «ровно один раз» через
   транзакцию у платёжной системы — чем они отличаются по гарантии? Приведите
   сценарий, в котором транзакционная версия не списывает, а guard-версия
   списывает дважды.
6. **Разберите аудит.** В скрипте `dlq.php` счётчик `dlq` равен `2`, хотя
   уникальная задача одна. Предложите, что именно должна считать метрика —
   события или уникальные задачи, — и как сделать так, чтобы это было
   согласовано с `retry()`, который удаляет запись.

## Что дальше

- **Глава 10** — TCP-сервер на одном цикле: соединения и буферы. Очереди
  закончились, дальше первая глава «Событийных серверов» и новый проект
  `php-mini-cache`, который принимает клиентов по TCP на одном потоке.
- **Глава 18** — WAL, `fsync` и восстановление: тот самый лог, на который
  опираются и `retry()` из DLQ, и долговечность ключей идемпотентности.
- **Глава 8** — аренды, ACK и повторы: откуда берутся `worker_crashes` и
  `stale_acks`, которые в этой главе превращаются в дубли, требующие
  дедупликации.
