# Глава 23. Как распространяются отказы

> Проект:
> [`php-systems-platform`](https://github.com/Researcher86/php-systems-platform)
> (коммит `9a6a916`). Нужно знать: пул воркеров (→ главы 6–7), очередь и повторы
> (→ главы 8–9), границы компонентов (→ глава 22). Продолжение — что видно
> снаружи, когда всё это падает (→ глава 24).
>
> Отказ компонента — это событие **системы**, а не компонента: у каждого есть
> путь к соседям. Глава про эти пути.

## 1. Проблема

Начало этой части было про то, как поставить границу. Это — про то, что через
неё проходит, когда что-то ломается. Три отказа с виду несвязанные:

- HTTP-воркер обрабатывает по запросу за раз (→ глава 5). Пока он пишет заказ в
  базу, следующий клиент ждёт. Если база тормозит, тормозит весь сервер, и ни
  один запрос не виноват.
- Воркер пула умирает посреди задания (OOM, сегфолт). Запрос, который он держал,
  никто не выполнит.
- Очередь заполняется быстрее, чем консьюмер её разбирает. Каждый новый заказ —
  это ещё одна запись в журнал и ещё одно обещание, которое система уже не
  успевает держать.

Общее у них одно: **отказ не локальный**. Проблема в базе становится проблемой
HTTP-пути; смерть воркера становится потерянным заданием; медленный консьюмер
становится отказом на записи. Поэтому вопрос главы не «как сделать, чтобы не
падало» — падать будет всё, — а **что делать, когда уже упало**. У этого вопроса
ровно три неправильных ответа, и все три встречались в истории этой лаборатории:

- **Проглотить.** Обработать исключение и ответить так, будто работа сделана.
  Опубликованное задание, которое на самом деле не выполнилось, — худший из
  отказов: он выглядит успехом.
- **Заблокироваться.** Дождаться, пока очередь разберут, прямо в HTTP-воркере. У
  сервера один поток, поэтому «подождать» здесь означает «остановить всех».
- **Уронить молча.** Отбросить работу, которую некуда положить, и не сказать об
  этом никому.

Правильный ответ тоже один, и он в двух частях. Первая: **отказ должен быть
виден на своей границе и не превращаться в чужой** — упавшее задание остаётся
упавшим заданием и не делает вид, что воркер цел; занятая очередь отвечает
отказом на своей границе, а не копится до сбоя. Вторая: **степень деградации
зависит от того, чем данные являются** — производные данные (кеш) можно потерять
и пережить, авторитетные (строка заказа) — нельзя.

Отсюда две оси, которые нельзя путать, и они разведены ещё в главах 6–9:

- **Отказ задания** — приговор попытке: она не удалась, её повторить или
  признать безнадёжной;
- **Отказ воркера** — исчезновение процесса: пул это замечает, замещает воркера
  и признаёт провалившимся только тот запрос, который умерший воркер держал.

Если их смешать, получается либо вечное «задание выполняется» на мёртвом
воркере, либо падение всего пула из-за одного плохого сообщения.

## 2. Устройство

### Матрица отказов

В `docs/failure-matrix.md` каждый отказ описан четырьмя полями: что ломается,
ожидаемое поведение, чем это наблюдается и как воспроизводится. Основные строки
— в этой таблице.

| Что ломается                        | Ожидаемое поведение                                            |
| ----------------------------------- | -------------------------------------------------------------- |
| Воркер умирает                      | пул замечает, убирает мёртвый pid, форкает замену              |
| Задание падает (можно повторить)    | повтор по политике до `max_attempts`, затем `FAILED`           |
| Задание с невалидным payload        | провал с первой попытки, без повторов                          |
| Очередь на пределе                  | `429` до разбора тела и до записи в базу                       |
| База недоступна                     | `500` на HTTP-границе, запись не сделана                       |
| Кеш недоступен                      | обход кеша, чтение из базы, отказ не возвращается клиенту      |
| Служба поиска просит переиндексацию | `503` и публикация `product.reindex` (→ глава 21)              |
| Порт занят при старте               | `serve` выходит ненулевым кодом и не оставляет ничего за собой |
| Клиент оборвал соединение           | запрос завершается безопасно, соединение возвращается          |

Строки выглядят не связанными, но их объединяет одно правило: **никогда не
молчать**. Разберём по частям.

### Две оси, которые нельзя путать

```text
a worker dies on request
    worker.crash ──► SIGKILL ──► Master reaps it ──► the held request fails
                                 (worker_crashed)        with worker_crashed
                                 └──► a replacement is forked; the pool keeps serving
    the job's own verdict: none — the job was never judged

a job throws
    a handler throws ──► the attempt fails ──► shouldRetry()?
                          │                      ├── yes ──► back to READY, attempts+1
                          │                      └── no  ──► FAILED, attempts stay
                          └──► the worker is fine; it takes the next delivery
```

Слева воркер исчезает — и **приговора заданию нет**: задача, которую держал
мёртвый воркер, проваливается как выполнение, а не как приговор содержимому.
Справа задание падает само, воркер остаётся жив и берёт следующую доставку.
Разница видна в счётчиках: первый случай двигает замену воркера, второй —
`attempts` задания.

### Backpressure: отвергнуть, а не ждать и не бросать

```text
POST /orders arrives
    │
    ├── BackpressurePolicy::evaluate()  ── at capacity? ──► 429 + Retry-After: 1
    │      depth from the journal                              (nothing written,
    │      (cross-process)                                      nothing enqueued)
    │
    └── not at capacity ──► decode ──► validate ──► write the row ──► publish the job
```

Проверка стоит **до** декодирования тела, до валидации и до записи: занятая
очередь — это ответ, а не отложенная работа. `depth` берётся из журнала, а не из
счётчика в памяти: HTTP-процесс только добавляет задания, а разбирает их другой
процесс, поэтому его собственная память умела бы только расти.

Ответ `429` несёт оба числа (`queueDepth`, `queueMaxSize`) и `Retry-After`,
чтобы клиент мог решить, ждать ему или уходить, — а не догадываться.

### Отказ задания: приговор и повтор — разные решения

`shouldRetry()` разделяет «попытку» и «содержимое». `max_attempts` — бюджет
попыток; но бюджет бессмысленен для payload, который не может пройти
`validate()`: сколько раз ни повторяй, он не станет валидным. Поэтому решение о
повторе (после сбоя) и решение об исходе (после `max_attempts`) — два разных
места, и первое может отказать раньше второго.

Неизвестный тип задания обрабатывается так же, как битый payload: это
**проваленная попытка**, а не тихий пропуск. Правило «не молчать» здесь важнее
удобства: задание, про которое некому сказать, что с ним делать, должно упасть
видимо.

### Деградация там, где данные производные

Три режима работы с соседом, и они разные:

- **Кеш.** Кеш — производные данные, база остаётся источником истины. Клиент
  кеша недоступен — это `CacheClientException` у вызывающего, и вызывающий
  решает: на пути чтения он обслуживает строку из базы и считает обход
  (`bypass`); на пути записи — отвечает `201`, а не отказом, потому что заказ
  уже записан. `CACHE_ENABLED=0` — **не** «кеш лежит»: это платформа без кеша,
  где каждая операция — промах без единого обращения к сокету.
- **База.** Строка заказа авторитетна. Исключение из-под домена проходит наверх
  к `Application`, где становится `500`; клиент не получает деталей, а оператор
  получает throwable в stderr. Валидация — исключение: явный
  `InvalidArgumentException` становится `400` до записи.
- **Поиск.** Производные данные с оговоркой: он не просто «может отстать», он
  сам сообщает о неготовности (→ глава 21). Тогда на его границе — `503`, а не
  пустой результат.

### Никогда не молчать: два правила в коде

Первое — **обработчик не глотает своё исключение**: упавшее задание обязано
выйти наружу, чтобы очередь посчитала попытку и сохранила `last_error`. Второе —
**воркер, форкнутый из родителя, сам открывает свои соединения**: унаследованный
сокет с двумя говорящими — это не оптимизация, а способ получить отказ, который
никто не сможет локализовать.

### Чего это стоит

- **Отказ по загрузке бьёт по клиенту.** `429` — честный, но всё же отказ: при
  слишком низком `max_size` пик записи превращается в отказы, хотя система
  работоспособна.
- **Повторы усиливают сбойную зависимость.** Политика повторов без потолка
  превращает медленную базу в шторм повторов; здесь потолок — `max_attempts`.
- **Замена воркера прячет утечку.** Если воркер умирает на каждом задании, пул
  исправно форкает замену, и внешне всё живо. Замена лечит симптом;
  повторяющиеся смерти — это сигнал, а не норма.
- **Честность стоит доступности.** `503` при незавершённой переиндексации —
  правильный ответ и неудобный: система «работает», но отказывает.
- **Границ стало больше, чем отказов.** Почти каждое поведение в этой главе —
  контракт между двумя процессами, поэтому появились таймауты на каждый (2 с на
  очередь, 5 с на задание, 30 с на выполнение), и их надо держать
  согласованными.

## 3. Код

### Решение о загрузке — отдельный класс

`BackpressurePolicy` не блокирует и не откладывает: `depth` читается из журнала
(то есть виден другому процессу), а на пределе решение — отказ.

```php
/**
 * PLAN Step 17: a configurable MAX_QUEUE_SIZE and one explicit policy at it -
 * reject. No blocking (the single HTTP worker cannot drain the queue itself)
 * and no silent delay: a producer asking while the queue is full is told so
 * immediately.
 *
 * `depth` comes from the journal, the same cross-process count
 * `GET /queue/status` reports. An in-memory counter would be wrong here: the
 * HTTP process only ever pushes (queue:consume pops, in another process), so
 * its own count could only grow.
 */
final readonly class BackpressurePolicy
{
    public function __construct(
        private QueueJournal $journal,
        private int $maxSize,
    ) {
    }

    public function evaluate(): BackpressureDecision
    {
        $depth = $this->journal->snapshot()['depth'];

        return new BackpressureDecision(
            depth: $depth,
            maxSize: $this->maxSize,
            atCapacity: $depth >= $this->maxSize,
        );
    }
```

[`src/Queue/BackpressurePolicy.php#L7-L35`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/BackpressurePolicy.php#L7-L35)

**Перевод комментариев:**
- `BackpressurePolicy` (PHPDoc): шаг 17 плана: настраиваемый `MAX_QUEUE_SIZE` и
  одна явная политика на пределе — отвергнуть. Никакой блокировки (единственный
  HTTP-воркер не может сам разобрать очередь) и никакой тихой задержки:
  продюсер, спросивший при полной очереди, сразу получает ответ. `depth` берётся
  из журнала — тот же межпроцессный счётчик, который отдаёт `GET /queue/status`.
  Счётчик в памяти здесь был бы неверен: HTTP-процесс только добавляет
  (`queue:consume` разбирает, в другом процессе), поэтому его собственный
  счётчик мог бы только расти.

Результат несёт числа вместе с вердиктом — именно поэтому отклонённый запрос
может объясниться, а не ответить пустым `429`.

```php
/**
 * One answer to "can the queue take more work right now" - PLAN Step 17.
 *
 * Carries the numbers along with the verdict so a caller that rejects a
 * request can say why, instead of a bare 429.
 */
final readonly class BackpressureDecision
{
    public function __construct(
        public int $depth,
        public int $maxSize,
        public bool $atCapacity,
    ) {
    }
```

[`src/Queue/BackpressureDecision.php#L7-L20`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/BackpressureDecision.php#L7-L20)

**Перевод комментариев:**
- `BackpressureDecision` (PHPDoc): один ответ на вопрос «может ли очередь сейчас
  взять ещё работу» — шаг 17 плана. Несёт числа вместе с вердиктом, чтобы
  вызывающий, отклоняя запрос, мог объяснить причину, а не вернуть голый `429`.

### Проверка стоит до всего остального

Обработчик `POST /orders` сначала спрашивает очередь, и только потом читает
тело. Порядок здесь и есть политика: при перегрузке в системе **ничего не
меняется**.

```php
/**
 * POST /orders - the synchronous write path: parse {"customer", "amount",
 * "product"?}, let OrderService validate and persist it, answer 201 with the
 * order and its Location. A bad payload is a 400; failures below the domain
 * (a dead database) are left for the Application boundary to turn into 500.
 *
 * Backpressure is checked BEFORE anything else, so an overloaded queue is a
 * 429 with nothing written and nothing enqueued - reject, not block or drop.
 * A null policy means no queue to protect.
 *
 * Cache: populate-on-write. A fresh UUID cannot collide with an existing
 * entry, so the new row is cached as-is; an unreachable cache is a bypass,
 * never a failed write.
 */
final readonly class OrderCreateHandler
{
    public function __construct(
        private OrderService $orders,
        private CacheService $cache,
        private ?BackpressurePolicy $backpressure = null,
        private ?Trace $trace = null,
    ) {
    }

    /**
     * @param array<string, string> $params route parameters (none for /orders)
     */
    public function __invoke(Request $request, array $params): Response
    {
        $decision = $this->backpressure?->evaluate();

        if ($decision?->atCapacity === true) {
            return Response::json([
                'error' => 'Queue is at capacity.',
                'queueDepth' => $decision->depth,
                'queueMaxSize' => $decision->maxSize,
            ], 429, ['Retry-After' => '1']);
        }
```

[`src/Application/Handlers/OrderCreateHandler.php#L16-L53`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Handlers/OrderCreateHandler.php#L16-L53)

**Перевод комментариев:**
- `OrderCreateHandler` (PHPDoc): `POST /orders` — синхронный путь записи:
  разобрать `{"customer", "amount", "product"?}`, дать `OrderService`
  провалидировать и сохранить, ответить `201` с заказом и его `Location`. Плохой
  payload — это `400`; отказы ниже домена (мёртвая база) остаются границе
  `Application`, чтобы та превратила их в `500`. Обратное давление проверяется
  ДО всего остального, поэтому перегруженная очередь — это `429`, при котором
  ничего не записано и ничего не поставлено в очередь: отвергнуть, а не
  блокироваться или отбрасывать. Нулевая политика означает, что защищать нечего.
  Кеш: заполнение при записи. Свежий UUID не может столкнуться с существующей
  записью, поэтому новая строка кешируется как есть; недоступный кеш — это
  обход, а не неудачная запись.
- `__invoke()` (PHPDoc): `@param array<string, string> $params` — параметры
  маршрута (для `/orders` их нет).

А ниже — вторая половина того же класса: недоступный кеш на пути записи
становится обходом, а не отказом, потому что строка уже записана.

```php
try {
    $this->cache->setOrder($order);
} catch (CacheClientException) {
    $this->cache->counters()->bypasses++;
}
```

[`src/Application/Handlers/OrderCreateHandler.php#L80-L84`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Handlers/OrderCreateHandler.php#L80-L84)

### Приговор заданию: повтор — не то же самое, что исход

`shouldRetry()` — единственное место, где спрашивается «а могло ли это вообще
получиться». Бюджет попыток (`max_attempts`) к содержимому безразличен; эта
функция — нет.

```php
/**
 * The retry-eligibility hook JobDispatcher consults after a failed
 * attempt, before its attempts-remaining check: a payload validate()
 * rules out can never succeed, however many attempts are left.
 *
 * @return Closure(QueueJob, Throwable): bool
 */
public static function shouldRetry(): Closure
{
    return static fn (QueueJob $job, Throwable $exception): bool => self::validate($job->getType(), $job->getPayload()) === null;
}
```

[`src/Queue/JobRegistry.php#L76-L86`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/JobRegistry.php#L76-L86)

**Перевод комментариев:**
- `shouldRetry()` (PHPDoc): хук проверки права на повтор, который
  `JobDispatcher` спрашивает после неудачной попытки, до проверки остатка
  попыток: payload, который отсекает `validate()`, не может стать успешным,
  сколько бы попыток ни осталось.

### Падение воркера: инъекция как способ проверить путь

Чтобы разобрать ось «воркер умер», а не только её пережить, платформа умеет
убивать воркер по требованию. `worker.crash` — задача пула, и она существует
только при включённой инъекции отказов.

```php
/**
 * PLAN Step 22's pool-side failure injection: the `worker.crash` task that
 * makes a worker die on demand, so the crash path - Master reaps it, the
 * request it held fails with worker_crashed, and a replacement is forked -
 * can be reproduced instead of only survived.
 *
 * Killing the worker with SIGKILL is deliberate: no cleanup, no chance to
 * answer, no chance to signal anything - exactly what an OOM-kill or a
 * segfault looks like from the pool's side. The Master's SIGCHLD handler
 * (reapCrashedWorkers) is the only witness, which is the point: this task
 * exists so a crash is triggered on demand against the machinery that
 * already detects and replaces crashes.
 *
 * PLAN: "Failure injection should only be enabled in development/demo
 * mode." The wire task is therefore armed only when the platform launches
 * with a development/demo environment (config failure_injection.enabled);
 * a production pool answers failure_injection_disabled instead of dying.
 */
```

[`src/Workers/WorkerFailureTasks.php#L10-L27`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerFailureTasks.php#L10-L27)

**Перевод комментариев:**
- `WorkerFailureTasks` (PHPDoc): инъекция отказов на стороне пула из шага 22
  плана: задача `worker.crash`, которая заставляет воркер умереть по команде,
  чтобы путь краха — мастер пожинает его, запрос, который он держал, падает с
  `worker_crashed`, а замена форкается — можно было воспроизвести, а не только
  пережить. Убийство воркера через `SIGKILL` намеренно: никакой очистки, ни
  шанса ответить, ни шанса что-то сообщить — именно так OOM-kill или сегфолт
  выглядят со стороны пула. Единственный свидетель — обработчик `SIGCHLD`
  мастера (`reapCrashedWorkers`), и в этом суть: задача существует, чтобы крах
  вызывался по требованию против механизма, который уже обнаруживает и заменяет
  падения. PLAN: «инъекция отказов должна включаться только в режиме
  development/demo». Поэтому проводная задача взводится, только когда платформа
  стартует в окружении development/demo (конфиг `failure_injection.enabled`);
  production-пул отвечает `failure_injection_disabled` вместо того, чтобы
  умирать.

Смерть — именно `SIGKILL`: ни очистки, ни ответа, ни шанса что-то сообщить, как
при OOM или сегфолте. Единственный свидетель — собственный обработчик `SIGCHLD`
мастера.

```php
private function crash(): Response
{
    if (!$this->injectionEnabled) {
        return Response::error('failure_injection_disabled');
    }

    // Never returns: the process is gone before it could answer, so the
    // only report of this death is the pool's own crash-reap path. The
    // exit() is unreachable fallback for a platform where posix_kill is
    // somehow unavailable.
    posix_kill(posix_getpid(), SIGKILL);
    exit(137);
}
```

[`src/Workers/WorkerFailureTasks.php#L46-L58`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerFailureTasks.php#L46-L58)

**Перевод комментариев:**
- `// Never returns: the process is gone before it could answer, so the` —
  процесс исчезает раньше, чем смог бы ответить, поэтому единственный отчёт об
  этой смерти — собственный путь мастера по пожинанию краха. `exit()` —
  недостижимый запасной вариант для платформы, где `posix_kill` почему-то
  недоступен.

Клиентская половина сценария — `WorkerFailureInjector`: один вызов гоняет всю
последовательность и записывает доказательства каждой фазы с самой бухгалтерии
пула, а не из своих догадок.

```php
/**
 * PLAN Step 22's client side of the worker-crash scenario, shared by
 * `failure:demo`, POST /debug/fail-worker and the integration tests.
 *
 * One call drives the whole PLAN sequence and records each phase's evidence
 * straight off the pool's own bookkeeping:
 *
 *     worker crashes          -> the worker.crash task SIGKILLs a worker
 *     worker manager detects  -> the client hears worker_crashed (the
 *                                Master failed the request the dead worker
 *                                held - reapCrashedWorkers)
 *     worker removed          -> WorkerPoolClient::stats() no longer lists
 *                                the dead pid
 *     replacement started     -> stats() lists a pid that was not there
 *                                before (reapDeadWorkers forked it)
 *
 * stats() is answered by the Master itself, never by a worker, so taking a
 * read after the crash costs no pool capacity and observes the replacement
 * even while the pool is one short.
 */
```

[`src/Workers/WorkerFailureInjector.php#L14-L33`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerFailureInjector.php#L14-L33)

**Перевод комментариев:**
- `WorkerFailureInjector` (PHPDoc): клиентская сторона сценария краха воркера из
  шага 22 плана, общая для `failure:demo`, `POST /debug/fail-worker` и
  интеграционных тестов. Один вызов прогоняет всю последовательность плана и
  записывает доказательства каждой фазы прямо из бухгалтерии пула:
  `worker.crash` убивает воркера через `SIGKILL`; менеджер воркеров замечает
  это, и клиент слышит `worker_crashed` (мастер проваливает запрос, который
  держал мёртвый воркер, — `reapCrashedWorkers`); удалённый воркер исчезает из
  `WorkerPoolClient::stats()`; запускается замена — `stats()` показывает pid,
  которого раньше не было (`reapDeadWorkers` его форкнул). `stats()` отвечает
  сам мастер, а не воркер, поэтому чтение после краха не стоит ёмкости пула и
  видит замену, даже когда пул недосчитывает одного.

Показательно, что три разных исхода на проводе (`worker_crashed`, оборванное
соединение, таймаут) сведены к одному наблюдаемому факту, а истина берётся из
`stats()`: «мёртвый pid ушёл из списка или нет» — это факт, а то, каким именно
образом клиент об этом узнал, — деталь.

```php
/**
 * Crash one worker and wait for the pool to notice and replace it.
 *
 * @return array{error: string, crash_detected: bool, detected_ms: ?float, crashed_pid: ?int, worker_removed: bool, removed_ms: ?float, replacement_started: bool, replaced_ms: ?float, replacement_pid: ?int, pool_size: int, before: list<int>, after: list<int>}
 */
public function crashOneWorker(): array
{
    $before = $this->pids() ?? [];
    $started = microtime(true);

    $error = 'no_error';

    try {
        $this->client->call(new WorkerRequest('worker.crash'));
    } catch (ServerErrorException $e) {
        // The pool's regular answer: the worker that took the request
        // was reaped before it could answer.
        $error = $e->error;
    } catch (ConnectionClosedException | ConnectionFailedException) {
        // A race: the crash was seen through the connection instead of
        // through the worker_crashed message. The observable fact is the
        // same one the demo reports - the worker died on request.
        $error = 'connection_dropped';
    } catch (RequestTimedOutException) {
        // The master did not answer within the request budget. Whatever
        // the reason, the poll loop below still gets the ground truth
        // off stats(): the dead pid either leaves the list or it does
        // not, and the report reflects what the pool's bookkeeping says.
        $error = 'request_timed_out';
    }

    $detectedMs = (microtime(true) - $started) * 1000;

    $removedMs = null;
    $replacedMs = null;
    $replacementPid = null;
    $after = $before;

    $deadline = microtime(true) + self::REPLACEMENT_DEADLINE_SECONDS;
```

[`src/Workers/WorkerFailureInjector.php#L45-L83`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerFailureInjector.php#L45-L83)

**Перевод комментариев:**
- `crashOneWorker()` (PHPDoc): убить одного воркера и дождаться, пока пул
  заметит это и заменит его.
- `// The pool's regular answer: the worker that took the request` — обычный
  ответ пула: воркер, взявший запрос, был пожнат раньше, чем успел ответить.
- `// A race: the crash was seen through the connection instead of` — гонка:
  крах увидели через соединение, а не через сообщение `worker_crashed`.
  Наблюдаемый факт тот же, что сообщает демо, — воркер умер по запросу.
- `// The master did not answer within the request budget. Whatever` — мастер не
  ответил в пределах бюджета запроса. Какова бы ни была причина, цикл опроса
  ниже всё равно получает истину из `stats()`: мёртвый pid либо уходит из
  списка, либо нет, и отчёт отражает то, что говорит бухгалтерия пула.

### Границы: клиент получает статус, оператор — детали

Один плохой запрос не должен останавливать сервер, но и не должен растворять
ошибку. `Application` разводит исходы на самой границе: промах маршрута — `404`,
не тот метод — `405`, всё остальное — `500`.

```php
try {
    return $this->toHttpResponse($this->router->dispatch($internal));
} catch (RouteNotFoundException) {
    return $this->error(HttpStatusCode::NOT_FOUND);
} catch (MethodNotAllowedException $e) {
    $headers = new Headers();
    $headers->set('Allow', implode(', ', $e->allowed));

    return $this->error(HttpStatusCode::METHOD_NOT_ALLOWED, $headers);
} catch (Throwable $e) {
    $this->reportHandlerFailure($request, $e);

    return $this->error(HttpStatusCode::INTERNAL_SERVER_ERROR);
}
```

[`src/Application/Application.php#L94-L107`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Application.php#L94-L107)

И то, ради чего этот код написан именно так: наружу — голый статус, внутрь —
throwable. Сообщение исключения регулярно содержит строку соединения или путь к
файлу, поэтому оно уходит в stderr оператора и никогда в тело ответа.

```php
/**
 * The client gets a bare 500; the operator gets the throwable on STDERR.
 * Never the other way round: an exception message is the one thing on
 * this path that routinely holds a connection string or a file path.
 */
private function reportHandlerFailure(HttpRequest $request, Throwable $e): void
{
    $logger = $this->logger ?? new StderrLogger();

    $logger->log(sprintf(
        'unhandled %s: %s: %s at %s:%d',
        $request->method->value,
        $request->path(),
        $e::class,
        $e->getFile(),
        $e->getLine(),
    ));
    $logger->log($e->getTraceAsString());
}
```

[`src/Application/Application.php#L110-L128`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Application/Application.php#L110-L128)

**Перевод комментариев:**
- `reportHandlerFailure()` (PHPDoc): клиент получает голый `500`; оператор
  получает throwable в `STDERR`. Никогда наоборот: сообщение исключения —
  единственное на этом пути, что регулярно содержит строку соединения или путь к
  файлу.

### Производное можно потерять

Докблок `CacheService` формулирует, кто в паре главный: кеш — производное
состояние, база — источник истины, а «деградировать или нет» решает вызывающий,
а не клиент.

```php
/**
 * The platform's facade over one php-mini-cache connection, plus the
 * read-path bookkeeping.
 *
 * The cache is derived state: it only stores an order's JSON keyed by id,
 * and the database stays the source of truth. Client failures surface as
 * CacheClientException; degrading from them is the caller's decision.
 *
 * Disabled (CACHE_ENABLED=0) is not the same as down: a disabled cache
 * answers every lookup with a miss without touching a socket, so "what does
 * this read cost without a cache" is measured without timeout noise.
 */
```

[`src/Cache/CacheService.php#L14-L25`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cache/CacheService.php#L14-L25)

**Перевод комментариев:**
- `CacheService` (PHPDoc): фасад платформы над одним соединением php-mini-cache
  плюс учёт на пути чтения. Кеш — производное состояние: он хранит только JSON
  заказа по ключу id, а база остаётся источником истины. Отказы клиента
  проявляются как `CacheClientException`; решать, деградировать ли из-за них, —
  дело вызывающего. Выключенный кеш (`CACHE_ENABLED=0`) — не то же самое, что
  лежащий: выключенный кеш отвечает на каждое обращение промахом, не трогая
  сокет, поэтому «сколько стоит это чтение без кеша» измеряется без шума
  таймаутов.

На пути чтения это видно в деталях: недоступный кеш **не считается** ни
промахом, ни попаданием — он бросает, и вызывающий записывает обход.

```php
/**
 * The cached payload for an order, or null on a miss. Every lookup
 * counts exactly once, as a hit or a miss - a disabled cache and an
 * entry that no longer parses (not servable) are misses too - so
 * hits + misses always equals lookups. An unreachable cache counts
 * neither: it throws, and the caller records the bypass.
 *
 * @return array<string, mixed>|null
 *
 * @throws CacheClientException the cache is unreachable
 */
public function getOrder(string $id): ?array
{
    $json = $this->enabled ? $this->client->get(self::ORDER_KEY_PREFIX . $id) : null;
    $order = $json === null ? null : json_decode($json, true);

    if (!is_array($order)) {
        $this->recordMiss();

        return null;
    }

    $this->counters->hits++;
    $this->metrics?->increment(MetricsRegistry::CACHE_HITS);
    $this->metrics?->increment(MetricsRegistry::CACHE_OPERATIONS);

    return $order;
}
```

[`src/Cache/CacheService.php#L63-L90`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cache/CacheService.php#L63-L90)

**Перевод комментариев:**
- `getOrder()` (PHPDoc): закешированный payload заказа или `null` при промахе.
  Каждое обращение считается ровно один раз — как попадание или промах;
  выключенный кеш и запись, которая больше не парсится (не обслуживаема), — тоже
  промахи, поэтому `hits + misses` всегда равно числу обращений. Недоступный кеш
  не считается ни тем, ни другим: он бросает исключение, а вызывающий записывает
  обход. `@return array<string, mixed>|null`; `@throws CacheClientException` —
  кеш недоступен.

### Воркер открывает соединения сам

`JobExecutor` работает внутри форкнутого процесса, и это определяет всё: ни одно
соединение не наследуется от родителя, каждое строится лениво на первом задании.

```php
/**
 * The handler a queue worker runs - the worker's entry point into the
 * platform, and the counterpart of WorkerTasks on the worker-pool side.
 *
 * It runs inside a forked worker process, so every connection is built
 * lazily there, on the first job, never inherited from the parent: two
 * processes speaking on one socket is how protocols die.
 *
 * Job metadata (started_at/completed_at/last_error) is stamped by the
 * component's JobDispatcher on its own Job; this class only runs the job.
 */
```

[`src/Queue/JobExecutor.php#L22-L32`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/JobExecutor.php#L22-L32)

**Перевод комментариев:**
- `JobExecutor` (PHPDoc): обработчик, который выполняет воркер очереди, — вход
  воркера в платформу и аналог `WorkerTasks` на стороне пула воркеров. Он
  работает внутри форкнутого процесса воркера, поэтому каждое соединение
  строится там лениво, на первом задании, и никогда не наследуется от родителя:
  два процесса, говорящие через один сокет, — так протоколы и умирают.
  Метаданные задания (`started_at`/`completed_at`/`last_error`) проставляет
  `JobDispatcher` компонента на своём `Job`; этот класс только выполняет
  задание.

И исключение задания не глотается: попытка записывается в трейс и
перебрасывается наружу, где очередь уже сама решает, повтор это или приговор.

```php
    try {
        $this->registry ??= new JobRegistry();
        $this->registry->execute($job, $context);
    } catch (Throwable $e) {
        $this->recordJobSpan($job, $startedAt, ['outcome' => 'failed', 'error' => $e->getMessage()]);

        throw $e;
    }

    $this->recordJobSpan($job, $startedAt, ['outcome' => 'completed']);

    return null;
}
```

[`src/Queue/JobExecutor.php#L89-L101`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/JobExecutor.php#L89-L101)

### Консьюмер: остановиться, не потеряв

Цикл консьюмера останавливается по флагу (поставил его обработчик сигнала), а
вся тяжёлая остановка происходит в одном `finally` на его собственном потоке.

```php
/**
 * Run the loop while $keepRunning returns true and stop() has not been
 * called - the shape for a benchmark, which decides on its own when the
 * work is finished. The workers are shut down on the way out, even when
 * starting them failed halfway.
 *
 * @param Closure(): bool $keepRunning
 */
public function runWhile(Closure $keepRunning): void
{
    try {
        $this->dispatcher->start();
        $nextResync = $this->clock->now() + $this->resyncInterval;

        while (!$this->stopping && $keepRunning()) {
            if ($this->clock->now() >= $nextResync) {
                $this->resync();
                $nextResync = $this->clock->now() + $this->resyncInterval;
            }

            $this->tick();
        }
    } finally {
        $this->dispatcher->shutdown($this->shutdownGrace);
    }
}
```

[`src/Queue/QueueConsumer.php#L88-L113`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/QueueConsumer.php#L88-L113)

**Перевод комментариев:**
- `runWhile()` (PHPDoc): крутить цикл, пока `$keepRunning` возвращает true и
  `stop()` не вызван, — форма для бенчмарка, который сам решает, когда работа
  закончена. Воркеры останавливаются на выходе, даже если их запуск сорвался на
  середине. `@param Closure(): bool $keepRunning`.

Отдельная забота — задания, опубликованные другим процессом (HTTP-путь пишет в
тот же журнал). Их надо принять, но нельзя принять дважды, поэтому «терминальное
задание, увиденное впервые» — это не работа, а завершение, которое мы
пропустили, и оно вычёркивается из известных.

```php
/**
 * Admit every job another process published since the last pass. A job
 * first seen already terminal finished while we were away and is not
 * work; a DELAYED one keeps its own availableAt (the queue ignores the
 * push delay for anything but a CREATED job).
 */
private function resync(): void
{
    foreach ($this->tail->read() as $id => $data) {
        $job = Job::fromArray($data);

        if ($job->getState()->isTerminal()) {
            unset($this->known[$id]);

            continue;
        }

        if (isset($this->known[$id])) {
            continue;
        }

        $this->known[$id] = true;
        $this->queue->push($job);
    }
}
```

[`src/Queue/QueueConsumer.php#L154-L178`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/QueueConsumer.php#L154-L178)

**Перевод комментариев:**
- `resync()` (PHPDoc): принять каждое задание, опубликованное другим процессом с
  прошлого прохода. Задание, впервые увиденное уже терминальным, завершилось,
  пока нас не было, и работой не является; `DELAYED` сохраняет свой
  `availableAt` (очередь игнорирует задержку публикации для всего, кроме задания
  в состоянии `CREATED`).

Наконец, сигнал только просит остановиться. Вся последовательность выхода — на
потоке цикла, поэтому `SIGTERM` не может прервать остановку на середине.

```php
public function run(): void
{
    pcntl_async_signals(true);
    $previous = [];

    foreach (self::SIGNALS as $signal) {
        $previous[$signal] = pcntl_signal_get_handler($signal);
        pcntl_signal($signal, $this->stop(...));
    }

    try {
        $this->runWhile(static fn (): bool => true);
    } finally {
        foreach ($previous as $signal => $handler) {
            pcntl_signal($signal, $handler);
        }
    }
}
```

[`src/Queue/QueueConsumer.php#L69-L86`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/QueueConsumer.php#L69-L86)

## 4. Эксперимент

### Подготовка

```bash
cd php-systems-platform
make build install
```

Все четыре эксперимента выполняются в контейнере `php-systems-platform`, проект
смонтирован в `/app`. Там, где нужен дочерний процесс, запускается настоящий:
мастер пула в эксперименте 2 и целая `serve` в эксперименте 4. Все выводы
детерминированы: каждый скрипт прогонялся дважды и совпал байт-в-байт. Pid-ы,
миллисекунды и номера портов в вывод не попадают: они меняются от прогона к
прогону, а проверяются здесь классификации, а не числа.

### 1. Два «нет»: приговор заданию

Два задания с одинаковым бюджетом в три попытки. Одно не может получиться
никогда: его payload не проходит проверку. Второе падает на каждой попытке, но в
принципе могло бы пройти. Первое обязано провалиться с **одной** попытки, второе
— потратить все три.

```bash
docker compose cp /tmp/ch23-verdicts.php php:/tmp/ch23-verdicts.php
docker compose exec php php /tmp/ch23-verdicts.php
```

```php
<?php

declare(strict_types=1);

/**
 * The two kinds of "no": a job that keeps failing retries its way to a
 * verdict, one that could never succeed is refused a retry at all.
 *
 * Both jobs are published into a real journal and drained by a real
 * consumer loop (`QueueConsumer::runWhile()`), with the component's own
 * dispatcher and its workers. The retry eligibility is the platform's own
 * `JobRegistry::shouldRetry()`, consulted exactly where the dispatcher
 * consults it: after a failure, before the attempts-remaining check.
 *
 * The platform's consumer forwards each job to the php-worker-pool Master
 * (see WorkerManager); here the handler runs in the job-queue worker
 * directly, so what is under test is the queue's retry-and-verdict logic
 * and not the pool.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch23-verdicts.php php:/tmp/ch23-verdicts.php
 *   docker compose exec php php /tmp/ch23-verdicts.php
 */

require '/app/vendor/autoload.php';

use PhpJobQueue\Dispatcher\JobDispatcher;
use PhpJobQueue\Job\Job;
use PhpJobQueue\Metrics\MetricsCollector;
use PhpJobQueue\Persistence\FileStorage;
use PhpJobQueue\Producer\JobFactory;
use PhpJobQueue\Producer\Producer;
use PhpJobQueue\Queue\InMemoryQueue;
use PhpJobQueue\Retry\FixedDelayRetry;
use PhpJobQueue\Support\SystemClock;
use PhpJobQueue\Worker\WorkerPool;
use PhpSystemsPlatform\Queue\JobRegistry;
use PhpSystemsPlatform\Queue\QueueConsumer;
use PhpSystemsPlatform\Queue\QueueJournal;
use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch23-verdicts-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

// Only the process that started the run may clean up: a forked job-queue
// worker inherits this shutdown function, and a worker deleting the journal
// its parent is still reading is exactly the kind of fork surprise the
// platform's own per-worker services avoid.
$owner = getmypid();

register_shutdown_function(static function () use ($directory, $owner): void {
    if (getmypid() !== $owner) {
        return;
    }

    OwnedProcess::removeTree($directory);
});

$logPath = $directory . 'queue.log';
$clock = new SystemClock();
$metrics = new MetricsCollector();
$storage = new FileStorage($logPath);
$queue = InMemoryQueue::restoreFromStorage($storage, $clock);
$producer = new Producer($queue, new JobFactory($clock, $metrics));

printf("the payload check, on its own\n");
printf("  validate('order.process', []): %s\n", JobRegistry::validate('order.process', []) ?? 'no opinion');
printf("  validate('demo.failing', []): %s\n", JobRegistry::validate('demo.failing', []) ?? 'no opinion');

$shouldRetry = JobRegistry::shouldRetry();
$failure = new RuntimeException('as if a worker had failed');

$foregone = $producer->dispatch('order.process', [], maxAttempts: 3);
$retryable = $producer->dispatch('demo.failing', [], maxAttempts: 3);

printf(
    "  a failed order.process is retried: %s\n",
    $shouldRetry($foregone, $failure) ? 'yes' : 'no',
);
printf(
    "  a failed demo.failing is retried: %s\n",
    $shouldRetry($retryable, $failure) ? 'yes' : 'no',
);

// What the real worker's handler does with these two types: it throws. The
// production path is WorkerJobs -> JobExecutor -> JobRegistry -> the job.
$pool = new WorkerPool(
    size: 1,
    handler: static function (Job $job): void {
        throw new RuntimeException(match ($job->getType()) {
            'demo.failing' => 'Injected failure (demo.failing).',
            default => 'The payload names no order.',
        });
    },
    metrics: $metrics,
);

$dispatcher = new JobDispatcher(
    queue: $queue,
    workerPool: $pool,
    retryPolicy: new FixedDelayRetry(0),
    clock: $clock,
    visibilityTimeout: 5,
    storage: $storage,
    metrics: $metrics,
    shouldRetry: JobRegistry::shouldRetry(),
);

$journal = new QueueJournal($logPath);
$consumer = new QueueConsumer(
    dispatcher: $dispatcher,
    queue: $queue,
    logPath: $logPath,
    clock: $clock,
    shutdownGrace: 5.0,
);

printf("\npublished two jobs, max attempts 3 each\n");
printf("  depth before the consumer runs: %d\n", $journal->snapshot()['depth']);

$consumer->runWhile(static fn (): bool => $journal->snapshot()['depth'] > 0);

$snapshot = $journal->snapshot();
printf(
    "after the consumer drained both: depth=%d completed=%d failed=%d retried=%d\n",
    $snapshot['depth'],
    $snapshot['completed'],
    $snapshot['failed'],
    $snapshot['retried'],
);

$rows = $journal->rows();
usort($rows, static fn (array $a, array $b): int => strcmp((string) $a['type'], (string) $b['type']));

foreach ($rows as $row) {
    printf(
        "  %-14s attempts=%d/%d state=%s\n",
        $row['type'],
        (int) $row['attempts'],
        (int) $row['maxAttempts'],
        $row['state'],
    );
}
```

**Перевод комментариев:**
- `ch23-verdicts.php` (PHPDoc): два вида «нет»: задание, которое продолжает
  падать, повторяет свой путь до приговора, а задание, которое никогда не могло
  бы получиться, вообще лишено повтора. Оба задания публикуются в настоящий
  журнал и разбираются настоящим циклом консьюмера
  (`QueueConsumer::runWhile()`), с собственным диспетчером компонента и его
  воркерами. Право на повтор — это собственная `JobRegistry::shouldRetry()`
  платформы, которую спрашивают ровно там же, где её спрашивает диспетчер: после
  отказа, до проверки остатка попыток. Консьюмер платформы пересылает каждое
  задание мастеру php-worker-pool (см. `WorkerManager`); здесь обработчик
  работает прямо в воркере очереди заданий, поэтому проверяется логика повторов
  и приговоров очереди, а не пул. Запуск внутри контейнера, где проект
  смонтирован в `/app`:
  `docker compose cp /tmp/ch23-verdicts.php php:/tmp/ch23-verdicts.php`;
  `docker compose exec php php /tmp/ch23-verdicts.php`.
- `// Only the process that started the run may clean up: a forked job-queue` —
  убирать за собой может только процесс, начавший прогон: форкнутый воркер
  очереди заданий наследует эту shutdown-функцию, а воркер, удаляющий журнал,
  который родитель ещё читает, — ровно та неожиданность форка, которой избегают
  собственные сервисы платформы на каждого воркера.
- `// What the real worker's handler does with these two types: it throws. The`
  — что делает настоящий обработчик воркера с этими двумя типами: он бросает
  исключение. Production-путь — `WorkerJobs` -> `JobExecutor` -> `JobRegistry`
  -> задание.

```text
the payload check, on its own
  validate('order.process', []): order.process payload is missing order_id.
  validate('demo.failing', []): no opinion
  a failed order.process is retried: no
  a failed demo.failing is retried: yes

published two jobs, max attempts 3 each
  depth before the consumer runs: 2
after the consumer drained both: depth=0 completed=0 failed=2 retried=1
  demo.failing   attempts=3/3 state=FAILED
  order.process  attempts=1/3 state=FAILED
```

Первые четыре строки — решение, принятое **до** очереди:
`validate('order.process', [])` возвращает причину, а `shouldRetry()` для того
же задания отвечает «нет». Для `demo.failing` мнения нет — и повтор разрешён.

Дальше то же самое происходит уже в очереди. `demo.failing` доходит до
`attempts=3/3`, то есть три доставки, и только потом становится `FAILED`.
`order.process` становится `FAILED` на `attempts=1/3`: две оставшиеся попытки не
потрачены, потому что тратить их не на что. `retried=1` в счётчике журнала
считает именно задания, которым понадобилась не одна доставка, и это ровно одно
задание из двух.

Обратите внимание, что оба задания всё-таки **видны**: `failed=2`, у каждого
свой `state`, а `last_error` сохранён. Отказ здесь — запись в журнале, а не
тишина. Именно на ней строится всё остальное: `GET /queue/status`, счётчики и
разбор инцидента.

### 2. Падение воркера — не падение задания

Здесь запускается настоящий мастер пула на своём сокете, а `worker.crash`
заставляет воркер убить себя `SIGKILL` прямо во время запроса. Платформенный
`WorkerFailureInjector` записывает каждую фазу с бухгалтерии мастера.

```bash
docker compose cp /tmp/ch23-worker-crash.php php:/tmp/ch23-worker-crash.php
docker compose exec php php /tmp/ch23-worker-crash.php
```

```php
<?php

declare(strict_types=1);

/**
 * A worker crash is not a job failure: the pool notices, the pool replaces,
 * and the pool keeps serving - the two recovery axes of the failure matrix
 * are separate.
 *
 * The pool here is the real one: `bin/worker.php` (a php-worker-pool Master)
 * started as a child on its own socket, with failure injection armed by
 * PLATFORM_ENV=dev. `worker.crash` makes the worker that takes the request
 * SIGKILL itself mid-request, which is what an OOM kill looks like from the
 * pool's side, and the platform's own `WorkerFailureInjector` records each
 * phase off the Master's bookkeeping: the crash is detected, the dead pid
 * leaves `stats()`, a pid that was not there before appears.
 *
 * Timings and pids are deliberately not printed: the numbers change every
 * run, and what the phases prove is the classification, not the millisecond.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch23-worker-crash.php php:/tmp/ch23-worker-crash.php
 *   docker compose exec php php /tmp/ch23-worker-crash.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\OwnedProcess;
use PhpSystemsPlatform\Workers\WorkerFailureInjector;
use PhpWorkerPool\Protocol\Request as WorkerRequest;
use PhpWorkerPool\Sdk\WorkerPoolClient;

$directory = sys_get_temp_dir() . '/ch23-crash-' . getmypid() . '/';

@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$socket = $directory . 'worker.sock';

$environment = array_merge((array) getenv(), [
    // Its own addresses and its own temp tree, so nothing here touches a
    // platform that may already be running.
    'TMPDIR' => $directory,
    'WORKER_POOL_SOCKET' => $socket,
    'WORKER_POOL_MIN' => '2',
    'WORKER_POOL_MAX' => '2',
    'WORKER_POOL_TIMEOUT' => '5',
    // The one switch that arms worker.crash.
    'PLATFORM_ENV' => 'dev',
]);

$master = OwnedProcess::start(
    [PHP_BINARY, '/app/bin/worker.php'],
    'worker',
    $directory . 'logs',
    $environment,
);

$up = OwnedProcess::waitForQuietly(
    static fn (): bool => OwnedProcess::endpointAnswers('unix://' . $socket, 0.2),
    10.0,
);

printf("the pool Master answers on its socket: %s\n", $up ? 'yes' : 'no');

$client = new WorkerPoolClient($socket, 5.0);

printf("workers before the crash: %d\n", count($client->stats()));

$injector = new WorkerFailureInjector($client);
$evidence = $injector->crashOneWorker();

printf("the crash was detected by the pool: %s\n", $evidence['crash_detected'] ? 'yes' : 'no');
printf("the dead worker left the pool's list: %s\n", $evidence['worker_removed'] ? 'yes' : 'no');
printf("a replacement was started: %s\n", $evidence['replacement_started'] ? 'yes' : 'no');
printf(
    "the replacement's pid is one that was not there before: %s\n",
    $evidence['replacement_pid'] !== null && !in_array($evidence['replacement_pid'], $evidence['before'], true) ? 'yes' : 'no',
);
printf("workers after the crash: %d\n", $evidence['pool_size']);

$answered = true;

try {
    $client->call(new WorkerRequest('ping'));
} catch (Throwable) {
    $answered = false;
}

printf("the pool still answers a fresh request: %s\n", $answered ? 'yes' : 'no');

$code = $master->stop(10.0);
printf("the Master stops politely: %s\n", $code === 0 ? 'yes' : 'no');
```

**Перевод комментариев:**
- `ch23-worker-crash.php` (PHPDoc): крах воркера — не отказ задания: пул
  замечает, пул заменяет и пул продолжает обслуживать — две оси восстановления
  матрицы отказов раздельны. Пул здесь настоящий: `bin/worker.php` (мастер
  php-worker-pool), запущенный дочерним процессом на своём сокете, с инъекцией
  отказов, взведённой `PLATFORM_ENV=dev`. `worker.crash` заставляет воркера,
  взявшего запрос, убить себя через `SIGKILL` посреди запроса — так OOM-kill
  выглядит со стороны пула, — а собственный `WorkerFailureInjector` платформы
  записывает каждую фазу с бухгалтерии мастера: крах замечен, мёртвый pid уходит
  из `stats()`, появляется pid, которого раньше не было. Тайминги и pid-ы
  намеренно не печатаются: числа меняются от прогона к прогону, а фазы
  доказывают классификацию, а не миллисекунды. Запуск внутри контейнера, где
  проект смонтирован в `/app`:
  `docker compose cp /tmp/ch23-worker-crash.php php:/tmp/ch23-worker-crash.php`;
  `docker compose exec php php /tmp/ch23-worker-crash.php`.
- `// Its own addresses and its own temp tree, so nothing here touches a` — свои
  адреса и своё временное дерево, чтобы здесь ничто не задело платформу,
  которая, возможно, уже запущена.
- `// The one switch that arms worker.crash.` — единственный переключатель,
  который взводит `worker.crash`.

```text
the pool Master answers on its socket: yes
workers before the crash: 2
the crash was detected by the pool: yes
the dead worker left the pool's list: yes
a replacement was started: yes
the replacement's pid is one that was not there before: yes
workers after the crash: 2
the pool still answers a fresh request: yes
the Master stops politely: yes
```

Все пять следствий — `yes`: крах замечен, мёртвый pid ушёл из списка, замена
запущена, её pid раньше в списке не было, и воркеров снова двое. Затем пул
**отвечает на новый запрос** — то есть падение воркера не сделало пул
недоступным.

Тут важно, чего в выводе **нет**: ни одной строки про состояние задания. Этот
эксперимент не публикует задание вообще. Крах воркера — событие пула; у очереди
на него нет отдельного приговора, и это правильно: задание, которое держал
мёртвый воркер, проваливается как попытка и подчиняется той же политике
повторов, что и любая другая неудачная доставка. А вот «провал задания» без
потери воркера — это эксперимент 1: там `demo.failing` падает трижды, и ни один
воркер при этом не умер.

### 3. Backpressure: принять → отвергнуть → принять

Предел очереди — 3. Сначала она пуста, и политика разрешает. Потом в журнал
уходит ровно три задания, и тот же вызов отвечает «на пределе». Затем консьюмер
разбирает задания, и политика снова разрешает.

```bash
docker compose cp /tmp/ch23-backpressure.php php:/tmp/ch23-backpressure.php
docker compose exec php php /tmp/ch23-backpressure.php
```

```php
<?php

declare(strict_types=1);

/**
 * Backpressure is a signal, not a wall: the same call that is refused at
 * capacity is accepted again once the queue drains.
 *
 * The depth is read the way the platform reads it - off the append-only
 * journal, through `QueueJournal` - and the decision is the platform's own
 * `BackpressurePolicy`, which is what the order route consults before it
 * decodes a body or touches the database. Nothing here reaches HTTP: the
 * 429 itself is `OrderCreateHandler`'s, and the integration tests cover it.
 * What this shows is the part that decides: accept, reject, accept.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch23-backpressure.php php:/tmp/ch23-backpressure.php
 *   docker compose exec php php /tmp/ch23-backpressure.php
 */

require '/app/vendor/autoload.php';

use PhpJobQueue\Dispatcher\JobDispatcher;
use PhpJobQueue\Job\Job;
use PhpJobQueue\Metrics\MetricsCollector;
use PhpJobQueue\Persistence\FileStorage;
use PhpJobQueue\Producer\JobFactory;
use PhpJobQueue\Producer\Producer;
use PhpJobQueue\Queue\InMemoryQueue;
use PhpJobQueue\Retry\FixedDelayRetry;
use PhpJobQueue\Support\SystemClock;
use PhpJobQueue\Worker\WorkerPool;
use PhpSystemsPlatform\Queue\BackpressurePolicy;
use PhpSystemsPlatform\Queue\QueueConsumer;
use PhpSystemsPlatform\Queue\QueueJournal;
use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch23-backpressure-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

// Only the process that started the run may clean up: a forked job-queue
// worker inherits this shutdown function, and a worker deleting the journal
// its parent is still reading is exactly the kind of fork surprise the
// platform's own per-worker services avoid.
$owner = getmypid();

register_shutdown_function(static function () use ($directory, $owner): void {
    if (getmypid() !== $owner) {
        return;
    }

    OwnedProcess::removeTree($directory);
});

$logPath = $directory . 'queue.log';
$clock = new SystemClock();
$metrics = new MetricsCollector();
$storage = new FileStorage($logPath);
$queue = InMemoryQueue::restoreFromStorage($storage, $clock);
$producer = new Producer($queue, new JobFactory($clock, $metrics));
$journal = new QueueJournal($logPath);

$limit = 3;
$policy = new BackpressurePolicy($journal, $limit);

$report = static function (string $label, BackpressurePolicy $policy): void {
    $decision = $policy->evaluate();

    printf(
        "%-28s depth=%d limit=%d at capacity: %s\n",
        $label,
        $decision->depth,
        $decision->maxSize,
        $decision->atCapacity ? 'yes' : 'no',
    );
};

$report('before anything is published:', $policy);

for ($number = 1; $number <= $limit; $number++) {
    $producer->dispatch('bench.noop');
}

$report(sprintf('after publishing %d jobs:', $limit), $policy);

$pool = new WorkerPool(size: 1, handler: static function (Job $job): void {}, metrics: $metrics);
$dispatcher = new JobDispatcher(
    queue: $queue,
    workerPool: $pool,
    retryPolicy: new FixedDelayRetry(0),
    clock: $clock,
    visibilityTimeout: 5,
    storage: $storage,
    metrics: $metrics,
);
$consumer = new QueueConsumer(
    dispatcher: $dispatcher,
    queue: $queue,
    logPath: $logPath,
    clock: $clock,
    shutdownGrace: 5.0,
);

$consumer->runWhile(static fn (): bool => $journal->snapshot()['depth'] > 0);

$report('after the consumer drained them:', $policy);

printf("completed jobs in the journal: %d\n", $journal->snapshot()['completed']);
```

**Перевод комментариев:**
- `ch23-backpressure.php` (PHPDoc): обратное давление — сигнал, а не стена: тот
  же вызов, который отвергнут на пределе, снова принимается, когда очередь
  разбирают. Глубина читается так, как её читает платформа, — из append-only
  журнала через `QueueJournal`, — а решение принимает собственная
  `BackpressurePolicy` платформы, которую маршрут заказов спрашивает до того,
  как декодировать тело или трогать базу. Здесь ничто не доходит до HTTP: сам
  `429` — `OrderCreateHandler` 'а, и его покрывают интеграционные тесты. Здесь
  показана часть, которая решает: принять, отвергнуть, принять. Запуск внутри
  контейнера, где проект смонтирован в `/app`:
  `docker compose cp /tmp/ch23-backpressure.php php:/tmp/ch23-backpressure.php`;
  `docker compose exec php php /tmp/ch23-backpressure.php`.
- `// Only the process that started the run may clean up: a forked job-queue` —
  убирать за собой может только процесс, начавший прогон: форкнутый воркер
  очереди заданий наследует эту shutdown-функцию, а воркер, удаляющий журнал,
  который родитель ещё читает, — ровно та неожиданность форка, которой избегают
  собственные сервисы платформы на каждого воркера.

```text
before anything is published: depth=0 limit=3 at capacity: no
after publishing 3 jobs:     depth=3 limit=3 at capacity: yes
after the consumer drained them: depth=0 limit=3 at capacity: no
completed jobs in the journal: 3
```

Три строки вывода — три состояния одного и того же решения, и между ними нет ни
одной ручной правки счётчика: глубина читается из журнала, поэтому «очередь
разобрана» — это факт, который политика видит сама.

Здесь демонстрируется разница между стеной и сигналом. Стена отказывала бы, пока
её не выключат руками; сигнал говорит «сейчас не могу» и **сам** перестаёт
говорить это, когда причина исчезла. Последняя строка —
`completed jobs in the journal: 3` — подтверждает, что работа не потеряна, а
сделана: отказ на пределе был отказом принять новое, а не отказом сохранить уже
принятое.

Сам `429` здесь не воспроизводится: его формирует обработчик на HTTP-пути, и на
него есть интеграционный тест (§6). Проверять политику через HTTP значило бы
держать ради этого поднятую `serve`, а решение о загрузке прекрасно проверяется
там, где оно принимается.

### 4. Занятый порт: упасть и не оставить ничего

Флагманская строка матрицы и настоящий дефект из истории платформы. Порт
занимает сам скрипт, `serve` получает свои порты, свой сокет пула и свой
`TMPDIR`, а затем запускается и наблюдается — ожиданием его **собственного**
выхода, а не остановкой.

```bash
docker compose cp /tmp/ch23-occupied-port.php php:/tmp/ch23-occupied-port.php
docker compose exec php php /tmp/ch23-occupied-port.php
```

```php
<?php

declare(strict_types=1);

/**
 * The occupied-port row of the failure matrix, end to end: a serve that
 * cannot bind its HTTP port starts its four servers, fails, exits non-zero,
 * and leaves NONE of them behind.
 *
 * This is the bug `ShutdownStack` was written for (see the chapter): the
 * first version stopped each resource behind its own ownership flag, so a
 * path that threw before the stops skipped all of them - and the next serve
 * then adopted the orphans instead of owning them.
 *
 * The child is waited on by polling, not stopped: this experiment has to
 * observe serve's own exit, and asking it to stop would test the wrong
 * thing. The port is occupied by this script itself, and serve gets its own
 * ports, its own pool socket and its own TMPDIR, so the check afterwards is
 * unambiguous: the journal of a clean run is that exactly the four servers
 * it printed were up, and none of them is up any more.
 *
 * The database daemon is the one that could survive: it daemonizes, so
 * stopping it goes through its pid file rather than a child handle.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch23-occupied-port.php php:/tmp/ch23-occupied-port.php
 *   docker compose exec php php /tmp/ch23-occupied-port.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch23-occupied-' . getmypid() . '/';

@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

/**
 * A port the kernel says is free: bind port 0, read back what was assigned,
 * release it.
 */
$freePort = static function (): int {
    $probe = stream_socket_server('tcp://127.0.0.1:0', $errno, $error);
    $name = (string) stream_socket_get_name($probe, false);
    fclose($probe);

    return (int) substr($name, (int) strrpos($name, ':') + 1);
};

$http = $freePort();
$database = $freePort();
$cache = $freePort();
$search = $freePort();
$socket = $directory . 'worker.sock';

$occupier = stream_socket_server('tcp://127.0.0.1:' . $http, $errno, $error);
$occupied = $occupier !== false;

printf("the HTTP port is deliberately occupied: %s\n", $occupied ? 'yes' : 'no');

$environment = array_merge((array) getenv(), [
    'TMPDIR' => $directory,
    'HTTP_PORT' => (string) $http,
    'DATABASE_PORT' => (string) $database,
    'CACHE_PORT' => (string) $cache,
    'SEARCH_PORT' => (string) $search,
    'WORKER_POOL_SOCKET' => $socket,
    'PLATFORM_ENV' => 'dev',
]);

$stdout = $directory . 'logs/serve.out';
$stderr = $directory . 'logs/serve.err';

$process = proc_open(
    [PHP_BINARY, '/app/bin/platform.php', 'serve'],
    [
        1 => ['file', $stdout, 'a'],
        2 => ['file', $stderr, 'a'],
    ],
    $pipes,
    null,
    $environment,
);

if (!is_resource($process)) {
    printf("could not start serve\n");
    exit(1);
}

$deadline = microtime(true) + 60.0;
$status = proc_get_status($process);
$code = null;

while ($status['running'] && microtime(true) < $deadline) {
    usleep(50_000);
    $status = proc_get_status($process);
}

if (!$status['running']) {
    // Read the code off the transition: a later call reports -1.
    $code = $status['exitcode'];
}

if ($code === null) {
    proc_terminate($process, SIGKILL);
    $code = -1;
}

$output = (string) file_get_contents($stdout) . (string) file_get_contents($stderr);

printf(
    "serve started its four servers before it failed: %s\n",
    substr_count($output, 'listening on') === 4 ? 'yes' : 'no',
);
printf("serve exited with a zero code: %s\n", $code === 0 ? 'yes' : 'no');
printf("the failure names the HTTP port: %s\n", str_contains($output, 'Cannot start server on') ? 'yes' : 'no');

// The four things it started. All of them must be down now.
printf("a worker pool is still listening: %s\n", OwnedProcess::endpointAnswers('unix://' . $socket, 0.3) ? 'yes' : 'no');
printf("a cache server is still listening: %s\n", OwnedProcess::portAnswers('127.0.0.1', $cache, 0.3) ? 'yes' : 'no');
printf("a database server is still listening: %s\n", OwnedProcess::portAnswers('127.0.0.1', $database, 0.3) ? 'yes' : 'no');
printf("a search service is still listening: %s\n", OwnedProcess::portAnswers('127.0.0.1', $search, 0.3) ? 'yes' : 'no');

$pidFile = $directory . 'php-systems-platform/db/minidb.pid';
$daemonPid = is_file($pidFile) ? (int) trim((string) file_get_contents($pidFile)) : 0;

printf(
    "the pid file of the database daemon names a live process: %s\n",
    $daemonPid > 0 && posix_kill($daemonPid, 0) ? 'yes' : 'no',
);

proc_close($process);

if ($occupied) {
    fclose($occupier);
}
```

**Перевод комментариев:**
- `ch23-occupied-port.php` (PHPDoc): строка матрицы отказов про занятый порт, от
  начала до конца: `serve`, который не может занять свой HTTP-порт, запускает
  четыре сервера, падает, выходит с ненулевым кодом и не оставляет НИ ОДНОГО из
  них. Это баг, ради которого написан `ShutdownStack` (см. главу): первая версия
  останавливала каждый ресурс за его собственным флагом владения, поэтому путь,
  бросивший исключение до остановок, пропускал их все, — и следующий `serve`
  принимал сирот вместо того, чтобы владеть ими. Дочерний процесс ждут опросом,
  а не останавливают: этот эксперимент должен наблюдать собственный выход
  `serve`, а просьба остановиться проверяла бы не то. Порт занимает сам этот
  скрипт, а `serve` получает свои порты, свой сокет пула и свой `TMPDIR`,
  поэтому проверка после однозначна: журнал чистого прогона состоит в том, что
  ровно четыре сервера, которые он напечатал, были подняты, и ни один из них
  больше не поднят. Демон базы — единственный, кто мог бы выжить: он
  демонизируется, поэтому его остановка идёт через pid-файл, а не через
  дескриптор ребёнка. Запуск внутри контейнера, где проект смонтирован в `/app`:
  `docker compose cp /tmp/ch23-occupied-port.php php:/tmp/ch23-occupied-port.php`;
  `docker compose exec php php /tmp/ch23-occupied-port.php`.
- `$freePort` (PHPDoc): порт, который ядро называет свободным: занять порт 0,
  прочитать, какой выдан, и освободить.
- `// Read the code off the transition: a later call reports -1.` — прочитать
  код на переходе: более поздний вызов сообщает `-1`.
- `// The four things it started. All of them must be down now.` — четыре вещи,
  которые он запустил. Все они теперь должны быть остановлены.

```text
the HTTP port is deliberately occupied: yes
serve started its four servers before it failed: yes
serve exited with a zero code: no
the failure names the HTTP port: yes
a worker pool is still listening: no
a cache server is still listening: no
a database server is still listening: no
a search service is still listening: no
the pid file of the database daemon names a live process: no
```

`serve started its four servers before it failed: yes` — это ключ ко всему
эксперименту. Прежде чем упасть на HTTP-порту, команда успела поднять четыре
сервера: базу (демон), кеш, мастер пула и службу поиска. Если бы владение было
расставлено флагами, как в первой версии, исключение на пятом шаге пропустило бы
все четыре остановки — и следующий `serve` принял бы сирот как «уже запущенных».

`serve exited with a zero code: no` и `the failure names the HTTP port: yes` —
отказ виден и локализован. А четыре следующие строки — ценность всей главы: **ни
один** из четырёх серверов не остался слушать. Демон базы при этом отдельный
случай: он демонизирован, поэтому останавливается через pid-файл, а не через
ручку ребёнка, — и последняя строка подтверждает, что pid-файл не указывает на
живой процесс.

Обратите внимание, что проверка идёт не по логам и не по отсутствию исключения,
а по сокетам и портам: это единственный ответ на вопрос «осталось ли что-то
слушать», который нельзя получить поспешным выводом. Эксперимент также
показывает, почему наивная «остановка по флагу» так коварна: она выглядит
правильной ровно до первого пути, который не дошёл до флагов.

## 5. Отказы

| Что ломается                        | Чем обнаруживается                              | Что происходит                                                                            |
| ----------------------------------- | ----------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Воркер умер                         | обработчик `SIGCHLD` мастера                    | пул признаёт провал запроса, который держал мёртвый воркер, и форкает замену              |
| Пул ответил `worker_crashed`        | ответ клиенту                                   | задание проваливается как **попытка**, без отдельного приговора содержимому               |
| Задание бросило исключение          | очередь считает попытку и пишет `last_error`    | повтор до `max_attempts`, затем `FAILED` — воркер жив                                     |
| Payload не проходит `validate()`    | `shouldRetry()` вернул «нет»                    | `FAILED` с первой попытки, бюджет попыток не тратится                                     |
| Тип задания неизвестен              | `execute()` бросил исключение                   | проваленная попытка, как у битого payload; молчаливого пропуска нет                       |
| Очередь на пределе                  | `BackpressurePolicy::evaluate()`                | `429` с `queueDepth`, `queueMaxSize`, `Retry-After`; ничего не записано и не опубликовано |
| Очередь на пределе, но работа течёт | глубина в журнале падает                        | политика снова разрешает: сигнал, а не стена                                              |
| База недоступна                     | исключение под доменом доходит до `Application` | `500` без деталей клиенту; оператор получает throwable в stderr                           |
| База тормозит                       | инъекция задержки (`DATABASE_LATENCY_MS`)       | каждая операция платит задержку; счётчик задержек виден                                   |
| Валидация не прошла                 | `InvalidArgumentException` в домене             | `400` **до** записи; базы это не касается                                                 |
| Кеш недоступен на чтении            | `CacheClientException`                          | чтение из базы, обход посчитан (`bypass`), клиент отказа не видит                         |
| Кеш недоступен на записи            | то же исключение                                | `201` всё равно: строка уже записана, кеш — производное                                   |
| Кеш выключен                        | `CACHE_ENABLED=0`                               | промах без обращения к сокету; метрики описывают платформу без кеша                       |
| Поиск просит переиндексацию         | `notReadyReason()` клиента                      | `503` и публикация `product.reindex` (→ глава 21)                                         |
| HTTP-порт занят при старте          | исключение при `Server::start()`                | `serve` выходит ненулевым кодом и останавливает всё, что успел поднять                    |
| Клиент оборвал соединение           | журнал `serve`                                  | запрос завершается безопасно, праздное соединение возвращается                            |
| Медленный клиент на заголовках      | журнал `serve` (обход заголовков)               | соединение забирается по таймауту (защита от Slowloris)                                   |
| Воркер умирает на каждом задании    | замены в списке воркеров                        | пул исправно форкает замены: внешне всё живо, поэтому это сигнал, а не норма              |
| Отказ зависимости под нагрузкой     | рост числа повторов и глубины                   | повторы ограничены `max_attempts`; шторм не бесконечен, но растёт очередь                 |

## 6. Как это проверено

Три набора тестов, и каждый отвечает на свой вопрос главы.

**Решение о загрузке.** `tests/Unit/BackpressurePolicyTest.php` — три метода про
само решение: `testUnderTheLimitIsNotAtCapacity`,
`testReachingTheLimitIsAtCapacity` и `testACompletedJobDoesNotCountTowardDepth`.
Последний — про то, что глубина считает работу, а не записи в журнале:
завершённое задание работе не равно. Это ровно тот факт, который разворачивает
эксперимент 3 назад «принять».

**Две оси.** `tests/Integration/Boundary/QueueWorkerPoolBoundaryTest.php` — семь
методов через настоящий пул:
`testATaskIsDispatchedToAWorkerAndItsAnswerComesBack`,
`testTheAnswerComesFromAWorkerProcessAndNotFromTheCaller`,
`testSeveralTasksRunAtTheSameTimeOnDifferentWorkers`,
`testThePoolReportsTheWorkersItIsRunning`,
`testAnUnknownTaskIsAnErrorAndThePoolKeepsServing`,
`testACrashedWorkerFailsItsRequestAndIsReplaced` (эксперимент 2) и
`testThePoolIsFullyUsableAgainAfterAWorkerCrashed` — тот, что проверяет не факт
замены, а пригодность пула **после** неё.

**Сценарии целиком.** `tests/Integration/FailureDemoCommandTest.php`
(`testPrintsBothFailureSequencesAndExitsZero`) и
`tests/Integration/FailureExperimentsCommandTest.php`
(`testRunsAllFiveExperimentsAndExitsZero`) прогоняют оба демонстрационных пути —
падение задания и падение воркера — против поднятой платформы. Два теста, но 25
утверждений: там, где поведение — это последовательность из нескольких
компонентов, проверяется каждая фаза, а не только «команда завершилась нулём».

**Деградация.** `tests/Integration/DisabledCacheReadPathTest.php` — шесть
методов про платформу **без** кеша, и первый же из них
(`testNoCacheServerIsListeningAtAll`) проверяет, что различить «выключен» и
«лежит» вообще можно; остальные — что первое и второе чтения одинаково
промахиваются, что ответ приходит из базы, а не из запасного пути, и что
`/health` и метрики описывают платформу без кеша честно.
`tests/Integration/DatabaseDelayTest.php` — три метода про инъекцию задержки:
читает, пишет, и «соединение без задержки её не платит».

Прогоны: `docker compose exec php php vendor/bin/phpunit tests/Unit` — **145
тестов, 348 утверждений, OK**. Целевой набор `BackpressurePolicyTest`,
`QueueWorkerPoolBoundaryTest` и `DisabledCacheReadPathTest` — **16 тестов, 53
утверждения, OK**. Демонстрационные сценарии (`FailureDemoCommandTest`,
`FailureExperimentsCommandTest`) — **2 теста, 25 утверждений, OK** (около 46
секунд: они поднимают настоящую платформу).

## 7. Цифры

| Файл                                              | Строк | Что делает                                            |
| ------------------------------------------------- | ----- | ----------------------------------------------------- |
| `src/Queue/BackpressurePolicy.php`                | 36    | глубина из журнала, один вердикт                      |
| `src/Queue/BackpressureDecision.php`              | 21    | вердикт вместе с числами                              |
| `src/Queue/QueueJournal.php`                      | 123   | глубина, завершённые, провалившиеся, повторы          |
| `src/Queue/QueueConsumer.php`                     | 194   | цикл, сигналы, досинхронизация с журналом             |
| `src/Queue/JobRegistry.php`                       | 87    | тип → поведение, `validate()`, `shouldRetry()`        |
| `src/Queue/JobExecutor.php`                       | 200   | вход воркера в платформу, ленивые соединения          |
| `src/Workers/WorkerFailureTasks.php`              | 59    | `worker.crash` под переключателем среды               |
| `src/Workers/WorkerFailureInjector.php`           | 158   | четыре фазы краха как доказательства                  |
| `src/Application/Application.php`                 | 154   | `404`/`405`/`500`: кому детали, кому нет              |
| `src/Application/Handlers/OrderCreateHandler.php` | 88    | `429` до всего остального                             |
| `src/Cache/CacheService.php`                      | 214   | производное состояние и его обход                     |
| `docs/failure-matrix.md`                          | 52    | каждая строка: поведение, наблюдение, воспроизведение |

| Предел или таймаут                              | Значение                                           |
| ----------------------------------------------- | -------------------------------------------------- |
| Ёмкость очереди по умолчанию (`QUEUE_MAX_SIZE`) | 500                                                |
| Бюджет попыток задания                          | 3                                                  |
| Задержка повтора                                | 1 с (целые секунды: дробное значение дало бы ноль) |
| Консьюмеров на команду                          | 4                                                  |
| Воркеров в пуле                                 | 2…16                                               |
| Таймаут задачи у клиента пула                   | 5 с                                                |
| Максимум удержания задачи воркером              | 30 с                                               |
| Таймаут служебных операций очереди              | 2 с                                                |
| Таймаут старта службы поиска                    | 60 с                                               |
| Интервал досинхронизации с журналом             | 100 мс                                             |

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что                  | Значение                                                                            |
| -------------------- | ----------------------------------------------------------------------------------- |
| `ch23-verdicts`      | 145 строк; `demo.failing` — 3 попытки, `order.process` — одна; `failed=2 retried=1` |
| `ch23-worker-crash`  | 97 строк; пять следствий `yes`; пул после краха отвечает                            |
| `ch23-backpressure`  | 110 строк; принять → отвергнуть → принять; `completed=3`                            |
| `ch23-occupied-port` | 141 строка; четыре сервера подняты, выход ненулевой, ни один не остался             |

## 8. Упражнения

1. **Проследите.** Воркер убит `SIGKILL` посреди задания, которое через секунду
   после этого публикуется повторно (тем же ключом идемпотентности). Опишите
   путь события от `SIGCHLD` до ответа клиенту и укажите, в каком месте
   решается, будет ли задание признано проваленным.
2. **Докажите гипотезу.** Инженер предлагает увеличить `max_attempts` до 50,
   «чтобы ничего не терялось». Придумайте сценарий, где это превращает медленную
   базу в отказ всего стенда, и покажите, какое число (или какая политика)
   ограничило бы ущерб.
3. **Найдите правило.** Почему проверка backpressure стоит **до** разбора тела
   запроса, а не после валидации? Приведите запрос, на котором порядок меняет
   наблюдаемое поведение, и объясните, что именно меняется.
4. **Сравните.** Кеш и база подключены одинаково — клиентом-библиотекой, — но
   деградация у них разная. Сформулируйте критерий, по которому для клиента
   выбирается «обойти и посчитать» или «ответить 500», и примените его к службе
   поиска (→ глава 21): к какому случаю она относится?
5. **Спроектируйте тест.** В эксперименте 2 проверяется, что пул отвечает после
   краха. Как проверить, что **задание**, которое держал мёртвый воркер, не
   пропало, не полагаясь на время и не читая pid-ы? Какие швы нужны для этого
   теста?
6. **Разберитесь в цене.** Эксперимент 4 показывает, что упавший `serve` ничего
   не оставляет. Но у этого есть цена на каждом старте: перечислите, что команда
   обязана сделать, прежде чем считать запуск удачным, и что она теряет, если
   пропустит каждую из этих проверок.

## Что дальше

Отказы видимы на своих границах, деградация выбрана осознанно, и ни один из них
не молчит. Но «не молчать» — это ещё не наблюдаемость: строка в stderr помогает
тому, кто рядом с терминалом, а не тому, кто разбирает инцидент через час.
Следующая глава — про то, что видно **снаружи**: метрики, счётчики и трейсы,
которые связывают HTTP-запрос с заданием, которое он породил в другом процессе,
и с падением воркера, которое случилось ещё позже.
