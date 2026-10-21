# Глава 7. Корректное завершение: draining, таймауты, сигналы

> Проекты: [`php-worker-pool`](https://github.com/Researcher86/php-worker-pool),
> релиз `v1.1.0`, коммит `b616246`;
> [`php-mini-search`](https://github.com/Researcher86/php-mini-search), релиз
> `v1.0.0`, коммит `7ecddc0`. Нужно знать: процессы и супервизию (→ глава 6),
> цикл событий (→ глава 4). Пригодится дальше: очередь задач (→ глава 8),
> надёжность хранилища (→ глава 18).

Сервер запускается один раз и останавливается редко — именно поэтому остановка
почти всегда написана плохо. `kill` дефолтного действия убивает процесс на
месте: соединения рвутся, запросы теряются, дочерние процессы остаются сиротами,
а файл сокета — висеть в `/tmp` и путать следующий запуск. Для учебного скрипта
это неважно; для сервиса, который перезапускают при деплое, это разница между
«просадка без потерь» и «инцидент».

Корректное завершение — это три отдельных умения, которые обычно путают в одно:

1. **Сигналы** — узнать, что пора останавливаться, и не умереть от сигнала
   раньше, чем код к этому готов.
2. **Draining** (осушение) — перестать принимать новое, но **доработать
   принятое**, с жёстким дедлайном.
3. **Таймауты** — решить, что воркер «завис» и его пора снять, причём для
   каждого вида зависания — свой срок.

Плюс четвёртое, без которого первые три хрупкие: **всё это должно работать, пока
процесс одновременно обслуживает десятки дескрипторов**. Сигнал приходит между
двумя инструкциями чего угодно, и наивная обработка приводит к состоянию
«наполовину изменено».

---

## 1. Проблема

Возьмём мастер из главы 6 и посмотрим, что происходит на `SIGTERM`, если ничего
специально не делать:

- сигнал доставляется **внутри** какой-то операции — например, посреди цикла
  `foreach ($workers as ...)`; обработчик меняет ту же коллекцию → итерация
  ломается;
- `SIGTERM` может прийти **до** того, как зарегистрирован обработчик — тогда
  сработает дефолтное действие (завершение), и master умрёт, оставив после себя
  форки, сегмент разделяемой памяти и файл сокета;
- если просто закрыть сокет, клиенты получат обрыв вместо ответа — даже те, чьи
  запросы уже приняты;
- воркер, который не читает команду на выход, будет держать слот вечно;
- воркер, который не прогрелся (висит в `STARTING`), не занят ничем, но занимает
  ёмкость, и никто этого не замечает.

Хочется ровно обратного: `SIGTERM` → **перестать принимать**, **ответить всем,
кого уже принял**, дать воркерам доработать, снять зависших по таймауту, убрать
за собой сокет — и выйти с кодом 0. А если что-то не уложилось в дедлайн —
сказать клиенту понятную ошибку и выйти с понятным кодом.

## 2. Устройство

### Сигналы блокируются, а не «ловятся по возможности»

Первое решение — не вешать обработчики и надеяться, а **заблокировать все нужные
сигналы на всё время работы** и открывать окно ровно там, где безопасно:

```text
run():   SIG_BLOCK(SIGINT, SIGTERM, SIGCHLD, SIGHUP, SIGUSR1)
         ... вся инициализация: fork, shm, сокет ...
EventLoop::tick():
         SIG_UNBLOCK(...)          ← окно открыто
         stream_select(...)        ← сигнал прерывает select (EINTR)
         SIG_BLOCK(...)            ← окно закрыто
         ... обработчики дескрипторов: тут сигнал не выстрелит ...
```

Заблокированный сигнал не теряется — ядро держит его **pending** и доставит, как
только маска откроется. Поэтому `SIGTERM`, пришедший посреди стартапа, не
убивает master: он ждёт в ядре до момента, когда обработчик уже установлен. А
`SIGTERM`, пришедший во время `select`, прерывает его (EINTR), обработчик
выполняется (в окне ничего не бросает исключений), и цикл сразу видит флаг.

Ключевая идея: обработчик выполняется **только в окне вокруг `select`**, где
никакой код не находится в середине изменения состояния. Вот почему снаружи окна
сигнал даже не может прервать `foreach`.

### Self-pipe: работа сигнала — один байт в пайп

Даже в окне обработчик сигнала не должен делать произвольную работу: он
выполняется «между двумя инструкциями» любого кода. Поэтому все обработчики
делают одно и то же — пишут один байт в pipe, читающий конец которого
зарегистрирован в том же event loop:

| Байт | Сигнал             | Что значит                            |
| ---- | ------------------ | ------------------------------------- |
| `T`  | `SIGINT`/`SIGTERM` | остановиться (`stop()` уже снял флаг) |
| `C`  | `SIGCHLD`          | ребёнок умер — реапинг                |
| `H`  | `SIGHUP`           | reload                                |
| `U`  | `SIGUSR1`          | снять метрики                         |

`drainSignalPipe()` читает накопленные байты и выполняет работу в **обычном
последовательном контексте** цикла. Байты при этом **схлопываются**
(`array_unique`): пять `SIGCHLD` требуют одного прохода реапинга, а не пяти.

Исключение — `SIGTERM`: флаг `running = false` ставится **прямо в обработчике**,
а не остаётся на потом. Причина тонкая: пайп неблокирующий, и байт, который
нашёл его полным (буря `SIGCHLD`, которую ещё не разгребли), будет **потерян**.
Для пробуждения это нормально — байты уже в пайпе всё равно разбудят цикл, — но
для самого решения об остановке терять нельзя. Поэтому «останавливайся» живёт в
флаге, а байт `T` — только чтобы разбудить `select` немедленно, не дожидаясь
истечения таймаута.

### Draining: сначала дочитай то, что уже принято

`SIGTERM` не означает «закрой всё». Порядок такой:

1. **Закрыть сокет прослушивания** — новые соединения не принимаются.
2. **Один неблокирующий проход цикла** — дочитать то, что клиенты уже успели
   записать в сокет, но master ещё не прочитал. Эти байты формально принадлежат
   нам: запрос принят в тот момент, когда лёг в наш сокет. Без этого прохода
   `SIGTERM` сразу после всплеска видел `count() === 0`, пропускал draining
   целиком и молча терял эти запросы.
3. **Крутить цикл, пока есть незавершённые запросы**, до дедлайна
   (`gracefulShutdownTimeoutSeconds`), продолжая диспетчеризацию и снимая
   таймауты.
4. **Остаток по дедлайну** — не бросать молча, а ответить
   `server_shutting_down`.

Тот же принцип в `php-mini-search`, но там объект draining — **соединение**:
`Server::drain()` перестаёт принимать, закрывает **простаивающие** соединения и
помечает остальные, чтобы те ответили с `Connection: close`; `Service::drain()`
крутит цикл, пока счётчик соединений не ноль или пока не истёк `drainSeconds`.

### Таймауты: у каждого вида зависания свой срок

Дедлайн draining — про клиента («не заставляй его ждать вечно»). Но в пуле есть
и другие «вечности», и все они означают одно: этот воркер сам не станет
полезным. `terminateStuckWorkers()` — один проход, три разных лимита:

| Что висит                                            | Лимит                           | Почему отдельный                                                                                 |
| ---------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------ |
| **Запрос** (execution timeout)                       | `workerExecutionTimeoutSeconds` | это запрос, который никогда не кончится; клиент давно ушёл, воркер всё держит                    |
| **Воркер до готовности** (bootstrap timeout)         | `workerBootstrapTimeoutSeconds` | форкнут, никогда не сказал `READY`; без лимита навсегда в `STARTING`, ёмкость исчезает незаметно |
| **Воркер, которому велели уйти** (departure timeout) | `workerDepartureTimeoutSeconds` | единственное состояние, чей выход никто больше не ждёт с дедлайном                               |

Обратите внимание на `DRAINING`: воркер, которого осушают, **сохраняет весь
бюджет выполнения** дорабатываемого запроса — лимит засчитывается по самому
запросу, а не по моменту, когда воркер решили заменить.

### Эскалация: SIGTERM, потом SIGKILL

Снятие зависшего — тоже двухшаговое, как любое вежливое завершение в Unix:

```text
первый проход:  posix_kill($pid, SIGTERM)   → markTerminating()
второй проход:  posix_kill($pid, SIGKILL)   → нельзя перехватить/заблокировать
```

`SIGTERM` можно проигнорировать или не успеть обработать, `SIGKILL` — нельзя,
поэтому второй проход гарантирует, что процесс исчезнет и следующий `waitpid`
ограничен. Для ожидающего выхода (`DRAINING` → `STOPPING`) воркер помечается
`STOPPING` **до** сигнала — тогда реапер прочитает его выход как ожидаемый: без
зачёта краха и **без замены** (замена уже была запущена тем, кто его осушил).

### Сделать операции атомарными для реапера

`SIGCHLD` может прийти между **любыми** двумя инструкциями. Если в этот момент
код итерирует по `$workers` или считает `waitpid`, обработчик перетряхнёт ту же
коллекцию под ним. Решение — `withSigchldDeferred()`: на время одной изменяющей
операции `SIGCHLD` **блокируется** (не игнорируется — доставится сразу после), и
операция становится атомарной относительно реапера. Один и тот же приём защищает
`stop`, `reload`, `scaleUp/scaleDown`, `terminateStuckWorkers` и запись в
воркер.

### Сигнал дважды: «drain» и «выйди немедленно»

`php-mini-search` добавляет к этому ясную семантику для оператора: **первый**
`SIGTERM` — это `drain`, **второй** — «выходи немедленно, код 1». Плюс особый
случай: сигнал во время стартового replay не должен ничего ждать — там ещё нет
соединений, отвечать некому, можно выходить сразу с кодом 0.

## 3. Код

### Блокировка сигналов на время стартапа

```php
public function run(): void
{
    // Every signal registerSignalHandling() is about to catch is blocked
    // from here until run() returns, and only ever let through while the
    // loop waits. Two reasons, one per half of run():
    //
    // Startup. SIGTERM, SIGINT and SIGHUP all TERMINATE a process by
    // default, and between here and the handlers there are forks, a
    // shared memory segment and a socket file - a shutdown signal in
    // that window used to kill the Master outright, leaving all three
    // behind. Blocked, it is held pending by the kernel instead, and
    // handled the moment a handler exists for it.
    //
    // The loop. PHP runs a handler between two instructions of whatever
    // code is executing, and silently drops it if that code is mid-throw
    // - which the Master's is whenever a worker or a client disappears.
    // A shutdown signal landing there was simply lost. Taken only inside
    // the wait, where nothing throws, a signal cannot be.
    //
    // Workers forked meanwhile inherit the mask, and ForkedWorkerLauncher
    // resets both handlers and mask in the child. See docs/DECISIONS.md
    // "Signals during startup, and pipes nobody reads".
    pcntl_sigprocmask(SIG_BLOCK, self::HANDLED_SIGNALS, $previousMask);
```

[`src/Master/Master.php#L173-L195`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Master/Master.php#L173-L195)

**Перевод комментариев:**
- `// Every signal registerSignalHandling() is about to catch is blocked` —
  каждый сигнал, который собирается ловить `registerSignalHandling()`,
  заблокирован отсюда и до возврата из `run()` и пропускается только на время
  ожидания в цикле. Две причины, по одной на каждую половину `run()`.
  **Стартап.** `SIGTERM`, `SIGINT` и `SIGHUP` по умолчанию завершают процесс, а
  между этим местом и обработчиками есть форки, сегмент разделяемой памяти и
  файл сокета: сигнал остановки в этом окне раньше убивал Master на месте,
  оставляя все три следа. Заблокированный, он вместо этого держится ядром в
  состоянии pending и обрабатывается в момент, когда для него появляется
  обработчик. **Цикл.** PHP выполняет обработчик между двумя инструкциями любого
  исполняемого кода и молча теряет его, если этот код находится посреди `throw`,
  — а Master как раз там, когда исчезает воркер или клиент. Сигнал остановки,
  попавший туда, просто терялся. Принимаемый только внутри ожидания, где ничего
  не бросает, сигнал не может потеряться. Воркеры, форкнутые тем временем,
  наследуют маску, а `ForkedWorkerLauncher` сбрасывает и обработчики, и маску в
  ребёнке. См. `docs/DECISIONS.md`, раздел «Signals during startup, and pipes
  nobody reads».

Здесь стоит прочитать комментарий целиком: это два **разных** бага, каждый со
своей причиной. Первый — сигнал во время стартапа убивал процесс (дефолтное
действие) и оставлял мусор. Второй — PHP выполняет обработчик между двумя
инструкциями и **молча теряет** сигнал, если код в этот момент находится посреди
`throw` (а master как раз в таком состоянии, когда исчезает воркер или клиент).
Блокировка лечит оба: сигнал ждёт в ядре, а не в опасном месте.

### Окно вокруг `select` — единственное место, где сигнал стреляет

```php
// The window, and nothing but the select inside it: a signal pending
// since the last wait is delivered the instant it opens (its handler
// runs before the select is even entered), one arriving during the
// wait interrupts it, and closing it straight after means no handler
// ever runs inside the callbacks below - which are exactly the code
// that throws when a peer disappears.
if ($this->signalsDuringWait !== []) {
    pcntl_sigprocmask(SIG_UNBLOCK, $this->signalsDuringWait);
}

$ready = @stream_select($read, $write, $except, $seconds, $microseconds);

if ($this->signalsDuringWait !== []) {
    pcntl_sigprocmask(SIG_BLOCK, $this->signalsDuringWait);
}
```

[`src/EventLoop/EventLoop.php#L178-L192`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/EventLoop/EventLoop.php#L178-L192)

**Перевод комментариев:**
- `// The window, and nothing but the select inside it: a signal pending` —
  окно, и ничего кроме `select` внутри него: сигнал, ожидавший с прошлого
  ожидания, доставляется в момент открытия окна (его обработчик выполняется ещё
  до входа в `select`); пришедший во время ожидания прерывает его; а закрытие
  окна сразу после означает, что ни один обработчик никогда не выполнится внутри
  колбэков ниже — а это ровно тот код, который бросает исключения, когда
  исчезает собеседник.

Это ядро всей техники: окно открыто только на время `select`. Всё, что ниже
(обработка готовых дескрипторов), выполняется уже с заблокированными сигналами —
а именно там код бросает исключения при обрыве связи. `@` перед `stream_select`
гасит предупреждение EINTR: прерывание `select` сигналом — не ошибка, а
ожидаемый способ проснуться.

### Self-pipe: обработчик только будит

```php
/**
 * Main-loop side of the self-pipe: read whatever signal bytes have
 * accumulated and act on each distinct one once. Coalescing repeats is
 * deliberate - five SIGCHLDs still need only one reap pass (it drains
 * every zombie in one go), and reload() is already a no-op while one is
 * in progress. An empty read is harmless and simply ignored.
 */
private function drainSignalPipe(): void
{
    $bytes = fread($this->signalRead, 64);

    if ($bytes === false || $bytes === '') {
        return;
    }

    foreach (array_unique(str_split($bytes)) as $byte) {
        match ($byte) {
            'C' => $this->reapCrashedWorkers(),
            'H' => $this->pool->reload(),
            'U' => $this->dumpMetrics(),
            // stop() already cleared $running from the handler itself -
            // the byte's whole job was waking the select, and it has.
            'T' => null,
            default => null,
        };
    }
}
```

[`src/Master/Master.php#L421-L447`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Master/Master.php#L421-L447)

**Перевод комментариев:**
- `drainSignalPipe()` (PHPDoc): сторона главного цикла self-pipe: прочитать
  накопившиеся байты сигналов и отреагировать на каждый различный байт один раз.
  Схлопывание повторов намеренно: пять `SIGCHLD` всё равно требуют лишь одного
  прохода реапинга (он собирает всех зомби за один раз), а `reload()` уже
  является no-op, пока один выполняется. Пустое чтение безвредно и просто
  игнорируется.
- `// stop() already cleared $running from the handler itself -` — `stop()` уже
  очистил `$running` из самого обработчика; вся работа этого байта состояла в
  том, чтобы разбудить `select`, и он это сделал.

`match` по байту — мини-протокол между обработчиком сигнала и главным циклом.
`array_unique` перед разбором — не оптимизация ради оптимизации: без него пять
`SIGCHLD` вызвали бы пять проходов `waitpid`, каждый из которых всё равно
собирает всех зомби. Схлопывание — это **корректность**, а не микрооптимизация.

### Атомарность для реапера

```php
private function withSigchldDeferred(callable $operation): mixed
{
    pcntl_sigprocmask(SIG_BLOCK, [SIGCHLD], $previous);

    try {
        return $operation();
    } finally {
        pcntl_sigprocmask(SIG_SETMASK, $previous);
    }
}
```

[`src/Worker/WorkerPool.php#L877-L886`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerPool.php#L877-L886)

`SIG_BLOCK`, а не `SIG_IGN`: заблокированный сигнал **доставится** сразу после
восстановления маски. Операция просто перестаёт бояться, что реапер вклинится
между двумя её инструкциями. `finally` гарантирует, что маска восстановится даже
если операция бросит исключение.

### Draining при `SIGTERM`

```php
private function shutdown(UnixSocketServer $server): float
{
    $server->close();

    // Absorb what clients have already sent but we haven't read yet.
    // Those bytes are ours - the request was accepted the moment it
    // landed in our socket - but until this pass runs they exist
    // nowhere the drain below can see: PendingRequestRegistry is still
    // empty for them. Without it, a SIGTERM arriving just after a burst
    // was written found count() === 0, skipped draining entirely, and
    // dropped every one of those requests silently. One non-blocking
    // pass over every ready fd is enough, and it can't block shutdown:
    // it reads what is already there and returns.
    //
    // The cutoff is here on purpose. A request written after this pass
    // is one that arrived after we began shutting down, and it gets the
    // connection closing under it - the same answer any server gives
    // for "you were too late".
    $this->loop->tick(0.0);

    $deadline = $this->clock->now() + $this->gracefulShutdownTimeoutSeconds;

    while ($this->pendingRequests->count() > 0 && $this->clock->now() < $deadline) {
        $this->loop->tick(min($deadline - $this->clock->now(), self::TIMEOUT_CHECK_INTERVAL_SECONDS));

        $this->dispatcher->dispatchQueued(); // "finish QUEUED requests" too, not just in-flight ones
        $this->sendTimeouts();

        // A client that can no longer be written to would otherwise
        // keep its pending entries here for the whole drain window,
        // holding shutdown open to deliver answers it cannot receive.
        $this->clients->removeBroken();
    }

    // Whatever's left didn't finish inside the safety timeout - tell
    // those clients rather than just abandoning them (pool->stop(),
    // right after this returns, is about to forcibly end the workers
    // still holding some of these anyway).
    foreach ($this->pendingRequests->drainAll() as $stillPending) {
        $this->requestMetrics->recordFailed();
        $this->answerWithError($stillPending, 'server_shutting_down');
    }
```

[`src/Master/Master.php#L653-L694`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Master/Master.php#L653-L694)

**Перевод комментариев:**
- `// Absorb what clients have already sent but we haven't read yet.` —
  поглотить то, что клиенты уже отправили, но мы ещё не прочитали. Эти байты
  наши — запрос был принят в момент, когда лёг в наш сокет, — но до этого
  прохода они нигде не существуют для draining ниже: `PendingRequestRegistry`
  для них ещё пуст. Без него `SIGTERM`, пришедший сразу после записанного
  всплеска, видел `count() === 0`, полностью пропускал draining и молча терял
  каждый из этих запросов. Достаточно одного неблокирующего прохода по всем
  готовым fd, и он не может заблокировать shutdown: читает то, что уже есть, и
  возвращается. Отсечка здесь намеренно. Запрос, записанный после этого прохода,
  — тот, что пришёл после начала остановки, и соединение закроется у него под
  ногами: тот же ответ, который любой сервер даёт на «ты опоздал».
- `// "finish QUEUED requests" too, not just in-flight ones` — «завершить и
  `QUEUED`-запросы», а не только те, что уже в полёте.
- `// A client that can no longer be written to would otherwise` — клиент,
  которому больше нельзя писать, иначе держал бы свои ожидающие записи здесь всё
  окно draining, удерживая shutdown открытым, чтобы доставить ответы, которые не
  может получить.
- `// Whatever's left didn't finish inside the safety timeout - tell` — то, что
  осталось, не уложилось в страховочный таймаут: скажи этим клиентам, а не
  просто бросай их (`pool->stop()` сразу после возврата всё равно принудительно
  завершит воркеров, которые ещё держат часть из них).

Три детали, которые делают это **корректным**, а не просто работающим:
`$this->loop->tick(0.0)` до дедлайна (дочитать уже принятое — иначе «молчаливая»
потеря запросов), `dispatchQueued()` внутри окна (запросы, стоявшие в очереди,
тоже должны дожить, а не только те, что уже на воркере) и последний `drainAll()`
с ответом `server_shutting_down` (не оставлять клиента гадать).

### Один проход — три таймаута, эскалация на втором

```php
public function terminateStuckWorkers(
    float $limitSeconds,
    ?float $bootstrapLimitSeconds = null,
    ?float $departureLimitSeconds = null,
): int {
    return $this->withSigchldDeferred(function () use ($limitSeconds, $bootstrapLimitSeconds, $departureLimitSeconds): int {
        $now = $this->clock->now();
        $signalled = 0;

        foreach ($this->workers as $pid => $worker) {
            $reason = $this->stuckReason($worker, $now, $limitSeconds, $bootstrapLimitSeconds, $departureLimitSeconds);

            if ($reason === null) {
                continue;
            }

            $escalate = $worker->isTerminating();

            // A worker that was already leaving goes to STOPPING before
            // the signal, and that is what keeps this from overshooting:
            // the reaper reads a STOPPING exit as expected - no crash
            // counted, and no replacement, because whoever drained it
            // launched one already. Its socket goes with it (a drained
            // worker's is still open), so the Dispatcher stops watching
            // a worker that is about to die.
            if ($worker->isDraining()) {
                $worker->stop($now);
                $worker->close();
            }

            posix_kill($pid, $escalate ? SIGKILL : SIGTERM);

            if (!$escalate) {
                $worker->markTerminating();
                $signalled++;

                $this->logger->log(sprintf('terminating worker %d: %s', $pid, $reason));
            }
        }

        return $signalled;
    });
```

[`src/Worker/WorkerPool.php#L522-L563`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerPool.php#L522-L563)

**Перевод комментариев:**
- `// A worker that was already leaving goes to STOPPING before` — воркер,
  который уже уходил, переводится в `STOPPING` до сигнала, и именно это не даёт
  проскочить: реапер читает выход из `STOPPING` как ожидаемый — крах не
  засчитывается, замена не нужна, потому что тот, кто его осушил, уже запустил
  одну. Его сокет уходит вместе с ним (у осушённого воркера он ещё открыт),
  поэтому `Dispatcher` перестаёт следить за воркером, который вот-вот умрёт.

`$escalate = $worker->isTerminating()` — вот и вся эскалация: первый проход
пометил воркер терминируемым и послал `SIGTERM`; если он переживёт лимит выхода,
следующий проход увидит `isTerminating() === true` и пошлёт `SIGKILL`.
`markTerminating()` вызывается **только** для `SIGTERM` — это и есть память о
первом шаге.

### Финальный `SIGKILL` при остановке пула

```php
// pcntl_waitpid(-1, ...) reaps whichever child exits next, not a
// specific pid, so there's no way to know which WorkerProcess it
// belonged to - $awaiting just counts however many are still out
// there, regardless of which. No sleep between WNOHANG polls would
// busy-spin the CPU for the whole timeout whenever a worker takes
// any real time to exit, so back off slightly between attempts.
$deadline = microtime(true) + $timeoutSeconds;

while ($awaiting > 0 && microtime(true) < $deadline) {
    $pid = pcntl_waitpid(-1, $status, WNOHANG);

    if ($pid > 0) {
        $awaiting--;
    } elseif ($pid === -1) {
        // No child processes exist at all (errno ECHILD) - nothing
        // left to wait for. Real workers that already exited take
        // this path too, but it's also what a WorkerLauncher test
        // double with no real process behind it looks like from the
        // very first check - without this, $awaiting would never
        // reach 0 and this loop would just spin for the full
        // timeout every time.
        break;
    } else {
        usleep(10_000);
    }
}

// Anything still alive past the timeout is stuck (or just never
// read the SHUTDOWN message) - force it. SIGKILL can't be caught,
// blocked, or ignored, so every remaining worker is guaranteed to
// actually exit almost immediately, bounding the final reap below
// even though that wait isn't itself time-limited.
foreach ($this->workers as $worker) {
    if ($worker->getState() !== WorkerState::DEAD) {
        posix_kill($worker->getPid(), SIGKILL);
    }
}

while (pcntl_waitpid(-1, $status) !== -1) {
    // reap whatever's left
}

foreach ($this->workers as $worker) {
    if ($worker->getState() !== WorkerState::DEAD) {
        $worker->markDead();
    }
}
```

[`src/Worker/WorkerPool.php#L810-L856`](https://github.com/Researcher86/php-worker-pool/blob/b616246957c0fac9bbb71100c14663b86f74c2a3/src/Worker/WorkerPool.php#L810-L856)

**Перевод комментариев:**
- `// pcntl_waitpid(-1, ...) reaps whichever child exits next, not a` —
  `pcntl_waitpid(-1, ...)` собирает того ребёнка, который завершится следующим,
  а не конкретный pid, поэтому нельзя узнать, какому `WorkerProcess` он
  принадлежал: `$awaiting` просто считает, сколько их ещё не завершилось,
  независимо от того, каких именно. Без паузы между опросами `WNOHANG` процесс
  сжигал бы CPU весь таймаут всякий раз, когда воркеру нужно сколько-то
  реального времени на выход, поэтому между попытками — небольшая задержка.
- `// No child processes exist at all (errno ECHILD) - nothing` — дочерних
  процессов вообще нет (errno `ECHILD`) — ждать нечего. Реальные воркеры,
  которые уже вышли, тоже идут этим путём, но так же выглядит и тестовый двойник
  `WorkerLauncher` без реального процесса за ним, причём с самой первой
  проверки: без этого `$awaiting` никогда не достиг бы 0, и цикл крутился бы
  весь таймаут каждый раз.
- `// Anything still alive past the timeout is stuck (or just never` — всё, что
  ещё живо после таймаута, зависло (или так и не прочитало сообщение `SHUTDOWN`)
  — принудить. `SIGKILL` нельзя поймать, заблокировать или проигнорировать,
  поэтому каждый оставшийся воркер гарантированно завершится почти сразу,
  ограничив финальный реап ниже, хотя само это ожидание не ограничено по
  времени.
- `// reap whatever's left` — собрать всех, кто остался.

Здесь видны три вещи: `WNOHANG` плюс `usleep`, чтобы не сжигать CPU во время
ожидания; ветка `$pid === -1` (errno `ECHILD`) — чтобы цикл не крутился впустую,
когда детей уже нет; и финальный `SIGKILL` как гарантия, что последующий
`waitpid` без таймаута отработает мгновенно.

### Второй сигнал: `drain`, потом «выходи немедленно»

```php
private function drain(Server $server): int
{
    $server->drain();
    $this->logger->info('draining', ['connections' => $server->connectionCount(), 'deadline_s' => $this->config->drainSeconds]);

    $deadline = hrtime(true) / 1e9 + $this->config->drainSeconds;
    while ($server->connectionCount() > 0 && hrtime(true) / 1e9 < $deadline) {
        $server->tick(0.1);
    }

    $left = $server->connectionCount();
    $server->close();
    if ($left > 0) {
        $this->logger->error('drain_deadline_passed', ['connections' => $left]);

        return 1;
    }

    return 0;
}

private function onSignal(int $signal): void
{
    $this->signals++;

    if (!$this->serving) {
        $this->logger->info('stopped', ['reason' => 'signal during replay']);
        exit(0);
    }
    if ($this->signals > 1) {
        $this->logger->error('stopped', ['reason' => 'second signal']);
        exit(1);
    }

    $this->logger->info('signal', ['signal' => $signal]);
}
```

[`src/Runtime/Service.php#L94-L129`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Runtime/Service.php#L94-L129)

Другой вкус того же блюда: `drain()` возвращает **код выхода** — 0, если все
соединения закрылись до дедлайна, и 1, если дедлайн прошёл, а соединения
остались. А `onSignal()` кодирует операторский контракт: первый сигнал — просьба
уйти красиво, второй — приказ уйти немедленно. Особый случай `!$serving` —
сигнал во время replay: ждать нечего, выходим сразу.

## 4. Эксперимент

### `SIGTERM`: draining без потерь и уборка за собой

`php-mini-search`, чистый запуск и один `SIGTERM`:

```text
$ CI=1 docker compose exec -T -e MINI_SEARCH_TOKEN=demo-token-demo-token-demo-token-0 \
    php bash -c 'php bin/mini-search serve --data=/tmp/msdata & SRV=$!; sleep 1.2; kill -TERM $SRV; wait $SRV; echo exit=$?'
{"time":"...","level":"info","event":"replayed","status":"missing","records":0,...,"ms":6.6}
{"time":"...","level":"info","event":"listening","address":"127.0.0.1:8080"}
{"time":"...","level":"info","event":"signal","signal":15}
{"time":"...","level":"info","event":"draining","connections":0,"deadline_s":10}
{"time":"...","level":"info","event":"stopped","reason":"signal","exit_code":0}
exit=0
```

Полный жизненный цикл в пяти строках: `signal 15` → `draining` (с дедлайном 10
с) → `stopped` с `exit_code 0`. Сервис не пропустил `SIGTERM` и не умер от него
— он **обработал** его.

`php-worker-pool`, тот же сценарий плюс проверка файла сокета:

```text
$ WORKER_POOL_SOCKET=/tmp/shut.sock php bin/server.php & SRV=$!; sleep 1
### socket before shutdown ###
srw------- 1 root root 0 Oct 15 07:25 /tmp/shut.sock
### SIGTERM -> 545 ###
exit=0
### socket after shutdown ###
ls: cannot access '/tmp/shut.sock': No such file or directory
```

Сокет `0600` существовал до сигнала и **исчез** после — `run()` в `finally`
вызывает `pool->stop()`, воркеры собраны, файл удалён. Без этого следующий
запуск наткнулся бы на «живой» сокет мёртвого процесса.

### Эскалация `SIGTERM` → `SIGKILL`

Сценарий «воркер, которому велели уйти, но который не уходит» проверяется
детерминированно — тестом, а не гонкой в терминале:

```text
$ vendor/bin/phpunit --filter testAStuckWorkerIsSignalledOnceThenEscalated
PHPUnit 11.5.56 by Sebastian Bergmann and contributors.
Runtime:       PHP 8.5.11
.                                                                   1 / 1 (100%)
Time: 00:00.025, Memory: 12.00 MB
OK (1 test, 4 assertions)
```

Тест назван прямо по механизму: «зависший воркер сигналится **один раз**, затем
эскалируется». Он держит воркер-двойник, который не реагирует на `SIGTERM`, и
проверяет, что первый проход послал `SIGTERM` ровно один раз, а второй —
`SIGKILL`.

### Reload под нагрузкой: замена пула без потери запросов

`SIGHUP` посреди активного трафика:

```text
$ php bin/bench.php --clients=2 --requests=3000 &  # фоном
$ kill -HUP $SRV                                    # на ходу
2 clients x 3000 requests (pipeline 1) -> /tmp/rb.sock
  duration        0.74s
  requests        6000 (0 failed)
  throughput      8153 req/s
  latency  p50    0.14 ms
  latency  p95    0.22 ms
  latency  p99    0.27 ms
  latency  max    261.09 ms
### one more request ###
1 clients x 1 requests (pipeline 1) -> /tmp/rb.sock
  ... 1 (0 failed) ...
```

6000 запросов во время reload, **0 failed**: воркеры, занятые запросом, никуда
не делись, а замена подхватила новый трафик. Это `reload()` из главы 6 в
действии: новое поколение запускается первым, старое — осушается.

### Живая находка: `SIGHUP` при простое роняет master

А теперь честный результат, который я не ожидал. Если послать `SIGHUP`, когда
воркеры **простаивают** (нет трафика), master падает. Воспроизведение — три раза
из трёх:

```text
$ for i in 1 2 3; do
    WORKER_POOL_SOCKET=/tmp/rr$i.sock php bin/server.php > /tmp/rr$i.log 2>&1 &
    srv=$!; # дождаться двух "reporting ready"
    kill -HUP $srv; sleep 0.6
    kill -0 $srv 2>/dev/null && echo "run $i: ALIVE" || echo "run $i: CRASHED"
  done
run 1: CRASHED
run 2: CRASHED
run 3: CRASHED
$ grep -m1 -A6 'Fatal error' /tmp/rr1.log
Fatal error: Uncaught TypeError: stream_select(): supplied resource is not a valid stream resource in /app/src/IPC/Socket.php:92
Stack trace:
#0 /app/src/IPC/Socket.php(92): stream_select(Array, Array, Array, 0, 0)
#1 /app/src/Worker/WorkerProcess.php(395): PhpWorkerPool\IPC\Socket->readAvailable()
#2 /app/src/Dispatcher/Dispatcher.php(166): PhpWorkerPool\Worker\WorkerProcess->readAvailable()
#3 /app/src/EventLoop/EventLoop.php(204): PhpWorkerPool\Dispatcher\Dispatcher->{closure:PhpWorkerPool\Dispatcher\Dispatcher::watch():164}()
#4 /app/src/Master/Master.php(282): PhpWorkerPool\EventLoop\EventLoop->tick(1.0)
#5 /app/bin/server.php(63): PhpWorkerPool\Master\Master->run()
#6 {main}
Next ValueError: No stream arrays were passed in /app/src/IPC/Socket.php:92
```

Что происходит: `reload()` при простое сразу переводит старых воркеров в
`DRAINING` и вызывает `retireIdleWorkers()`, а тот **закрывает** их сокеты
(`$worker->close()`) прямо во время обработки сигнала — то есть внутри
`foreach ($read as $resource)` в `EventLoop::tick()`. Закрытый ресурс уже не
валиден, но его обработчик всё ещё в `$readHandlers`, и вызов доходит до
`Socket::readAvailable()`, где `stream_select()` на пустом (после отсева
невалидных ресурсов) массиве в PHP 8 бросает `ValueError`, а не возвращает
`false`. `forgetClosedResources()` чистит закрытые ресурсы только **в начале**
следующего тика — этого окна достаточно, чтобы кликнуло.

Почему тесты это не ловят:
`ChaosTest::testReloadReplacesEveryWorkerWithoutLosingRequests` шлёт запросы
**до** `SIGHUP`, поэтому воркеры заняты и `reload()` их не трогает — путь
«закрыть сокет простаивающего воркера посреди тика» просто не исполняется. Под
нагрузкой reload корректен (эксперимент выше), при простое — нет.

Я оставляю это здесь как есть, потому что это ровно то, о чём глава: обработка
сигнала сходится с циклом событий, и **редкий путь** (закрытие дескриптора в
середине тика) оказывается незакрытым. Проверенное лекарство напрашивается само:
снимать регистрацию читаемости вместе с `close()`, либо не вызывать обработчик
для ресурса, который перестал быть `is_resource()`, прямо в `EventLoop::tick()`.
Это баг проекта `v1.1.0`, а не свойство дизайна; строка `Socket.php:92` и номер
версии зафиксированы, чтобы находка была проверяемой.

## 5. Отказы

| Что ломается                                        | Чем это проявится                     | Что мешает                                                       |
| --------------------------------------------------- | ------------------------------------- | ---------------------------------------------------------------- |
| Сигнал приходит посреди изменения состояния         | обработчик портит `$workers`/счётчики | окно только вокруг `select`; `withSigchldDeferred()`             |
| Сигнал приходит до установки обработчика            | дефолтное действие убивает процесс    | `SIG_BLOCK` на стартапе, сигнал ждёт в ядре                      |
| Сигнал теряется при `throw` в главном цикле         | остановка не наступает                | обработчики выполняются только в окне, где не бросают            |
| Байт сигнала не влез в полный пайп                  | потеря пробуждения                    | `SIGTERM` ставит флаг в самом обработчике, байт — только wake-up |
| `SIGTERM` сразу после всплеска запросов             | «молчаливая» потеря принятого         | неблокирующий `tick(0.0)` до draining                            |
| Запросы зависли в очереди при shutdown              | клиенты не получили ответа            | `dispatchQueued()` внутри окна + `server_shutting_down`          |
| Воркер игнорирует команду выхода                    | слот занят вечно                      | `SIGTERM` → `SIGKILL` по `workerDepartureTimeout`                |
| Воркер завис в `STARTING`                           | ёмкость исчезает незаметно            | `workerBootstrapTimeoutSeconds`                                  |
| Воркер дорабатывает долгий запрос                   | его снимают раньше времени            | `DRAINING` сохраняет весь execution-бюджет                       |
| Стрим-закрытие в середине тика (reload при простое) | fatal `ValueError` в `stream_select`  | **не закрыто** — см. живую находку выше                          |
| Клиент отключился во время draining                 | висящая запись держит shutdown        | `clients->removeBroken()` в цикле                                |
| Пул не уложился в дедлайн                           | —                                     | `SIGKILL` каждому живому воркеру, выход ограничен                |

## 6. Как это проверено

`php-worker-pool` покрывает эту главу **отдельными тестовыми файлами на каждый
таймаут**, и это правильный уровень детализации, потому что у каждого — своя
причина существовать:

- `tests/Worker/ExecutionTimeoutTest.php` — включая
  `testAnIdleWorkerIsNeverTerminatedHoweverLongItHasExisted` (у простоя **нет**
  execution-лимита), `testTheClockStartsAgainForEachRequest` (бюджет считается
  по запросу, а не по воркеру) и `testAStuckWorkerIsSignalledOnceThenEscalated`
  (тот самый `SIGTERM` → `SIGKILL`).
- `tests/Worker/DepartureTimeoutTest.php` —
  `testARetiredWorkerThatNeverExitsIsSignalledThenEscalated`,
  `testTheDepartureClockStartsAtTheDrainNotAtTheRetirement`.
- `tests/Worker/ReadinessTest.php` —
  `testNoBootstrapLimitMeansNoBootstrapTimeout`,
  `testTheExecutionLimitDoesNotApplyToAWorkerStillStarting`.
- `tests/Worker/WorkerPoolTest.php` —
  `testStopKillsAWorkerThatNeverRespondsToShutdown`.
- `tests/Master/MasterSignalTest.php` — сигналы **во время стартапа**:
  `testAShutdownSignalDuringStartupIsAShutdownNotADeath` и
  `testAReloadSignalDuringStartupDoesNotKillTheMaster` — это про ту самую
  блокировку из раздела 3.
- `tests/E2E/ChaosTest.php` и `tests/E2E/InvariantsTest.php` — сценарии
  «целиком»: `testShutdownDeliversAcceptedWorkAndCleansUp` (принятое отвечено,
  сокет убран), `testNothingSurvivesShutdown`,
  `testReloadReplacesEveryWorkerWithoutLosingRequests`.

`php-mini-search` держит ту же дисциплину: `Service::drain()` возвращает код
выхода, а `onSignal()` — чистая функция от числа сигналов, что делает оба легко
тестируемыми в изоляции.

И честная оговорка: набор тестов **не** покрывал reload при простое — ровно
поэтому находка выше и дожила до чтения кода. Тест, который бы её поймал, шлёт
`SIGHUP` на **простаивающий** пул и проверяет, что процесс жив, — я упоминаю это
как недостающий тест, а не как существующий.

## 7. Цифры

Замеры этой главы (Docker, PHP 8.5.11):

| Что                         | Значение                                                                 |
| --------------------------- | ------------------------------------------------------------------------ |
| `php-mini-search` `SIGTERM` | `signal 15` → `draining` (`deadline_s 10`) → `stopped`, `exit_code 0`    |
| `php-worker-pool` `SIGTERM` | `exit=0`, файл сокета удалён                                             |
| Reload под нагрузкой        | 6 000 запросов, **0 failed**, p99 0.27 ms                                |
| Reload при простое          | падение, **3 из 3** прогонов                                             |
| Эскалация                   | `testAStuckWorkerIsSignalledOnceThenEscalated` — OK (1 тест, 4 проверки) |

Цифры, задающие таймауты по умолчанию (`php-worker-pool`, `Master`):

| Лимит                            | Значение | Про кого                        |
| -------------------------------- | -------- | ------------------------------- |
| `requestTimeoutSeconds`          | 30 с     | клиент: перестать ждать         |
| `workerExecutionTimeoutSeconds`  | 60 с     | воркер: запрос не завершится    |
| `gracefulShutdownTimeoutSeconds` | 30 с     | сигнал → доработать или сдаться |
| `workerBootstrapTimeoutSeconds`  | 30 с     | воркер: не прогрелся            |
| `workerDepartureTimeoutSeconds`  | 10 с     | воркер: велено уйти, не уходит  |

Обратите внимание на порядок: execution (60 с) **больше** request (30 с) — и это
осознанно (см. комментарий в конструкторе из главы 6). Когда срабатывает
execution-лимит, запрос не «опоздал», а **никогда не завершится**, поэтому
клиенту уже сказали `request_timeout`, а воркер всё равно снимается и
заменяется.

## 8. Упражнения

1. **Объясните.** Почему `SIGTERM` блокируется, а не просто ловится? Что именно
   ломается в окне между `run()` и установкой обработчика, и почему «pending в
   ядре» это чинит?
2. **Почему.** Обработчик сигнала только пишет один байт в пайп, а работу делает
   главный цикл. Приведите три разных способа, которыми «сделать работу прямо в
   обработчике» сломало бы программу.
3. **Найдите ошибку.** В `drainSignalPipe()` байты схлопываются через
   `array_unique`. Что произойдёт, если убрать `array_unique`, и почему для
   `SIGCHLD` это не только про производительность?
4. **Посчитайте.** Воркеров 4, execution-лимит 60 с, request-лимит 30 с. Клиент
   шлёт запрос, который виснет. В какой момент клиент получит ошибку, в какой —
   воркер получит `SIGTERM`, а в какой, если не отреагирует, — `SIGKILL`?
5. **Спроектируйте.** `reload()` при простое падает, потому что сокет воркера
   закрывается посреди тика. Предложите два независимых исправления (одно — в
   момент закрытия, другое — в момент вызова обработчика) и объясните, какое из
   них надёжнее.
6. **Сравните.** `php-worker-pool` при `SIGTERM` ждёт до
   `gracefulShutdownTimeoutSeconds`, а `php-mini-search` — до `drainSeconds` и
   возвращает код 1, если не успел. Почему это **разные** вопросы («сколько
   ждать») и («что считать успехом»), и как второй кодируется в exit code?

## Что дальше

- **Глава 8** — надёжная очередь задач: аренды, `ACK`, повторы. Draining из этой
  главы отвечает на вопрос «что с запросом, который сейчас в работе»; очередь
  задач отвечает на тот же вопрос **после** падения.
- **Глава 6** — под signals/self-pipe: супервизия, `SIGCHLD`, реапинг и замена
  воркеров, на которых всё это опирается.
- **Глава 4** — тот самый `stream_select`, окно вокруг которого определяет,
  когда сигнал вообще может выстрелить.
