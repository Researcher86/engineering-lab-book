# Глава 6. Webhook Delivery Platform: подпись, retry и breaker

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: distributed cache (глава 5). Следующая глава: search engine
> (глава 7).

Webhook — это исходящий HTTP-запрос на чужой endpoint. В отличие от обычного
HTTP-клиента, здесь нельзя просто повторить запрос при ошибке: получатель мог
уже выполнить side effect, а ответ мог потеряться. В отличие от очереди, здесь
недостаточно хранить задачу: нужно подписать точные байты, объяснить получателю,
как отличить повтор от нового события, и не убить чужой сервис retry-штормом.

В лаборатории webhook-платформа состоит из диспетчера подписок, подписанта
HMAC-SHA256, worker-а с retry budget и dead-letter list, а также per-endpoint
circuit breaker. Доставка — at-least-once. Получатель обязан быть идемпотентным
по стабильному delivery id. Это не exactly-once и не попытка его изобразить.

---

## 1. Проблема

Пусть событие `link.created` произошло один раз. Подписок пятьдесят, но не все
хотят это событие. Диспетчер создаёт по доставке на каждую заинтересованную
подписку. Одно событие превращается в десятки независимых HTTP-запросов, у
каждого свой endpoint, свой секрет, свой retry budget и свой breaker.

Теперь получатель обработал запрос, создал ссылку и упал до отправки ответа.
Worker видит сетевую ошибку и повторяет запрос. Если получатель не умеет
отличать повтор, side effect выполнится дважды. Если worker не повторяет, есть
шанс потерять событие навсегда. Выбор снова бинарный: at-least-once с
идемпотентным получателем или at-most-once с риском потери. Exactly-once между
двумя независимыми системами без общей транзакции не получается.

Подпись решает другую задачу: получатель должен убедиться, что запрос отправлен
именно им и не изменён по дороге. Подпись над телом недостаточно, если её можно
переиграть: злоумышленник может повторно отправить старый корректный запрос.
Поэтому в подписанные байты входит timestamp, а проверка ограничивает окно
времени. Но timestamp — это граница replay-окна, а не идемпотентность.

Третья проблема — отказ endpoint-а. Если он лежит дольше, чем весь retry budget,
то к моменту восстановления все доставки уже станут dead letters. Circuit
breaker не делает endpoint здоровым; он перестаёт тратить попытки на заведомо
мёртвый сервис и откладывает работу до probe. Это меняет, кто платит за отказ:
очередь помнит доставки, пока breaker ждёт, вместо того чтобы сжечь их retry
budget.

---

## 2. Устройство

### 2.1. Подписка и fan-out

`Subscription` привязана к tenant-у, хранит URL, секрет и список интересующих
типов событий. Пустой список означает «все события». Секрет принадлежит
подписке, а не tenant-у: ротация одного endpoint-а не должна ломать остальные.

`WebhookDispatcher` — это чистое планирование. Он не подписывает, не отправляет
и не повторяет. Для каждого события он перебирает подписки и создаёт доставку
только для совпавших по tenant-у и типу.

```php
    public function dispatch(WebhookEvent $event): array
    {
        $this->counters->add('events');
        $deliveries = [];

        foreach ($this->subscriptions as $subscription) {
            if ($subscription->tenantId !== $event->tenantId || !$subscription->wants($event->type)) {
                continue;
            }

            $this->counters->add('matchedSubscriptions');
            $deliveries[] = new WebhookDelivery(
                id: $event->tenantId . ':' . $event->id . ':' . $subscription->id,
                event: $event,
                subscription: $subscription,
                createdAtMs: $event->createdAtMs,
            );
            $this->counters->add('deliveriesCreated');
        }

        return $deliveries;
    }
```

[`src/Webhook/WebhookDispatcher.php` L41-L62](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/WebhookDispatcher.php#L41-L62)

Fan-out — это не «число событий × число подписок», а «число событий × число
заинтересованных подписок». Фильтр по типу сокращает объём, но широкая подписка
всё равно умножает подписи, попытки и retry на каждое событие.

### 2.2. Подпись точных байтов и timestamp

Подписываются байты `"<timestamp>.<payload>"`, где timestamp — целые
миллисекунды. Заголовок имеет вид `t=<timestamp>,v1=<hex>`. Проверка сначала
разбирает формат, затем проверяет окно времени, и только потом сравнивает
дайджест через `hash_equals`, чтобы не давать timing-сигнал по префиксу.

```php
    public function sign(string $secret, string $payload, int $timestampMs): string
    {
        if ($secret === '') {
            throw new InvalidArgumentException('Signing secret cannot be empty');
        }

        if ($timestampMs < 0) {
            throw new InvalidArgumentException('Signature timestamp cannot be negative');
        }

        $digest = hash_hmac('sha256', $timestampMs . '.' . $payload, $secret);

        return sprintf('t=%d,v1=%s', $timestampMs, $digest);
    }

    /**
     * @param int $toleranceMs Maximum accepted distance between the signature
     *                         timestamp and the verifier's clock.
     */
    public function verify(
        string $secret,
        string $payload,
        string $signature,
        int $nowMs,
        int $toleranceMs = 300_000,
    ): bool {
        if ($secret === '' || $nowMs < 0 || $toleranceMs < 0) {
            return false;
        }

        if (preg_match('/^t=(\d+),v1=([0-9a-f]{64})$/', $signature, $matches) !== 1) {
            return false;
        }

        $timestamp = (int) $matches[1];

        if (abs($nowMs - $timestamp) > $toleranceMs) {
            return false;
        }

        $expected = hash_hmac('sha256', $timestamp . '.' . $payload, $secret);

        return hash_equals($expected, $matches[2]);
    }
```

[`src/Webhook/Signer.php` L18-L61](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/Signer.php#L18-L61)

Окно по умолчанию — 300 000 мс, то есть пять минут. Чем шире окно, тем больше
времени есть у повторной отправки перехваченного запроса; чем уже, тем чаще
страдают часы получателя. Timestamp не спасает от replay внутри окна. Для этого
нужен стабильный idempotency key и хранилище уже обработанных ключей на стороне
получателя.

### 2.3. Delivery id как ключ идемпотентности

`WebhookDelivery` — одна пара «событие → подписка». Её id собирается как
`tenant:event:subscription` и одновременно является idempotency key. Он не
меняется между попытками, поэтому получатель может связать повтор с уже
выполненной работой.

Worker помнит завершённые delivery id в TTL. Повторный `enqueue()` активной или
уже завершённой доставки отклоняется, не создавая вторую работу.

```php
    public function enqueue(WebhookDelivery $delivery, int $nowMs): bool
    {
        if ($nowMs < 0) {
            throw new InvalidArgumentException('Timestamp cannot be negative');
        }

        if (!$delivery->isPending()) {
            throw new LogicException(sprintf('Delivery %s is already finished', $delivery->id));
        }

        if (isset($this->pending[$delivery->id]) || $this->processed->isProcessed($delivery->id, $nowMs)) {
            $this->counters->add('duplicateEnqueues');

            return false;
        }

        $this->pending[$delivery->id] = $delivery;
        $this->counters->add('enqueued');

        return true;
    }
```

[`src/Webhook/WebhookWorker.php` L69-L89](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/WebhookWorker.php#L69-L89)

Это защита от повторного планирования, а не от повторного side effect. Если
получатель уже выполнил работу, а ответ потерялся, worker всё равно повторит
запрос: у него нет способа узнать о side effect. Получатель обязан сам подавить
второй эффект по тому же ключу.

### 2.4. Попытка: breaker, подпись и запрос

Перед каждой попыткой worker спрашивает breaker endpoint-а. Если breaker открыт,
доставка откладывается без расхода попытки. Если разрешено, worker увеличивает
счётчик попыток, подписывает payload и формирует запрос. Все попытки несут один
и тот же idempotency key, но новый attempt number и новый timestamp.

```php
    private function attempt(WebhookDelivery $delivery, int $nowMs): void
    {
        $breaker = $this->breaker($delivery->subscription->url);

        if (!$breaker->allow($nowMs)) {
            $delay = max(1, $breaker->retryAfterMs($nowMs));
            $delivery->scheduleRetry($nowMs, $delay);
            $this->counters->add('circuitDeferred');

            return;
        }

        $attempt = $delivery->recordAttempt();
        $this->counters->add('attempts');
        $signature = $this->signer->sign(
            $delivery->subscription->secret,
            $delivery->event->payload,
            $nowMs,
        );
        $request = new WebhookRequest(
            url: $delivery->subscription->url,
            body: $delivery->event->payload,
            signature: $signature,
            idempotencyKey: $delivery->idempotencyKey(),
            attempt: $attempt,
            timestampMs: $nowMs,
            eventId: $delivery->event->id,
            subscriptionId: $delivery->subscription->id,
        );
```

[`src/Webhook/WebhookWorker.php` L168-L196](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/WebhookWorker.php#L168-L196)

Breaker привязан к URL endpoint-а, а не к подписке или tenant-у. Две подписки на
один URL делят одно состояние: если endpoint лежит, нет смысла проверять его
дважды.

### 2.5. Ответ, dead letter и retry

Успешный ответ закрывает доставку: breaker получает success, доставка помечается
delivered, id попадает в processed store. Любой другой ответ — это провал. Если
попытки исчерпаны, доставка уходит в dead-letter list. Иначе планируется retry с
экспоненциальной задержкой.

```php
        if ($response->isSuccessful()) {
            $breaker->recordSuccess($nowMs);
            $delivery->markDelivered($nowMs);
            $this->processed->mark($delivery->id, $nowMs);
            unset($this->pending[$delivery->id]);
            $this->counters->add('delivered');

            return;
        }

        $breaker->recordFailure($nowMs);
        $this->counters->add('failed');

        if ($attempt >= $this->maxAttempts) {
            $delivery->deadLetter($nowMs, $response->error);
            unset($this->pending[$delivery->id]);
            $this->deadLetters[] = $delivery;
            $this->counters->add('deadLettered');

            return;
        }

        $delay = $this->backoff->delayMs($attempt);
        $delivery->scheduleRetry($nowMs, $delay);
        $this->counters->add('retried');
        $this->counters->add('retryDelayMs', $delay);
    }
```

[`src/Webhook/WebhookWorker.php` L205-L231](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/WebhookWorker.php#L205-L231)

Исключение транспорта превращается в ответ с кодом 0 и обрабатывается как
обычный retryable failure. Dead-letter list — это массив в памяти; replay из
него не реализован, и книга этого не обещает.

### 2.6. Circuit breaker

Breaker считает последовательные провалы. После порога он открывается на
cooldown. В открытом состоянии запросы не отправляются. После cooldown наступает
half-open: ровно один probe пропускается, остальные ждут. Успешный probe
закрывает breaker и сбрасывает счётчик; неудачный снова открывает его на полный
cooldown.

```php
    public function state(int $nowMs): CircuitState
    {
        if (!$this->open) {
            return CircuitState::Closed;
        }

        return $nowMs < $this->openUntilMs ? CircuitState::Open : CircuitState::HalfOpen;
    }

    public function allow(int $nowMs): bool
    {
        $state = $this->state($nowMs);

        if ($state === CircuitState::Closed) {
            $this->counters->add('breakerAllowed');

            return true;
        }

        if ($state === CircuitState::Open) {
            $this->counters->add('breakerRejected');

            return false;
        }

        if ($this->probeInFlight) {
            $this->counters->add('breakerRejected');

            return false;
        }

        $this->probeInFlight = true;
        $this->counters->add('breakerAllowed');
        $this->counters->add('breakerProbes');

        return true;
    }
```

[`src/Webhook/CircuitBreaker.php` L46-L82](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/CircuitBreaker.php#L46-L82)

Провал во время открытого состояния не просто увеличивает счётчик: он
перезапускает cooldown. Поэтому один неудачный probe не закрывает breaker
преждевременно.

```php
    public function recordFailure(int $nowMs): void
    {
        $this->counters->add('breakerFailures');
        $this->probeInFlight = false;

        if ($this->open) {
            $this->openUntilMs = $nowMs + $this->cooldownMs;
            $this->counters->add('breakerOpens');

            return;
        }

        ++$this->consecutiveFailures;

        if ($this->consecutiveFailures >= $this->failureThreshold) {
            $this->open = true;
            $this->openUntilMs = $nowMs + $this->cooldownMs;
            $this->counters->add('breakerOpens');
        }
    }
```

[`src/Webhook/CircuitBreaker.php` L98-L117](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Webhook/CircuitBreaker.php#L98-L117)

Пока breaker открыт, доставка не тратит попытку: `attempt()` вызывает
`scheduleRetry()` и возвращается. Это принципиально для эксперимента с
длительным отказом. Failed probe, наоборот, является настоящей попыткой и может
приблизить доставку к dead letter.

---

## 3. Эксперимент

### 3.1. Retry budget

`webhook:retry` отправляет 1 000 событий с бюджетом четыре попытки и базовой
задержкой 100 мс. Каждая третья доставка падает один раз, каждая третья — два
раза, каждая десятая — всегда.

```bash
make experiment ARGS="webhook:retry"
```

| метрика              |   значение |
| -------------------- | ---------: |
| доставок создано     |      1 000 |
| доставлено           |        900 |
| dead letter          |        100 |
| попыток              |      2 200 |
| провалов             |      1 300 |
| retry                |      1 200 |
| суммарная задержка   | 190 000 мс |
| симулированное время |     700 мс |
| запросов к endpoint  |      2 200 |
| processed ids        |        900 |

Арифметика сходится: `failed = retried + deadLettered`, то есть 1 300 = 1
200 + 100. Попытки — это `delivered + failed` = 900 + 1 300 = 2 200. Retry — не
новая доставка, а ещё одна попытка той же доставки с тем же idempotency key.
Суммарная задержка 190 000 мс — это сумма запланированных пауз, а не время
работы процесса.

### 3.2. Потерянный ответ и дубликаты

`webhook:duplicates` сравнивает чистый ответ с ситуацией, когда endpoint
выполнил side effect, но ответ потерялся. Во втором режиме worker получает
failure и повторяет запрос; endpoint подавляет второй эффект по стабильному
ключу.

```bash
make experiment ARGS="webhook:duplicates"
```

| режим            | попыток | уникальных эффектов | подавлено дублей |  retry | replay отклонён | доставлено | dead letter |
| ---------------- | ------: | ------------------: | ---------------: | -----: | --------------: | ---------: | ----------: |
| чистый ответ     |  10 000 |              10 000 |                0 |      0 |          10 000 |     10 000 |           0 |
| потерянный ответ |  20 000 |              10 000 |           10 000 | 10 000 |          10 000 |     10 000 |           0 |

Потерянный ответ удвоил число HTTP-попыток, но не число side effects. Получатель
выполнил 10 000 эффектов и подавил 10 000 повторов. После завершения все 10 000
событий были отправлены повторно, и worker отклонил все 10 000 replay-ев в
пределах TTL. Это at-least-once плюс идемпотентный получатель, а не
exactly-once.

### 3.3. Fan-out

`webhook:fanout` отправляет 10 000 событий в 50 подписок с тремя типами.
Подписки выбирают типы по модулю, поэтому «заинтересованных» подписок меньше 50.

```bash
make experiment ARGS="webhook:fanout"
```

| тип события  | событий | заинтересованных подписок | доставок | доставок на событие |
| ------------ | ------: | ------------------------: | -------: | ------------------: |
| link.created |   3 334 |                        25 |   83 350 |               25.00 |
| link.clicked |   3 333 |                        17 |   56 661 |               17.00 |
| link.deleted |   3 333 |                        24 |   79 992 |               24.00 |
| итого        |  10 000 |                        50 |  220 003 |               22.00 |

Fan-out определяется числом заинтересованных подписок, а не общим числом
зарегистрированных. 25 подписок на `link.created` дают 25 доставок на каждое
такое событие. Каждая доставка потом требует подписи, попытки и своего retry
budget, поэтому широкий фильтр умножает всю downstream-стоимость.

### 3.4. Подпись

`webhook:signature` выполняет по 10 000 проверок на каждый сценарий. Валидная
подпись должна приниматься, а пять классов некорректных — отклоняться.

```bash
make experiment ARGS="webhook:signature"
```

| сценарий               | проверок | принято | отклонено |
| ---------------------- | -------: | ------: | --------: |
| валидная подпись       |   10 000 |  10 000 |         0 |
| изменённое тело        |   10 000 |       0 |    10 000 |
| неправильный секрет    |   10 000 |       0 |    10 000 |
| старый timestamp       |   10 000 |       0 |    10 000 |
| будущий timestamp      |   10 000 |       0 |    10 000 |
| некорректный заголовок |   10 000 |       0 |    10 000 |

Старый timestamp отклоняется, даже если сам дайджест корректен: timestamp входит
в подписанные байты и дополнительно проверяется по окну. Это ограничивает
replay, но не устраняет его внутри окна. Получателю всё равно нужен idempotency
key.

### 3.5. Circuit breaker при длительном отказе

`webhook:circuit` отправляет 100 событий в endpoint, который лежит 5 000 мс.
Бюджет — шесть попыток, базовая задержка 100 мс, порог breaker-а — пять
последовательных провалов, cooldown — 500 мс.

```bash
make experiment ARGS="webhook:circuit"
```

| режим       | попыток | попыток в down endpoint | открытий | отложено | доставлено | dead letter |    время |
| ----------- | ------: | ----------------------: | -------: | -------: | ---------: | ----------: | -------: |
| без breaker |     600 |                     600 |        0 |        0 |          0 |         100 | 3 100 мс |
| с breaker   |     113 |                      14 |       10 |      994 |         99 |           1 | 5 000 мс |

Без breaker каждая доставка потратила все шесть попыток: 100 × 6 = 600. К
моменту 3 100 мс бюджет исчерпан, а endpoint восстанавливается только на 5 000
мс, поэтому все 100 доставок стали dead letters. С breaker до down endpoint
дошло только 14 попыток; 994 проверки были отложены без расхода попытки. 99
доставок дождались восстановления, одна стала dead letter — это доставка, на
которую пришлись неудачные probe-ы.

Breaker не ускорил endpoint и не сделал retry бесплатным. Он изменил, что
происходит с очередью: вместо сжигания бюджета в пустоту доставки ждут
следующего probe. Плата — память и задержка, а не отказ.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="webhook --repetitions=3"
```

| операция               | итерации | медиана, мс |   ops/sec |
| ---------------------- | -------: | ----------: | --------: |
| sign                   |  100 000 |     205.776 |   485 966 |
| verify                 |  100 000 |     220.410 |   453 699 |
| dispatch 1             |   20 000 |      14.432 | 1 385 829 |
| dispatch 10            |   10 000 |      44.318 |   225 641 |
| breaker allow closed   |  100 000 |      11.439 | 8 741 801 |
| breaker reject open    |  100 000 |      12.725 | 7 858 520 |
| worker deliver success |   20 000 |      90.980 |   219 828 |

Подпись и проверка доминируются HMAC-SHA256 и строковой работой, а не объектной
моделью. Проверка чуть дороже подписи: она разбирает заголовок, проверяет окно
времени и сравнивает дайджест.

Dispatch с одной подпиской стоит 14.432 мс на 20 000 операций; с десятью —
44.318 мс на 10 000. На одну операцию это заметно дороже, потому что каждый
совпавший подписчик создаёт объект доставки и увеличивает счётчики. Breaker
allow/reject — это почти только проверка состояния и счётчики; успешная доставка
worker-а включает подпись, вызов транспорта, обновление breaker-а, запись
processed id и счётчики.

Числа описывают конкретную машину, PHP 8.5.11, OPcache CLI off, JIT off и Zend
MM. Это относительные ориентиры, а не универсальные константы.

---

## 5. Что ломается

**At-least-once — это дубликаты.** Worker не знает, выполнил ли получатель side
effect до потери ответа. Повтор неизбежен. Без идемпотентного получателя вебхуки
будут создавать дубликаты, и никакая подпись этого не исправит.

**Timestamp — не защита от replay.** Он ограничивает окно, но перехваченный
запрос внутри окна всё ещё можно повторить. Нужен idempotency key и хранилище
обработанных ключей на стороне получателя. Окно — компромисс между clock skew и
временем жизни украденного запроса.

**Секрет — это секрет.** В лаборатории он хранится строкой в подписке и
передаётся в `Signer`. Нет ротации, версий ключей и безопасного хранилища.
Утечка секрета позволяет подписывать произвольные запросы. Ротация должна быть
per-subscription, иначе один endpoint блокирует остальные.

**URL — это ввод пользователя.** Лаборатория проверяет только схему http/https.
Нет защиты от SSRF, DNS rebinding, редиректов на внутренние адреса и ограничений
egress. В production отправка webhook-ов на произвольные URL — это отдельная
поверхность атаки.

**Fan-out взрывается.** 25 заинтересованных подписок дают 25 доставок на
событие. При 10 000 событий это 250 000 подписей и попыток. Фильтр по типу
помогает, но широкая подписка всё равно умножает стоимость. Нужны батчинг,
лимиты и отдельные квоты на tenant-а.

**Breaker не бесконечен.** Пока breaker открыт, доставки копятся в памяти. Если
endpoint не вернётся, очередь вырастет и упрётся в память процесса. Нужны TTL,
лимит pending и политика, что делать со старыми доставками.

**Dead letter без replay — это архив.** Лаборатория хранит dead letters в
массиве и не умеет их повторно запускать. В production нужен replay с новым
idempotency key или явным подтверждением, иначе dead letter — просто потерянная
работа.

**Очередь в памяти.** Падение процесса теряет все pending-доставки. Нет durable
outbox, транзакции с бизнес-данными и восстановления после рестарта.
Webhook-платформа должна переживать падение worker-а, а не только падение
endpoint-а.

**`deliverDue()` сканирует pending.** Тик стоит `O(pending)`, а не `O(log n)`,
как heap. Для лаборатории это делает состояние видимым, но для большой очереди —
это отдельная стоимость, которую нельзя прятать.

**Exactly-once.** Между независимыми сервисами её нет. Формулировка «доставим
ровно один раз» в этом контексте — ошибка. Честная гарантия: at-least-once плюс
идемпотентный получатель.

---

## 6. Как это проверено

- `tests/Webhook/SignerTest.php` проверяет HMAC, привязку к телу и секрету,
  границы timestamp, malformed header и `hash_equals`.
- `tests/Webhook/WebhookDispatcherTest.php` проверяет tenant-фильтр, типы
  событий, стабильные delivery id и отсутствие доставок без совпадений.
- `tests/Webhook/WebhookDeliveryTest.php` проверяет переходы pending → delivered
  / dead-lettered, attempts, retry delay и запрет неверных переходов.
- `tests/Webhook/WebhookWorkerTest.php` покрывает подпись, duplicate enqueue,
  TTL processed id, retry с backoff, dead letter, breaker deferral, failed probe
  и сохранение idempotency key между попытками.
- `tests/Webhook/CircuitBreakerTest.php` проверяет closed/open/half-open,
  cooldown, один probe, закрытие после успеха и повторное открытие после
  провала.
- Эксперименты `webhook:retry`, `webhook:duplicates`, `webhook:fanout`,
  `webhook:signature` и `webhook:circuit` дают независимые счётчики для каждой
  гарантии.

К моменту завершения главы набор содержал 330 тестов; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` включает PHPUnit, PHPStan level 8 и
PHP-CS-Fixer.

---

## 7. Что запомнить

1. Один event — это много deliveries: fan-out считается по заинтересованным
   подпискам, а не по всем зарегистрированным.
2. Подпись покрывает точные байты и timestamp. `hash_equals` нужен для сравнения
   без early-exit.
3. Timestamp ограничивает replay-окно, но не заменяет идемпотентность.
4. Delivery id — стабильный idempotency key. Повтор несёт тот же ключ, новый
   attempt number и новый timestamp.
5. Retry — это ещё одна попытка той же доставки, а не новая доставка.
   `failed = retried + deadLettered`.
6. Открытый breaker откладывает доставку без расхода попытки; failed probe
   тратит попытку и перезапускает cooldown.
7. Гарантия — at-least-once плюс идемпотентный получатель. Exactly-once здесь не
   реализована и не обещана.

---

## 8. Упражнения

1. Запустите `webhook:retry` с `--max-attempts=6`. Как изменятся attempts,
   retries и dead letters? Сойдётся ли `failed = retried + dead`?
2. Увеличьте `--delay-ms` до 500. Почему simulated time вырастет не в пять раз,
   а иначе? Посчитайте сумму задержек вручную.
3. Запустите `webhook:duplicates` с `--max-attempts=1`. Что произойдёт с
   потерянным ответом и почему это уже не at-least-once?
4. Добавьте подписку на все три типа и сравните fan-out с текущим. Во сколько
   раз вырастет число доставок?
5. Уменьшите `--cooldown-ms` до 100 в `webhook:circuit`. Успеет ли breaker
   сделать больше probe-ов до восстановления? Что станет с числом down attempts?
6. Предложите, как сделать replay dead letter-ов безопасным. Какой ключ должен
   получить повтор и какие гарантии изменятся?

---

Дальше — Search Engine. Webhook показал, как доставлять события и не ломать
получателя повторами; следующая глава показывает, как искать по индексу, который
разложен на шарды.
