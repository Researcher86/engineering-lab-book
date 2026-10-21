# Глава 22. Границы компонентов и владение процессами

> Проект:
> [`php-systems-platform`](https://github.com/Researcher86/php-systems-platform)
> (коммит `9a6a916`) — первая из трёх глав этой части. Нужно знать: как
> компонент становится процессом (→ главы 6–7), очередь и воркеров (→ главы 8–9)
> и производные данные (→ глава 21). Пригодится дальше: как отказы
> распространяются через эти границы (→ глава 23) и что видно снаружи (→ глава
> 24).
>
> Платформа собирает шесть компонентов лаборатории в одну систему. Главы 1–21
> разбирали каждый по отдельности; здесь вопрос в другом: **где проходит
> граница** между узлом и узлом и **кто владеет** тем, что границу пересекает.

## 1. Проблема

Интеграционный проект соединяет шесть проектов: HTTP-сервер, базу, кеш, очередь,
пул воркеров и поиск. У каждого — свой репозиторий, свои версии в
`composer.json`, свои представления о том, что такое «сервер». У платформы есть
ровно два вопроса к каждому из них:

1. **Как её достигают** — импортом класса в своём процессе, дочерним процессом
   или сетевым вызовом к отдельной службе?
2. **Кто её останавливает** — тот, кто запустил, или кто-то другой?

Вопросы выглядят административными, но ошибки здесь дороже ошибок в алгоритме.
Два конкретных провала из истории платформы это показывают.

**Первый: остановить чужое.** Кеш в этой лаборатории не имеет режима демона:
сервер кеша — это дочерний процесс того, кто его запустил, а не служба с
pid-файлом. Если команда, которая кеш **не** запускала, всё равно «наведёт
порядок» на выходе, она убьёт инфраструктуру соседа. Поэтому в
`docs/ownership.md` записано правило, что команда останавливает только то, что
начала сама, а уже работающий сервер **принимает** (adopt) и оставляет работать.

**Второй: не остановить своё.** Первая версия `serve` останавливала ресурсы
вручную: каждый — под своим флагом владения. Флаги были расставлены правильно;
неправильно было **место** проверки. Путь, который выбрасывал исключение **до**
блока остановок, пропускал их все. Занятый порт делал ровно это: `serve`
печатала `Could not start HTTP server`, возвращалась — и оставляла работающими
пул воркеров, сервер кеша и демон базы. Следующая `serve` их принимала как «уже
запущенные» — и уже никогда не останавливала. Утечка была самоподдерживающейся,
и никакой отдельный флаг её не лечил: флаг не защищает путь, который до флага не
дошёл.

Отсюда третий, самый коварный случай: **остановить вовремя**. Процесс, которому
послали `SIGTERM`, обязан остановиться сам; процесс, который `SIGTERM`
проигнорировал, надо убивать. Эти два исхода нельзя путать: проверка «воркеры
завершились корректно» строится ровно на различии между «остановился» и
«пришлось убить». А ещё в CI ловился случай, когда `SIGTERM` уходил в пул,
обработчик выставлял флаг, и мастер продолжал жить минутами — и останавливался
на **повторном** `SIGTERM`. Один непрочитанный сигнал вешал ожидающего навсегда.

## 2. Устройство

### Карта границ

```text
minidb.php (daemonized, reparented to init)
     │  owns database state, tracked by minidb.pid
     │
serve
 ├── cache.php           serve owns the cache server (a child; no daemon mode)
 ├── mini-search serve   serve owns the search service (a child, over HTTP)
 ├── worker.php          serve owns the pool Master
 │   ├── worker #1       the Master forks the floor (min 2)
 │   └── worker #2
 │
queue:consume            adopts the pool Master — owns nothing it adopted
 └── forwarder x4        the consumer's own dispatch processes
```

Пять из шести компонентов подключаются как библиотеки; **один** — нет. Разница
не в аккуратности, а в том, что именно пересекает границу: структуры данных в
адресном пространстве или байты по проводу.

| Компонент              | Как достигнут                                               | Владение                                                   |
| ---------------------- | ----------------------------------------------------------- | ---------------------------------------------------------- |
| `php-mini-http-server` | библиотека, `Server` в процессе `serve`                     | сокет держит `serve`                                       |
| `php-mini-database`    | клиент-библиотека + демон, отдельный процесс                | `minidb.pid`; порт — контрольная проверка                  |
| `php-mini-cache`       | клиент-библиотека + дочерний сервер                         | тот, кто запустил; режима демона нет                       |
| `php-job-queue`        | библиотека: продюсер в `serve`, консьюмер в `queue:consume` | журнал; ни один процесс не «владеет» очередью              |
| `php-worker-pool`      | SDK-клиент + мастер `worker.php` дочерним процессом         | запустивший мастер; остальные принимают                    |
| `php-mini-search`      | **только HTTP**, ни одного импортированного класса          | дочерний процесс `serve`; журнал поиска принадлежит службе |

Последняя строка — единственная настоящая граница по сети. Платформа требует
пакет `researcher86/php-mini-search` (ей нужен бинарь `vendor/bin/mini-search`),
но не импортирует из него **ни одного** класса; в `src/` и `tests/` нет ни
одного упоминания пространства имён `PhpMiniSearch`. Это проверяется
экспериментом 4, а не принимается на веру.

### Три правила владения

`docs/ownership.md` сводит всё к трём правилам, и каждое отвечает на свой способ
ошибиться:

1. **Процесс останавливает только то, что запустил.** Кеш — самый резкий пример:
   у него нет режима демона, поэтому он дочерний процесс того, кто его запустил.
   `serve` принимает работающий кеш и **не** останавливает его; шаг остановки на
   `ShutdownStack` получает только тот сервер, который `serve` породил сам.
2. **Pid-файл авторитетен, порт — контрольная проверка.** Демон базы оставляет
   `minidb.pid`; но pid-файл от упавшего процесса лжёт. Поэтому
   `ensureDatabaseServer()` спрашивает и статус по pid-файлу, и порт, и только
   если **ни то, ни другое** не говорит «работает», запускает и присваивает
   сервер себе. Иначе два процесса считали бы, что владеют одним сервером, и
   второй останавливал бы инфраструктуру первого.
3. **Пул воркеров запускается один раз и принимается свободно.** Мастер
   запускает `serve`; `queue:consume` и `ConcurrentTaskRunner` принимают
   работающий пул через клиентов. Принявший не останавливает пул никогда. Кому
   нужен изолированный пул — поднимает свой мастер на своём сокете (переменные
   `WORKER_POOL_*`), и это единственный законный способ получить одноразовый
   пул, не трогая живой `serve`.

### `ShutdownStack`: три свойства вместо флагов

Стек остановок (`src/Support/ShutdownStack.php`) — ответ на второй провал из §1.
Он регистрирует шаг освобождения **в момент**, когда ресурс реально получен, а
не пишет флаг «я владею». Отсюда три свойства, каждое из которых приходилось
раньше обеспечивать руками:

- **LIFO.** Шаги выполняются от последнего к первому, то есть в порядке,
  обратном захвату: клиент раньше своего сервера, пул раньше кеша, которым его
  воркеры пользуются. Новая очистка — это ещё один `push`, а не перестановка
  блока.
- **Изоляция.** Шаг, который бросил исключение, попадает в stderr, и стек
  продолжает: база, отказавшаяся остановиться, не должна мешать остановке пула.
- **Идемпотентность.** `run()` выгребает стек и возвращает один и тот же
  результат при повторных вызовах, поэтому сигнал, пришедший посреди остановки,
  или `finally` после явной остановки не останавливают одно и то же дважды.

Плюс запрет на регистрацию после запуска (`push()` бросает `LogicException`):
шаг, добавленный после `run()`, никогда не выполнится, а молча невыполненный шаг
— это ровно та утечка, из-за которой стек и появился.

### Как решается «запускать или принять»

```text
probe the address (pid status, TCP connect, or a pool ping)
        │
        ├── answers ──► adopt: use it, register NO stop step, leave it running
        │
        └── silent ───► start it, register its stop on the stack,
                        then wait for it to ANSWER (not merely to launch)
```

Второе ожидание — не формальность. `startAndWaitForPort()` следит и за дочерним
процессом: ребёнок, умерший на старте (ошибка конфигурации, занятый порт),
проваливается сразу, а не через весь дедлайн, и **никогда не принимается за
готового только потому, что на его порту кто-то отвечает**.

Дедлайны разные, и это следствие разной цены запуска: `serve` даёт серверам 10
с, а службе поиска — `search.start_deadline` (60 с), потому что та перед началом
прослушивания переигрывает свой журнал (→ глава 18). Проверка портов короткая
(0,2–0,5 с) — она отвечает на вопрос «уже кто-то есть», а не «уже готово».

### Контракт корректной остановки

`OwnedProcess::stop()` посылает `SIGTERM`, ждёт, при необходимости посылает ещё
раз и только потом `SIGKILL` — и **возвращает ненулевой код, если пришлось
убивать**. Это не косметика: `queue:consume` в своём финальном отчёте сканирует
журнал на задания, которые могли потеряться молча, и выходит ненулевым кодом,
если такие есть. Потребитель, который встал, не имеет права утверждать, что
доставил всё, что ему дали.

Повторный `SIGTERM` — тоже следствие реального наблюдения: мастер пула
выставляет флаг в обработчике, и в CI был случай, когда он продолжал основной
цикл спустя минуты после «отправленного» `SIGTERM` — и останавливался на
следующем. Все серверы здесь трактуют повторный `SIGTERM` как ту же просьбу,
поэтому спросить ещё раз стоит ноль, а один непрочитанный сигнал раньше вешал
ожидающего навсегда.

### Чего это стоит

- **Принятие хранит лишнее.** Устаревший демон базы, оставшийся от упавшей
  `serve`, переживёт следующую: pid-файл и порт скажут «работает», и его примут.
  Для этого и есть отдельный шаг `stopStaleDatabaseServer()` — холодный старт
  сам вытирает чужой демон, читая pid-файл.
- **Проверки стоят времени.** Каждый холодный старт платит за опросы и ожидания
  (доли секунды на порт, до 60 с на поиск). Это цена того, что «запущено» не
  путают с «готово».
- **Граница по сети дороже границы в памяти.** Поиск — единственный компонент,
  до которого доходят байты по проводу; у него свои таймауты (1 с на соединение,
  5 с на ответ, → глава 21), свой журнал и своя служба.
- **Владение надо писать руками.** Стек не выводит владение из ничего: он лишь
  гарантирует, что зарегистрированный шаг выполнится на **любом** пути выхода.
  Решение «запускать или принять» остаётся делом команды.

## 3. Код

### Владение записывается один раз

Докблок `ShutdownStack` объясняет, почему это стек, а не флаги: «do I own it» —
это существование шага, а не отдельная переменная, и `run()` не зависит от того,
каким путём команда дошла до выхода.

```php
/**
 * The resources one command started, released in reverse order on every
 * path out of it.
 *
 * A command can start a daemonized database server, a cache server, a
 * worker-pool Master and clients to them. Releasing them by hand at each exit
 * is easy to get wrong, and a leaked server is then adopted by the next run
 * and never cleaned up. So ownership is recorded once:
 *
 *   push()  only once this process really holds the resource, so "do I own
 *           it" is simply whether a step exists
 *   run()   every step, newest first (a client before its server, a pool
 *           before the cache its workers use); a step that throws is
 *           reported and does not skip the rest
 */
```

[`src/Support/ShutdownStack.php#L10-L24`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/ShutdownStack.php#L10-L24)

**Перевод комментариев:**
- `ShutdownStack` (PHPDoc): ресурсы, запущенные одной командой, освобождаются в
  обратном порядке на любом пути выхода. Команда может запустить
  демонизированный сервер базы, сервер кеша, мастер пула воркеров и клиентов к
  ним. Легко ошибиться, освобождая их вручную на каждом выходе, а утёкший сервер
  затем принимается следующим запуском и никогда не чистится. Поэтому владение
  записывается один раз: `push()` — только когда процесс действительно держит
  ресурс, так что «владею ли я им» — это просто существование шага; `run()` —
  все шаги, от новейшего к первому (клиент раньше своего сервера, пул раньше
  кеша, которым пользуются его воркеры); шаг, бросивший исключение, попадает в
  отчёт и не пропускает остальные.

Регистрация — только до запуска. Шаг, добавленный после `run()`, не выполнился
бы никогда, а это и есть утечка в чистом виде.

```php
/** Register one release step; $name is what a failing step reports. */
public function push(Closure $step, string $name = ''): void
{
    if ($this->ran) {
        throw new \LogicException('Cannot push a shutdown step after the stack has run.');
    }

    $this->steps[] = ['name' => $name, 'step' => $step];
}
```

[`src/Support/ShutdownStack.php#L39-L47`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/ShutdownStack.php#L39-L47)

**Перевод комментариев:**
- `// Register one release step; $name is what a failing step reports.` —
  зарегистрировать один шаг освобождения; `$name` — то, что сообщает упавший
  шаг.

`run()` — тот самый метод из §1, который чинит «путь, не дошедший до остановок»:
`finally` вызывает его на любом выходе, а `try` вокруг каждого шага изолирует
упавшую очистку от остальных.

```php
/**
 * Release everything, newest first. Idempotent, so an explicit call
 * followed by an outer finally does not stop the same server twice.
 */
public function run(): void
{
    if ($this->ran) {
        return;
    }

    $this->ran = true;
    $steps = $this->steps;
    $this->steps = [];

    foreach (array_reverse($steps) as $entry) {
        try {
            ($entry['step'])();
        } catch (Throwable $e) {
            fwrite($this->errorOutput, sprintf(
                "[%s] could not release the %s: %s\n",
                $this->command,
                $entry['name'] === '' ? 'resource' : $entry['name'],
                $e->getMessage(),
            ));
        }
    }
}
```

[`src/Support/ShutdownStack.php#L49-L75`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/ShutdownStack.php#L49-L75)

**Перевод комментариев:**
- `run()` (PHPDoc): освободить всё, от новейшего к первому. Идемпотентно,
  поэтому явный вызов, за которым следует внешний `finally`, не остановит один и
  тот же сервер дважды.

### `OwnedProcess`: ручка и есть владение

У `OwnedProcess` нет pid-файла и нет режима демона: «дочерний процесс
принадлежит тому, кто его запустил, и ручка, возвращённая отсюда, это
доказывает».

```php
/**
 * A child process this process started, plus the small toolkit the commands
 * that own a whole platform (demo, load test, failure experiments) share:
 * port and condition polling, stale database cleanup, temp-tree handling.
 *
 *   start()  proc_open with stdout and stderr appended to named log files, so
 *            a child's output survives a failure that killed its parent
 *   stop()   SIGTERM, wait, then the exit code - a child that had to be
 *            SIGKILLed is reported non-zero, never as a polite stop, because
 *            the graceful-shutdown checks built on this rely on the difference
 *
 * There is no daemon mode and no pid file: a child belongs to the process
 * that started it, and the handle returned here is what proves it.
 */
```

[`src/Support/OwnedProcess.php#L10-L23`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L10-L23)

**Перевод комментариев:**
- `OwnedProcess` (PHPDoc): дочерний процесс, запущенный этим процессом, плюс
  небольшой набор инструментов, общий для команд, которые владеют целой
  платформой (демо, нагрузочный тест, эксперименты с отказами): опрос порта и
  условия, очистка устаревшей базы, работа с временным деревом. `start()` —
  `proc_open` с дописыванием stdout и stderr в именованные файлы, чтобы вывод
  ребёнка пережил сбой, убивший родителя; `stop()` — `SIGTERM`, ожидание, затем
  код выхода: ребёнок, которого пришлось убивать через `SIGKILL`, сообщается
  ненулевым кодом, а не как вежливая остановка, потому что проверки корректного
  завершения, построенные на этом, держатся на различии. Режима демона и
  pid-файла нет: ребёнок принадлежит процессу, который его запустил, и
  возвращённая ручка это доказывает.

`start()` — `proc_open` с выводом в именованные файлы, чтобы объяснение упавшего
ребёнка пережило смерть родителя. Обратите внимание на
`$env === [] ? null : $env`: непустое окружение — это **всё** окружение ребёнка,
а не заплатка на родительское.

```php
public static function start(array $argv, string $name, string $logDir, array $env = []): self
{
    self::mkdir($logDir);

    $child = new self($logDir . '/' . $name . '.out', $logDir . '/' . $name . '.err');
    $process = proc_open(
        $argv,
        [
            1 => ['file', $child->stdoutPath, 'a'],
            2 => ['file', $child->stderrPath, 'a'],
        ],
        $pipes,
        null,
        $env === [] ? null : $env,
    );

    if (!is_resource($process)) {
        throw new RuntimeException(sprintf('Could not start the %s process.', $name));
    }

    $child->process = $process;
    $child->pid = (int) proc_get_status($process)['pid'];

    return $child;
}
```

[`src/Support/OwnedProcess.php#L89-L113`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L89-L113)

`stop()` возвращает код выхода, и в этом коде различаются две разные вещи:
ребёнок остановился по `SIGTERM` или его пришлось убивать. Строка
`$exited ? $code : ($code === 0 ? 1 : $code)` — единственное место, где это
различие превращается в наблюдаемый факт.

```php
/**
 * Ask the child to stop, the way an operator would, and wait for it.
 *
 * @return int the exit code, or 0 when there was no child left to stop
 */
public function stop(float $deadlineSeconds = self::STOP_DEADLINE_SECONDS): int
{
    if (!is_resource($this->process)) {
        return 0;
    }

    // Ignored SIGTERM ends in a SIGKILL, reported as non-zero below.
    $exited = self::terminate($this->process, $deadlineSeconds);

    // proc_close() returns the exit code proc_get_status() already
    // observed (PHP >= 8.3), so the wait above does not lose it.
    $code = proc_close($this->process);
    $this->process = null;

    return $exited ? $code : ($code === 0 ? 1 : $code);
}
```

[`src/Support/OwnedProcess.php#L164-L184`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L164-L184)

**Перевод комментариев:**
- `stop()` (PHPDoc): попросить ребёнка остановиться так, как это сделал бы
  оператор, и дождаться его. Возвращает код выхода или `0`, если останавливать
  уже некого.
- `// Ignored SIGTERM ends in a SIGKILL, reported as non-zero below.` —
  проигнорированный `SIGTERM` заканчивается `SIGKILL`, что ниже сообщается
  ненулевым кодом.
- `// proc_close() returns the exit code proc_get_status() already` —
  `proc_close()` возвращает код выхода, который `proc_get_status()` уже наблюдал
  (PHP >= 8.3), поэтому ожидание выше его не теряет.

Внутри — цикл повторного `SIGTERM` и `SIGKILL` по дедлайну. Докблок объясняет и
то, и другое: наблюдение из CI и осторожность вокруг `proc_terminate()`, который
«отправил» сигнал, но не дождался эффекта.

```php
/**
 * SIGTERM $process and wait for it to exit, sending SIGTERM again every
 * SIGTERM_RESEND_SECONDS while it still runs, and SIGKILL once
 * $deadlineSeconds have passed. The caller still proc_close()s it.
 *
 * The repeat is not paranoia. The worker pool's Master, which only sets
 * a flag in its SIGTERM handler, was caught in CI still running its main
 * loop minutes after a SIGTERM that proc_terminate() reported as sent -
 * and stopping on the very next one. Every server here takes a repeated
 * SIGTERM as the same request, so asking again costs nothing, while a
 * single unanswered one used to hang whoever was waiting for good.
 *
 * @param resource $process
 *
 * @return bool whether it exited on SIGTERM, without needing the SIGKILL
 */
public static function terminate(mixed $process, float $deadlineSeconds = self::STOP_DEADLINE_SECONDS): bool
{
    $deadline = microtime(true) + $deadlineSeconds;
    $exited = static fn (): bool => !proc_get_status($process)['running'];

    do {
        proc_terminate($process);

        if (self::waitForQuietly($exited, min(self::SIGTERM_RESEND_SECONDS, max(0.0, $deadline - microtime(true))))) {
            return true;
        }
    } while (microtime(true) < $deadline);

    proc_terminate($process, 9);

    return false;
}
```

[`src/Support/OwnedProcess.php#L186-L218`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L186-L218)

**Перевод комментариев:**
- `terminate()` (PHPDoc): послать `$process` `SIGTERM` и ждать выхода, повторяя
  `SIGTERM` каждые `SIGTERM_RESEND_SECONDS`, пока процесс жив, и `SIGKILL`,
  когда истечёт `$deadlineSeconds`. Вызывающий всё равно делает `proc_close()`.
  Повтор — не паранойя. Мастер пула воркеров, который в обработчике `SIGTERM`
  только выставляет флаг, был пойман в CI за работой основного цикла спустя
  минуты после `SIGTERM`, который `proc_terminate()` отчитался как отправленный,
  — и останавливался на следующем. Каждый сервер здесь трактует повторный
  `SIGTERM` как ту же просьбу, поэтому спросить ещё раз ничего не стоит, а один
  неотвеченный сигнал раньше вешал ожидающего навсегда. `$process` — ресурс;
  возвращает, вышел ли процесс по `SIGTERM`, без `SIGKILL`.

### Опрос адреса: «есть» — это ответ, а не попытка подключиться

`portAnswers()` повторяет попытку до дедлайна, поэтому полный таймаут тратится
ровно тогда, когда никто не слушает, — и на короткой проверке это как раз нужное
поведение.

```php
/**
 * Whether a TCP connect succeeds within $timeoutSeconds. It keeps retrying
 * until then, so the full timeout is spent whenever nothing listens: keep
 * it short for "is something already running" checks.
 */
public static function portAnswers(string $host, int $port, float $timeoutSeconds): bool
{
    return self::endpointAnswers(sprintf('tcp://%s:%d', $host, $port), $timeoutSeconds);
}

/** portAnswers() for any stream address: tcp://host:port or unix:///path. */
public static function endpointAnswers(string $address, float $timeoutSeconds): bool
{
    $deadline = microtime(true) + $timeoutSeconds;

    while (microtime(true) < $deadline) {
        $socket = @stream_socket_client($address, $code, $message, self::PORT_PROBE_CONNECT_SECONDS);

        if ($socket !== false) {
            fclose($socket);

            return true;
        }

        usleep((int) (self::POLL_INTERVAL_SECONDS * 1_000_000));
    }

    return false;
}
```

[`src/Support/OwnedProcess.php#L226-L254`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L226-L254)

**Перевод комментариев:**
- `portAnswers()` (PHPDoc): успевает ли TCP-подключение за `$timeoutSeconds`.
  Функция повторяет попытки до этого момента, поэтому полный таймаут тратится
  всякий раз, когда никто не слушает: держите его коротким для проверок «уже
  что-то запущено».
- `endpointAnswers()` (PHPDoc): `portAnswers()` для любого адреса потока:
  `tcp://host:port` или `unix:///path`.

Отдельный случай — устаревший демон базы. Холодному старту он мешает, но убить
его можно только по pid-файлу; порт, отвечающий без pid-файла, принадлежит
кому-то другому и не трогается.

```php
/**
 * A serve that was killed can leave the daemonized database server it
 * started behind. Its pid file is the only unambiguous owner, so it is
 * read (and the server stopped) before a run wipes the data directory
 * for a fresh platform. A port that answers without a pid file belongs
 * to someone else and is left alone.
 */
public static function stopStaleDatabaseServer(
    string $host,
    int $port,
    string $dataDir,
    float $deadlineSeconds = self::STOP_DEADLINE_SECONDS,
): void {
    if (!self::portAnswers($host, $port, 0.5)) {
        return;
    }

    $pidFile = $dataDir . '/minidb.pid';
    $pid = is_file($pidFile) ? (int) trim((string) file_get_contents($pidFile)) : 0;

    if ($pid > 0) {
        posix_kill($pid, SIGTERM);
        self::waitFor(
            static fn (): bool => !posix_kill($pid, 0),
            $deadlineSeconds,
            'the stale database server did not stop in time.',
        );
    }
}
```

[`src/Support/OwnedProcess.php#L292-L320`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Support/OwnedProcess.php#L292-L320)

**Перевод комментариев:**
- `stopStaleDatabaseServer()` (PHPDoc): убитая `serve` может оставить после себя
  демонизированный сервер базы, который она запустила. Его pid-файл —
  единственный однозначный владелец, поэтому его читают (и сервер останавливают)
  перед тем, как запуск вытрет каталог данных для свежей платформы. Порт,
  который отвечает без pid-файла, принадлежит кому-то другому и не трогается.

### Одна стратегия на все команды

Докблок `PlatformCli` формулирует правило целиком: режим демона у базы и
отсутствие его у кеша и пула — это и есть причина, по которой владение решается
по-разному, а единый стек делает так, что ни один путь выхода не забывает
освобождение.

```php
/**
 * The whole CLI surface of the platform, in one place.
 *
 * Deliberately small - no framework, no DI, no command classes for their own
 * sake. Each command is an entry in COMMANDS and an arm in dispatch(). See
 * docs/architecture.md for where each command sits in the process model.
 *
 * Process ownership, shared by every command that needs infrastructure:
 *
 *   database server  daemonizes and is tracked by a pid file, so ownership is
 *                    decided once at start-up (did this call start it?)
 *   cache server,    no daemon mode: spawned as direct children, and the
 *   worker pool      process handle captured by the stop step IS the ownership
 *
 * An already-running server of any kind is adopted and left running. Every
 * release is pushed onto one ShutdownStack as soon as the resource is held,
 * so no exit path can forget one.
 */
```

[`src/Cli/PlatformCli.php#L91-L108`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L91-L108)

**Перевод комментариев:**
- `PlatformCli` (PHPDoc): вся CLI-поверхность платформы в одном месте. Намеренно
  небольшая: без фреймворка, без DI, без командных классов ради самих классов.
  Каждая команда — запись в `COMMANDS` и ветка в `dispatch()`. Где каждая
  команда сидит в модели процессов, см. `docs/architecture.md`. Владение
  процессами, общее для каждой команды, которой нужна инфраструктура: сервер
  базы демонизируется и отслеживается pid-файлом, поэтому владение решается один
  раз при старте (запустил ли его этот вызов?); сервер кеша и пул воркеров
  режима демона не имеют: они порождаются прямыми детьми, и захваченная шагом
  остановки ручка процесса **и есть** владение. Уже работающий сервер любого
  рода принимается и оставляется работать. Каждое освобождение кладётся в один
  `ShutdownStack`, как только ресурс получен, поэтому ни один путь выхода не
  может его забыть.

Порядок захвата в `serve()` читается как список владений: база (демон), кеш
(ребёнок), пул (ребёнок), служба поиска (ребёнок), и всё — на одном стеке.

```php
 * Everything serve starts is pushed onto one ShutdownStack in acquisition
 * order and released by the single finally. Before that, two of the error
 * paths forgot the pool Master - and the next serve then adopted the
 * orphan instead of owning it, so nothing ever stopped it.
 */
private function serve(): int
{
    $config = $this->config();
    $workersConfig = $config['workers'];
    $shutdown = new ShutdownStack('serve');

    try {
        // One registry every component reports into, and one trace journal
        // shared with the pool workers, so a request's whole span chain
        // (http/db here, job.execute in the workers) reads from one file.
        $systemMetrics = new MetricsRegistry();
        $systemTrace = new Trace($this->traceStorePath($config));

        $database = $this->openDatabase($shutdown, $config['database'], $systemMetrics, $systemTrace);

        $cacheConfig = $config['cache'];
        $cache = CacheService::fromConfig($cacheConfig, $systemMetrics);
        $this->ensureCacheServer($shutdown, $cacheConfig);
        $shutdown->push(static fn (): null => $cache->close(), 'cache');

        $this->ensureWorkerPool($shutdown, $workersConfig);

        $producer = $this->producer($config['queue']);
        $search = $this->ensureSearchServer($shutdown, $config['search']);
        $this->requestReindexIfNeeded($search, $producer);
```

[`src/Cli/PlatformCli.php#L212-L241`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L212-L241)

**Перевод комментариев:**
- `// One registry every component reports into, and one trace journal` — один
  реестр, в который отчитывается каждый компонент, и один журнал трассировки,
  общий с воркерами пула, чтобы вся цепочка спанов запроса (`http/db` здесь,
  `job.execute` в воркерах) читалась из одного файла.

Один `finally` на весь метод — то, ради чего стек и существует:

```php
    $this->runHttpLoop($server, $serverConfig, $application, $logger);

    $server->stop();
    printf("Shutdown complete.\n");

    return 0;
} catch (RuntimeException $e) {
    return $this->fail($e);
} finally {
    $shutdown->run();
}
```

[`src/Cli/PlatformCli.php#L287-L297`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L287-L297)

Правило «pid-файл авторитетен, порт — контрольная проверка» живёт в
`ensureDatabaseServer()`: шаг остановки регистрируется **только** после удачного
запуска, и остановка идёт через pid-файл, потому что сервер демонизирован и
ручки у родителя нет.

```php
private function ensureDatabaseServer(ShutdownStack $shutdown, array $config): void
{
    $script = $this->binary('minidb.php');
    $pidFile = $config['data_dir'] . '/minidb.pid';
    $address = sprintf('tcp://%s:%d', $config['host'], $config['port']);

    // The pid file alone is not enough: it can be stale, or belong to
    // another platform process. The port is the ground truth, and only
    // when neither says "running" may this process start and own one -
    // otherwise two processes would each think they own the same server
    // and the second would stop the first's infrastructure on the way out.
    [$statusCode] = $this->runServerCli([$script, 'status', '--pid-file', $pidFile]);

    if ($statusCode === 0 || OwnedProcess::endpointAnswers($address, 0.3)) {
        printf("Database server already running on %s\n", $address);

        return;
    }

    OwnedProcess::mkdir($config['data_dir']);

    [$code, $output] = $this->runServerCli([
        $script,
        'start',
        '--host', $config['host'],
        '--port', (string) $config['port'],
        '--data', $config['data_dir'],
        '--daemon',
        '--pid-file', $pidFile,
        '--log-file', $config['data_dir'] . '/minidb.log',
    ]);

    if ($code !== 0) {
        throw new RuntimeException('Could not start the database server: ' . $output);
    }

    $shutdown->push(function () use ($script, $pidFile): void {
        [$code, $output] = $this->runServerCli([$script, 'stop', '--pid-file', $pidFile]);

        if ($code !== 0) {
            throw new RuntimeException($output);
        }

        printf("Database server stopped\n");
    }, 'database server');

    if (!OwnedProcess::endpointAnswers($address, self::START_DEADLINE_SECONDS)) {
        throw new RuntimeException(sprintf('Database server did not start listening on %s in time.', $address));
    }

    printf("Database server listening on %s\n", $address);
}
```

[`src/Cli/PlatformCli.php#L460-L511`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L460-L511)

**Перевод комментариев:**
- `// The pid file alone is not enough: it can be stale, or belong to` — одного
  pid-файла недостаточно: он может быть устаревшим или принадлежать другому
  процессу платформы. Порт — источник истины, и только когда ни один из них не
  говорит «работает», этот процесс может запустить и присвоить сервер — иначе
  два процесса решат, что владеют одним и тем же сервером, и второй на выходе
  остановит инфраструктуру первого.

`ensureSearchServer()` — та же схема «запустить или принять», но ребёнок
получает **явное** окружение и отдельный дедлайн (поиск переигрывает журнал
перед прослушиванием). Заодно видно, что платформа говорит с ним клиентом
`SearchClient`, то есть по HTTP.

```php
/**
 * php-mini-search, the sixth component (PLAN Step 41): started as
 * `vendor/bin/mini-search serve` like any service would be, and spoken to
 * only over HTTP. Start-or-adopt like the cache: a search service already
 * answering on the port is used and left running.
 *
 * The child gets an explicit environment — its port, data directory and
 * the shared token — and nothing else from this process. It replays its
 * log before it listens, so it gets `start_deadline`, not the 10 s the
 * other servers get. SIGTERM is its graceful drain.
 *
 * @param array<string, mixed> $config the `search` config block
 */
private function ensureSearchServer(ShutdownStack $shutdown, array $config): ?SearchClient
{
    $address = sprintf('tcp://%s:%d', $config['host'], $config['port']);

    if (!(bool) ($config['enabled'] ?? true)) {
        printf("Search disabled (SEARCH_ENABLED): no search service on %s\n", $address);

        return null;
    }

    $client = SearchClient::fromConfig($config);

    if (OwnedProcess::portAnswers((string) $config['host'], (int) $config['port'], 0.5)) {
        printf("Search service already running on %s\n", $address);

        return $client;
    }

    $dataDir = (string) $config['data_dir'];
    OwnedProcess::mkdir($dataDir);

    $this->spawnOwned($shutdown, 'search service', dirname(__DIR__, 2) . '/vendor/bin/mini-search', $dataDir . '/search', [
        'MINI_SEARCH_HOST' => (string) $config['host'],
        'MINI_SEARCH_PORT' => (string) $config['port'],
        'MINI_SEARCH_DATA_DIR' => $dataDir,
        'MINI_SEARCH_TOKEN' => (string) $config['token'],
        'MINI_SEARCH_LOG_LEVEL' => 'error',
    ], ['serve']);

    if (!OwnedProcess::endpointAnswers($address, (float) ($config['start_deadline'] ?? self::START_DEADLINE_SECONDS))) {
        throw new RuntimeException(sprintf('Search service did not start listening on %s in time.', $address));
    }

    printf("Search service listening on %s\n", $address);

    return $client;
}
```

[`src/Cli/PlatformCli.php#L580-L629`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L580-L629)

**Перевод комментариев:**
- `ensureSearchServer()` (PHPDoc): `php-mini-search`, шестой компонент (PLAN
  Step 41): запускается как `vendor/bin/mini-search serve`, как любой сервис, и
  с ним говорят только по HTTP. Запустить-или-принять, как с кешем: служба
  поиска, которая уже отвечает на порту, используется и оставляется работать.
  Ребёнок получает явное окружение — свой порт, каталог данных и общий токен, —
  и ничего больше от этого процесса. Перед прослушиванием он переигрывает свой
  журнал, поэтому получает `start_deadline`, а не 10 s, как остальные серверы.
  `SIGTERM` — его корректное завершение. `@param`: блок конфигурации `search`.

`spawnOwned()` связывает две половины вместе: запустить ребёнка и **сразу**
зарегистрировать его остановку — принятый сервер до этого метода не доходит.

```php
/**
 * Spawn a child server and register its stop at once. Neither the cache
 * nor the pool has a daemon mode, so the handle captured by the shutdown
 * step IS the ownership: an adopted server never reaches this method.
 *
 * @param array<string, string>|null $env
 * @param list<string>               $arguments after the script name
 */
private function spawnOwned(ShutdownStack $shutdown, string $name, string $script, string $logPrefix, ?array $env, array $arguments = []): void
{
    $process = $this->spawn($script, $logPrefix, $env, sprintf('Could not start the %s process.', $name), $arguments);

    $shutdown->push(function () use ($process, $name): void {
        $this->terminate($process);
        printf("%s stopped\n", ucfirst($name));
    }, $name);
}
```

[`src/Cli/PlatformCli.php#L744-L760`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Cli/PlatformCli.php#L744-L760)

**Перевод комментариев:**
- `spawnOwned()` (PHPDoc): породить дочерний сервер и сразу зарегистрировать его
  остановку. Ни у кеша, ни у пула нет режима демона, поэтому ручка, захваченная
  шагом остановки, **и есть** владение: принятый сервер до этого метода не
  доходит. `@param`: `$env` — окружение; `$arguments` — аргументы после имени
  скрипта.

### Мастер пула: процесс, который владеет воркерами

`bin/worker.php` — отдельный исполняемый файл, а не класс внутри `serve`, и это
решение про границу: пул живёт своим процессом, чтобы владение воркерами не
смешивалось с обработкой HTTP-запросов.

```php
/*
 * The platform's worker-pool Master: one process that owns a pool of forked
 * workers over a unix socket. serve spawns this child whenever no pool
 * answers on the configured socket and stops it again on shutdown; the HTTP
 * control plane reaches it through Workers\ConcurrentTaskRunner, and the
 * queue consumer reaches it through Workers\WorkerManager. Everything the
 * pool does is the component's - this file only decides which tasks its
 * workers run and with what sizing.
 *
 * Five task families live here: the hash tasks (ping/hash_chunk,
 * WorkerTasks) behind GET /parallel, job.execute (WorkerJobs) that runs
 * queue jobs, the catalog.* reads (CatalogTasks) the concurrent order loader
 * fans out, memory.* (WorkerMemoryTasks) that lets `workers:memory` measure
 * and grow a worker's own memory, and worker.* (WorkerFailureTasks) that
 * crashes a worker on demand in development/demo modes. The pool itself never
 * knows what a task means - it only forks processes and moves
 * Request/Response between them.
 */
```

[`bin/worker.php#L17-L34`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/bin/worker.php#L17-L34)

**Перевод комментариев:**
- `bin/worker.php` (PHPDoc): мастер пула воркеров платформы — один процесс,
  который владеет пулом форкнутых воркеров поверх unix-сокета. `serve` порождает
  этого ребёнка, когда на настроенном сокете не отвечает ни один пул, и
  останавливает его при завершении; плоскость управления HTTP доходит до него
  через `Workers\ConcurrentTaskRunner`, а потребитель очереди — через
  `Workers\WorkerManager`. Всё, что делает пул, — это компонент; этот файл лишь
  решает, какие задачи выполняют его воркеры и с какими размерами. Здесь живут
  пять семейств задач: хеш-задачи (`ping/hash_chunk`, `WorkerTasks`) за
  `GET /parallel`; `job.execute` (`WorkerJobs`), который выполняет задачи
  очереди; чтения `catalog.*` (`CatalogTasks`), которые разветвляет параллельный
  загрузчик заказов; `memory.*` (`WorkerMemoryTasks`), позволяющие
  `workers:memory` измерять и растить память самого воркера; и `worker.*`
  (`WorkerFailureTasks`), которые роняют воркер по требованию в режимах
  разработки/демо. Сам пул никогда не знает, что означает задача, — он только
  форкает процессы и переносит `Request` /`Response` между ними.

Размеры пула и три таймаута читаются из конфигурации с переопределением через
переменные окружения — так изолированный пул для теста или бенчмарка поднимается
на своём сокете, не трогая живой `serve`.

```php
$config = require __DIR__ . '/../config/platform.php';
$workers = $config['workers'];

// serve launches the pool with the config's socket and autoscaling bounds
// (workers.min_workers..max_workers, 2..16 by default). config/platform.php
// reads WORKER_POOL_SOCKET/MIN/MAX itself, which is what lets a benchmark,
// the demo or an experiment run a fixed-size pool, or an isolated one on its
// own socket, while every client reads the same socket from the same place.
$socket = (string) $workers['socket'];
$minWorkers = (int) $workers['min_workers'];
$maxWorkers = (int) $workers['max_workers'];
$requestTimeout = (float) (getenv('WORKER_POOL_TIMEOUT') ?: $workers['task_timeout']);

// The Master's own timeouts (enforced by php-worker-pool's
// terminateStuckWorkers()): execution - a worker held on ONE task past this is
// killed and replaced; bootstrap/departure - how long a worker may sit
// STARTING or STOPPING. A test pool overrides the execution one to show the
// mechanism on a short deadline.
$executionTimeout = (float) (getenv('WORKER_POOL_EXECUTION_TIMEOUT') ?: $workers['execution_timeout']);
$bootstrapTimeout = (float) $workers['bootstrap_timeout'];
$departureTimeout = (float) $workers['departure_timeout'];
```

[`bin/worker.php#L36-L56`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/bin/worker.php#L36-L56)

**Перевод комментариев:**
- `// serve launches the pool with the config's socket and autoscaling...` —
  `serve` запускает пул с сокетом и границами автомасштабирования из
  конфигурации (`workers.min_workers..max_workers`, 2..16 по умолчанию).
  `config/platform.php` сам читает `WORKER_POOL_SOCKET/MIN/MAX`, и именно
  поэтому бенчмарк, демо или эксперимент могут запустить пул фиксированного
  размера или изолированный на своём сокете, пока каждый клиент читает тот же
  сокет из того же места.
- `// The Master's own timeouts (enforced by php-worker-pool's` — собственные
  таймауты мастера (их обеспечивает `terminateStuckWorkers()` из
  php-worker-pool): execution — воркер, задержавшийся на одной задаче дольше
  этого, убивается и заменяется; bootstrap/departure — сколько воркер может
  находиться в состоянии `STARTING` или `STOPPING`. Тестовый пул переопределяет
  execution, чтобы показать механизм на коротком дедлайне.

Маршрутизация пяти семейств задач и запуск мастера: процесс не знает, что
означает задача, — он только порождает процессы и переносит `Request`/`Response`
между ними.

```php
$handler = static function (Request $request) use ($catalogTasks, $failureTasks, $memoryTasks, $workerJobs, $workerTasks): Response {
    if ($request->action === 'job.execute') {
        return $workerJobs($request);
    }

    if (str_starts_with($request->action, 'worker.')) {
        return $failureTasks($request);
    }

    if (str_starts_with($request->action, 'catalog.')) {
        return $catalogTasks($request);
    }

    return str_starts_with($request->action, 'memory.')
        ? $memoryTasks($request)
        : $workerTasks($request);
};

$master = new Master(
    socketPath: $socket,
    minWorkers: $minWorkers,
    maxWorkers: $maxWorkers,
    maxQueueSize: 10_000,
    requestTimeoutSeconds: $requestTimeout,
    workerExecutionTimeoutSeconds: $executionTimeout,
    handler: $handler,
    workerBootstrapTimeoutSeconds: $bootstrapTimeout,
    workerDepartureTimeoutSeconds: $departureTimeout,
);

$master->run();
```

[`bin/worker.php#L71-L101`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/bin/worker.php#L71-L101)

### Мост через границу: очередь → пул

`WorkerManager` — единственный шов между очередью и пулом, и он не владеет ни
одним процессом: жизненным циклом воркеров занимается мастер.

```php
/**
 * The platform's one seam between the queue and the php-worker-pool: it
 * hands a queue Job to the pool as a `job.execute` task and treats the
 * pool's answer as the job's outcome.
 *
 * This is the "Worker Manager" of PLAN Step 10 - the layer between the
 * queue consumer and the actual worker processes. It does not own any
 * process itself: the pool Master does worker lifecycle, dispatch, failure
 * and shutdown, and this adapter only speaks the client protocol, exactly
 * like ConcurrentTaskRunner speaks it for hash tasks. A task the pool
 * rejects (job_failed) becomes a RuntimeException so the queue's retry and
 * failure accounting sees a failed attempt; a pool that is gone propagates
 * the connection error the same way - a delivery that did not happen.
 */
```

[`src/Workers/WorkerManager.php#L14-L27`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerManager.php#L14-L27)

**Перевод комментариев:**
- `WorkerManager` (PHPDoc): единственный шов платформы между очередью и
  php-worker-pool: он отдаёт задачу очереди пулу как задачу `job.execute` и
  считает ответ пула результатом задачи. Это «Worker Manager» из PLAN Step 10 —
  слой между потребителем очереди и собственно процессами воркеров. Сам он не
  владеет ни одним процессом: жизненным циклом воркеров, диспетчеризацией,
  отказами и остановкой занимается мастер пула, а этот адаптер лишь говорит на
  клиентском протоколе — точно как `ConcurrentTaskRunner` для хеш-задач. Задача,
  которую пул отверг (`job_failed`), становится `RuntimeException`, чтобы повтор
  и учёт отказов в очереди увидели неудачную попытку; исчезнувший пул передаёт
  ошибку соединения так же — доставка не состоялась.

Клиент строится **внутри** обработчика, а не наследуется от родителя: каждый
форк-форвардер открывает своё соединение, вместо того чтобы делить сокет с
родителем, — та же ошибка, что «форкнутый сокет, на котором отвечают два
процесса», из главы 14.

```php
public static function forwarder(string $socketPath, float $timeoutSeconds): Closure
{
    $manager = null;

    return static function (Job $job) use (&$manager, $socketPath, $timeoutSeconds): mixed {
        $manager ??= new self(new WorkerPoolClient($socketPath, $timeoutSeconds));
        $manager->execute($job);

        return null;
    };
}
```

[`src/Workers/WorkerManager.php#L44-L54`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Workers/WorkerManager.php#L44-L54)

## 4. Эксперимент

### Подготовка

```bash
cd php-systems-platform
make build install
```

Все четыре эксперимента выполняются в контейнере `php-systems-platform`, проект
смонтирован в `/app`. Дочерние процессы в экспериментах 2 и 3 — настоящие:
сценарии пишут их на диск и запускают через `OwnedProcess`. Pid нигде не
печатается (он меняется от прогона к прогону), а всё, что печатается,
детерминировано: каждый скрипт прогонялся дважды и совпал байт-в-байт.

### 1. Стек остановок: порядок, изоляция, идемпотентность

Три свойства `ShutdownStack` проверяются одной командой, которая «захватила» три
ресурса и отпускает их. Один из шагов падает нарочно — во время освобождения кеш
«занят».

```bash
docker compose cp /tmp/ch22-shutdown-stack.php php:/tmp/ch22-shutdown-stack.php
docker compose exec php php /tmp/ch22-shutdown-stack.php
```

```php
<?php

declare(strict_types=1);

/**
 * The shutdown stack's three properties, one at a time: the release order,
 * one failing step not skipping the rest, and a second run doing nothing.
 *
 * A command acquires the database server, the cache server and the worker
 * pool; the stack holds one release step per resource and the command runs
 * it on every path out. The interesting part is not the happy path - it is
 * the step that throws. Before this class, each stop was gated on its own
 * ownership flag and an exception raised before the stops skipped all of
 * them, so the stack reports the failure and carries on instead.
 *
 * The last check is the one that keeps the class honest: a step registered
 * after run() would never execute, so push() refuses it.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch22-shutdown-stack.php php:/tmp/ch22-shutdown-stack.php
 *   docker compose exec php php /tmp/ch22-shutdown-stack.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\ShutdownStack;

$directory = sys_get_temp_dir() . '/ch22-stack-' . getmypid() . '/';

@mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    foreach (glob($directory . '*') ?: [] as $path) {
        @unlink($path);
    }

    @rmdir($directory);
});

$errorFile = $directory . 'errors.log';
$errors = fopen($errorFile, 'w');

if ($errors === false) {
    exit(1);
}

/** @var list<string> $released */
$released = [];

$stack = new ShutdownStack('demo', $errors);

// Acquired in this order, so the stack must release them in the reverse one.
$stack->push(static function () use (&$released): void {
    $released[] = 'database server';
}, 'database server');

$stack->push(static function () use (&$released): void {
    $released[] = 'cache';
    throw new RuntimeException('the cache is still busy');
}, 'cache');

$stack->push(static function () use (&$released): void {
    $released[] = 'worker pool';
}, 'worker pool');

printf("acquired: database server, cache, worker pool\n");

$stack->run();
printf("released: %s\n", implode(' then ', $released));
printf("a step that threw did not skip the ones behind it: %s\n", count($released) === 3 ? 'yes' : 'no');

fclose($errors);
printf("what the failing step reported: %s\n", trim((string) file_get_contents($errorFile)));

$released = [];
$stack->run();
printf("a second run released anything: %s\n", $released === [] ? 'no' : 'yes');

try {
    $stack->push(static function (): void {}, 'too late');
    printf("a step registered after run(): accepted (wrong)\n");
} catch (LogicException $e) {
    printf("a step registered after run(): refused (%s)\n", $e->getMessage());
}
```

**Перевод комментариев:**
- `ch22-shutdown-stack.php` (PHPDoc): три свойства стека остановок по очереди:
  порядок освобождения, один упавший шаг не пропускает остальные и второй запуск
  ничего не делает. Команда захватывает сервер базы, сервер кеша и пул воркеров;
  стек хранит по одному шагу освобождения на ресурс, и команда запускает его на
  каждом пути выхода. Интересен не счастливый путь, а шаг, который бросает
  исключение. До этого класса каждая остановка была привязана к своему флагу
  владения, и исключение, возникшее до остановок, пропускало их все; вместо
  этого стек сообщает об отказе и продолжает. Последняя проверка держит класс
  честным: шаг, зарегистрированный после `run()`, никогда бы не выполнился,
  поэтому `push()` его отклоняет. Запуск в контейнере, где проект смонтирован в
  `/app`: `docker compose cp ...` и `docker compose exec ...`.
- `// Acquired in this order, so the stack must release...` — захвачено в этом
  порядке, значит стек должен освобождать их в обратном.

```text
acquired: database server, cache, worker pool
released: worker pool then cache then database server
a step that threw did not skip the ones behind it: yes
what the failing step reported: [demo] could not release the cache: the cache is still busy
a second run released anything: no
a step registered after run(): refused (Cannot push a shutdown step after the stack has run.)
```

Четыре строки вывода — четыре утверждения главы.

`released: worker pool then cache then database server` — порядок захвата был
`database server, cache, worker pool`, значит освобождение идёт в обратном: пул
раньше кеша (воркеры пула пользуются кешем), кеш раньше базы. Это и есть LIFO.

`a step that threw did not skip the ones behind it: yes` — шаг кеша бросил
исключение, но и база, и пул всё равно освобождены, а сбой попал в поток ошибок:
`[demo] could not release the cache: the cache is still busy`. Формат отчёта —
это `$command` и `$name` из `push()`, то есть имя шага задаётся там же, где сам
шаг, и оператор видит, что именно не отпустилось.

`a second run released anything: no` — идемпотентность. `finally` после явной
остановки не отпустит сервер второй раз.

`a step registered after run(): refused` — `push()` после запуска бросает
`LogicException`. Это защита от самой опасной утечки: шага, который никто
никогда не выполнит.

### 2. Контракт остановки: по-хорошему или убить

Два ребёнка, два поведения на `SIGTERM`. Первый выходит из обработчика, второй
обработчик игнорирует — как залипший процесс. Каждый пишет файл готовности после
установки обработчика, и родитель ждёт оба, прежде чем сигналить: `SIGTERM`,
пришедший **до** `pcntl_signal()`, убивает по умолчанию, и именно так «тест на
корректную остановку» превращается во флаки.

```bash
docker compose cp /tmp/ch22-owned-process.php php:/tmp/ch22-owned-process.php
docker compose exec php php /tmp/ch22-owned-process.php
```

```php
<?php

declare(strict_types=1);

/**
 * The graceful-stop contract: a child that honours SIGTERM stops politely,
 * one that ignores it is killed, and the exit code tells them apart.
 *
 * The distinction is not cosmetic. `queue:consume` decides whether it may
 * claim it drained everything by looking at exactly this difference, so a
 * killed child must never be reported as a polite stop. `stop()` asks with
 * SIGTERM, asks again if the first goes unanswered, and only then uses
 * SIGKILL - and returns non-zero for the child that needed it.
 *
 * Both children are written here, so the whole experiment is one file: the
 * polite one exits from its SIGTERM handler, the stubborn one installs a
 * handler that does nothing. Each writes a ready file once its handler is
 * installed, and the parent waits for both before signalling - a signal that
 * arrives before pcntl_signal() runs is killed by the default disposition,
 * which is exactly how a "graceful stop" test turns into a flaky one.
 *
 * No pids are printed: they change every run, and what is being checked is
 * the classification, not the number.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch22-owned-process.php php:/tmp/ch22-owned-process.php
 *   docker compose exec php php /tmp/ch22-owned-process.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch22-owned-' . getmypid() . '/';

@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$polite = <<<'CHILD'
<?php

declare(strict_types=1);

$readyFile = $argv[1];

pcntl_async_signals(true);
pcntl_signal(SIGTERM, static function (): void {
    exit(0);
});

file_put_contents($readyFile, "ready\n");

while (true) {
    usleep(100_000);
}
CHILD;

$stubborn = <<<'CHILD'
<?php

declare(strict_types=1);

$readyFile = $argv[1];

pcntl_async_signals(true);
pcntl_signal(SIGTERM, static function (): void {
    // Deliberately ignore it, the way a stuck process does.
});

file_put_contents($readyFile, "ready\n");

while (true) {
    usleep(100_000);
}
CHILD;

file_put_contents($directory . 'polite.php', $polite);
file_put_contents($directory . 'stubborn.php', $stubborn);

$politeReady = $directory . 'polite.ready';
$stubbornReady = $directory . 'stubborn.ready';

$politeChild = OwnedProcess::start([PHP_BINARY, $directory . 'polite.php', $politeReady], 'polite', $directory . 'logs');
$stubbornChild = OwnedProcess::start([PHP_BINARY, $directory . 'stubborn.php', $stubbornReady], 'stubborn', $directory . 'logs');

$up = OwnedProcess::waitForQuietly(
    static fn (): bool => is_file($politeReady) && is_file($stubbornReady),
    5.0,
);

printf("both children started, handlers installed: %s\n", $up ? 'yes' : 'no');
printf("both still running: %s\n", $politeChild->isRunning() && $stubbornChild->isRunning() ? 'yes' : 'no');

$politeCode = $politeChild->stop(5.0);
printf("the child that honours SIGTERM: exit code zero: %s\n", $politeCode === 0 ? 'yes' : 'no');

$stubbornCode = $stubbornChild->stop(1.0);
printf("the child that ignores SIGTERM: exit code zero: %s\n", $stubbornCode === 0 ? 'yes' : 'no');

printf(
    "both stopped, neither running any more: %s\n",
    !$politeChild->isRunning() && !$stubbornChild->isRunning() ? 'yes' : 'no',
);
```

**Перевод комментариев:**
- `ch22-owned-process.php` (PHPDoc): контракт корректной остановки: ребёнок,
  который слушает `SIGTERM`, останавливается вежливо, тот, который игнорирует,
  убивается, и код возврата их различает. Различие не косметическое.
  `queue:consume` решает, может ли он заявить, что слил всё, именно по этой
  разнице, поэтому убитый ребёнок никогда не должен отчитываться как вежливо
  остановленный. `stop()` спрашивает через `SIGTERM`, спрашивает ещё раз, если
  первый остался без ответа, и только потом использует `SIGKILL` — и возвращает
  ненулевой код для ребёнка, которому это понадобилось. Оба ребёнка написаны
  здесь, поэтому весь эксперимент — один файл: вежливый выходит из своего
  обработчика `SIGTERM`, упрямый ставит обработчик, который ничего не делает.
  Каждый пишет файл готовности после установки обработчика, и родитель ждёт оба,
  прежде чем сигналить, — сигнал, пришедший до запуска `pcntl_signal()`, убивает
  по умолчанию, и именно так «тест на корректную остановку» превращается во
  флаки. Pid не печатаются: они меняются от прогона к прогону, а проверяется
  классификация, не число. Запуск в контейнере, где проект смонтирован в `/app`:
  `docker compose cp ...` и `docker compose exec ...`.
- `// Deliberately ignore it, the way a stuck process does.` — намеренно
  игнорировать его, как это делает залипший процесс.

```text
both children started, handlers installed: yes
both still running: yes
the child that honours SIGTERM: exit code zero: yes
the child that ignores SIGTERM: exit code zero: no
both stopped, neither running any more: yes
```

`the child that honours SIGTERM: exit code zero: yes` — ребёнок вышел сам,
`stop()` вернул 0. `the child that ignores SIGTERM: exit code zero: no` — его
пришлось убить, и это видно в коде возврата, а не только в логе.

Проверка времени: `stop(1.0)` у упрямого — не «на всякий случай подождать», а
контракт: `terminate()` посылает `SIGTERM`, при неответе посылает ещё раз через
2 с, и по дедлайну переходит к `SIGKILL`. Обратите внимание, что тест **не
печатает pid** и не меряет секунды: проверяется классификация, а
`both stopped, neither running any more: yes` подтверждает, что после контракта
не осталось живых процессов.

### 3. Запустить или принять

Настоящий дочерний процесс-«пул» на unix-сокете, и опрос адреса до и после.
Здесь видно, как решение о владении принимается по факту «отвечает ли», а не по
флагу в конфигурации.

```bash
docker compose cp /tmp/ch22-start-or-adopt.php php:/tmp/ch22-start-or-adopt.php
docker compose exec php php /tmp/ch22-start-or-adopt.php
```

```php
<?php

declare(strict_types=1);

/**
 * Start-or-adopt, with a real child: the probe is what decides ownership.
 *
 * Nothing is listening, so the address is free. The command starts a child
 * (a tiny unix-socket server), waits until it ANSWERS, and registers its stop
 * - because it started it. A second command probing the same address sees it
 * occupied and adopts it: it uses the server and leaves it running, which is
 * what keeps one command from stopping another command's infrastructure.
 *
 * Step six is the other half of the same rule: once the owner (and only the
 * owner) stops it, the probe goes back to "free". Adopting is not a promise
 * to stop later.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch22-start-or-adopt.php php:/tmp/ch22-start-or-adopt.php
 *   docker compose exec php php /tmp/ch22-start-or-adopt.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch22-adopt-' . getmypid() . '/';

@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$child = <<<'CHILD'
<?php

declare(strict_types=1);

$path = $argv[1];

// Installed before the socket exists, so a probe that says "occupied"
// already means "this child will stop politely".
pcntl_async_signals(true);
pcntl_signal(SIGTERM, static function (): void {
    exit(0);
});

@unlink($path);

$server = stream_socket_server('unix://' . $path, $errno, $error);

if ($server === false) {
    fwrite(STDERR, "no socket: {$error}\n");
    exit(1);
}

while (true) {
    $client = @stream_socket_accept($server, 1.0);

    if ($client !== false) {
        fclose($client);
    }
}
CHILD;

$script = $directory . 'pool.php';
$socket = $directory . 'pool.sock';
$address = 'unix://' . $socket;

file_put_contents($script, $child);

$probe = static fn (): bool => OwnedProcess::endpointAnswers($address, 0.2);

printf("1. before anything starts, the probe says: %s\n", $probe() ? 'occupied' : 'free');

$process = OwnedProcess::start([PHP_BINARY, $script, $socket], 'pool', $directory . 'logs');
$up = OwnedProcess::waitForQuietly($probe, 5.0);

printf("2. after starting a child and waiting for an answer: %s\n", $up ? 'occupied' : 'free');
printf("3. so a second command: %s\n", $up ? 'adopts it and registers no stop' : 'starts one and owns it');
printf("4. adopting did not stop it, the owner still holds it: %s\n", $process->isRunning() ? 'yes' : 'no');

$code = $process->stop(5.0);

printf("5. the owner stops it: exit code zero: %s\n", $code === 0 ? 'yes' : 'no');
printf("6. the probe after the owner stopped it: %s\n", $probe() ? 'occupied' : 'free');
```

**Перевод комментариев:**
- `ch22-start-or-adopt.php` (PHPDoc): запустить-или-принять с настоящим
  ребёнком: владение решает опрос. Никто не слушает, значит адрес свободен.
  Команда запускает ребёнка (крошечный unix-socket сервер), ждёт, пока он
  ОТВЕТИТ, и регистрирует его остановку — потому что запустила его. Вторая
  команда, опрашивая тот же адрес, видит его занятым и принимает: она пользуется
  сервером и оставляет его работать, и это не даёт одной команде остановить
  инфраструктуру другой. Шаг шесть — другая половина того же правила: как только
  владелец (и только владелец) остановил его, опрос снова говорит «свободен».
  Принятие — не обещание остановить позже. Запуск в контейнере, где проект
  смонтирован в `/app`: `docker compose cp ...` и `docker compose exec ...`.
- `// Installed before the socket exists, so a probe that says "occupied"` —
  обработчик установлен до появления сокета, поэтому опрос, говорящий «занято»,
  уже означает «этот ребёнок остановится вежливо».

```text
1. before anything starts, the probe says: free
2. after starting a child and waiting for an answer: occupied
3. so a second command: adopts it and registers no stop
4. adopting did not stop it, the owner still holds it: yes
5. the owner stops it: exit code zero: yes
6. the probe after the owner stopped it: free
```

Шесть шагов читаются как правила из §2:

1. до запуска опрос говорит `free` — значит, можно запускать и владеть;
2. после запуска и **ожидания ответа** — `occupied`;
3. вторая команда на этом адресе `adopts it and registers no stop`;
4. принятие не остановило ребёнка — владелец всё ещё держит его (`yes`), потому
   что принявший не регистрирует шаг остановки;
5. владелец останавливает — и, поскольку обработчик `SIGTERM` установлен до
   создания сокета, остановка корректная (`exit code zero: yes`);
6. после остановки владельцем опрос снова говорит `free`.

Шаг 2 — самый важный. Это не «процесс запустился», а «процесс отвечает»:
`waitForQuietly()` крутит опрос до дедлайна и проверяет ответ, а не факт `fork`.

### 4. Где на самом деле границы

Последний эксперимент — не про процессы, а про импорты: он читает дерево
исходников и `composer.json` и считает, какие пространства имён платформа
действительно подключает, а какие только запускает.

```bash
docker compose cp /tmp/ch22-boundary.php php:/tmp/ch22-boundary.php
docker compose exec php php /tmp/ch22-boundary.php
```

```php
<?php

declare(strict_types=1);

/**
 * Where the boundaries actually are: which components are imported, and
 * which of them the platform reaches without importing anything.
 *
 * The claim this checks is the one the chapter rests on - php-mini-search is
 * the single component the platform speaks to over HTTP and never imports.
 * It is checked by reading the source tree, not by trusting the prose: every
 * `use <Namespace>\` line under src/ and tests/ is counted per top-level
 * namespace, and the result is compared with the packages composer.json
 * declares.
 *
 * Two facts together mean a real boundary: the package is required (the
 * binary has to exist) and no namespace of it is imported anywhere. The last
 * section also checks the converse - every namespace imported has a
 * declaration behind it.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch22-boundary.php php:/tmp/ch22-boundary.php
 *   docker compose exec php php /tmp/ch22-boundary.php
 */

$root = '/app';

/**
 * Count `use <TopLevelNamespace>\...` lines per namespace under one tree.
 *
 * @return array<string, int>
 */
$scan = static function (string $directory): array {
    $counts = [];
    $files = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator('/app/' . $directory, FilesystemIterator::SKIP_DOTS),
    );

    foreach ($files as $file) {
        /** @var SplFileInfo $file */
        if ($file->getExtension() !== 'php') {
            continue;
        }

        $found = preg_match_all(
            '/^use\s+([A-Za-z_][A-Za-z0-9_]*)\\\\/m',
            (string) file_get_contents($file->getPathname()),
            $matches,
        );

        foreach ($matches[1] as $namespace) {
            $counts[$namespace] = ($counts[$namespace] ?? 0) + 1;
        }
    }

    ksort($counts);

    return $counts;
};

$composer = json_decode((string) file_get_contents($root . '/composer.json'), true);
$requiredAll = array_keys((array) $composer['require']);
$required = array_values(array_map(
    static fn (string $name): string => str_starts_with($name, 'researcher86/') ? substr($name, strlen('researcher86/')) : $name,
    $requiredAll,
));

/**
 * namespace => [package, how the platform reaches it]
 */
$components = [
    'PhpMiniHttpServer' => ['php-mini-http-server', 'a library inside this process'],
    'PhpMiniDatabase' => ['php-mini-database', 'a library client, a daemonized child server'],
    'PhpMiniCache' => ['php-mini-cache', 'a library client, a child server it owns'],
    'PhpJobQueue' => ['php-job-queue', 'a library: producer here, consumer elsewhere'],
    'PhpWorkerPool' => ['php-worker-pool', 'an SDK client, the Master as a child'],
    'PhpMiniSearch' => ['php-mini-search', 'HTTP only, no class imported'],
];

$src = $scan('src');
$tests = $scan('tests');

printf(
    "%-19s %-22s %-9s %4s %6s  %s\n",
    'namespace',
    'package',
    'declared',
    'src',
    'tests',
    'reached by',
);

foreach ($components as $namespace => [$package, $how]) {
    printf(
        "%-19s %-22s %-9s %4d %6d  %s\n",
        $namespace,
        $package,
        in_array($package, $required, true) ? 'yes' : 'no',
        $src[$namespace] ?? 0,
        $tests[$namespace] ?? 0,
        $how,
    );
}

$packageOf = ['Ramsey' => 'ramsey/uuid'];
$undeclared = [];

foreach ($src as $namespace => $count) {
    if ($namespace === 'PhpSystemsPlatform') {
        continue;
    }

    $package = $packageOf[$namespace] ?? null;

    foreach ($components as $componentNamespace => [$componentPackage, $how]) {
        if ($componentNamespace === $namespace) {
            $package = $componentPackage;
        }
    }

    if ($package === null || !in_array($package, $required, true)) {
        $undeclared[] = $namespace;
    }
}

printf(
    "\nnamespaces imported in src without a declared package: %s\n",
    $undeclared === [] ? 'none' : implode(', ', $undeclared),
);
printf(
    "php-mini-search classes imported in src or tests: %d\n",
    ($src['PhpMiniSearch'] ?? 0) + ($tests['PhpMiniSearch'] ?? 0),
);
```

**Перевод комментариев:**
- `ch22-boundary.php` (PHPDoc): где на самом деле границы: какие компоненты
  импортируются, а до каких платформа доходит, ничего не импортируя.
  Утверждение, которое это проверяет, — то, на котором стоит глава:
  `php-mini-search` — единственный компонент, с которым платформа говорит по
  HTTP и никогда не импортирует. Проверяется чтением дерева исходников, а не
  доверием к прозе: каждая строка `use <Namespace>\` под `src/` и `tests/`
  считается по пространству имён верхнего уровня, и результат сравнивается с
  пакетами, объявленными в `composer.json`. Два факта вместе означают настоящую
  границу: пакет требуется (бинарь должен существовать), и ни одно его
  пространство имён нигде не импортируется. Последний раздел проверяет и
  обратное: за каждым импортированным пространством имён стоит объявление.
  Запуск в контейнере, где проект смонтирован в `/app`: `docker compose cp ...`
  и `docker compose exec ...`.
- `$scan` (PHPDoc): считает строки `use <TopLevelNamespace>\...` по
  пространствам имён в одном дереве. `@return`: массив `string => int`.
- `$file` (`@var`): итератор отдаёт `SplFileInfo`.
- `$components` (PHPDoc): пространство имён →
  `[пакет, как платформа его достигает]`.

```text
namespace           package                declared   src  tests  reached by
PhpMiniHttpServer   php-mini-http-server   yes         18      5  a library inside this process
PhpMiniDatabase     php-mini-database      yes          5      5  a library client, a daemonized child server
PhpMiniCache        php-mini-cache         yes          8      2  a library client, a child server it owns
PhpJobQueue         php-job-queue          yes         53     46  a library: producer here, consumer elsewhere
PhpWorkerPool       php-worker-pool        yes         31     10  an SDK client, the Master as a child
PhpMiniSearch       php-mini-search        yes          0      0  HTTP only, no class imported

namespaces imported in src without a declared package: none
php-mini-search classes imported in src or tests: 0
```

Таблица отвечает на первый вопрос §1 «как её достигают» списком, который
посчитан, а не выписан: 18 импортов `PhpMiniHttpServer` — HTTP-сервер живёт в
этом же процессе; 53 импорта `PhpJobQueue` — очередь библиотека; 31 импорт
`PhpWorkerPool` — SDK-клиент, за которым стоит дочерний мастер.

И одна строка, ради которой эксперимент и написан (в выводе это строка
`PhpMiniSearch  php-mini-search  yes  0  0  HTTP only, no class imported`):

Пакет **объявлен** (`yes` — бинарь `vendor/bin/mini-search` нужен), но импортов
**ноль** и в `src/`, и в `tests/`. Итоговая строка это подтверждает:
`php-mini-search classes imported in src or tests: 0`. Вот что значит настоящая
граница: не «мы стараемся не лезть внутрь», а «внутрь нельзя попасть, потому что
классы не подключены».

Вторая проверка — обратная:
`namespaces imported in src without a declared package: none`. Никакой импорт не
появляется «случайно», из транзитивной зависимости: всё, что используется,
объявлено в `composer.json`.

## 5. Отказы

| Что ломается                                     | Чем обнаруживается                             | Что происходит                                                                                              |
| ------------------------------------------------ | ---------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| Путь выхода выбрасывает исключение до остановок  | пропущенные шаги                               | `finally` на `ShutdownStack` освобождает всё; флаги так не умели (реальный дефект `serve`)                  |
| Шаг очистки сам падает                           | сообщение в stderr                             | стек продолжает остальные шаги; имя шага указывает, что не отпустилось                                      |
| `run()` вызван дважды                            | флаг `$ran`                                    | второй вызов ничего не делает; одинаковая остановка дважды невозможна                                       |
| `push()` после `run()`                           | `LogicException`                               | шаг отклонён: он всё равно никогда не выполнился бы                                                         |
| Сервер остановлен не владельцем                  | правило «только то, что запустил»              | чужой кеш/пул не трогается; принявший не регистрирует шаг                                                   |
| Устаревший демон базы после упавшей `serve`      | pid-файл `minidb.pid`                          | `stopStaleDatabaseServer()` останавливает его при холодном старте; порт без pid-файла — чужой, не трогается |
| Ребёнок умирает на старте                        | ожидание следит и за процессом                 | провал сразу, а не через весь дедлайн; чужой ответ на порту не принимается за готовность                    |
| `SIGTERM` проигнорирован                         | `terminate()` вернул `false`                   | повторный `SIGTERM`, затем `SIGKILL`; `stop()` возвращает ненулевой код                                     |
| `SIGTERM` пришёл до установки обработчика        | —                                              | убивает по умолчанию: тесты ждут файл готовности ребёнка, а не просто факт запуска                          |
| Повторный `SIGTERM` посреди корректной остановки | обработчики идемпотентны                       | трактуется как та же просьба; это чинит зависание в CI (мастер пула)                                        |
| Пул запущен дважды                               | пинг по сокету                                 | второй не запускается: принятие вместо второго мастера                                                      |
| Клиент пула унаследован форком                   | сокет отвечает двум процессам                  | `WorkerManager::forwarder()` строит клиент лениво, внутри форка                                             |
| `php-mini-search` не отвечает                    | `endpointAnswers` до дедлайна `start_deadline` | `RuntimeException`; `serve` не стартует с неготовой службой                                                 |
| Служба поиска отвечает, но просит переиндексацию | `notReadyReason()`                             | `product.reindex` публикуется (→ глава 21)                                                                  |

## 6. Как это проверено

Правила главы проверяются на трёх уровнях: юнит-тесты класса, тесты процессов и
интеграционные тесты границ.

`tests/Unit/ShutdownStackTest.php` — 5 методов, названных свойствами, а не
методами: `testReleasesInReverseOrderOfAcquisition`,
`testRunningTwiceDoesNotReleaseTwice`,
`testAThrowingStepDoesNotSkipTheOnesBehindIt`,
`testRunIsIdempotentAfterATruncatedRun`, `testRegisteringAfterRunIsRefused`. Это
буквально эксперимент 1, разложенный на пять утверждений.

Процессы: `tests/Unit/OwnedProcessTerminateTest.php` —
`testASigtermThatGoesUnansweredIsSentAgain` (повторный `SIGTERM` из §2) и
`testAChildThatNeverStopsOnSigtermIsKilledAtTheDeadline`;
`tests/Unit/OwnedProcessAddressEnvironmentTest.php` — три метода о том, что
адресное окружение переносится ребёнку целиком и ничего лишнего
(`testCarriesNothingElse`).

Границы: `tests/Integration/Boundary/` — 40 тестов, 169 утверждений, пять файлов
по одному шву, и каждый файл доказывает, что шов работает **сам по себе**, а не
в сборе: `HttpApplicationBoundaryTest`, `ApplicationDatabaseBoundaryTest`,
`ApplicationQueueBoundaryTest`, `ApplicationCacheBoundaryTest`,
`QueueWorkerPoolBoundaryTest`. Первые пять методов последнего — эксперимент 3 по
сути: задача уходит воркеру, ответ приходит **от процесса-воркера, а не от
вызывающего** (`testTheAnswerComesFromAWorkerProcessAndNotFromTheCaller`),
несколько задач идут одновременно на разных воркерах
(`testSeveralTasksRunAtTheSameTimeOnDifferentWorkers`), пул сообщает, каких
воркеров держит, упавший воркер проваливает свой запрос и заменяется
(`testACrashedWorkerFailsItsRequestAndIsReplaced`).

Остановка живого потребителя без потери заданий закреплена в
`tests/Integration/ServeIntegrationTest.php` (59 методов) методом
`testGracefulShutdownLosesNoJobs`: он посылает `SIGTERM` живому консьюмеру и
сливает оставшихся новым — то есть проверяет не «остановился», а «ничего не
потерял».

Прогоны: `docker compose exec php php vendor/bin/phpunit tests/Unit` — **145
тестов, 348 утверждений, OK**; `… tests/Integration/Boundary` — **40 тестов, 169
утверждений, OK**.

## 7. Цифры

| Файл                                    | Строк | Что делает                                          |
| --------------------------------------- | ----- | --------------------------------------------------- |
| `src/Support/ShutdownStack.php`         | 76    | LIFO, изоляция, идемпотентность                     |
| `src/Support/OwnedProcess.php`          | 348   | запуск, остановка, опрос, одноразовый пул           |
| `src/Cli/PlatformCli.php`               | 2150  | все команды и вся стратегия владения                |
| `bin/worker.php`                        | 101   | мастер пула: размеры, таймауты, пять семейств задач |
| `src/Workers/WorkerManager.php`         | 68    | мост очередь → пул                                  |
| `src/Application/ApplicationWiring.php` | 52    | швы, включая необязательный поиск                   |

| Константа или предел                    | Значение                       |
| --------------------------------------- | ------------------------------ |
| Дедлайн старта сервера у `serve`        | 10 с                           |
| Дедлайн старта службы поиска            | 60 с (`search.start_deadline`) |
| Дедлайн старта в `OwnedProcess`         | 25 с                           |
| Дедлайн остановки                       | 20 с                           |
| Повтор `SIGTERM`                        | каждые 2 с                     |
| Интервал опроса                         | 0.05 с                         |
| Таймаут подключения в опросе порта      | 0.2 с                          |
| Воркеров в пуле                         | 2…16 (пол 2 форкается сразу)   |
| Очередь пула до backpressure            | 10 000                         |
| Компонентов объявлено в `composer.json` | 6 (+ `ramsey/uuid`)            |

Импорты по компонентам (эксперимент 4, посчитано по дереву): HTTP-сервер 18/5,
база 5/5, кеш 8/2, очередь 53/46, пул 31/10, **поиск 0/0** (`src`/`tests`).

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что                   | Значение                                                                   |
| --------------------- | -------------------------------------------------------------------------- |
| `ch22-shutdown-stack` | 85 строк; порядок `worker pool → cache → database server`; сбой изолирован |
| `ch22-owned-process`  | 107 строк; `exit code zero`: `yes` у вежливого, `no` у упрямого            |
| `ch22-start-or-adopt` | 88 строк; `free → occupied → adopt → free`                                 |
| `ch22-boundary`       | 134 строки; `PhpMiniSearch` 0 импортов; необъявленных пакетов нет          |

## 8. Упражнения

1. **Проследите.** Команда захватила базу (демон), кеш (ребёнок) и пул
   (ребёнок), а затем на середине инициализации бросила исключение. Что и в
   каком порядке выполнит `ShutdownStack`? Что изменится, если шаг кеша бросит
   исключение, а шаг пула — нет?
2. **Найдите правило.** Почему `ShutdownStack` отказывается принимать шаги после
   `run()`, а не просто игнорирует их? Приведите путь кода, на котором молча
   проигнорированный шаг превратился бы в утечку.
3. **Сравните.** Демон базы отслеживается pid-файлом, а кеш — ручкой дочернего
   процесса. Опишите, как каждая схема ошибается: что происходит со «устаревшим»
   демоном и что — с «потерянной» ручкой. Почему для базы нужен и pid-файл, и
   порт?
4. **Разберитесь в контракте.** Ребёнок, убитый по умолчанию действием `SIGTERM`
   (без обработчика), тоже возвращает ненулевой код. Значит ли это, что `stop()`
   не отличает «убит» от «умер»? Что именно он различает и как это использует
   `queue:consume`?
5. **Спроектируйте тест.** В эксперименте 2 дети пишут файл готовности.
   Придумайте тест на «принятие» (adopt), который не зависит от времени и не
   печатает pid, и объясните, какой шов для этого нужен.
6. **Разберитесь в цене.** Граница по сети есть только у поиска. Какие проверки
   и таймауты добавляет эта граница по сравнению с импортом класса (→ глава 21)
   и что она взамен даёт?

## Что дальше

Границы определены и владение однозначно — но именно поэтому теперь видно, куда
распространяется отказ. Умерший воркер, недоступная служба поиска, заполненная
очередь и занятый порт — это не «проблемы компонента», а события системы: у
каждого есть путь к соседям. Глава 23 разбирает эти пути: что случается, когда
отказ пересекает границу, зачем нужен backpressure и как отказ одного узла не
превращается в отказ всего стенда.
