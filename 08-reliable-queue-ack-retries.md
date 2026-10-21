# Глава 8. Надёжная очередь задач: аренды, ACK, повторы

> Проект: [`php-job-queue`](https://github.com/Researcher86/php-job-queue),
> релиз `v1.1.0`, коммит `b969ae4`. Нужно знать: пул воркеров (→ глава 6),
> корректное завершение (→ глава 7). Пригодится дальше: идемпотентность и
> очередь мёртвых задач (→ глава 9), надёжность хранилища (→ глава 18).

В главе 6 мы научились держать N процессов живыми и занятыми. Но там был важный
пробел: **жизненный цикл воркера — это не жизненный цикл работы**. Воркер можно
перезапустить, заменить, убить — а задача, которая была у него в руках, всё это
время существует сама по себе. `php-job-queue` — про эту вторую половину задачи:
что происходит с работой, когда процессы вокруг неё умирают.

Наивная очередь выглядит так: положили задачу, воркер её забрал, воркер сказал
«готово». Достаточно одного вопроса, чтобы всё рассыпалось: **как очередь
узнаёт, что задача выполнена?** Воркер мог умереть до начала, во время работы и
после работы, но до того, как сообщить результат. Из очереди эти случаи выглядят
одинаково — «ответа нет». И ещё один вопрос, не менее неприятный: **что если
задача сейчас в руках у двух воркеров одновременно?** Это не гипотеза, а штатное
следствие таймаутов, и разбирать его придётся отдельно.

## 1. Проблема

Пусть очередь отдаёт задачу воркеру и ждёт ответа. Что может пойти не так:

- Воркер **умер, не начав работу**. Задача уже у него, из очереди ушла → она
  пропала: не в очереди, не выполнена, не провалена.
- Воркер **работает дольше, чем очередь готова ждать**. Если очередь просто
  вернёт задачу в очередь, её возьмёт второй воркер — и работа пойдёт дважды.
- Воркер **завис навсегда** (не упал, но и не отвечает). Если ждать его
  бесконечно, слот занят вечно; если не ждать — как отличить «завис» от
  «медленный»?
- Воркер **ответил, но ответ потерялся** по дороге (он умер между делом и
  отправкой результата). Очередь не может отличить «работа сделана, но не
  подтверждена» от «работа не сделана».

Из последних двух пунктов вырастает фундаментальный факт: очередь **не может**
гарантировать однократное выполнение. Информации для этого не существует.
Значит, надо честно выбрать гарантию **at-least-once** («не меньше одного раза»)
и построить всё так, чтобы лишний раз задача не ломала систему. А это, в свою
очередь, требует ответить на три вопроса:

1. **Когда задача считается выполненной?** Только по явному подтверждению (ACK)
   от того, кто её выполнял.
2. **Что делать с задачей, чей исполнитель замолчал?** Вернуть в оборот по
   дедлайну — но так, чтобы её не выполнили одновременно двое «навсегда».
3. **Как отличить ответ живого исполнителя от запоздалого ответа мёртвого
   арендатора?** У аренды должно быть имя.

Пункт 3 — сердце этой главы, и он же был настоящим багом проекта, а не
теоретической опасностью.

## 2. Устройство

### Жизненный цикл задачи — это таблица переходов, а не набор флагов

У задачи есть состояние, и оно меняется только через одну таблицу. Не «флаг
`isRunning` рядом со строкой статуса», а единственная точка, где вообще
присваивается состояние:

```text
FROM         ready   delay   dispatch   complete   fail    retry   requeue
────────────────────────────────────────────────────────────────────────────
CREATED      READY   DELAYED   -          -         -       -        -
DELAYED      READY     -       -          -         -       -        -
READY          -       -     PROCESSING   -         -       -        -
PROCESSING     -       -       -        COMPLETED  FAILED  READY     -
COMPLETED      -       -       -          -         -       -        -
FAILED         -       -       -          -         -       -       READY
```

Что здесь важно:

- переход, которого нет в таблице, **бросает исключение** (`LogicException`), а
  не молча ничего не делает. Это разница между «задача имеет состояние» и
  «задача находится в состоянии»: нет пути, по которому `COMPLETED` тихо станет
  `PROCESSING` снова;
- есть **два разных перехода в `READY`**, и путать их нельзя: `retry` идёт из
  `PROCESSING` (задача ещё не закончилась, она пошла на второй круг), а
  `requeue` — из `FAILED` (человек вручную вернул задачу из очереди мёртвых).
  Первый — это провал попытки. Второй — это провал задачи, который кто-то решил
  пересмотреть.

### Попытка считается по выдаче, а не по запуску

`markProcessing()` — это то, что увеличивает счётчик попыток. Значит, попытка
расходуется в момент, когда задача **ушла к воркеру**, а не когда он начал её
выполнять. Задача, отданная воркеру, который умер, не начав, уже потратила одну
попытку.

Это звучит как минус, но это и есть честный учёт для at-least-once. Реальные
очереди считают так же (квитанции, `receipt handle`). И именно это делает
счётчик попыток **токеном ограждения** (fencing token): он увеличивается ровно
один раз на каждую выдачу, а значит по нему можно отличить одну выдачу от
другой. Новый счётчик для этого не понадобился — он уже был в модели, просто не
использовался как токен.

### Три вида ответа, а не два

Воркер отчитывается не «успех/провал», а **`WorkerOutcome`**, в котором
результат может быть `null`:

| ответ                                                     | что значит                            | что делает диспетчер                  |
| --------------------------------------------------------- | ------------------------------------- | ------------------------------------- |
| `success`                                                 | ACK                                   | `COMPLETED`                           |
| `failure`, попытки остались, `shouldRetry` разрешил       | NACK                                  | `READY` после задержки повтора        |
| `failure`, попытки кончились (или `shouldRetry` запретил) | NACK                                  | `FAILED` + запись в очередь мёртвых   |
| **`null`**                                                | воркер умер, держа задачу             | `READY` немедленно, воркер заменяется |
| **не текущая аренда**                                     | запоздалый ответ за отозванную аренду | `stale_ack`, игнорируется             |

Последние две строки — причина, по которой результат не может быть просто
`JobResult`. «Задача провалилась» и «воркер исчез» требуют **разной** обработки:
NACK тратит попытку и может закончиться мёртвой очередью, а крах возвращает
задачу сразу и без задержки — ведь никто не сообщал о провале. `JobResult` не
умеет сказать второе; для этого результат обязан быть nullable.

### Повтор — это отложенная задача

Сразу важное упрощение: бэкофф (backoff) — это не отдельный механизм. Задержка
перед повтором — это то же, что задержка `delay: 3600` у обычной задачи. И то и
другое кладётся в один и тот же мини-куча планировщика:

```text
RetryPolicy::nextDelay($job)  →  delay
   →  markRetry(now + delay)  →  READY с availableAt в будущем
   →  DelayedJobScheduler     →  всплывает, когда дедлайн прошёл
```

Есть две политики:

- **`FixedDelayRetry`** — одинаково каждый раз (`1s, 1s, 1s`). Правильна, когда
  провалы независимы. Неправильна, когда причина одна и та же: упавший сервис
  сам не поднимется, а сто задач, повторяющихся каждую секунду, — это сто
  запросов в секунду по уже сломанному сервису.
- **`ExponentialBackoffRetry`** — удвоение (`1s, 2s, 4s, 8s`). Повторы перестают
  быть частью проблемы.

Без политики вообще повтор идёт немедленно — самый быстрый способ превратить
один упавший сервис в busy-loop.

### Аренда: у каждой выдачи есть имя

Вот центральная идея. Задача может законно оказаться в руках двух воркеров
одновременно:

```text
READY ──► воркер A ──► PROCESSING ──► A работает долго...
   ▲
   │ дедлайн ARN посмотрел expire
   │
   └── READY ──► воркер B ──► PROCESSING   (та же задача, другая выдача)
```

Дедлайн ACK истёк, пока A ещё работал, задача вернулась в `READY`, её взял B.
Теперь **оба** воркера рано или поздно ответят про один и тот же id задачи. Кто
из них имеет право подтвердить её?

Очевидный ответ — «тот, кто ответит, пока задача в `PROCESSING`» — **неверен**.
К моменту, когда придёт запоздалый ответ A, задача действительно снова
`PROCESSING` — но её туда положил B. Один и тот же объект, одно и то же
состояние, две выдачи. По состоянию их не различить.

Поэтому у выдачи есть идентичность — **`Delivery`**: задача, номер выдачи
(generation), воркер, время выдачи и дедлайн ACK. Монитор держит **не «задачи в
полёте»**, а «выдачу, которая вправе ответить за каждую задачу». Право
подтвердить принадлежит конкретной выдаче, а не тому, у кого оказался id:

```text
                    ЗАДАЧА #8f2a
                        │
            ┌───────────┴───────────┐
            ▼                       ▼
       Выдача 1                Выдача 2
        воркер A                воркер B
        просрочена              текущая     ◄── держит аренду
            │                       │
        запоздалый ACK             ACK
            │                       │
            ▼                       ▼
     STALE, игнор            COMPLETED
     аренда не тронута
```

### Таймаут видимости: сколько задача может оставаться без ACK

Дедлайн аренды — это **visibility timeout**. Он отвечает не на вопрос «сколько
может работать обработчик», а на вопрос «сколько задача может оставаться
неподтверждённой». Разница принципиальна:

- если обработчик работает дольше таймаута, задача уходит второму воркеру, а
  первый всё ещё работает над ней. Это **таймаут слишком короткий**, а не
  «обработчик медленный».

Это не таймаут выполнения — это гарантия «неподтверждённая задача всегда
возвращается». Без неё задача, чей воркер исчез, просто пропадает. С ней —
возвращается и идёт на второй круг. Обратная сторона — та самая одновременная
двойная выдача, и именно поэтому аренде нужно имя.

### Ограждение запоздалого ответа

Ограждение делается сравнением выдачи, а не состояния задачи. И у него есть
тонкость, которая выглядит мелочью, но таковой не является: **на запоздалый
ответ нельзя освобождать аренду**. Аренда под этим id принадлежит живой выдаче,
и «для аккуратности» освободить не ту — значит превратить допустимый дубль в
потерянную задачу (в полёте, но без дедлайна, который мог бы её вернуть).

### Инвариант: задача не бывает «нигде»

Между «в очереди» и «в руках воркера» не должно быть ни одного мгновения, в
которое задача не учтена. Поэтому перед отправкой в сокет диспетчер **сначала**
помечает задачу `PROCESSING`, **потом** берёт аренду в мониторе, **потом** пишет
её в хранилище — и только после этого пишет в сокет. Эта запись в лог и есть то,
что делает счётчик попыток **долговечным**: без неё последним словом лога о
задаче в полёте была запись `READY` с нулём попыток, и задача, которая стабильно
убивает воркер, после рестарта получала полный бюджет заново.

## 3. Код

### Таблица переходов

```php
/**
 * The whole lifecycle, stated once: event => (state it is legal from
 * => state it leads to). Anything absent throws.
 *
 * Keyed by ->name because enum cases cannot be array keys. Written as a
 * table rather than seven hand-rolled guards so that adding a state
 * means editing one place and immediately seeing every event it has to
 * answer for.
 *
 *   FROM        ready     delay     dispatch    complete   fail     retry   requeue
 *   ───────────────────────────────────────────────────────────────────────────────
 *   CREATED     READY     DELAYED   -           -          -        -       -
 *   DELAYED     READY     -         -           -          -        -       -
 *   READY       -         -         PROCESSING  -          -        -       -
 *   PROCESSING  -         -         -           COMPLETED  FAILED   READY   -
 *   COMPLETED   -         -         -           -          -        -       -
 *   FAILED      -         -         -           -          -        -       READY
 *
 * @var array<string, array<string, JobState>>
 */
private const array TRANSITIONS = [
    'ready' => [
        'CREATED' => JobState::READY,
        'DELAYED' => JobState::READY,
    ],
    'delay' => [
        'CREATED' => JobState::DELAYED,
    ],
    'dispatch' => [
        'READY' => JobState::PROCESSING,
    ],
    'complete' => [
        'PROCESSING' => JobState::COMPLETED,
    ],
    'fail' => [
        'PROCESSING' => JobState::FAILED,
    ],
    'retry' => [
        'PROCESSING' => JobState::READY,
    ],
    'requeue' => [
        'FAILED' => JobState::READY,
    ],
];
```

[`src/Job/Job.php#L72-L115`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Job/Job.php#L72-L115)

**Перевод комментариев:**
- `TRANSITIONS` (PHPDoc): весь жизненный цикл, высказанный один раз: событие =>
  (состояние, из которого оно легально => состояние, в которое оно ведёт). Всё
  отсутствующее бросает исключение. Ключи — по `->name`, потому что case enum не
  может быть ключом массива. Записано таблицей, а не семью рукописными
  проверками, чтобы добавление состояния означало правку в одном месте и сразу
  показывало все события, на которые оно обязано отвечать.
  `@var array<string, array<string, JobState>>`.

Таблица — это документация, которую нельзя рассинхронизировать с кодом: схема в
комментарии и массив рядом, и добавить состояние означает отредактировать одно
место и сразу увидеть все события, которые оно обязано обработать. Ключи — по
`->name`, потому что case enum не может быть ключом массива.

### Попытка расходуется на выдаче

```php
/**
 * Handed to a worker. Consumes an attempt - see the class docblock on
 * why an attempt is a delivery - and clears availableAt, because a job
 * in a worker's hands is not waiting for anything (that one IS always
 * cleared, unconditionally - it is not an optional recording parameter,
 * there is nothing to preserve once a job leaves READY).
 *
 * $now, if given, stamps this attempt's startedAt. Omitted, startedAt is
 * left exactly as it was - never cleared - the same "give it or leave
 * it alone" rule every optional recording parameter on this class
 * follows ($now on markCompleted()/markFailed(), $error on
 * markFailed()/markRetry()). Optional so a caller with no clock in
 * reach (most of the test suite) is not forced to supply one.
 */
public function markProcessing(?float $now = null): void
{
    $this->apply('dispatch');
    $this->attempts++;
    $this->availableAt = null;

    if ($now !== null) {
        $this->startedAt = $now;
    }
}
```

[`src/Job/Job.php#L300-L323`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Job/Job.php#L300-L323)

**Перевод комментариев:**
- `markProcessing()` (PHPDoc): передаётся воркеру. Расходует попытку — см.
  докблок класса о том, почему попытка — это выдача, — и очищает `availableAt`,
  потому что задача в руках воркера ничего не ждёт (это поле очищается всегда,
  безусловно: это не необязательный параметр записи, и сохранять нечего, когда
  задача покидает `READY`). `$now`, если задан, проставляет `startedAt` этой
  попытки. Если опущен, `startedAt` остаётся ровно таким, каким был, — никогда
  не очищается: то же правило «дай или не трогай», которому следует каждый
  необязательный параметр записи в этом классе (`$now` в `markCompleted()`
  /`markFailed()`, `$error` в `markFailed()` /`markRetry()`). Необязательный,
  чтобы вызывающий без доступа к часам (большая часть тестов) не был обязан его
  передавать.

`$this->attempts++` стоит здесь, рядом с `apply('dispatch')`, а не в конце
работы. Это и есть «попытка = выдача» в одной строке, и одновременно источник
generation у аренды.

### Возврат на второй круг

```php
public function markRetry(float $availableAt, ?string $error = null): void
{
    $this->apply('retry');
    $this->availableAt = $availableAt;

    if ($error !== null && $error !== '') {
        $this->lastError = $error;
    }
}
```

[`src/Job/Job.php#L385-L393`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Job/Job.php#L385-L393)

`availableAt` в будущем — вот что заставляет жить мини-кучу планировщика: повтор
с бэкоффом и отложенная задача неразличимы. `lastError` записывается и на
повторе тоже — провал попытки это провал, хоть и не последний.

### Сравнение выдач — примитив ограждения

```php
/**
 * Whether both refer to the same handing-out of the same job.
 *
 * The comparison the whole class exists for: same job AND same
 * generation. Same job with a different generation is a delivery that
 * has been superseded.
 */
public function is(self $other): bool
{
    return $this->generation === $other->generation && $this->jobId() === $other->jobId();
}
```

[`src/Delivery/Delivery.php#L122-L132`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Delivery/Delivery.php#L122-L132)

**Перевод комментариев:**
- `is()` (PHPDoc): указывают ли оба на одну и ту же выдачу одной и той же
  задачи. Сравнение, ради которого существует весь класс: та же задача И тот же
  generation. Та же задача с другим generation — выдача, которую уже заменили.

Вся защита — в одном сравнении: **та же задача и та же выдача**. Если задача та
же, а generation другой — выдача устарела, её ответ ничего не решает. `Delivery`
объявлен `readonly`: задача внутри мутабельная и общая, а всё _про выдачу_ —
снимок, сделанный при создании. Именно поэтому generation не может «съехать» и
случайно совпасть с более поздней выдачей.

### Монитор: кто вправе ответить и что значит «освободить»

```php
/**
 * Whether this delivery is still the one entitled to answer.
 *
 * False for a delivery whose deadline expired and whose job was handed
 * out again, and false for one whose job has already been resolved -
 * both are answers nobody is waiting for.
 */
public function isCurrent(Delivery $delivery): bool
{
    return ($this->current[$delivery->jobId()] ?? null)?->is($delivery) ?? false;
}

/**
 * The ACK (or the NACK) arrived: the lease is discharged.
 *
 * A delivery that is no longer current releases nothing. That guard is
 * load-bearing - releasing on a stale answer would drop the LIVE
 * delivery's lease and leave a job in flight with no deadline that
 * could ever bring it back.
 */
public function release(Delivery $delivery): void
{
    if ($this->isCurrent($delivery)) {
        unset($this->current[$delivery->jobId()]);
    }
}
```

[`src/Timeout/VisibilityMonitor.php#L92-L117`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Timeout/VisibilityMonitor.php#L92-L117)

**Перевод комментариев:**
- `isCurrent()` (PHPDoc): является ли эта выдача всё ещё той, что вправе
  отвечать. `false` для выдачи, чей дедлайн истёк и чья задача была выдана
  снова, и `false` для выдачи, чья задача уже разрешена, — оба ответа никто не
  ждёт.
- `release()` (PHPDoc): пришёл ACK (или NACK) — аренда погашена. Выдача, которая
  больше не текущая, ничего не освобождает. Этот сторож несущий: освобождение по
  устаревшему ответу сбросило бы аренду ЖИВОЙ выдачи и оставило бы задачу в
  полёте без дедлайна, который мог бы её вернуть.

Хранится «выдача, вправе ответить за задачу», а не «задачи в полёте». Когда
дедлайн истекает, запись **удаляется**, задача выдаётся заново с бо́льшим
generation — и старый воркер, когда наконец ответит, будет сослаться на
несуществующую аренду и получит отказ от `isCurrent()`. Сторож в `release()`
называется несущим не для красного словца: освободить аренду по чужому
(устаревшему) ответу — и есть тот самый преврат «допустимый дубль → потерянная
задача».

### Ограждение в диспетчере — и почему проверки состояния мало

```php
private function applyResult(WorkerOutcome $outcome): void
{
    $delivery = $outcome->getDelivery();

    // The fence. An answer is only authoritative for the delivery that
    // is still holding the lease.
    //
    // A stale answer happens without anything going wrong: the
    // visibility deadline passed while the handler was still running,
    // the job was handed to a second worker, and now the first one has
    // finished and is reporting on a lease that has been revoked. It is
    // also what an answer looks like for a job that has already been
    // resolved.
    //
    // Checking the JOB'S STATE instead of the delivery does not work,
    // and this is not a hypothetical - it was the bug this replaced.
    // By the time the late answer arrives the job genuinely IS
    // PROCESSING, because the second worker put it there. The obsolete
    // answer was therefore accepted: it completed a job the live worker
    // was still running, released the LIVE delivery's lease (leaving a
    // job in flight with no deadline that could bring it back), and the
    // live worker's real answer - a failure - was then discarded as
    // stale. See Delivery.
    //
    // Nothing is released here, on purpose. The lease belongs to the
    // live delivery.
    if (!$this->monitor->isCurrent($delivery)) {
        $this->metrics?->increment(MetricsCollector::STALE_ACKS);

        return;
    }

    $job = $delivery->getJob();
    $result = $outcome->getResult();
    $this->monitor->release($delivery);

    if ($result === null) {
        $this->redeliverOrDeadLetter($job, new DeliveryLostException('Worker died while holding the job.'));
        $this->workerPool->replaceDeadWorkers();
        return;
    }
```

[`src/Dispatcher/JobDispatcher.php#L443-L483`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Dispatcher/JobDispatcher.php#L443-L483)

**Перевод комментариев:**
- `// The fence. An answer is only authoritative for the delivery that` —
  ограждение. Ответ авторитетен только для выдачи, которая всё ещё держит
  аренду. Устаревший ответ случается без всякой поломки: дедлайн видимости
  прошёл, пока обработчик ещё работал, задачу отдали второму воркеру, и теперь
  первый закончил и отчитывается по аренде, которую отозвали. Так же выглядит
  ответ по задаче, которая уже разрешена. Проверка СОСТОЯНИЯ ЗАДАЧИ вместо
  выдачи не работает, и это не гипотеза — это был баг, который здесь заменили. К
  моменту прихода запоздалого ответа задача действительно `PROCESSING`, потому
  что её туда положил второй воркер. Устаревший ответ поэтому принимался: он
  завершал задачу, над которой живой воркер ещё работал, освобождал аренду ЖИВОЙ
  выдачи (оставляя задачу в полёте без дедлайна, который мог бы её вернуть), а
  настоящий ответ живого воркера — провал — затем отбрасывался как устаревший.
  См. `Delivery`. Здесь намеренно ничего не освобождается. Аренда принадлежит
  живой выдаче.

Комментарий здесь — это запись о реальном баге, а не рассуждение. Проверка
«задача ещё `PROCESSING`?» принимала ответ A за ответ B: к моменту прихода A
задача действительно `PROCESSING`, но её туда положил B. Запоздалый ответ
завершал задачу, над которой ещё работал живой воркер, снимал аренду **живой**
выдачи — и настоящий ответ B (провал) отбрасывался как устаревший. Роли
перевернулись: судьбу задачи решал ответ, которого никто не ждал.

Отдельно: при `!isCurrent()` **ничего не освобождается**, и `return` стоит
раньше `release()`. Строка `null`-результата — это ветка краха воркера:
`redeliverOrDeadLetter()` вернёт задачу с учётом бюджета попыток, а
`replaceDeadWorkers()` починит пул.

### NACK, порог повторов и бэкофф

```php
private function handleFailure(Job $job, ?Throwable $exception): void
{
    // $shouldRetry is consulted on top of the attempts-remaining check, so
    // a "this can never work" verdict goes straight to FAILED however
    // much budget is left. No closure means every failure is eligible. A
    // null exception never actually reaches here - JobResult::failure()
    // always carries one - and is treated as eligible too.
    $eligible = $job->getAttempts() < $job->getMaxAttempts()
        && ($this->shouldRetry === null || $exception === null || ($this->shouldRetry)($job, $exception));

    if ($eligible) {
        $delay = $this->retryPolicy?->nextDelay($job) ?? 0;
        $job->markRetry($this->clock->now() + $delay, $exception?->getMessage());

        // No persist() here: push() writes the job it takes in. See
        // persist() for who owns which write.
        $this->queue->push($job);
        $this->metrics?->increment(MetricsCollector::JOBS_RETRIED);
        return;
    }

    $this->deadLetter($job, $exception);
}
```

[`src/Dispatcher/JobDispatcher.php#L504-L526`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Dispatcher/JobDispatcher.php#L504-L526)

**Перевод комментариев:**
- `// $shouldRetry is consulted on top of the attempts-remaining check, so` —
  `$shouldRetry` спрашивается поверх проверки оставшихся попыток, поэтому
  вердикт «это никогда не сработает» идёт прямо в `FAILED`, сколько бы бюджета
  ни осталось. Отсутствие замыкания означает, что подходит любой провал. `null`
  -исключение сюда на самом деле никогда не доходит — `JobResult::failure()`
  всегда несёт одно, — и тоже считается подходящим.
- `// No persist() here: push() writes the job it takes in. See` — здесь нет
  `persist()`: `push()` записывает задачу, которую принимает. См. `persist()` о
  том, кому принадлежит какая запись.

`RetryPolicy` отвечает **как долго** ждать, но не **стоит ли** ждать вообще — и
`attempts < maxAttempts` само по себе тоже не отвечает. Полезная нагрузка, в
которой не хватает поля, провалится на третьей попытке точно так же, как на
первой; потратить на неё весь бюджет — значит просто отложить попадание в
очередь мёртвых. Поэтому есть `shouldRetry`: он проверяется **поверх** счёта
попыток, и «так никогда не сработает» уходит в `FAILED` немедленно, сколько бы
бюджета ни оставалось.

### Экспоненциальный бэкофф

```php
/**
 * Doubling delays: 1s, 2s, 4s, 8s - README § Retries.
 *
 * The delay grows with the attempt count, so a job that keeps failing asks
 * less and less often. Which is the useful behaviour when the reason it
 * fails is that something else is overloaded: the retries themselves stop
 * being part of the problem.
 *
 * No jitter here, deliberately - it would be the right thing in production
 * (a thousand jobs failing together retry together, and the pattern
 * repeats at every doubling) and it would make the delays unpredictable to
 * read in an example. This is a project for reading.
 */
final readonly class ExponentialBackoffRetry implements RetryPolicy
{
    public function __construct(
        private int $baseDelay = 1,
        private float $factor = 2.0,
    ) {
    }

    public function nextDelay(Job $job): int
    {
        $attempt = max($job->getAttempts(), 1);

        return (int) ($this->baseDelay * ($this->factor ** ($attempt - 1)));
    }
}
```

[`src/Retry/ExponentialBackoffRetry.php#L9-L36`](https://github.com/Researcher86/php-job-queue/blob/b969ae4d84571b8fba59a53b096276425d3edcec/src/Retry/ExponentialBackoffRetry.php#L9-L36)

**Перевод комментариев:**
- `ExponentialBackoffRetry` (PHPDoc): удваивающиеся задержки: 1 с, 2 с, 4 с, 8 с
  — README § Retries. Задержка растёт с числом попыток, поэтому задача, которая
  продолжает падать, спрашивает всё реже. Это полезное поведение, когда причина
  провала — перегруженность чего-то другого: сами повторы перестают быть частью
  проблемы. Джиттера здесь нет намеренно: в продакшене он был бы правильным
  (тысяча задач, падающих вместе, повторяется вместе, и картина повторяется на
  каждом удвоении), но сделал бы задержки непредсказуемыми для чтения в примере.
  Это проект для чтения.

Задержка считается от **номера попытки**, а не от количества прошедших повторов,
— и это работает ровно потому, что попытка увеличивается в момент выдачи.
Джиттера нет намеренно: в продакшене он был бы правильным (тысяча задач, упавших
вместе, повторятся вместе, и картина повторится на каждом удвоении), но сделал
бы задержки в примере нечитаемыми.

## 4. Эксперимент

### Жизненный цикл один раз

`make run` проводит три задачи через полный цикл:

```text
$ php bin/run.php
3 job(s) queued, 3 worker(s)

  [worker 64] send_email
  [worker 65] generate_report
  [worker 66] resize_image

dispatched 3, queue now holds 0
counters {"created":3,"completed":3}
```

Три задачи, три воркера, три `COMPLETED`. Пока всё скучно — и это правильно.

### Провал: повторы, бэкофф, мёртвая очередь

Задача, которая всегда падает, идёт по всей лестнице:

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

Обратите внимание: **`retried: 2`, а не 3**. Три попытки, две из них — повтор,
третья — конец. Задержки здесь настоящие (`ExponentialBackoffRetry`: 1 с, 2 с),
поэтому скрипт идёт несколько секунд: эта пауза и есть механизм, пропустить её —
пропустить суть.

### Живая находка: работа выполняется дважды, и это правильно

Теперь самое интересное. Обработчик работает дольше таймаута видимости.
Сохраните этот сценарий как `/tmp/vis.php` внутри контейнера (он лежит вне
репозитория, чтобы не менять проект):

```php
<?php

declare(strict_types=1);

use PhpJobQueue\Dispatcher\JobDispatcher;
use PhpJobQueue\Job\Job;
use PhpJobQueue\Metrics\MetricsCollector;
use PhpJobQueue\Queue\InMemoryQueue;
use PhpJobQueue\Support\SystemClock;
use PhpJobQueue\Worker\WorkerPool;

require "/app/vendor/autoload.php";

$clock = new SystemClock();
$metrics = new MetricsCollector();
$queue = new InMemoryQueue($clock);
$runs = "/tmp/vis-runs.txt";
@unlink($runs);

// Two workers; the handler sleeps LONGER than the visibility timeout.
$pool = new WorkerPool(2, static function (Job $job) use ($runs): void {
    file_put_contents($runs, "  worker " . getmypid() . " running attempt " . $job->getAttempts() . "\n", FILE_APPEND);
    usleep(1_500_000); // 1.5s > 1s visibility timeout
});
$pool->start();

$job = Job::create(type: "slow", maxAttempts: 5, clock: $clock);
$queue->push($job);

$dispatcher = new JobDispatcher($queue, $pool, clock: $clock, visibilityTimeout: 1, metrics: $metrics);

printf("delivery 1 -> dispatched %d, state=%s, attempts=%d\n", $dispatcher->dispatchPending(), $job->getState()->name, $job->getAttempts());

usleep(1_100_000); // let the 1s deadline pass while worker A is still busy
printf("expired leases reclaimed: %d\n", $dispatcher->requeueExpired());
printf("after reclaim: state=%s\n", $job->getState()->name);

printf("delivery 2 -> dispatched %d, attempts=%d\n", $dispatcher->dispatchPending(), $job->getAttempts());

$dispatcher->collect(null); // worker A answers first, for a revoked lease
$dispatcher->collect(null); // worker B answers, the live delivery
printf("final: state=%s attempts=%d\n", $job->getState()->name, $job->getAttempts());

$dispatcher->shutdown(2.0);
echo file_get_contents($runs);
printf("counters %s\n", json_encode($metrics->getCounters()));
```

**Перевод комментариев:**
- `// Two workers; the handler sleeps LONGER than the visibility timeout.` — два
  воркера; обработчик спит ДОЛЬШЕ таймаута видимости.
- `// 1.5s > 1s visibility timeout` — 1.5 с > 1 с таймаута видимости.
- `// let the 1s deadline pass while worker A is still busy` — дать пройти
  дедлайну в 1 с, пока воркер A ещё занят.
- `// worker A answers first, for a revoked lease` — воркер A отвечает первым,
  по отозванной аренде.
- `// worker B answers, the live delivery` — воркер B отвечает, живая выдача.

Запуск и вывод:

```text
$ php /tmp/vis.php
delivery 1 -> dispatched 1, state=PROCESSING, attempts=1
expired leases reclaimed: 1
after reclaim: state=READY
delivery 2 -> dispatched 1, attempts=2
final: state=COMPLETED attempts=2
  worker 82 running attempt 1
  worker 83 running attempt 2
counters {"stale_acks":1,"completed":1}
```

Разберём по строкам:

1. Выдача 1 уходит воркеру 82, задача `PROCESSING`, `attempts=1`.
2. Дедлайн (1 с) проходит, пока воркер ещё работает. `requeueExpired()`
   возвращает 1 аренду: задача снова `READY`.
3. Выдача 2 уходит **другому** воркеру 83, `attempts=2`.
4. Воркер 82 заканчивает первым и отвечает — но это ответ за **отозванную**
   аренду. Монитор его отбрасывает: `stale_acks=1`.
5. Воркер 83 заканчивает и отвечает за живую аренду → `COMPLETED`.
6. Итог: `state=COMPLETED attempts=2`, две строки в файле — разные pid. Задача
   выполнилась дважды, ничего не упало, ничего не провалилось.

Это версия at-least-once, которая удивляет больше всего: **дубль без единой
ошибки**. Никто не падал и не таймаутил по-настоящему — просто таймаут оказался
короче работы. Именно поэтому `stale_acks` — диагностический счётчик: он растёт
ровно тогда, когда таймаут видимости короче того, что он измеряет.

### Крах воркера: задача не теряется

```text
$ php examples/worker-crash.php
pool: {"ready":1,"delayed":0,"processing":0,"workers":1,"busy_workers":0,"idle_workers":1,"dead_lettered":0}

  [worker 73] attempt 1, working...
  [worker 73] about to be killed mid-job
  [worker 74] attempt 2, working...
  [worker 74] finished cleanly

job is COMPLETED after 2 attempt(s)
counters {"created":1,"worker_crashes":1,"completed":1}

the worker that died was replaced; the job it held was not lost.
```

Другой pid, вторая попытка, та же задача. `worker_crashes: 1`, `completed: 1` —
жизненный цикл воркера и жизненный цикл работы разведены: умер процесс, а не
задача.

## 5. Отказы

| Что ломается                                    | Чем обнаруживается              | Что с задачей                                                                                              |
| ----------------------------------------------- | ------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| Обработчик бросает исключение                   | NACK                            | повтор, затем мёртвая очередь                                                                              |
| Воркер убит, пока держал задачу                 | EOF на его сокете               | сразу `READY`, воркер заменён; `FAILED` + мёртвая очередь, когда попытки кончились                         |
| Воркер убит без работы                          | реап в `WorkerPool::maintain()` | задачи нет; восстанавливается ёмкость                                                                      |
| Воркер убит между проверкой и записью           | `WorkerDiedException`           | сразу `READY`; `FAILED` + мёртвая очередь, когда попытки кончились                                         |
| Обработчик дольше visibility timeout            | дедлайн истёк                   | выдана снова; ответ старой выдачи ограждён как `stale_ack`                                                 |
| Воркер не отвечает вообще                       | visibility timeout              | `READY`, когда дедлайн прошёл; `FAILED` + мёртвая очередь, когда попытки кончились                         |
| Рантайм получил `SIGTERM`, даже во время старта | обработчик сигнала              | задачи в полёте доживают в пределах grace-периода                                                          |
| Рантайм убит `SIGKILL`                          | ничем, до рестарта              | `PROCESSING`-записи в логе возвращаются в `READY` (или `FAILED` + мёртвая очередь, если попытки кончились) |
| Процесс очереди умер без хранилища              | ничем                           | задачи потеряны — для этого и нужно хранилище                                                              |
| Задача провалила все попытки                    | попытки кончились               | `FAILED` + запись в мёртвую очередь                                                                        |

Ключевая строка — предпоследняя. Всё остальное в таблице восстанавливается
**автоматически**, а вот «рантайм убит `SIGKILL` без хранилища» — это уже
свойство конфигурации, а не механизма. `visibility timeout` здесь не спасает:
рантайм, который должен был бы его обслужить, сам умер.

## 6. Как это проверено

Механика этой главы покрыта тестами, которые читаются как оглавление:

- `tests/Chaos/ChaosTest.php` — три теста ровно про ограждение:
  `testAHandlerSlowerThanTheVisibilityTimeoutRunsTwice` (дубль на двух разных
  воркерах), `testALateAnswerFromAnExpiredDeliveryIsNotAppliedToTheNewOne`
  (запоздалый ответ отброшен, `JOBS_COMPLETED = 0`), и
  `testOnlyTheLiveDeliveryDecidesTheOutcome` (судьбу решает живая выдача:
  `stale_acks = 1`, `retried = 1`, аренда снята только живой выдачей).
- `tests/Timeout/VisibilityMonitorTest.php` — сам монитор, включая
  `testAnExpiredDeliveryIsNoLongerCurrentOnceTheJobIsReissued` и
  `testAStaleDeliveryReleasesNothing` (тот самый несущий сторож `release()`).
- `tests/Dispatcher/JobDispatcherTest.php` — таблица ACK/NACK целиком:
  `testFailedJobIsRetriedWhenAttemptsRemain`,
  `testFailedJobIsFailedAfterExhaustingAttempts`,
  `testAJobWhoseLeaseKeepsExpiringIsDeadLetteredWhenItsAttemptsRunOut`.
- `tests/Retry/RetryPolicyTest.php` — `testFixedDelayRetryReturnsConstantDelay`
  и `testExponentialBackoffGrowsGeometrically` (1, 2, 4, 8).

Запущенная тройка про ограждение проходит целиком:

```text
$ vendor/bin/phpunit --filter 'testAHandlerSlowerThanTheVisibilityTimeoutRunsTwice|testALateAnswerFromAnExpiredDeliveryIsNotAppliedToTheNewOne|testOnlyTheLiveDeliveryDecidesTheOutcome'
PHPUnit 11.5.56 by Sebastian Bergmann and contributors.

Runtime:       PHP 8.5.11
Configuration: /app/phpunit.xml

...                                                                 3 / 3 (100%)

Time: 00:01.363, Memory: 12.00 MB

OK (3 tests, 27 assertions)
```

### Что тесты не поймали с первого раза

Стоит проговорить честно, потому что это важнее самих тестов. Первый вариант
ограждения проверял **состояние задачи**, а не выдачу. Тест на медленного
обработчика (`testAHandlerSlowerThanTheVisibilityTimeoutRunsTwice`) при этом
проходил: он смотрел на «задача выполнилась дважды», а не на «кто её завершил».
Баг вылез только когда написали тест, где вторая выдача **проваливается**, а
первая — успешна: тогда стало видно, что запоздалый успех записывается как
`COMPLETED`, хотя живой воркер ещё работает. Ровно поэтому в проекте есть три
теста про одно и то же ограждение с разных сторон.

## 7. Цифры

Замеры живых прогонов этой главы (Docker, PHP 8.5.11):

| Что                          | Значение                                         |
| ---------------------------- | ------------------------------------------------ |
| Полный цикл, 3 задачи        | `counters {"created":3,"completed":3}`           |
| Провал с бэкоффом, 3 попытки | `{"created":1,"retried":2,"failed":1,"dlq":1}`   |
| Дубль при коротком таймауте  | 2 разных pid, `{"stale_acks":1,"completed":1}`   |
| Крах воркера                 | `{"created":1,"worker_crashes":1,"completed":1}` |
| Задержки бэкоффа             | 1 с, 2 с, 4 с, 8 с                               |

Пропускная способность и латентность (`make bench`, 8 воркеров, no-op
обработчики, без хранилища):

|     задач |       за | пропускная | память | ожидание в очереди | выполнение |
| --------: | -------: | ---------: | -----: | -----------------: | ---------: |
|     1 000 |  0.077 с |   13 000/с |   4 МБ |              27 мс |    0.16 мс |
|    10 000 |  0.369 с |   27 100/с |  14 МБ |             186 мс |    0.13 мс |
|   100 000 |  3.631 с |   27 500/с |  98 МБ |           1 877 мс |    0.14 мс |
| 1 000 000 | 30.088 с |   33 200/с | 906 МБ |          18 765 мс |          — |

Тут важнее всего **предпоследний столбец против последнего**. На 100 000 задач
среднее время в очереди — почти 1.9 секунды, а среднее время обработчика — 0.14
мс. Задачи не медленные. Они **ждали**. Это и есть разница между «медленный
обработчик» и «медленная очередь», и она не выводится из одного числа
«end-to-end»: задача может проходить через ожидание и выполнение несколько раз
(по разу на попытку) внутри одного сквозного промежутка.

Ещё одно число, объясняющее, почему это единый цикл, а не набор случайностей:
через хранилище каждая задача пишет **три записи** (`READY` → `PROCESSING` →
итог), и средняя запись — постоянные **5 µs** независимо от глубины. Три
четверти из них — это сам `append` в файловую систему, а не сериализация.

## 8. Упражнения

1. **Объясните.** Почему попытка считается в момент **выдачи**, а не в момент
   завершения? Приведите случай, где другая трактовка дала бы бесконечный цикл.
2. **Найдите ошибку.** В `applyResult()` при `!isCurrent($delivery)` есть строка
   `return` **до** `release()`. Что сломается, если поставить `release()`
   первым, «чтобы не держать лишнее»?
3. **Спроектируйте.** `Delivery` неизменяем (`readonly`), а `Job` — нет. Почему
   generation обязан быть снимком, а не ссылкой на `$job->getAttempts()` в
   момент сравнения?
4. **Посчитайте.** Таймаут видимости 30 с, обработчик работает 90 с. Сколько
   выдач успеет появиться и сколько из них дадут `stale_acks`? Что изменится,
   если обработчик станет идемпотентным?
5. **Сравните.** `markRetry()` и `markRequeued()` оба ведут в `READY`. Почему
   это два разных перехода из разных состояний, а не один? Что каждый из них
   говорит о задаче?
6. **Разберите аудит.** В таблице отказов строка «рантайм убит `SIGKILL`»
   восстанавливается только при включённом хранилище. Опишите, что именно должно
   быть записано в лог, чтобы `PROCESSING`-задача вернулась, и почему запись
   делается **до** отправки в сокет, а не после.

## Что дальше

- **Глава 9** — at-least-once как контракт: очередь мёртвых задач и
  идемпотентность. Эта глава оградила ответ; следующая отвечает на вопрос, что
  делать, когда задача исчерпала попытки, и как обработчик защищается от
  неизбежного дубля.
- **Глава 18** — WAL, `fsync` и восстановление: тот самый лог, на который
  опирается восстановление `PROCESSING`-задач после `SIGKILL`.
- **Глава 7** — graceful shutdown, при котором задача, убитая grace-периодом,
  возвращается не через таймаут (рантайм уже уходит), а через хранилище.
