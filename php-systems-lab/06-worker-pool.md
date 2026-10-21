# Глава 6. Пул воркеров: master, воркеры, супервизия

> Проект: [`php-worker-pool`](https://github.com/Researcher86/php-worker-pool),
> релиз `v1.1.0`, коммит `b616246`. Нужно знать: память и `fork` (→ главы 1–2),
> IPC и сокеты (→ глава 3), цикл событий (→ глава 4), Fibers (→ глава 5).
> Пригодится дальше: плавное завершение и сигналы (→ глава 7), очередь задач (→
> глава 8).

Классический PHP-воркер умирает вместе с запросом. Каждый запрос — новый
процесс, который заново подключается к базе данных, греет кэш, загружает
фреймворк, и только потом доходит до дела. Для «отдай одну строку из БД» это
нормально; для сервиса, где накладные расходы старта сопоставимы с работой, —
расточительно.

**Пул воркеров** переворачивает это: процесс создаётся один раз, делает дорогой
bootstrap один раз и обслуживает много запросов. Но у вечности есть цена —
постоянный процесс накапливает то, что per-request хранители мусора выбрасывают
даром: утечки, устаревшие соединения, статическое состояние. И чтобы процесс не
мог «пропасть» тихо, кому-то нужно за ним смотреть.

Этот «кто-то» — **master**, и вся глава про него: как он рождает воркеров, как
понимает, что воркер готов, как ловит его смерть, как вовремя заменяет его на
свежего и как под этим всем не терять запросы.

---

## 1. Проблема

Сравним два подхода на одной задаче — «обслужи 100 000 запросов, каждый требует
открытого соединения с БД».

**Процесс на запрос (per-request).** `fork` плюс bootstrap на каждый запрос.
Память освобождается сама, состояние никогда не устаревает — но 100 000 раз
заплачено за одно и то же подключение, и под нагрузкой форки сами становятся
узким местом.

**Постоянные воркеры (persistent).** N процессов, каждый подключился один раз и
обслуживает поток запросов. Дорогой старт амортизирован. Но теперь есть три
вопроса, которых не было раньше:

1. **Кто владеет воркерами?** Если их запустил `docker`/`systemd`, то
   перезапуск, масштабирование и знание «сколько их» уезжают наружу. Если ими
   владеет сам рантайм — нужен master.
2. **Когда воркер считается готовым?** `fork` вернулся — это ещё не
   «обслуживает»: до готовности нужно подключиться к БД, прогреть кэш. Отдать
   такому воркеру запрос — значит заставить клиента ждать этот прогрев.
3. **Что делать со смертью?** Воркер может упасть (баг, OOM-kill, `kill -9`),
   зависнуть, никогда не прогреться или просто прожить слишком долго. Без
   хозяина его место останется пустым.

Именно эти три вопроса и есть предмет главы. `php-worker-pool` — учебный, но
настоящий ответ: один **master** (control plane) и N **воркеров** (data plane).
Master не делает работу — он владеет сокетами, очередью, состоянием воркеров и
их жизненным циклом.

## 2. Устройство

### Разделение ролей

```text
        ┌───────────────────────── Master (один процесс) ─────────────────────────┐
        │  Unix-socket front door   ←  клиенты (PHP-FPM и кто угодно)             │
        │  очередь запросов         ←  FIFO, ограниченная (backpressure)          │
        │  event loop (select)      —  приём, маршрутизация, ответы               │
        │  реестр воркеров          —  кто IDLE / BUSY / STARTING / DRAININ       │
        └───────────────┬─────────────────────┬──────────────────────┬────────────┘
                    socket pair           socket pair          socket pair
                        │                     │                      │
                    ┌───▼───┐             ┌───▼───┐              ┌───▼───┐
                    │worker │             │worker │      ...     │worker │
                    │bootstrap→READY→loop │       │              │        │
                    └───────┘             └───────┘              └───────┘
```

Master однопоточный и событийный (→ глава 4). Воркеры — отдельные процессы (→
глава 2), каждый со своим стеком и своей копией памяти после `fork`.

### `fork` доносит конфигурацию до всех — включая будущих

Обработчик запроса задаётся один раз, до старта, в `bin/server.php`. Никакой
сериализации: `fork` копирует замыкание в каждого ребёнка. Это работает и для
воркеров, которых родят через часы под нагрузкой:

- замыкание `$handler` живёт в памяти master'а,
- новый `fork` копирует всю память, включая замыкание,
- поэтому замена упавшего воркера получает тот же обработчик **бесплатно**, без
  файлов конфигурации и перезапуска.

Это свойство `fork` (→ глава 2, copy-on-write) превращает его из «просто способа
создать процесс» в способ доставки кода и настроек.

### Сокет-пара на каждого воркера

Master и воркер общаются по своей **сокет-паре** (`stream_socket_pair`, →
глава 3) — как две трубы, по одной на каждое направление, но с сохранением
границ кадров. Master пишет в свою половину, воркер читает из своей. Никакой
общей памяти и семафоров: у каждого воркера приватный канал, и это принципиально
— общий канал потребовал бы арбитража, а приватный нет.

Запросы кодируются length-prefixed кадрами (`MessageEncoder`/`MessageDecoder`)
поверх сокета: длина, потом тело. Это то же решение, что в главе 3, и по той же
причине — поток байтов не сохраняет границы сообщений сам.

### Readiness handshake: `fork` — это ещё не воркер

Самое важное решение всей архитектуры. Воркер после `fork` находится в состоянии
**`STARTING`** и не получает ни одного запроса, пока сам не пришлёт сообщение
**READY** — после того, как выполнит прикладной bootstrap (подключение к БД,
нагрев кэша). Состояния воркера:

| Состояние  | Что значит                    | Можно ли давать запрос |
| ---------- | ----------------------------- | ---------------------- |
| `STARTING` | форкнут, выполняет bootstrap  | **нет**                |
| `IDLE`     | готов, ждёт работу            | да                     |
| `BUSY`     | выполняет запрос              | нет                    |
| `DRAINING` | дорабатывает текущее и уходит | нет                    |
| `STOPPING` | получил команду на выход      | нет                    |
| `DEAD`     | завершился (терминальное)     | нет                    |

Почему handshake, а не таймер? Потому что **никто, кроме самого воркера, не
знает, закончился ли bootstrap**. Master видит только «процесс жив»; он не может
узнать, что `PDO` подключился. Значит, единственный источник правды — воркер, и
он его сообщает одним сообщением. Отсюда же известное правило из
`bin/server.php`: hook bootstrap'а обязан **создавать** ресурсы, а не
захватывать их. Соединение, открытое до `fork`, — это один сокет, унаследованный
всеми воркерами, каждый из которых пишет в чужой MySQL-поток. Открытое внутри
hook'а — по одному на процесс.

### Супервизия: заметить смерть и заменить

Master ловит смерть двумя независимыми путями:

- **`SIGCHLD`** — ядро сообщает о завершении любого ребёнка;
- **EOF на сокете воркера** — канал закрылся, значит процесс ушёл.

Оба ведут к одной процедуре: `pcntl_waitpid(-1, ..., WNOHANG)` в цикле собирает
всех зомби (→ глава 2), воркер помечается `DEAD`, его незавершённый запрос
проваливается как `worker_crashed`, и — если master ещё принимает соединения —
немедленно форкается замена. **Ожидаемые** выходы (`STOPPING`) не считаются
падениями: воркер, который уходит по плану, и воркер, который упал, различаются
по состоянию, а не по факту `waitpid`.

Двух путей обнаружения не случайность: сокет закрывается при разрыве связи даже
если сигнал почему-то потерян, а `SIGCHLD` приходит даже если сокет остался
открытым (например, воркер завис, но его половина пары ещё держится). Кто первый
заметил — тот и сообщает, у второго уже пусто.

### Recycling: не убить, а «доработать и уйти»

Постоянный процесс накапливает утечки, устаревшие соединения и статическое
состояние. Ответ всех серьёзных рантаймов — периодическая замена воркера:

- **`maxRequests`** — заменить после N запросов (аналог `pm.max_requests`);
- **`maxLifetime`** — заменить по возрасту;
- **`maxMemoryBytes`** — заменить по памяти.

Ключевое: превысивший лимит воркер **не убивается**, а **осушается**
(`DRAINING`). Он заканчивает текущий запрос, отвечает на него как обычно, и
только потом уходит; замена форкается **заранее**. Ни один запрос не теряется —
поэтому лимит можно ставить агрессивно. Причина замены возвращается строкой, а
не `bool`, чтобы в логе было видно, **почему** воркера заменили.

### Autoscaling: очередь как сигнал

Реактивный ответ на нагрузку: если в очереди есть работа, а свободных воркеров
нет — подрастить пул; если работы нет, а воркеры простаивают сверх минимума —
подрезать. Между двумя действиями — cooldown, чтобы один всплеск не вызвал
`scale up`, за которым сразу `scale down`.

Здесь спрятан поучительный баг (он записан прямо в комментарии кода): воркеры в
состоянии `STARTING` — это **ёмкость в пути**, а не отсутствующая ёмкость. Если
их не учитывать, пул с минимумом 2 и трёхсекундным прогревом под нагрузкой
разрастётся до 4, пока первые два ещё подключаются, форкнет ещё двух и только
потом сожмётся, — всплеск подключений к БД в самый неудачный момент. Поэтому в
условии масштабирования есть `$starting === 0`: «сначала дождись того, что уже
едет».

### Сигналы и self-pipe

Обработчики сигналов не могут делать произвольную работу: пока они выполняются,
состояние master'а может быть в середине изменения. Поэтому обработчик `SIGCHLD`
не разбирает воркеров сам, а **пишет один байт в pipe**, который event loop
читает как обычный дескриптор. Когда loop доходит до этого байта, разбор
`waitpid`/`respawn` идёт в нормальном последовательном контексте. Туда же
приходят `SIGHUP` (reload), `SIGUSR1` (dump метрик) и `SIGTERM`/`SIGINT`
(shutdown). Детали — в главе 7.

## 3. Код

### Состояния воркера

```php
enum WorkerState
{
    /**
     * Forked, but still running whatever the application does before it can
     * serve anything - connecting to a database, warming a cache. Leaves
     * only when the worker says READY. Nothing is dispatched here.
     */
    case STARTING;
    case IDLE;
    case BUSY;
    /** Finishing its current request, if any, then leaving - see WorkerProcess::drain(). */
    case DRAINING;
    case STOPPING;
    case DEAD;
}
```

[`src/Worker/WorkerState.php#L7-L21`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerState.php#L7-L21)

**Перевод комментариев:**
- `STARTING` (PHPDoc): форкнут, но всё ещё выполняет то, что приложение делает
  до готовности обслуживать, — подключение к базе, нагрев кэша. Уходит из этого
  состояния, только когда воркер сам сообщает `READY`. Сюда ничего не
  диспетчеризуется.
- `DRAINING` (PHPDoc): дорабатывает текущий запрос, если он есть, затем уходит —
  см. `WorkerProcess::drain()`.

Enum, а не набор булевых флагов, — потому что состояния **взаимоисключающие**:
воркер не может быть одновременно `IDLE` и `DRAINING`. Комментарий у `STARTING`
— это и есть readiness handshake, приклеенный к типу: «пока воркер не сказал
`READY`, сюда ничего не отправляется».

### Диаграмма переходов — в докблоке

```php
* State machine (see WorkerState). A worker starts STARTING and becomes
* dispatchable only when it says so - the readiness handshake. A fork does
* not mean a usable worker: the application's own bootstrap (database
* connection, warm cache) runs first, and the Master cannot see when that
* finished. Only the worker can, so only the worker says it, with one READY
* message.
*
*   STARTING ──markReady()──> IDLE ──beginRequest()──> BUSY
*     │                         │                       │
*     │                         │       drain()         │      drain()
*     │        drain()          └───────────┬───────────┴──────────┐
*     └────────────────────────────────────►▼                      ▼
*                                       DRAINING ◀─finishRequest()─DRAINING
*                                       (no work)                (still working)
*                                           │
*                                           │  stop()   (legal from any state
*                                           ▼            but DEAD, so shutdown
*                                       STOPPING         is never blocked)
*                                           │
*                                           ▼
*   any state ──markDead()──> DEAD (terminal, no way out)
```

[`src/Worker/WorkerProcess.php#L15-L35`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerProcess.php#L15-L35)

Три вещи, которые стоит прочитать с диаграммы:

- `IDLE → BUSY → IDLE` — воркер возвращается в `IDLE` **не** через `STARTING`:
  готовность — это одноразовый факт процесса, а не то, что он зарабатывает
  заново на каждый запрос.
- `DRAINING` достижим и из `IDLE` (нечего дорабатывать — уходит сразу), и из
  `BUSY` (дорабатывает, потом уходит). Это и есть «не убиваем, а осушаем».
- `STOPPING` легален из любого состояния, кроме `DEAD`, — поэтому shutdown
  никогда не блокируется состоянием воркера.

### `fork` возвращается дважды

```php
/**
 * pcntl_fork() runs this same function body in TWO processes at once and
 * tells them apart only by its return value: the parent gets the child's
 * pid, the child gets 0. So everything from `if ($pid === 0)` down to
 * `exit(0)` only ever executes inside the freshly forked child — it never
 * returns from there, it just runs the worker loop forever and exits.
 * Everything below that `if` runs only in the parent, which is why the
 * method can return a WorkerProcess: that return only happens for the
 * parent.
 */
public function launch(): WorkerProcess
```

[`src/Worker/ForkedWorkerLauncher.php#L46-L56`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/ForkedWorkerLauncher.php#L46-L56)

**Перевод комментариев:**
- `launch()` (PHPDoc): `pcntl_fork()` выполняет одно и то же тело функции в двух
  процессах сразу и различает их только по возвращённому значению: родитель
  получает pid ребёнка, ребёнок — `0`. Поэтому всё от `if ($pid === 0)` до
  `exit(0)` исполняется только в свежефоркнутом ребёнке — оттуда оно никогда не
  возвращается, а просто вечно крутит цикл воркера и завершается. Всё, что ниже
  этого `if`, выполняется только в родителе; поэтому метод может вернуть
  `WorkerProcess` — этот `return` бывает только у родителя.

Это самая частая точка непонимания при чтении кода на `fork`: одна и та же
функция выполняется в двух процессах, и единственное, что их различает, —
значение, которое вернул `pcntl_fork()`. Родитель получает pid ребёнка, ребёнок
— `0`. Всё внутри `if ($pid === 0)` исполняется только в ребёнке и никогда не
«возвращается» — оно уходит в вечный цикл воркера и `exit(0)`. Поэтому метод
может возвращать `WorkerProcess`: этот `return` видит только родитель.

### Ветка ребёнка: сброс унаследованных обработчиков

```php
if ($pid === 0) {
    // fork() copies the parent's pcntl signal handlers, and a worker
    // forked after Master registered its own (a crash replacement, a
    // scale-up) inherits closures over Master's state: a SIGHUP
    // delivered to this child would run Master's reload handler
    // INSIDE the worker and start forking grandchildren, SIGUSR1
    // would dump Master's metrics from the wrong process, SIGTERM
    // would flip a shutdown flag nothing in the worker reads. Reset
    // every signal Master is known to catch back to the OS default
    // before entering the worker loop.
    foreach ([SIGINT, SIGTERM, SIGCHLD, SIGHUP, SIGUSR1] as $signal) {
        pcntl_signal($signal, SIG_DFL);
    }

    // The signal MASK is inherited too - a worker forked from inside
    // one of WorkerPool's SIGCHLD-deferred sections (a scale-up, a
    // reload wave) would otherwise start life with SIGCHLD blocked,
    // and one forked during Master::run()'s startup with SIGTERM
    // blocked as well. (On PHP 8.5 the pcntl_signal() calls above
    // already unblock the signals they reset; this clears the rest.)
    pcntl_sigprocmask(SIG_SETMASK, []);
```

[`src/Worker/ForkedWorkerLauncher.php#L79-L99`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/ForkedWorkerLauncher.php#L79-L99)

**Перевод комментариев:**
- `// fork() copies the parent's pcntl signal handlers, and a worker` — `fork()`
  копирует обработчики сигналов `pcntl` родителя, и воркер, форкнутый после
  того, как Master зарегистрировал свои (замена после краха, масштабирование),
  наследует замыкания над состоянием Master'а: `SIGHUP`, доставленный этому
  ребёнку, выполнил бы обработчик reload от Master **внутри** воркера и начал бы
  форкать внуков; `SIGUSR1` свалил бы метрики Master из не того процесса;
  `SIGTERM` переключил бы флаг shutdown, который в воркере никто не читает.
  Сбросить каждый сигнал, который Master, как известно, ловит, в значение ОС по
  умолчанию перед входом в цикл воркера.
- `// The signal MASK is inherited too - a worker forked from inside` — маска
  сигналов тоже наследуется: воркер, форкнутый внутри одной из секций
  `WorkerPool` с отложенным `SIGCHLD` (масштабирование, волна reload), иначе
  начал бы жизнь с заблокированным `SIGCHLD`, а форкнутый во время старта
  `Master::run()` — ещё и с заблокированным `SIGTERM`. (В PHP 8.5 вызовы
  `pcntl_signal()` выше уже разблокируют сигналы, которые сбрасывают; этот вызов
  очищает остальные.)

Обработчики сигналов и **маска сигналов** наследуются через `fork` так же, как
память. Без этого сброса `SIGHUP`, посланный воркеру, выполнил бы **обработчик
master'а внутри воркера** и начал бы форкать внуков; `SIGUSR1` свалил бы метрики
из не того процесса. Это тонкий, но абсолютно реальный класс багов: наследуется
не только то, что вы явно объявили, но и всё, что оставил после себя родитель.

### Реапинг и замена

```php
public function reapDeadWorkers(): array
{
    $crashes = [];

    while (($pid = pcntl_waitpid(-1, $status, WNOHANG)) > 0) {
        $worker = $this->workers[$pid] ?? null;

        if ($worker === null) {
            continue; // not a worker we're tracking (already handled elsewhere)
        }

        $expected = $worker->getState() === WorkerState::STOPPING;

        // Capture before markDead() clears it - Dispatcher may
        // independently detect and report the same crash via the
        // worker's closed socket; whichever of the two gets here first
        // is the one that actually has a non-null id to report.
        $lostRequestId = $worker->getCurrentRequestId();
        $worker->markDead();
        unset($this->workers[$pid]);

        if ($expected) {
            continue;
        }

        $crashes[] = new WorkerCrash($worker, $lostRequestId);

        $worker->isTerminating()
            ? $this->totalTerminated++
            : $this->totalCrashed++;

        if ($this->accepting) {
            try {
                $this->register($this->launcher->launch());
            } catch (Throwable $e) {
                // Same reasoning as advanceReload(): don't let a failed
                // launch() propagate out of a SIGCHLD handler. Unlike
                // there, there's no pid to requeue for a specific retry -
                // this one replacement is simply skipped, leaving the
                // pool one short of its configured size until something
                // else grows it (e.g. Autoscaler, under actual load).
                // What matters here is that this loop keeps going: a
```

[`src/Worker/WorkerPool.php#L279-L320`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerPool.php#L279-L320)

**Перевод комментариев:**
- `// not a worker we're tracking (already handled elsewhere)` — не тот воркер,
  которого мы отслеживаем (уже обработан в другом месте).
- `// Capture before markDead() clears it - Dispatcher may` — захватить до того,
  как `markDead()` это очистит: `Dispatcher` может независимо обнаружить и
  сообщить о том же крахе через закрытый сокет воркера; кто из двух доберётся
  сюда первым, тот и будет иметь ненулевой id для отчёта.
- `// Same reasoning as advanceReload(): don't let a failed` — та же логика, что
  в `advanceReload()`: не дать упавшему `launch()` вылететь из обработчика
  `SIGCHLD`. В отличие от того случая, здесь нет pid, чтобы вернуть его в
  очередь на конкретный повтор, — одна замена просто пропускается, и пул
  остаётся на одного воркера меньше настроенного размера, пока что-то другое его
  не вырастит (например, `Autoscaler` под реальной нагрузкой). Важно здесь то,
  что этот цикл продолжается.

Читается как рецепт супервизии целиком:

- `pcntl_waitpid(-1, $status, WNOHANG)` в `while` — собрать **всех** зомби за
  один проход (одна `SIGCHLD` может принести несколько смертей);
- `$expected` различает плановый выход (`STOPPING`) и падение — ожидаемый выход
  не считается крахом и не увеличивает счётчик;
- `markDead()` до `unset` — воркер помечается мёртвым **прежде**, чем исчезнет
  из карты, чтобы параллельные пути увидели одно и то же состояние;
- `if ($this->accepting)` — замена форкается сразу, но только пока master
  принимает соединения: во время shutdown замены не нужны;
- `try/catch` вокруг `launch()` — упавший `fork` не должен вылетать из
  обработчика `SIGCHLD` и ронять весь цикл: «пул остаётся на одного воркера
  меньше до следующего масштабирования» лучше, чем смерть master'а.

### Autoscaling: считать «стартующих» ёмкостью в пути

```php
$total = $this->pool->count();
$active = $this->pool->countActive();
// countIdle(), not count() - countBusy(): a retiring (STOPPING)
// worker is neither busy nor available for dispatch, but count()
// still includes it until reapDeadWorkers() catches up - subtracting
// countBusy() from that would wrongly count it as spare capacity.
$idle = $this->pool->countIdle();

// Workers still warming up are capacity ON ITS WAY, not capacity
// missing - and they are not counted as idle, so without this the
// scaler reads a queue waiting on a bootstrap as a queue waiting on
// too few workers. Measured before this line existed: a pool with a
// floor of 2 and a 3s warm-up grew to 4 while its first two workers
// were still connecting, forked two more that also had to connect,
// and shrank back once they all reported ready - a burst of
// database connections at exactly the moment a deploy is at its most
// fragile. Wait for what is already coming before asking for more.
$starting = $this->pool->countStarting();

// Queue is growing and there's no spare capacity to absorb it.
if (!$this->queue->isEmpty() && $idle === 0 && $starting === 0 && $total < $this->maxWorkers) {
    $this->pool->scaleUp(min($this->step, $this->maxWorkers - $total));
```

[`src/Worker/Autoscaler.php#L58-L79`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/Autoscaler.php#L58-L79)

**Перевод комментариев:**
- `// countIdle(), not count() - countBusy(): a retiring (STOPPING)` —
  `countIdle()`, а не `count() - countBusy()`: уходящий воркер (`STOPPING`) не
  занят и недоступен для диспетчеризации, но `count()` всё ещё включает его,
  пока `reapDeadWorkers()` не догонит; вычитание `countBusy()` из этого ошибочно
  посчитало бы его свободной ёмкостью.
- `// Workers still warming up are capacity ON ITS WAY, not capacity` — воркеры,
  которые ещё прогреваются, — это ёмкость **в пути**, а не отсутствующая
  ёмкость, и они не считаются простаивающими; без этого масштабировщик читал бы
  очередь, ждущую bootstrap, как очередь, ждущую слишком мало воркеров. Замерено
  до появления этой строки: пул с минимумом 2 и прогревом 3 с разрастался до 4,
  пока первые два воркера ещё подключались, форкал ещё двух, которым тоже нужно
  было подключаться, и сжимался обратно, когда все они сообщали о готовности, —
  всплеск подключений к базе ровно в момент, когда деплой наиболее хрупок.
  Дождись того, что уже едет, прежде чем просить больше.
- `// Queue is growing and there's no spare capacity to absorb it.` — очередь
  растёт, а свободной ёмкости, чтобы её поглотить, нет.

Здесь стоит задержаться ради стиля: решение о масштабировании опирается на **три
разных счётчика**, и каждый считает именно то, что нужно, а не то, что проще.
`count()` включает и уходящих `STOPPING` — поэтому для «слишком много
простаивающих» берут `countIdle()`, а не `count() - countBusy()`. А строка
`$starting === 0` — это записанный баг, который **стоил всплеска подключений к
БД** и теперь закреплён условием и комментарием. Хороший пример того, как проект
хранит не только код, но и причину, по которой код такой.

## 4. Эксперимент

### `make run-example` — пул, handshake и чистый выход

```text
$ CI=1 make run-example
docker compose exec -T php php bin/client_and_server.php
[2026-10-15 07:18:34] worker 14: warmed up, reporting ready
[2026-10-15 07:18:34] worker 15: warmed up, reporting ready
{"result":30}
answered in 216ms (a cold worker had to warm up first)
```

Команда поднимает master и двух воркеров **в одном процессе-демо**, задаёт один
вопрос и корректно останавливает всё. Видно ровно то, что обещано: воркеры
сначала греются и сообщают `ready`, и только после этого отвечают.
`{"result": 30}` — ответ от воркера через socket pair, а не от master'а: master
работу не делает. Строка «216ms» — это цена холодного старта первого запроса; в
persistent-режиме её платят **один раз**, а не на каждый запрос.

### Пропускная способность и автоскейл

```text
$ # терминал 1: php bin/server.php
$ docker compose exec php php bin/bench.php --clients=4 --requests=200
4 clients x 200 requests (pipeline 1) -> /tmp/php-worker-pool.sock

  duration        0.07s
  requests        800 (0 failed)
  throughput      11886 req/s
  latency  p50    0.23 ms
  latency  p95    0.32 ms
  latency  p99    0.41 ms
  latency  max    8.10 ms
--- server log ---
[2026-10-15 07:18:35] worker 24: warmed up in 253ms, reporting ready
[2026-10-15 07:18:35] worker 25: warmed up in 253ms, reporting ready
[2026-10-15 07:18:36] worker 31: warmed up in 252ms, reporting ready
[2026-10-15 07:18:36] worker 32: warmed up in 251ms, reporting ready
```

800 запросов, ни одного отказа, ~11 886 req/s. В логе master'а четыре воркера,
хотя пул стартовал с двух: под нагрузкой `$starting === 0` перестало быть
истинным только после того, как первые два прогрелись, и тогда пул вырос — ровно
тот сценарий, который описан в комментарии `Autoscaler`. `max 8.10 ms` при
`p50 0.23 ms` — это первый запрос, попавший на ещё прогревающийся воркер (или на
только что форкнутого): хвост здесь — цена честного старта, а не проблема
маршрутизации.

### Recycling: «handled 7 requests (max 7)»

```text
$ # php bin/chaos-server.php (RecyclingPolicy maxRequests: 7) + нагрузка
[2026-10-15 07:20:19] recycling worker 44: handled 7 requests (max 7)
[2026-10-15 07:20:19] recycling worker 45: handled 7 requests (max 7)
...
[2026-10-15 07:20:19] recycling worker 267: handled 7 requests (max 7)
```

и следом — снимок метрик по `SIGUSR1`:

```text
Worker Pool Status

Workers:
  Total: 4
  Idle: 4
  Busy: 0
  Draining: 0
  Crashed (lifetime): 0
  Recycled (lifetime): 21

Requests:
  Total: 163
  Completed: 163
  Failed: 0
```

**21 воркер переработан, 0 запросов потеряно.** Это прямое доказательство
свойства, обещанного в докблоке `RecyclingPolicy`: лимит `maxRequests: 7`
агрессивный (воркер живёт семь запросов), но `DRAINING` — «закончи текущее,
потом уходи» — делает агрессивный лимит бесплатным. Каждый `recycling worker N`
печатает **причину**: «handled 7 requests (max 7)». `Crashed: 0` — потому что
переработка не падение.

### Супервизия: `kill -9` и замена

Обычный сервер (`bin/server.php`, bootstrap с логированием готовности), затем
`SIGKILL` одному воркеру:

```text
### log before crash ###
[2026-10-15 07:20:54] worker 518: warmed up in 254ms, reporting ready
[2026-10-15 07:20:54] worker 517: warmed up in 255ms, reporting ready
### SIGKILL -> worker 517 ###
### log after crash (replacement ready) ###
[2026-10-15 07:20:54] worker 518: warmed up in 254ms, reporting ready
[2026-10-15 07:20:54] worker 517: warmed up in 255ms, reporting ready
[2026-10-15 07:20:54] worker 532: warmed up in 251ms, reporting ready
```

Воркер 517 убит `SIGKILL` (его нельзя перехватить) — и через доли секунды его
место занял воркер 532, **пройдя тот же readiness handshake** (251 мс). Master
не перезапускал сервер и не терял второго воркера. Проверим, что запросы после
краха обслуживаются, а счётчик падений вырос:

```text
Worker Pool Status

Workers:
  Total: 2
  Idle: 2
  Busy: 0
  Draining: 0
  Crashed (lifetime): 1
  Recycled (lifetime): 0
  Terminated (lifetime): 0

Queue:
  Pending: 0

Requests:
  Total: 3
  Completed: 3
  Failed: 0
  Timeout: 0
  Rejected: 0
  In flight: 0
```

`Crashed (lifetime): 1`, пул снова `Total: 2`, и три запроса после краха
завершены, **0 failed**. Так выглядит супервизия в действии: падение замечено,
запрос (если он был в работе) провален честно, ёмкость восстановлена, сервис не
остановился.

## 5. Отказы

Модель отказов проекта записана в `docs/FAILURE-MODEL.md#L64-L78`; таблица ниже
— её переложение для этой главы.

| Что ломается         | Чем обнаружено                         | Последствие                                             | Восстановление                                                           |
| -------------------- | -------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------ |
| Воркер падает        | `SIGCHLD` и, независимо, EOF на сокете | его запрос (если был) → `worker_crashed`                | замена форкается сразу, пул возвращается к размеру меньше чем за секунду |
| Воркер завис         | execution timeout (60 с)               | `SIGTERM`, при неудаче — `SIGKILL` на следующем проходе | замена форкается, слот не теряется навсегда                              |
| Воркер не прогрелся  | bootstrap timeout (30 с)               | `SIGTERM`, затем `SIGKILL`                              | замена; иначе воркер навсегда в `STARTING`                               |
| Воркер течёт/стареет | лимиты recycling                       | `DRAINING`, доработал текущее                           | замена запущена **до** его ухода, ёмкость не проседает                   |
| `fork()` не удался   | `launch()` бросил исключение           | залогировано, пул продолжает с меньшим числом           | повторится на следующем проходе/автоскейле                               |
| Клиент отключился    | EOF на его сокете                      | его ожидающие запросы сброшены сразу                    | ответ воркера отбрасывается                                              |
| Клиент шлёт мусор    | ошибка кадра / JSON                    | клиент отключён                                         | остальные не затронуты                                                   |
| Очередь переполнена  | ограниченная очередь                   | новые запросы → `server_overloaded`                     | клиент получает немедленный ответ                                        |
| **Master падает**    | ничем                                  | **всё в памяти потеряно**: очередь, реестр, состояние   | ничего нет — single point of failure (см. ниже)                          |

Отдельно стоит сказать вслух: у этой архитектуры есть граница, и она не
маскируется. `docs/FAILURE-MODEL.md#L82-L105` прямо называет master **единой
точкой отказа**: если он умрёт, пропадут и очередь, и все запросы в полёте, и
состояние воркеров; осиротевшие воркеры завершатся по EOF, но никто их не
заменит. Никакой репликации, лидер-элекшна или WAL здесь нет. Это честная
учебная позиция: production-_inspired_, но не production-_ready_, и граница
названа, а не спрятана.

## 6. Как это проверено

В отличие от курса из глав 4–5, это библиотека, и у неё есть настоящий набор
тестов (`make test` → phpunit) и статический анализ (`make analyse` → phpstan).
Для пула воркеров проверяется не одна функция, а «уцелело ли свойство под
сбоями»:

- **Юнит-тесты по фазам.** `docs/PHASES.md` — это не план, а **запись о том, как
  строился проект**; каждая фаза заканчивается списком тестов, держащих её
  Definition of Done. Например, Phase 7 (Dispatcher) держится тестами
  `testProcessesMoreRequestsThanWorkersThroughTheQueue` и
  `testDispatchInvokesOnResponseCallbackAsResponsesArrive` — второй доказывает,
  что диспетчеризация **событийная**, а не опрос в цикле.
- **E2E-тесты против настоящего master'а с настоящими форкнутыми воркерами**
  (`tests/E2E/`): `MasterEndToEndTest`, `ChaosTest` и `InvariantsTest`.
  Последний проверяет именно инварианты супервизии — например,
  `testRepeatedCrashesNeverGrowThePoolPastItsCeiling`: сколько бы воркеров ни
  убивали подряд, пул **не превысит** `maxWorkers`. `ChaosTest` гоняет
  recycling, reload, crash recovery и автоскейл в одном прогоне (для этого и
  существует `bin/chaos-server.php` с `maxRequests: 7`) — то есть то, что в
  реальности случается раз в час, здесь ужато в наблюдаемый тест.
- **Контракт рядом с кодом.** У `WorkerProcess`, `RecyclingPolicy`,
  `ForkedWorkerLauncher` — не только реализация, но и докблоки, фиксирующие
  **почему** так, включая конкретные баги («measured before this line existed»).
  Это делает ревью регрессий осмысленным: изменение поведения видно как
  изменение записанной причины.

Итого: 20 фаз, каждая с «Definition of Done» и списком тестов; E2E-уровень
специально построен так, чтобы воспроизводить гонки жизни и смерти воркеров.
Именно на этот набор опираются числа и сценарии выше.

## 7. Цифры

Документированные замеры — из `docs/BENCHMARKS.md` (PHP 8.5, 12 ядер у
контейнера, всё воспроизводимо через `bin/bench.php`). Работа = 20 000 итераций
`sqrt` (~2 мс CPU) на запрос, 8 клиентов × 200 запросов:

| Воркеров | Пропускная способность | Ускорение |      p50 |
| -------: | ---------------------: | --------: | -------: |
|        1 |               691 rq/s |      1.0× | 11.41 ms |
|        2 |             1 284 rq/s |      1.9× |  6.04 ms |
|        4 |             2 531 rq/s |      3.7× |  2.96 ms |
|        8 |             4 206 rq/s |      6.1× |  1.73 ms |

[`docs/BENCHMARKS.md#L39-L44`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/docs/BENCHMARKS.md#L39-L44)

Почти линейно: удвоил пул — удвоил пропускную способность, вдвое снизил
латентность. Разрыв между 6.1× и теоретическими 8× — это однопоточный master и
конкуренция клиентских процессов за те же ядра.

А вот та же форма для **тривиальной** работы (просто сложить два числа):

| Воркеров | Пропускная способность |     p50 |
| -------: | ---------------------: | ------: |
|        1 |            11 330 rq/s | 0.66 ms |
|        2 |            19 180 rq/s | 0.35 ms |
|        4 |            19 658 rq/s | 0.32 ms |
|        8 |            21 627 rq/s | 0.30 ms |

[`docs/BENCHMARKS.md#L58-L63`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/docs/BENCHMARKS.md#L58-L63)

После двух воркеров рост прекращается: узкое место — не воркеры, а единственный
master, через который проходит каждый запрос. Вывод проекта записан прямо:

> **more workers only buy throughput while the handler costs more than the
> plumbing.**

[`docs/BENCHMARKS.md#L70-L72`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/docs/BENCHMARKS.md#L70-L72)

И, наконец, перегрузка и обратное давление (медленный воркер 20 мс, 128
конкурентных запросов против пула на ~50 rq/s):

| Лимит очереди | Завершено | Отклонено | Что видит клиент                             |
| ------------: | --------: | --------: | -------------------------------------------- |
|        10 000 |       256 |         0 | все ответы, каждый ждёт ~5 с                 |
|            16 |        34 |       222 | 34 ответа, 222 × `server_overloaded`, быстро |

[`docs/BENCHMARKS.md#L110-L113`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/docs/BENCHMARKS.md#L110-L113)

Это и есть компромисс ограниченной очереди: не копить до исчерпания памяти, а
ответить «сейчас не могу» сразу — чтобы клиент мог осмысленно повторить,
сбросить или деградировать, а не ждать таймаута через пять секунд.

Замеры этой главы (живой прогон в Docker, PHP 8.5):

| Что                                     | Значение                             |
| --------------------------------------- | ------------------------------------ |
| `make run-example`, первый ответ        | 216 ms (холодный воркер)             |
| Нагрузка 4×200 на дефолтный сервер      | 11 886 rq/s, p50 0.23 ms, 0 failed   |
| Автоскейл под нагрузкой                 | 2 → 4 воркера                        |
| Recycling `maxRequests: 7`, 163 запроса | 21 переработка, 0 потерь             |
| `SIGKILL` воркера                       | замена готова через 251 ms, 0 failed |

## 8. Упражнения

1. **Объясните.** Почему master не может узнать о готовности воркера по таймеру,
   а обязан ждать сообщения `READY`? Что именно теряется, если отправить запрос
   в воркер сразу после `fork`?
2. **Почему.** Обработчик `SIGCHLD` всего лишь пишет байт в pipe. Почему нельзя
   разобрать `waitpid` и форкнуть замену прямо в обработчике? Какое состояние
   master'а могло бы оказаться «наполовину изменённым»?
3. **Найдите ошибку.** В `bin/server.php` подключение к БД открыто **до** пула и
   захвачено замыканием `use ($pdo)`. Что произойдёт с воркерами и почему именно
   так — вспомните главу 2 про то, что копируется при `fork`.
4. **Посчитайте.** Воркеров 4, `maxRequests: 100`. Через сколько запросов
   произойдёт первая переработка и сколько воркеров будет заменено на каждые 100
   запросов пула? Что произойдёт с p99, если bootstrap занимает 2 с?
5. **Спроектируйте.** `Autoscaler` смотрит на `countIdle()` и `countStarting()`.
   Добавьте третий сигнал — «средняя длина очереди за последнюю секунду» — и
   объясните, как он защитит от scale-up на единичном всплеске, но не помешает
   вырасти на устойчивой нагрузке.
6. **Сравните.** Здесь воркер, превысивший лимит, **осушается**, а не убивается.
   Какую цену платит система за `pm.max_requests` без `DRAINING` (как в наивном
   FPM-подобном решении)?

## Что дальше

- **Глава 7** — плавное завершение, таймауты и сигналы: что происходит между
  `SIGTERM` и `SIGKILL`, как self-pipe и маска сигналов делают shutdown
  безопасным, и почему `terminateStuckWorkers` смотрит на три разных таймаута.
- **Глава 8** — надёжная очередь задач: аренды, `ACK`, повторы и восстановление
  задач, которые держал упавший воркер.
- **Глава 4** — под всем этим: тот же `stream_select`, который master использует
  как диспетчер.
