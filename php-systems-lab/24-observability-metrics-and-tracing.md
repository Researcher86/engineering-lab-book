# Глава 24. Наблюдаемость: метрики и трассировка

> Проект:
> [`php-systems-platform`](https://github.com/Researcher86/php-systems-platform)
> (коммит `9a6a916`). Нужно знать: процесс-модель (→ глава 22) и как
> распространяются отказы (→ глава 23). Это последняя глава про платформу;
> дальше — инженерная практика: как измерять и как писать.
>
> «Не молчать» (→ глава 23) — это ещё не наблюдаемость. Строка в stderr помогает
> тому, кто стоит рядом с терминалом, а не тому, кто разбирает инцидент через
> час и через два процесса.

## 1. Проблема

В главе 23 отказ перестал быть локальным: сбой воркера, падение задания,
заполненная очередь — у каждого есть путь к соседям. Но каждое из этих событий
наблюдалось **в момент, когда оно случалось**, и в том процессе, где оно
случилось. У оператора же есть три совсем других вопроса, и на них в stderr
ответа нет:

1. **Насколько система загружена прямо сейчас?** Сколько запросов, какова
   глубина очереди, сколько воркеров занято, сколько памяти.
2. **Что случилось с этим конкретным запросом?** Он пришёл по HTTP, породил
   задание, задание выполнил другой процесс — и, вполне возможно, упал.
3. **Живы ли воркеры?** И если нет — заменены ли они, и видно ли это снаружи, а
   не в предположении.

Опасность этой главы не в том, что наблюдать сложно, а в трёх соблазнах, каждый
из которых даёт **отчёт о чём-то другом**:

- **Инструментировать тесты.** Собрать числа внутри теста и напечатать их — и
  показывать потом читателю не систему, а её измерительную копию. Тогда тест
  проверяет тест.
- **Держать второй экземпляр состояния.** Хранить в HTTP-процессе счётчик
  очереди, чтобы «не ходить в журнал». Он будет неверным: задания разбирает
  другой процесс, поэтому локальный счётчик умеет только расти.
- **Хранить всё в памяти.** Копить спаны в массиве, пока процесс жив. Тогда
  отчёт ограничен временем жизни процесса — а самый интересный отчёт тот, где
  виновника уже нет с нами.

Правила этой главы — ответы на соблазны: наблюдаемость принадлежит **той же
системе**, а не второй; цифры, которые можно вывести, берутся **из источника**,
а не из копии; и у памяти есть граница, а у диска — нет, причём это разные
границы, и путать их не нужно.

## 2. Устройство

### Три вида, один ответ на каждый вопрос

```text
GET /metrics              GET /queue/status          GET /workers
    │                          │                          │
    ▼                          ▼                          ▼
name value                depth/published/           worker id, pid,
(текст)                   completed/failed/          state, current job,
                          retried                    tasks completed/failed
    │                          │                          │
    └───────── одна и та же система отчитывается ─────────┘
               (никакой второй инструментации)
                        │
                        ▼
        trace <request_id> — цепочка одного запроса
        через все процессы, из общего JSONL-журнала
```

Три вида отвечают на три разных вопроса и не дублируют друг друга: метрики — про
**нагрузку**, очередь — про **отставание**, воркеры — про **жизненный цикл**.
Трасса — единственный вид, который связывает их через **один идентификатор**.

### Метрики: один реестр, три вида значений

`MetricsRegistry` — общий объект, который `serve` отдаёт компонентам при
создании, чтобы база и кеш писали в одни и те же имена. В нём три вида метрик, и
разница между ними существенна:

| Вид          | Метод         | Семантика                                              |
| ------------ | ------------- | ------------------------------------------------------ |
| счётчик      | `increment()` | монотонный с момента запуска процесса                  |
| показатель   | `gauge()`     | только текущее значение; истории нет                   |
| длительность | `observe()`   | выборка латентности в секундах; хранится `sum`+`count` |

Длительность представляется парой «сумма и количество», а в дамп попадает
**среднее**. Это не упрощение, а следствие того, что имя `http.request_duration`
— одно число: в тексте `name value` нельзя выразить распределение. Среднее
округляется до шести знаков, чтобы сумма бинарных дробей не выливалась в
`0.30000000000000004`.

Имена метрик — **константы класса**, а не строки: опечатка становится фатальной
ошибкой, а не счётчиком, который молча остаётся нулевым.

### Снимок: собрать, а не вспомнить

`MetricsReporter` смешивает три источника в один снимок:

1. **Реестр** — то, что компоненты накопили в процессе (`http.*`, `cache.*`,
   `db.*`).
2. **Журнал очереди** — то, что имеет смысл читать свежим (`queue.*`): глубина и
   счётчики очереди принадлежат журналу, а не процессу, потому что очередь
   разбирает другой процесс.
3. **Живой пул** — то, что отвечает только на месте (`workers.*`), плюс память
   процесса (`process.rss`).

Имена, которыми владеет реестр, присутствуют **всегда** — нулями, пока ничего не
записано. Из-за этого свежая `serve` уже отвечает полным контрактом, а разность
двух снимков никогда не начинается с отсутствующего ключа. Источники, которых
нет, не добавляют строк и не ломают чтение: пул, который не отвечает, теряет
`workers.*`, а не весь снимок.

### Трасса: один идентификатор вместо трёх отчётов

```text
serve   ── http.request ── db.write ──┐
   request_id в заголовке X-Request-ID ┤
   request_id прокидывается в payload  │   общий JSONL-журнал
                                        │   (jobs.trace_store)
worker  ── job.execute ── db.read ────┘
   re-opens the SAME request_id из payload

trace <request_id>  ──►  вся цепочка, из одного файла
```

Три решения делают эту цепочку возможной.

Во-первых, **идентификатор приходит извне и проверяется**. Хорошо сформированный
`X-Request-ID` сохраняется, поэтому клиент может пронести свою цепочку через всю
систему; всё остальное заменяется новым. Проверка здесь не формальность: без неё
клиент мог бы подсунуть в идентификатор перевод строки и «дописать» строку в
JSONL-журнал.

Во-вторых, **идентификатор едет в payload задания**. Именно это — а не общая
память — связывает HTTP-запрос с заданием, которое выполнится в другом процессе,
возможно, через минуту.

В-третьих, **журнал общий, а процесс — нет**. Спан пишется в файл в момент
записи, поэтому он переживает процесс, который его породил. Воркер, убитый
`SIGKILL` (→ глава 23), не успел бы ничего сообщить — но успел записать свой
спан до смерти, и цепочка его помнит.

Отсюда и `meta.ok`: неудавшаяся операция всё равно записывает спан. Тогда отказ
в трассе — это **звено**, а не пропуск. И поэтому же `db.read` и `db.write`
сообщают через один общий путь: раньше у них было четыре копии учёта (успех и
падение, дважды), и эти копии могли разойтись — спан с `ok: false` рядом с
метрикой об успехе рассказывает об операции, которой не было.

### Ограничено в памяти, полно на диске

`Trace` держит в памяти окно последних спанов (`DEFAULT_MEMORY_SPANS` = 1000) и
**выбрасывает самый старый**, когда окно полно. Это граница памяти, а не потеря
данных: журнал дописывается по спану и хранит всё, поэтому `trace <request_id>`
отвечает и про цепочку длиннее тысячи звеньев.

`WorkerRegistry` ограничен так же и по той же причине: он живёт всё время работы
`serve`, поэтому записи о завершившихся задачах вычищаются по TTL (300 с), а не
накапливаются по одной на каждую выполненную задачу. `resolvedCount` при этом
**не** вычищается: это счётчик, а не множество.

### Наблюдаемость включается проводкой

База и кеш принимают метрики и трассу **необязательными** параметрами. Без них
они ведут себя точно так же; с ними — отчитываются. Это философия проводки в
одной фразе: **решение наблюдать принимает тот, кто соединяет, а не тот, кого
соединяют**. В приложении всегда соединяет `serve`, поэтому демо и нагрузочные
тесты читают настоящие числа, а не инструментацию, добавленную самими тестами.

### Чего это стоит

- **Среднее скрывает хвост.** `db.operation_duration` — среднее, а не
  перцентиль: редкие очень медленные операции в нём растворяются. Для учебной
  платформы этого достаточно, и это записано как ограничение, а не как свойство.
- **Журнал растёт.** Трасса — append-only JSONL; ротации в лаборатории нет, и
  долго живущая система накопила бы файл без границы. Тот же выбор, что и у
  журнала очереди (→ глава 8).
- **Окно памяти может «забыть» цепочку.** Если трасса включена без журнала,
  старые спаны исчезают безвозвратно: память тогда единственное хранилище.
- **Валидация идентификатора — обязательный шаг.** Пропустить её — значит
  открыть журнал для чужой записи.
- **Появился ещё один контракт, который надо держать.** Имя метрики, имя
  операции, поля спана — всё это соглашения между процессами; они ломаются так
  же тихо, как любое соглашение, если их не проверять (→ §6).

## 3. Код

### Реестр: три вида значений и один снимок

```php
/**
 * The shared in-process metrics model: components report into it under the
 * standard names the README documents, and snapshot() turns them into
 * numbers.
 *
 * Three kinds of metric share the registry:
 *
 *   counters   increment()   monotonic since the process started - how many
 *                            requests, how many cache misses
 *   gauges     gauge()       a number that is only current, not cumulative -
 *                            the latest value wins, there is no history
 *   durations  observe()     a latency sample in seconds; the registry keeps
 *                            sum+count and snapshot() reports the average,
 *                            which is the only rendering a single name like
 *                            http.request_duration can carry
 *
 * The names are constants so a typo is a fatal error instead of a counter
 * that silently stays at zero. A metric appears in snapshot() only once
 * something recorded it; MetricsReporter fills in the rest of the standard
 * set.
```

[`src/Observability/MetricsRegistry.php#L9-L28`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsRegistry.php#L9-L28)

**Перевод комментариев:**
- `MetricsRegistry` (PHPDoc): общая модель метрик внутри процесса: компоненты
  пишут в неё под стандартными именами, которые документирует README, а
  `snapshot()` превращает накопленное в числа. В реестре три вида метрик:
  счётчики (`increment()`, монотонные с запуска процесса — сколько запросов,
  сколько промахов кеша), показатели (`gauge()`, только текущее значение,
  побеждает последнее, истории нет) и длительности (`observe()`, выборка
  латентности в секундах; реестр хранит `sum` +`count`, а `snapshot()` отдаёт
  среднее — единственное представление, которое может нести одно имя вроде
  `http.request_duration`). Имена объявлены константами, поэтому опечатка —
  фатальная ошибка, а не счётчик, молча стоящий в нуле. Метрика появляется в
  `snapshot()` только после первой записи; остальные имена стандартного набора
  добавляет `MetricsReporter`.

Имена — это контракт, поэтому они объявлены константами: `http.*`, `cache.*`,
`db.*`, `queue.*`, `workers.*`, `process.*`.

```php
public const string HTTP_REQUESTS = 'http.requests';

public const string HTTP_ERRORS = 'http.errors';

public const string HTTP_REQUEST_DURATION = 'http.request_duration';

public const string CACHE_HITS = 'cache.hit';

public const string CACHE_MISSES = 'cache.miss';

public const string CACHE_OPERATIONS = 'cache.operations';

public const string DB_OPERATIONS = 'db.operations';

public const string DB_ERRORS = 'db.errors';

public const string DB_OPERATION_DURATION = 'db.operation_duration';

public const string QUEUE_DEPTH = 'queue.depth';

public const string QUEUE_PUBLISHED = 'queue.published';

public const string QUEUE_COMPLETED = 'queue.completed';

public const string QUEUE_FAILED = 'queue.failed';

public const string QUEUE_RETRIED = 'queue.retried';

public const string WORKERS_ACTIVE = 'workers.active';

public const string WORKERS_BUSY = 'workers.busy';

public const string WORKERS_IDLE = 'workers.idle';

public const string WORKERS_FAILED = 'workers.failed';

public const string WORKER_TASK_DURATION = 'worker.task_duration';

public const string PROCESS_RSS = 'process.rss';
```

[`src/Observability/MetricsRegistry.php#L32-L70`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsRegistry.php#L32-L70)

Три метода записи отделяют монотонное от текущего и от выборки:

```php
public function increment(string $name, int $by = 1): void
{
    $this->counters[$name] = ($this->counters[$name] ?? 0) + $by;
}

public function gauge(string $name, int|float $value): void
{
    $this->gauges[$name] = $value;
}

public function observe(string $name, float $seconds): void
{
    if ($seconds < 0) {
        throw new InvalidArgumentException('Duration cannot be negative');
    }

    $sample = $this->durations[$name] ?? ['count' => 0, 'total' => 0.0];
    $sample['count']++;
    $sample['total'] += $seconds;
    $this->durations[$name] = $sample;
}
```

[`src/Observability/MetricsRegistry.php#L83-L103`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsRegistry.php#L83-L103)

А снимок сводит всё в один отсортированный словарь, превращая длительности в
среднее (и округляя его, чтобы двоичные дроби не протекали):

```php
/**
 * Every metric this registry holds, name → value, alphabetically sorted
 * so a dump is stable. Counters are ints, gauges int|float, durations
 * their running average in seconds.
 *
 * @return array<string, int|float>
 */
public function snapshot(): array
{
    $snapshot = $this->counters;

    foreach ($this->gauges as $name => $value) {
        $snapshot[$name] = $value;
    }

    foreach (array_keys($this->durations) as $name) {
        $snapshot[$name] = $this->average($name);
    }

    ksort($snapshot);

    return $snapshot;
}

/**
 * Rounded so a sum of binary floats like 0.1 + 0.3 does not leak
 * 0.30000000000000004 into a dump.
 */
private function average(string $name): float
{
    $sample = $this->durations[$name];

    return round($sample['total'] / $sample['count'], 6);
}
```

[`src/Observability/MetricsRegistry.php#L122-L155`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsRegistry.php#L122-L155)

**Перевод комментариев:**
- `snapshot()` (PHPDoc): все метрики реестра в виде «имя → значение»,
  отсортированные по имени, чтобы дамп был стабильным. Счётчики — `int`,
  показатели — `int|float`, длительности — их текущее среднее в секундах.
  Возвращает `array<string, int|float>`.
- `average()` (PHPDoc): округление, чтобы сумма двоичных дробей вроде
  `0.1 + 0.3` не протекала в дамп как `0.30000000000000004`.

### Репортёр: собрать и отдать текстом

Три источника — реестр, журнал, пул — и разная природа у каждого: одно уже
посчитано, второе надо прочитать, третье можно только спросить.

```php
/**
 * The platform's one metrics reader: the standard metric set as one
 * snapshot, mixing what components push into the shared MetricsRegistry
 * with what only makes sense pulled fresh at read time.
 *
 *   registry            http.*, cache.*, db.* - accumulated in-process by
 *                       the components that touch them
 *   queue journal       queue.* - the durable journal is the observable
 *                       truth of the queue, read the same way queue:status
 *                       reads it
 *   worker pool stats   workers.*, worker.task_duration, worker.rss -
 *                       the Master answers them live, counting exactly the
 *                       workers a moment holds
 *   memory              process.rss - this process, right now
 *
 * The registry-owned names are always present (0 until first recorded), so
 * a fresh serve already answers with the full contract and a delta between
 * two reads never starts from a missing key. The other sources are
 * optional and appear only when wired; the pool is read best-effort - a pool
 * that will not answer leaves the workers.* lines out instead of failing
 * the whole read.
 */
```

[`src/Observability/MetricsReporter.php#L11-L32`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsReporter.php#L11-L32)

**Перевод комментариев:**
- `MetricsReporter` (PHPDoc): единственный читатель метрик платформы —
  стандартный набор в виде одного снимка, смешивающий то, что компоненты кладут
  в общий `MetricsRegistry`, с тем, что имеет смысл читать заново в момент
  чтения. Источники: реестр (`http.*`, `cache.*`, `db.*` — накоплено в процессе
  компонентами, которые их трогают); журнал очереди (`queue.*` — устойчивый к
  сбоям журнал и есть наблюдаемая правда очереди, читается так же, как
  `queue:status`); статистика пула воркеров (`workers.*`,
  `worker.task_duration`, `worker.rss` — `Master` отвечает ими вживую, считая
  ровно тех воркеров, которые есть в этот момент); память (`process.rss` — этот
  процесс прямо сейчас). Имена, которыми владеет реестр, присутствуют всегда
  (`0` до первой записи), поэтому свежий `serve` уже отвечает полным контрактом,
  а разность двух чтений никогда не начинается с отсутствующего ключа. Остальные
  источники необязательны и появляются только при проводке; пул читается «по
  возможности» — пул, который не отвечает, оставляет строки `workers.*` вне
  снимка, вместо того чтобы провалить всё чтение.

Слияние устроено так, что имена реестра есть всегда, а живой пул читается «по
возможности»: не ответивший пул оставляет `workers.*` вне снимка, вместо того
чтобы провалить всё чтение.

```php
/**
 * The full standard snapshot, name → value, sorted.
 *
 * @return array<string, int|float>
 */
public function snapshot(): array
{
    $snapshot = array_replace(
        self::REGISTRY_DEFAULTS,
        $this->registry->snapshot(),
        $this->queueSnapshot(),
        $this->poolSnapshot(),
    );

    if ($this->memory !== null) {
        $snapshot[MetricsRegistry::PROCESS_RSS] = $this->memory->snapshot()->rss ?? 0;
    }

    ksort($snapshot);

    return $snapshot;
}
```

[`src/Observability/MetricsReporter.php#L61-L82`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsReporter.php#L61-L82)

**Перевод комментариев:**
- `snapshot()` (PHPDoc): полный стандартный снимок «имя → значение»,
  отсортированный. Возвращает `array<string, int|float>`.

Текст — это и есть контракт эндпоинта: `GET /metrics` и CLI `metrics` отвечают
**одним** `toText()`, поэтому два представления не могут разойтись.

```php
/**
 * The snapshot as `name value` lines, the text GET /metrics and the
 * `metrics` CLI both answer with. The metric names are the contract;
 * floats are fixed at four decimals.
 */
public function toText(): string
{
    $text = '';

    foreach ($this->snapshot() as $name => $value) {
        $text .= sprintf(is_float($value) ? "%s %.4f\n" : "%s %d\n", $name, $value);
    }

    return $text;
}
```

[`src/Observability/MetricsReporter.php#L84-L98`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/MetricsReporter.php#L84-L98)

**Перевод комментариев:**
- `toText()` (PHPDoc): снимок в виде строк `name value` — тот текст, которым
  отвечают `GET /metrics` и CLI-команда `metrics`. Имена метрик — это контракт;
  числа с плавающей точкой фиксируются с четырьмя знаками после запятой.

### Трасса: контекст плюс журнал

```php
/**
 * A small educational tracer (explicitly not an OpenTelemetry clone): one
 * request_id carried from an HTTP request into its database calls, the job
 * its write publishes (in the payload) and that job's execution on a worker.
 *
 * A Trace is two things:
 *
 *  - a context: the request running right now, activeRequestId(). The
 *    Application opens one per HTTP request; a queue worker re-opens one
 *    for each job whose payload carries a request_id.
 *  - a span journal: one entry per operation (http.request, db.read,
 *    db.write, job.execute) with the request_id it ran under plus its own
 *    fields - job_id/worker_pid/attempt for a job, method/path/status for
 *    an HTTP answer. Each retry is its own job.execute span.
 *
 * Spans stay in a bounded in-memory window and, with a log path, are also
 * appended to a JSONL journal, which is what lets a worker's spans outlive
 * the worker and the `trace` CLI read a whole chain back. Every seam takes
 * the Trace as nullable: without one, nothing is traced.
 */
```

[`src/Observability/Trace.php#L9-L28`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/Trace.php#L9-L28)

**Перевод комментариев:**
- `Trace` (PHPDoc): небольшой учебный трассировщик (намеренно не клон
  OpenTelemetry): один `request_id` переносится из HTTP-запроса в его вызовы
  базы, в задание, которое публикует запись (в payload), и в выполнение этого
  задания воркером. `Trace` — это две вещи: контекст (запрос, выполняющийся
  прямо сейчас, `activeRequestId()`; `Application` открывает его на каждый
  HTTP-запрос, а воркер очереди переоткрывает для каждого задания, чей payload
  несёт `request_id`) и журнал спанов (одна запись на операцию — `http.request`,
  `db.read`, `db.write`, `job.execute` — с её `request_id` и собственными
  полями: `job_id` /`worker_pid`/`attempt` для задания, `method`
  /`path`/`status` для HTTP-ответа; каждая повторная попытка — отдельный спан
  `job.execute`). Спаны живут в ограниченном окне памяти и, если задан путь к
  журналу, дописываются в JSONL-журнал; именно это позволяет спанам воркера
  пережить воркер, а CLI `trace` — прочитать всю цепочку. Каждый шов принимает
  `Trace` как nullable: без него ничего не трассируется.

Контекст — это «какой запрос идёт сейчас»: его открывает `Application` на каждый
HTTP-запрос и переоткрывает воркер на каждое задание, чей payload несёт
`request_id`.

```php
/**
 * Open a request scope. A well-formed inbound X-Request-ID is kept, so
 * a client may carry its chain across the whole system (and the worker
 * re-opens the same scope from a job payload); anything absent or
 * malformed gets a fresh id. The id stays active until finishRequest(),
 * correlating every record() in between to this one request.
 */
public function beginRequest(?string $inbound): string
{
    $this->requestId = $this->normalize($inbound);

    return $this->requestId;
}

public function finishRequest(): void
{
    $this->requestId = null;
}
```

[`src/Observability/Trace.php#L57-L74`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/Trace.php#L57-L74)

**Перевод комментариев:**
- `beginRequest()` (PHPDoc): открывает область запроса. Корректный входящий
  `X-Request-ID` сохраняется, поэтому клиент может пронести свою цепочку через
  всю систему (а воркер переоткрывает ту же область из payload задания);
  отсутствующий или некорректный идентификатор заменяется свежим. Идентификатор
  остаётся активным до `finishRequest()`, связывая каждый `record()` между ними
  с этим запросом.

Запись спана делает три вещи сразу: отбрасывает спан без активного запроса,
обрезает окно памяти и дописывает строку в журнал. Именно последнее и позволяет
цепочке пережить процесс.

```php
/**
 * Record one span for the active request. Outside a request nothing is
 * recorded - a span that cannot answer "which request?" is noise.
 *
 * @param array<string, mixed> $extra span fields besides the common
 *                                    request_id/started_at/duration trio
 *                                    (job_id, worker_pid, attempt, meta); null
 *                                    values are dropped so the journal only
 *                                    ever holds fields that mean something
 */
public function record(string $operation, float $duration, array $extra = []): void
{
    if ($this->requestId === null) {
        return;
    }

    $span = [
        'operation' => $operation,
        'request_id' => $this->requestId,
        'started_at' => round(microtime(true) - $duration, 6),
        'duration' => round($duration, 6),
    ] + $this->withoutNulls($extra);

    $this->spans[] = $span;

    if (count($this->spans) > $this->maxSpans) {
        // Once full, every record drops the oldest span, so the window
        // is exactly the newest maxSpans. array_shift() reindexes the
        // buffer each time - O(maxSpans), microseconds at the default
        // size, and simpler than a ring buffer.
        array_shift($this->spans);
    }

    if ($this->logPath !== null) {
        $this->append($span);
    }
}
```

[`src/Observability/Trace.php#L81-L117`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/Trace.php#L81-L117)

**Перевод комментариев:**
- `record()` (PHPDoc): записывает один спан для активного запроса. Вне запроса
  ничего не записывается — спан, который не может ответить «какой запрос?», это
  шум. `@param array<string, mixed> $extra` — поля спана помимо общей тройки
  `request_id` /`started_at`/`duration` (`job_id`, `worker_pid`, `attempt`,
  `meta`); значения `null` отбрасываются, чтобы в журнале оставались только
  осмысленные поля.
- `// Once full, every record drops the oldest span, so the window...` — когда
  окно полно, каждая запись выбрасывает самый старый спан, поэтому в окне ровно
  `maxSpans` новейших. `array_shift()` каждый раз переиндексирует буфер —
  `O(maxSpans)`, микросекунды при размере по умолчанию, и это проще кольцевого
  буфера.

Чтение цепочки — это чтение **журнала**, а не памяти: ровно то, что делает
команда `trace` в другом процессе, когда того, кто писал, уже нет.

```php
/**
 * Read the full chain for one request back from the journal: the serve
 * process's request and database spans plus every worker's job.execute
 * span under the same request_id, no matter which process recorded
 * them. Empty without a journal or with nothing recorded for the id.
 *
 * @return list<array<string, mixed>>
 */
public function readLog(string $requestId): array
{
    if ($this->logPath === null || !is_file($this->logPath)) {
        return [];
    }

    $traces = [];

    foreach (file($this->logPath, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [] as $line) {
        try {
            $span = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
        } catch (JsonException) {
            // A torn tail line from a concurrent append - not this
            // request, and not worth failing the read over.
            continue;
        }

        if (is_array($span) && ($span['request_id'] ?? null) === $requestId) {
            $traces[] = $span;
        }
    }

    return $traces;
}
```

[`src/Observability/Trace.php#L139-L170`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/Trace.php#L139-L170)

**Перевод комментариев:**
- `readLog()` (PHPDoc): читает из журнала всю цепочку одного запроса: спаны
  запроса и обращений к базе из процесса `serve` плюс спаны `job.execute` всех
  воркеров с тем же `request_id`, независимо от того, какой процесс их записал.
  Пусто без журнала или если для идентификатора ничего не записано. Возвращает
  `list<array<string, mixed>>`.
- `// A torn tail line from a concurrent append - not this...` — оборванная
  хвостовая строка от параллельной дозаписи: это не этот запрос, и из-за неё не
  стоит проваливать чтение.

И то, без чего общий журнал стал бы общим для кого угодно: входящий
идентификатор должен выглядеть как идентификатор.

```php
    private function normalize(?string $inbound): string
    {
        if (is_string($inbound) && preg_match(self::INBOUND_PATTERN, $inbound) === 1) {
            return $inbound;
        }

        return 'req-' . bin2hex(random_bytes(8));
    }
}
```

[`src/Observability/Trace.php#L197-L205`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Observability/Trace.php#L197-L205)

### Эндпоинты: форма важнее содержания

`GET /health` — это каноническая форма обработчика: запрос и параметры маршрута
на входе, ответ на выходе.

```php
/**
 * GET /health - the liveness probe, and the canonical handler shape:
 * Request plus route parameters in, Response out.
 */
final class HealthHandler
{
    /**
     * @param array<string, string> $params route parameters (none for /health)
     */
    public function __invoke(Request $request, array $params): Response
    {
        return Response::json([
            'status' => 'ok',
            'service' => 'php-systems-platform',
        ]);
    }
```

[`src/Application/Handlers/HealthHandler.php#L10-L25`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Handlers/HealthHandler.php#L10-L25)

**Перевод комментариев:**
- `HealthHandler` (PHPDoc): `GET /health` — проверка живости и каноническая
  форма обработчика: `Request` и параметры маршрута на входе, `Response` на
  выходе.
- `__invoke()` (PHPDoc): `@param array<string, string> $params` — параметры
  маршрута (для `/health` их нет).

`GET /metrics` — тонкая обёртка над репортёром. Здесь нет своей логики
специально: любая «дополнительная» логика в представлении и есть та самая вторая
инструментация.

```php
/**
 * GET /metrics - the MetricsReporter snapshot as plain text, exactly what
 * the `metrics` CLI prints (see MetricsReporter::toText()).
 */
final readonly class MetricsHandler
{
    public function __construct(
        private MetricsReporter $reporter,
    ) {
    }

    /**
     * @param array<string, string> $params route parameters (none for /metrics)
     */
    public function __invoke(Request $request, array $params): Response
    {
        return Response::text($this->reporter->toText());
    }
```

[`src/Application/Handlers/MetricsHandler.php#L11-L28`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Handlers/MetricsHandler.php#L11-L28)

**Перевод комментариев:**
- `MetricsHandler` (PHPDoc): `GET /metrics` — снимок `MetricsReporter` в виде
  обычного текста, ровно то, что печатает CLI-команда `metrics` (см.
  `MetricsReporter::toText()`).
- `__invoke()` (PHPDoc): `@param array<string, string> $params` — параметры
  маршрута (для `/metrics` их нет).

### CLI: тот же отчёт вне `serve`

Команда `metrics` — тот же репортёр, но без реестра процесса: снаружи видны
живые источники (журнал, пул) и память, а процессные счётчики пусты. Это и есть
проверка, что отчёт принадлежит системе, а не одному процессу.

```php
/**
 * The CLI mirror of GET /metrics, over the same MetricsReporter. Outside a
 * serve the registry is empty, so this shows the pull side: queue
 * journal, the live pool (omitted when it does not answer) and this
 * process's RSS.
 */
private function metricsCommand(): int
{
    $config = $this->config();

    $reporter = new MetricsReporter(
        new MetricsRegistry(),
        new QueueJournal($this->queueLogPath($config)),
        $this->poolClient($config['workers']),
        new MemoryReporter(),
    );

    echo $reporter->toText();

    return 0;
}
```

[`src/Cli/PlatformCli.php#L1865-L1885`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L1865-L1885)

**Перевод комментариев:**
- `metricsCommand()` (PHPDoc): CLI-зеркало `GET /metrics` поверх того же
  `MetricsReporter`. Вне `serve` реестр пуст, поэтому команда показывает сторону
  вытягивания: журнал очереди, живой пул (опускается, если не отвечает) и RSS
  этого процесса.

А `trace` печатает цепочку целиком — и служебные поля задания (`job_id`,
`worker_pid`, `attempt`), и метаданные ответа, и исход попытки:

```php
/**
 * Every span recorded for one request_id, from the JSONL journal serve and
 * every pool worker write into: which request created the job, which
 * worker ran it, how long it took, how many retries it burned.
 *
 * @param list<string> $args
 */
private function traceCommand(array $args): int
{
    $requestId = $args[0] ?? '';

    if ($requestId === '') {
        fwrite(STDERR, "usage: php bin/platform.php trace <request_id>\n");

        return 1;
    }

    $spans = new Trace($this->traceStorePath($this->config()))->readLog($requestId);

    if ($spans === []) {
        echo sprintf("No spans recorded for %s.\n", $requestId);

        return 0;
    }

    foreach ($spans as $span) {
        $meta = is_array($span['meta'] ?? null) ? $span['meta'] : [];
        $fields = '';

        if (($span['job_id'] ?? null) !== null) {
            $fields .= sprintf(' job=%s', (string) $span['job_id']);
        }

        if (($span['worker_pid'] ?? null) !== null) {
            $fields .= sprintf(' worker_pid=%d', (int) $span['worker_pid']);
        }

        if (($span['attempt'] ?? null) !== null) {
            $fields .= sprintf(' attempt=%d', (int) $span['attempt']);
        }

        $detail = '';

        if (isset($meta['method'], $meta['path'], $meta['status'])) {
            $detail .= sprintf(' %s %s -> %d', (string) $meta['method'], (string) $meta['path'], (int) $meta['status']);
        }

        if (isset($meta['outcome'])) {
            $detail .= sprintf(' %s', (string) $meta['outcome']);

            if (isset($meta['error'])) {
                $detail .= sprintf(' (%s)', (string) $meta['error']);
            }
        }

        printf(
            "%-13s %7.4fs%s%s %s\n",
            (string) $span['operation'],
            (float) $span['duration'],
            $detail,
            $fields,
            (string) $span['request_id'],
        );
    }

    return 0;
}
```

[`src/Cli/PlatformCli.php#L1887-L1953`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L1887-L1953)

**Перевод комментариев:**
- `traceCommand()` (PHPDoc): все спаны, записанные для одного `request_id`, из
  JSONL-журнала, в который пишут `serve` и все воркеры пула: какой запрос создал
  задание, какой воркер его выполнил, сколько это заняло и сколько попыток
  сгорело. `@param list<string> $args` — аргументы команды.

### Воркеры: то, что можно наблюдать честно

Пул держит состояния своих воркеров внутри мастера, поэтому платформа наблюдает
тот жизненный цикл, которым действительно владеет: форвардеры, которые форкает
консьюмер.

```php
/**
 * The queue consumer's own worker lifecycle, made observable (PLAN Step 11).
 *
 * php-worker-pool keeps its workers' states private inside the Master, so
 * the lifecycle the platform can truthfully observe is the one it owns: the
 * php-job-queue forwarders queue:consume forks. Pid, state, current job and
 * the tasks_completed/tasks_failed counters are read straight off those
 * in-process Worker objects.
 *
 * The journal answers what Worker's counters cannot: WHICH job resolved and
 * WHEN (resolvedAt()/resolvedCount()), for per-job latency (QueueBenchmark).
 */
```

[`src/Workers/WorkerRegistry.php#L16-L27`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerRegistry.php#L16-L27)

**Перевод комментариев:**
- `WorkerRegistry` (PHPDoc): собственный жизненный цикл воркеров консьюмера
  очереди, сделанный наблюдаемым (PLAN Step 11). `php-worker-pool` держит
  состояния своих воркеров приватно внутри `Master`, поэтому жизненный цикл,
  который платформа может честно наблюдать, — тот, которым она владеет:
  форвардеры, которые форкает `queue:consume` из `php-job-queue`. Pid,
  состояние, текущее задание и счётчики `tasks_completed` /`tasks_failed`
  читаются прямо из этих объектов `Worker` в процессе. Журнал отвечает на то,
  что счётчики `Worker` не могут: какое задание завершилось и когда
  (`resolvedAt()`/`resolvedCount()`), для латентности по заданиям
  (`QueueBenchmark`).

Снимок делается в единственный момент, когда все задания видны сразу: после
отправки пачки и до сбора ответов.

```php
/**
 * Record the workers' state and which job each one is carrying, called
 * after a dispatch batch went out and before the answers are collected.
 */
public function capture(): void
{
    $now = $this->clock->now();
    $busy = 0;

    foreach ($this->pool->getWorkers() as $worker) {
        $this->observe($worker);
        $job = $worker->getCurrentJob();

        if ($job !== null) {
            $busy++;
            $this->inFlight[$job->getId()->toString()] = true;
        }
    }

    // Utilization: the busy count weighted by the time since the previous sample.
    if ($this->lastSampleAt > 0.0) {
        $this->busySeconds += ($now - $this->lastSampleAt) * $busy;
    }

    $this->lastSampleAt = $now;
}
```

[`src/Workers/WorkerRegistry.php#L87-L112`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerRegistry.php#L87-L112)

**Перевод комментариев:**
- `capture()` (PHPDoc): записывает состояние воркеров и то, какое задание несёт
  каждый, вызывается после отправки пачки и до сбора ответов.
- `// Utilization: the busy count weighted by the time since the previous sample.`
  — загрузка: число занятых воркеров, взвешенное по времени с предыдущей
  выборки.

## 4. Эксперимент

### Подготовка

```bash
cd php-systems-platform
make build install
```

Все четыре эксперимента выполняются в контейнере `php-systems-platform`, проект
смонтирован в `/app`. Ни один из них не поднимает платформу: предмет главы —
сами отчёты, и их можно проверить точнее и быстрее, чем через HTTP. Там, где в
реальной системе три процесса, здесь три объекта `Trace` над одним общим
журналом — та же топология, только без сети. Все выводы детерминированы: каждый
скрипт прогонялся дважды и совпал байт-в-байт. (Числа, которых в выводе нет —
время старта спанов, pid, RSS — в нём именно поэтому и отсутствуют.)

### 1. Три вида метрик и одно среднее

Счётчик, показатель и длительность ведут себя по-разному, и разница видна в
одной команде: счётчик складывается, показатель затирается, а длительность
превращается в среднее. Последнее — не потеря: имя метрики одно число, и среднее
— честный ответ на вопрос «сколько в среднем ждал вызывающий».

```bash
docker compose cp /tmp/ch24-metrics-registry.php php:/tmp/ch24-metrics-registry.php
docker compose exec php php /tmp/ch24-metrics-registry.php
```

```php
<?php

declare(strict_types=1);

/**
 * The shared registry's three kinds of metric, and the one rendering a single
 * name can carry: counters accumulate, gauges are only current, durations
 * snapshot as their average.
 *
 * This is the model every component reports into (the database and the cache
 * are handed this same object by serve), so its semantics are the semantics
 * of every number the platform publishes. The interesting cases are the
 * boring ones: a gauge that is set twice keeps only the last value, and a
 * duration is reported as an average because that is the only thing a name
 * like `http.request_duration` can honestly mean.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch24-metrics-registry.php php:/tmp/ch24-metrics-registry.php
 *   docker compose exec php php /tmp/ch24-metrics-registry.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Observability\MetricsRegistry;

$registry = new MetricsRegistry();

// A counter: monotonic since the process started.
$registry->increment(MetricsRegistry::HTTP_REQUESTS);
$registry->increment(MetricsRegistry::HTTP_REQUESTS);
$registry->increment(MetricsRegistry::HTTP_ERRORS);

// A gauge: only the current value; the earlier 7 leaves no trace.
$registry->gauge(MetricsRegistry::QUEUE_DEPTH, 7);
$registry->gauge(MetricsRegistry::QUEUE_DEPTH, 3);

// A duration: three samples in seconds.
$registry->observe(MetricsRegistry::HTTP_REQUEST_DURATION, 0.1);
$registry->observe(MetricsRegistry::HTTP_REQUEST_DURATION, 0.3);
$registry->observe(MetricsRegistry::HTTP_REQUEST_DURATION, 0.2);

printf("http.requests (a counter): %d\n", $registry->get(MetricsRegistry::HTTP_REQUESTS));
printf("queue.depth (a gauge, set 7 then 3): %d\n", $registry->get(MetricsRegistry::QUEUE_DEPTH));
printf("http.request_duration (0.1 + 0.3 + 0.2): %.4f\n", $registry->get(MetricsRegistry::HTTP_REQUEST_DURATION));
printf("a metric nothing recorded reads as: %d\n", $registry->get(MetricsRegistry::CACHE_HITS));

printf("\nevery metric this registry holds, name by name:\n");

foreach ($registry->snapshot() as $name => $value) {
    printf("  %-24s %s\n", $name, is_float($value) ? sprintf('%.4f', $value) : (string) $value);
}

$names = array_keys($registry->snapshot());
$sorted = $names;
sort($sorted);

printf("\nthe dump is sorted, so two dumps never differ by order: %s\n", $names === $sorted ? 'yes' : 'no');

try {
    $registry->observe(MetricsRegistry::DB_OPERATION_DURATION, -0.001);
    printf("a negative duration: accepted (wrong)\n");
} catch (InvalidArgumentException $e) {
    printf("a negative duration: refused (%s)\n", $e->getMessage());
}
```

**Перевод комментариев:**
- `ch24-metrics-registry.php` (PHPDoc): три вида метрик общего реестра и
  единственное представление, которое может нести одно имя: счётчики
  накапливаются, показатели только текущие, длительности в снимке — их среднее.
  Это модель, в которую отчитывается каждый компонент (база и кеш получают этот
  же объект от `serve`), поэтому её семантика — семантика каждого числа, которое
  публикует платформа. Интересные случаи здесь самые скучные: показатель,
  выставленный дважды, хранит только последнее значение, а длительность отдаётся
  средним, потому что это единственное, что имя вроде `http.request_duration`
  может честно означать. Запускается в контейнере, где проект смонтирован в
  `/app`, командами из докблока.
- `// A counter: monotonic since the process started.` — счётчик: монотонен с
  момента запуска процесса.
- `// A gauge: only the current value; the earlier 7 leaves no trace.` —
  показатель: только текущее значение; прежнее `7` не оставляет следа.
- `// A duration: three samples in seconds.` — длительность: три выборки в
  секундах.

```text
http.requests (a counter): 2
queue.depth (a gauge, set 7 then 3): 3
http.request_duration (0.1 + 0.3 + 0.2): 0.2000
a metric nothing recorded reads as: 0

every metric this registry holds, name by name:
  http.errors              1
  http.request_duration    0.2000
  http.requests            2
  queue.depth              3

the dump is sorted, so two dumps never differ by order: yes
a negative duration: refused (Duration cannot be negative)
```

`http.requests` — `2`, потому что счётчик монотонен; `queue.depth` — `3`, а не
`7`, потому что показатель хранит **последнее** значение, и истории у него нет;
`http.request_duration` — `0.2000`, то есть среднее из `0.1`, `0.3` и `0.2`,
представленное парой «сумма и количество».

Строка `a metric nothing recorded reads as: 0` — это поведение **до**
`MetricsReporter`: сам реестр знает только те имена, которые кто-то трогал.
Полный контракт — забота репортёра (§ эксперимент 4).

Дамп отсортирован по имени, и это проверяется сравнением с отсортированной
копией: два дампа одного состояния не могут отличаться порядком строк. Наконец,
отрицательная длительность отвергается с внятным сообщением — единственная
ошибка, которую `observe()` считает своей.

### 2. Одна цепочка на три процесса

Здесь три объекта `Trace` над одним журналом: первый пишет спаны `serve`, второй
переоткрывает тот же `request_id` и пишет спан задания, третий читает цепочку —
как это делает команда `trace` через час и в другом процессе.

```bash
docker compose cp /tmp/ch24-trace-chain.php php:/tmp/ch24-trace-chain.php
docker compose exec php php /tmp/ch24-trace-chain.php
```

```php
<?php

declare(strict_types=1);

/**
 * One request, one chain across processes: the serve side opens the scope,
 * the worker re-opens the SAME scope from the job payload, and a third
 * reader gets the whole chain back from the journal.
 *
 * The three Trace objects stand in for the three processes that actually
 * write here (serve, a pool worker, the `trace` CLI), and each one shares
 * only the journal file - exactly the real topology. What the experiment
 * checks is the property the chapter rests on: the chain is not lost when
 * the process that started it ends, a failed operation still records its
 * span, and a span with no active request is not recorded at all.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch24-trace-chain.php php:/tmp/ch24-trace-chain.php
 *   docker compose exec php php /tmp/ch24-trace-chain.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Observability\Trace;
use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch24-trace-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$logPath = $directory . 'trace.log';

// A well-formed inbound id, the kind a client carries across the system.
$requestId = 'req-echo-chain-0001';

// --- the serve process -------------------------------------------------
$serve = new Trace($logPath);
$kept = $serve->beginRequest($requestId);

$serve->record('http.request', 0.012, ['meta' => ['method' => 'POST', 'path' => '/orders', 'status' => 201]]);
$serve->record('db.write', 0.004, ['meta' => ['ok' => true]]);
$serve->finishRequest();

printf("the inbound request id is kept: %s\n", $kept === $requestId ? 'yes' : 'no');
printf("closing the scope forgets the request: %s\n", $serve->activeRequestId() === null ? 'yes' : 'no');

$before = count($serve->spans());
$serve->record('db.read', 0.002, ['meta' => ['ok' => true]]);

printf(
    "a span recorded outside a scope is dropped: %s\n",
    count($serve->spans()) === $before ? 'yes' : 'no',
);
printf("spans the serve process still holds: %d\n", count($serve->spans()));

// --- the worker process ------------------------------------------------
// It re-opens the same scope from the request_id in the job payload.
$worker = new Trace($logPath);
$reopened = $worker->beginRequest($requestId);

$worker->record('job.execute', 0.030, [
    'job_id' => 'job-7f3a',
    'worker_pid' => 4242,
    'attempt' => 2,
    'meta' => ['outcome' => 'failed', 'error' => 'database is unavailable'],
]);
$worker->record('db.read', 0.002, ['meta' => ['ok' => false, 'error' => 'connection lost']]);
$worker->finishRequest();

printf("the worker re-opened the same scope: %s\n", $reopened === $requestId ? 'yes' : 'no');

// --- the reader --------------------------------------------------------
$cli = new Trace($logPath);
$chain = $cli->readLog($requestId);

printf("\nthe whole chain, read back from the journal: %d spans\n", count($chain));

foreach ($chain as $span) {
    $meta = is_array($span['meta'] ?? null) ? $span['meta'] : [];

    printf(
        "  %-13s %s\n",
        (string) $span['operation'],
        implode(' ', array_map(
            static fn (string $key): string => $key . '=' . (is_bool($meta[$key]) ? ($meta[$key] ? 'true' : 'false') : (string) $meta[$key]),
            array_keys($meta),
        )),
    );
}

$failed = array_values(array_filter(
    $chain,
    static fn (array $span): bool => ($span['meta']['ok'] ?? null) === false,
));

printf("a failed operation is in the chain anyway: %s\n", $failed === [] ? 'no' : 'yes');
printf(
    "spans for a request nobody made: %d\n",
    count($cli->readLog('req-nobody-000000')),
);
```

**Перевод комментариев:**
- `ch24-trace-chain.php` (PHPDoc): один запрос — одна цепочка через процессы:
  сторона `serve` открывает область, воркер переоткрывает **ту же** область из
  payload задания, а третий читатель получает всю цепочку обратно из журнала.
  Три объекта `Trace` заменяют три процесса, которые реально пишут здесь
  (`serve`, воркер пула, CLI `trace`), и каждый делит только файл журнала —
  ровно реальная топология. Эксперимент проверяет свойство, на котором стоит
  глава: цепочка не теряется, когда процесс, начавший её, завершается; упавшая
  операция всё равно записывает свой спан; а спан без активного запроса не
  записывается вовсе. Запускается в контейнере, где проект смонтирован в `/app`,
  командами из докблока.
- `// A well-formed inbound id, the kind a client carries across the system.` —
  корректный входящий идентификатор, какой клиент проносит через систему.
- `// --- the serve process ---` — процесс `serve`.
- `// --- the worker process ---` /
  `// It re-opens the same scope from the request_id in the job payload.` —
  процесс воркера; он переоткрывает ту же область из `request_id` в payload
  задания.
- `// --- the reader ---` — читатель.

```text
the inbound request id is kept: yes
closing the scope forgets the request: yes
a span recorded outside a scope is dropped: yes
spans the serve process still holds: 2
the worker re-opened the same scope: yes

the whole chain, read back from the journal: 4 spans
  http.request  method=POST path=/orders status=201
  db.write      ok=true
  job.execute   outcome=failed error=database is unavailable
  db.read       ok=false error=connection lost
a failed operation is in the chain anyway: yes
spans for a request nobody made: 0
```

Три первых ответа — про контекст. Входящий идентификатор сохраняется;
`finishRequest()` его забывает; и спан, записанный **вне** области запроса, не
записывается вовсе — его нечем корреллировать, и такой спан был бы шумом.

Четвёртый ответ — про связь между процессами: воркер переоткрыл тот же
`request_id` (он приехал в payload задания, а не через память). После этого
чтение даёт **четыре** звена подряд: HTTP-запрос, запись в базу, выполнение
задания и ещё одно чтение из базы — уже из воркера. Порядок в журнале — это
порядок событий, а не порядок процессов: звенья из разных процессов просто
дописываются в один файл.

Обратите внимание на звено `job.execute outcome=failed` и на следующее за ним
`db.read ok=false`: отказ **в** цепочке. Именно поэтому спаны несут `meta.ok` —
пропущенное звено не отличить от звена, которое не случилось, а с звеньями отказ
читается на месте. И последняя строка — `0` для `req-nobody-000000`: чужая
цепочка не появляется из воздуха.

### 3. Ограничено в памяти, полно на диске

Окно памяти выставлено в три спана, а записано пять. Дальше сравниваются два
ответа на один и тот же вопрос: что помнит процесс и что помнит журнал.

```bash
docker compose cp /tmp/ch24-bounded-window.php php:/tmp/ch24-bounded-window.php
docker compose exec php php /tmp/ch24-bounded-window.php
```

```php
<?php

declare(strict_types=1);

/**
 * Bounded in memory, complete on disk: a window of three spans and a chain
 * of five.
 *
 * The in-memory window exists so a long-running serve can answer "what just
 * happened to this request" without an unbounded array. The journal exists
 * so the window is a memory bound and not a data loss: `trace <request_id>`
 * reads the whole chain back even when the window has dropped its oldest
 * spans. Both halves are checked here on one Trace, with the sizes small
 * enough to see the eviction happen.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch24-bounded-window.php php:/tmp/ch24-bounded-window.php
 *   docker compose exec php php /tmp/ch24-bounded-window.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Observability\Trace;
use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch24-window-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$logPath = $directory . 'trace.log';
$requestId = 'req-window-000001';

// A window of three spans, the way serve caps its own at a thousand.
$trace = new Trace($logPath, 3);
$trace->beginRequest($requestId);

for ($number = 1; $number <= 5; $number++) {
    $trace->record('db.read', 0.001, ['meta' => ['ok' => true, 'number' => $number]]);
}

$inMemory = $trace->spans($requestId);
$onDisk = $trace->readLog($requestId);

printf("spans recorded: %d\n", 5);
printf("spans the window holds: %d\n", count($inMemory));
printf(
    "the oldest span in the window is number: %d\n",
    (int) $inMemory[0]['meta']['number'],
);

printf("\nspans the journal answers with: %d\n", count($onDisk));
printf(
    "the journal still knows the ones the window dropped: %s\n",
    array_map(static fn (array $span): int => (int) $span['meta']['number'], $onDisk) === [1, 2, 3, 4, 5] ? 'yes' : 'no',
);

// A different request's spans are not in this chain, though they share the
// journal - the filter is the request_id, not the file.
$trace->finishRequest();
$trace->beginRequest('req-window-000002');
$trace->record('db.write', 0.001, ['meta' => ['ok' => true]]);
$trace->finishRequest();

printf(
    "another request's span is a different chain: %d\n",
    count($trace->readLog('req-window-000002')),
);
printf(
    "and the first chain is unchanged: %d\n",
    count($trace->readLog($requestId)),
);
```

**Перевод комментариев:**
- `ch24-bounded-window.php` (PHPDoc): ограничено в памяти, полно на диске — окно
  из трёх спанов и цепочка из пяти. Окно в памяти существует, чтобы долго
  живущий `serve` мог ответить «что только что случилось с этим запросом» без
  неограниченного массива. Журнал существует, чтобы окно было границей памяти, а
  не потерей данных: `trace <request_id>` читает всю цепочку обратно, даже когда
  окно выбросило самые старые спаны. Обе половины проверяются здесь на одном
  `Trace`, а размеры малы, чтобы вытеснение было видно. Запускается в
  контейнере, где проект смонтирован в `/app`, командами из докблока.
- `// A window of three spans, the way serve caps its own at a thousand.` — окно
  из трёх спанов — так `serve` ограничивает своё тысячью.
- `// A different request's spans are not in this chain, though they share the`
  — спаны другого запроса не входят в эту цепочку, хотя лежат в том же журнале:
  фильтр — это `request_id`, а не файл.

```text
spans recorded: 5
spans the window holds: 3
the oldest span in the window is number: 3

spans the journal answers with: 5
the journal still knows the ones the window dropped: yes
another request's span is a different chain: 1
and the first chain is unchanged: 5
```

`spans the window holds: 3` против `spans the journal answers with: 5` — и то,
что выброшенные спаны **на диске целы** (`yes`): номера с первого по пятый
читаются из журнала, хотя в памяти остались только третий, четвёртый и пятый.
`the oldest span in the window is number: 3` показывает, что вытеснение
происходит по одному спану, а не окнами.

Следующие две строки — про то, что фильтр цепочки — это `request_id`, а не файл:
второй запрос даёт ровно одно звено, и первая цепочка от чужой записи не
меняется. В реальной `serve` окно — тысяча спанов, и тот же механизм работает в
обе стороны: память ограничена, история — нет.

### 4. Снимок как его отдаёт `/metrics`

Последний эксперимент показывает тот текст, которым отвечают `GET /metrics` и
`metrics`: реестр с накопленными значениями, живой журнал очереди, и **не
подключённые** пул и память — то есть ровно вид снаружи `serve`.

```bash
docker compose cp /tmp/ch24-reporter-text.php php:/tmp/ch24-reporter-text.php
docker compose exec php php /tmp/ch24-reporter-text.php
```

```php
<?php

declare(strict_types=1);

/**
 * The metrics snapshot as it is actually answered: one text, three merged
 * sources, and the names that were never recorded still present at zero.
 *
 * This is what `GET /metrics` and the `metrics` CLI both return. The
 * registry here stands in for what serve accumulated in-process, the journal
 * is a real one with real jobs in it, and the pool and memory sources are
 * simply not wired - which is the "metrics outside a serve" case the CLI
 * documents. Two facts matter: the registry-owned names appear even though
 * nothing recorded them, and a source that is not wired contributes no lines
 * rather than failing the read.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch24-reporter-text.php php:/tmp/ch24-reporter-text.php
 *   docker compose exec php php /tmp/ch24-reporter-text.php
 */

require '/app/vendor/autoload.php';

use PhpJobQueue\Metrics\MetricsCollector;
use PhpJobQueue\Persistence\FileStorage;
use PhpJobQueue\Producer\JobFactory;
use PhpJobQueue\Producer\Producer;
use PhpJobQueue\Queue\InMemoryQueue;
use PhpJobQueue\Support\SystemClock;
use PhpSystemsPlatform\Observability\MetricsRegistry;
use PhpSystemsPlatform\Observability\MetricsReporter;
use PhpSystemsPlatform\Queue\QueueJournal;
use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch24-reporter-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$logPath = $directory . 'queue.log';
$clock = new SystemClock();
$storage = new FileStorage($logPath);
$queue = InMemoryQueue::restoreFromStorage($storage, $clock);
$producer = new Producer($queue, new JobFactory($clock, new MetricsCollector()));

$producer->dispatch('order.created');
$producer->dispatch('order.created');

// What the components reported into the shared registry in-process.
$registry = new MetricsRegistry();
$registry->increment(MetricsRegistry::HTTP_REQUESTS, 2);
$registry->increment(MetricsRegistry::HTTP_ERRORS);
$registry->observe(MetricsRegistry::DB_OPERATION_DURATION, 0.5);
$registry->observe(MetricsRegistry::DB_OPERATION_DURATION, 0.25);

$reporter = new MetricsReporter($registry, new QueueJournal($logPath));
$text = $reporter->toText();

echo $text;

printf("\nlines the reporter answered with: %d\n", substr_count($text, "\n"));

$lines = array_filter(explode("\n", $text), static fn (string $line): bool => $line !== '');
$exposed = [];

foreach ($lines as $line) {
    $exposed[explode(' ', $line)[0]] = true;
}

printf("http.errors is present although nothing recorded it: %s\n", isset($exposed['http.errors']) ? 'yes' : 'no');
printf("db.operation_duration is the average of 0.5 and 0.25: %s\n", str_contains($text, 'db.operation_duration 0.3750') ? 'yes' : 'no');
printf("queue.depth is read live from the journal: %s\n", str_contains($text, 'queue.depth 2') ? 'yes' : 'no');

$workers = array_filter(array_keys($exposed), static fn (string $name): bool => str_starts_with($name, 'workers.'));

printf("workers.* lines without a pool wired: %d\n", count($workers));
printf("process.rss lines without a memory source: %d\n", isset($exposed['process.rss']) ? 1 : 0);
```

**Перевод комментариев:**
- `ch24-reporter-text.php` (PHPDoc): снимок метрик таким, каким его на самом
  деле отдают: один текст, три слитых источника и имена, которые никогда не
  записывались, но всё равно присутствуют нулями. Это то, что возвращают
  `GET /metrics` и CLI `metrics`. Реестр здесь заменяет то, что `serve` накопил
  в процессе, журнал — настоящий, с настоящими заданиями, а источники пула и
  памяти просто не подключены — это и есть случай «метрики вне `serve` »,
  который документирует CLI. Важны два факта: имена, которыми владеет реестр,
  появляются, хотя никто их не записывал, а неподключённый источник не добавляет
  строк, вместо того чтобы провалить чтение. Запускается в контейнере, где
  проект смонтирован в `/app`, командами из докблока.
- `// What the components reported into the shared registry in-process.` — что
  компоненты накопили в общем реестре внутри процесса.

```text
cache.hit 0
cache.miss 0
cache.operations 0
db.errors 0
db.operation_duration 0.3750
db.operations 0
http.errors 1
http.request_duration 0.0000
http.requests 2
queue.completed 0
queue.depth 2
queue.failed 0
queue.published 2
queue.retried 0

lines the reporter answered with: 14
http.errors is present although nothing recorded it: yes
db.operation_duration is the average of 0.5 and 0.25: yes
queue.depth is read live from the journal: yes
workers.* lines without a pool wired: 0
process.rss lines without a memory source: 0
```

В тексте 14 строк, и три свойства видны прямо в нём.

**Имена реестра есть всегда.** `http.errors 1` — записано; но и `cache.hit 0`,
`db.errors 0`, `http.request_duration 0.0000` на месте, хотя ничего из этого не
трогали. Свежая `serve` поэтому уже отвечает полным контрактом, и
watchdog-запрос «есть ли метрика `db.operation_duration`» не завидует моменту
запуска.

**Живое читается живьём.** `queue.depth 2` и `queue.published 2` — это не
счётчики процесса, а чтение журнала в момент снимка: два опубликованных задания,
ни одно ещё не разобрано.

**Чего нет, то не выдумывают.** `workers.* lines without a pool wired: 0` и
`process.rss` отсутствует: источников не подключено — строк нет, а чтение не
проваливается. Это и есть «наблюдаемость включается проводкой» в действии:
репортёр без пула — валидный репортёр, а не сломанный.

Здесь же видно, почему длительность хранится парой:
`db.operation_duration 0.3750` — это `(0.5 + 0.25) / 2`, посчитанное на чтении,
а не сохранённое число.

## 5. Отказы

| Что ломается                                      | Чем обнаруживается                            | Что происходит                                                            |
| ------------------------------------------------- | --------------------------------------------- | ------------------------------------------------------------------------- |
| Компонент не отчитывается                         | метрика не появляется вовсе                   | поведение не меняется: метрики и трасса — необязательные параметры шва    |
| Метрика названа с опечаткой                       | константы вместо строк                        | фатальная ошибка вместо счётчика, молча стоящего в нуле                   |
| Два снимка сравнивают                             | имена реестра всегда присутствуют             | разность не начинается с отсутствующего ключа                             |
| Пул не отвечает                                   | `MetricsReporter` читает пул «по возможности» | строки `workers.*` пропускаются, чтение не проваливается                  |
| Очередь наблюдают не из консьюмера                | глубина читается из журнала                   | любой процесс видит очередь, не владея ею                                 |
| Длительность считают средним                      | дамп показывает среднее                       | редкие хвосты растворяются — записанное ограничение, не свойство          |
| Процесс с трассой умер                            | спан записан в журнал до смерти               | `trace` всё равно отвечает: из памяти он бы исчез                         |
| Трасса без журнала                                | только окно памяти                            | спаны старше окна теряются безвозвратно                                   |
| Цепочка длиннее окна                              | журнал полон, память ограничена               | в памяти новейшие, на диске все                                           |
| Клиент прислал свой `X-Request-ID`                | проверка 8–128 символов                       | корректный сохраняется, остальное заменяется свежим                       |
| Клиент прислал «идентификатор» с переводом строки | та же проверка                                | заменяется; иначе клиент дописал бы строку в журнал                       |
| Операция упала                                    | спан с `meta.ok: false`                       | отказ — звено цепочки, а не пропуск                                       |
| Учёт успеха и падения разошёлся                   | один путь `Database::run()`/`report()`        | четыре копии учёта свёрнуты в одну, спан и метрика не могут противоречить |
| Воркер заменён                                    | pid в `/workers` изменился                    | замена **наблюдается**, а не предполагается                               |
| Записи о завершённых задачах копятся              | TTL 300 с в `WorkerRegistry`                  | `resolvedAt` вычищается; `resolvedCount` — счётчик, он не чистится        |
| В трассе нет идентификатора                       | `trace` без аргумента                         | код возврата 1 и подсказка об использовании                               |
| Идентификатор не встречался                       | пустая цепочка                                | «No spans recorded» и успешный код: пустой ответ — тоже ответ             |

## 6. Как это проверено

Три набора тестов, по одному на каждый вид.

**Модель метрик.** `tests/Unit/Observability/MetricsRegistryTest.php` — пять
методов ровно про четыре вещи из эксперимента 1:
`testCountersStartAtZeroAndOnlyGoUp`, `testGaugeLatestValueWins`,
`testDurationsReadBackAsTheirAverage`, `testNegativeDurationIsRejected`,
`testSnapshotMergesEveryKindSorted`. Свойство «показатель хранит последнее
значение» звучит как деталь реализации, а на деле это единственное, что отличает
метрику-состояние от счётчика; поэтому оно закреплено тестом, а не комментарием.

**Трасса.** `tests/Unit/Observability/TraceTest.php` — десять методов, и они
делятся на три группы: проверка идентификатора
(`testGeneratesARequestIdWhenNoInboundHeaderArrives`,
`testKeepsAWellFormedInboundRequestId`,
`testMalformedInboundRequestIdFallsBackToAGeneratedOne`), область запроса
(`testRecordProducesASpanCorrelatedToTheActiveRequest`,
`testRecordWithoutAnActiveRequestIsSkipped`, `testFinishRequestEndsTheScope`) и
журнал (`testJournalAppendsSpansAndReadsTheChainBack` — эксперимент 2,
`testKeepsOnlyTheMostRecentSpansInMemory` — эксперимент 3,
`testSpansForRequestSeparatesMissingFromPresent`). Последний метод разводит «нет
такой цепочки» и «цепочка есть, но спанов в ней нет» — то же различие, что и
между двумя причинами `503` в главе 21.

**Эндпоинты и команды.** `tests/Integration/MetricsCommandTest.php`
(`testPrintsTheStandardSnapshotFromTheJournalAndRss`) проверяет снимок как
текст: имя и значение, включая строки, пришедшие из журнала и от RSS.
`tests/Integration/TraceCommandTest.php` — три
(`testPrintsTheChainForOneRequestId`,
`testUnknownRequestIdReportsAnEmptyChainAndSucceeds`,
`testMissingRequestIdIsAUsageError`). `tests/Integration/StatusCommandTest.php`
— два (`testPrintsEveryPlatformSection`, `testQueueCountersComeFromTheJournal`)
и подтверждает, что счётчики очереди берутся из журнала, а не из памяти команды.

**Идентификатор на проводе.**
`tests/Integration/Boundary/HttpApplicationBoundaryTest.php` — одиннадцать
методов, и два из них прямо про трассу:
`testTheRequestIdTheClientCarriesIsKeptAndEchoedOnTheAnswer` (идентификатор
клиента переживает запрос и возвращается в ответе) и
`testAnAnswerWithoutAClientRequestIdStillCarriesOne` (без него ответ всё равно
несёт сгенерированный).

Прогоны:
`docker compose exec php php vendor/bin/phpunit tests/Unit/Observability tests/Unit/Memory`
— **22 теста, 55 утверждений, OK** (отдельно `MetricsRegistryTest` — 5,
`TraceTest` — 10). Полный `tests/Unit` — **145 тестов, 348 утверждений**.

## 7. Цифры

| Файл                                          | Строк | Что делает                                       |
| --------------------------------------------- | ----- | ------------------------------------------------ |
| `src/Observability/MetricsRegistry.php`       | 156   | три вида значений, среднее, сортированный снимок |
| `src/Observability/MetricsReporter.php`       | 181   | три источника в один текст                       |
| `src/Observability/Trace.php`                 | 205   | контекст запроса, окно памяти, журнал            |
| `src/Workers/WorkerRegistry.php`              | 266   | жизненный цикл воркеров консьюмера               |
| `src/Application/Handlers/MetricsHandler.php` | 29    | эндпоинт без своей логики                        |
| `src/Application/Handlers/HealthHandler.php`  | 26    | каноническая форма обработчика                   |
| `src/Cli/PlatformCli.php`                     | 2150  | `metrics`, `trace` и все команды                 |

| Константа или предел                       | Значение                             |
| ------------------------------------------ | ------------------------------------ |
| Окно спанов в памяти                       | 1000 (`Trace::DEFAULT_MEMORY_SPANS`) |
| Проверка входящего идентификатора          | 8–128 символов `[A-Za-z0-9._-]`      |
| Округление среднего                        | 6 знаков                             |
| Знаков после запятой в тексте метрик       | 4                                    |
| Интервал снимка воркеров                   | 0.5 с                                |
| Интервал вычистки `resolvedAt`             | 0.05 с                               |
| TTL завершённых задач в реестре воркеров   | 300 с                                |
| Имён метрик-констант в реестре             | 21                                   |
| Строк в снимке `metrics` без пула и памяти | 14                                   |

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что                     | Значение                                                   |
| ----------------------- | ---------------------------------------------------------- |
| `ch24-metrics-registry` | 65 строк; счётчик 2, показатель 3, длительность `0.2000`   |
| `ch24-trace-chain`      | 105 строк; 4 звена под одним `request_id`; отказ в цепочке |
| `ch24-bounded-window`   | 76 строк; в памяти 3, на диске 5, выброшенные целы         |
| `ch24-reporter-text`    | 81 строка; 14 строк снимка; `0.3750` — среднее на чтении   |

## 8. Упражнения

1. **Прослежите.** Запрос `POST /orders` получил `201`, задание выполнилось
   через минуту и упало один раз, потом прошло. Сколько звеньев в цепочке, какие
   у них операции и что стоит в `attempt` у двух звеньев `job.execute`? Почему
   оба звена принадлежат одному `request_id`?
2. **Докажите гипотезу.** Инженер предлагает хранить спаны только в памяти и
   отдавать цепочку из памяти процесса. Приведите сценарий из главы 23, в
   котором такой сервис не сможет ответить на вопрос оператора, и обоснуйте,
   какой из трёх соблазнов §1 это решение реализует.
3. **Найдите правило.** Почему `record()` отказывается записывать спан **вне**
   области запроса, а не подставляет идентификатор вроде `unknown`? Какие
   вопросы оператора такой спан помог бы задать, а на какие — нет?
4. **Сравните.** Глубина очереди читается из журнала, а счётчик HTTP-запросов —
   из памяти процесса. Сформулируйте критерий, по которому для каждой величины
   выбирается источник, и примените его к `workers.active` (он читается у пула)
   и к `cache.hit` (он считается в процессе).
5. **Спроектируйте тест.** В эксперименте 2 цепочка собирается из трёх объектов
   над одним журналом. Как проверить «одна цепочка на всю систему» **через
   HTTP**, не полагаясь на время и не читая pid-ы? Какие швы для этого нужны, и
   что придётся подделать?
6. **Разберитесь в цене.** Среднее вместо перцентиля — это ограничение или
   ошибка? Приведите нагрузку, на которой среднее даёт прямо неверный вывод, и
   оцените, что стоило бы перцентилю: память, точность или сложность снимка.

## Что дальше

Платформа целиком собрана, наблюдаема и честно падает. Этим закрывается пятая
часть: шесть компонентов, одна система, и каждый отчёт у неё — свой. Дальше
начинается инженерная практика, и первая её глава — про то, без чего все
предыдущие двадцать три были бы наборами мнений: **измерение**. Глава 25 берёт
одну оптимизацию, снимает базовую линию на честных числах — и показывает, как
легко измерить не то, что собирался.
