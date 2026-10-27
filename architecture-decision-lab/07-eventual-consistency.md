# Глава 7. Eventual consistency: окно, в котором чтение видит старое

> Проект:
> [`architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
> Предыдущая глава: сага и двухфазный коммит.

Eventual consistency часто описывают как «данные когда-нибудь сойдутся». Это
правда, но бесполезная половина правды. Вторая половина — окно, в котором данные
ещё не сошлись. В этом окне чтение возвращает старое значение, и пользователь
видит не «когда-нибудь», а прямо сейчас: бронь отсутствует, доступность ещё не
уменьшилась, статус ещё `pending`.

Лаборатория измеряет именно окно. Strong-проекция применяет событие в момент
публикации. Eventual-проекция ставит его в очередь с временем `due_at` и
применяет позже. В эксперименте 100 чтений до разбора очереди возвращают старое
состояние, 100 чтений после — новое. Источник и проекция сходятся к одному
значению. Разница не в финале, а в том, сколько чтений успело увидеть
нефинальное состояние.

---

## 1. Проблема

После главы 5 событие надёжно переживает коммит. После главы 6 бронь либо
подтверждена, либо отменена, либо ждёт восстановления. Но у нас есть ещё
read-model: `trip_view` со статусами поездок и `availability_view` с остатками
ресурсов. Это не копия write-таблиц, а отдельная модель, построенная из событий.

Между публикацией события и его применением есть зазор:

1. Outbox-диспетчер читает pending-событие.
2. Публикует его в шину.
3. Слушатель либо применяет событие сразу (strong), либо кладёт в очередь
   (eventual).
4. Eventual-очередь применяет событие, когда наступает `due_at`.

Шаги 3 и 4 — разные моменты времени. Если клиент читает проекцию между ними, он
видит состояние до события. Это не баг конкретной реализации, а свойство модели.
Вопрос в том, какие чтения могут это пережить и что система обещает клиенту.

Есть три разных обещания, которые легко смешать:

- **read-your-writes**: после успешной записи читатель видит свою запись;
- **monotonic reads**: читатель не откатывается к более старому состоянию;
- **eventual convergence**: все реплики/проекции рано или поздно сходятся.

Лаборатория моделирует только третье и показывает, как нарушается первое. Второе
и третье в одном процессе не различимы: проекция одна, и после применения
события она не откатывается.

---

## 2. Устройство

### 2.1. Одна шина, два слушателя

`LabFactory` собирает проекцию, хранилище обработанных событий, очередь и
runner. Затем выбирает слушателя по режиму consistency:

```php
        $bus = new InMemoryEventBus($counters);
        $projection = new Projection($reportingDb, $counters, $clock);
        $processed = new ProcessedEventStore($reportingDb, $counters, $clock);
        $projector = new IdempotentProjector($projection, $processed, $reportingDb, $counters);
        $queue = new ProjectionQueue($counters);
        $runner = new ProjectionRunner($queue, $projector, $clock, $counters);
        $listener = $config->consistency === ConsistencyMode::Strong
            ? new ImmediateProjectionListener($projector)
            : new QueuedProjectionListener($queue, $clock, $config->projectionLagMs);
        $bus->subscribe('booking.confirmed', $listener);
        $bus->subscribe('booking.cancelled', $listener);
```

[`src/Integration/LabFactory.php` L116-L126](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Integration/LabFactory.php#L116-L126)

Различие только в слушателе. Домен, outbox, диспетчер и проекция одинаковы. Это
важно для сравнения: меняется момент применения события, а не логика
бронирования.

### 2.2. Strong применяет событие в publish

В strong-режиме слушатель вызывает проектор прямо из
`InMemoryEventBus::publish()`:

```php
final readonly class ImmediateProjectionListener implements EventListener
{
    public function __construct(
        private IdempotentProjector $projector,
    ) {
    }

    public function handle(EventEnvelope $event): void
    {
        $this->projector->handle($event);
    }
}
```

[`src/Consistency/ImmediateProjectionListener.php` L7-L18](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/ImmediateProjectionListener.php#L7-L18)

«Strong» здесь означает не распределённую линеаризуемость, а синхронное
применение в том же вызове. Если проектор бросит исключение, `publish()` тоже
бросит, outbox останется pending и повторит доставку. Это делает проекцию частью
пути публикации: она не отстаёт, но её отказ становится отказом доставки.

### 2.3. Eventual ставит событие в очередь с due_at

Eventual-слушатель не применяет событие, а вычисляет, когда оно станет видимым:

```php
final readonly class QueuedProjectionListener implements EventListener
{
    public function __construct(
        private ProjectionQueue $queue,
        private VirtualClock $clock,
        private int $lagMs,
    ) {
    }

    public function handle(EventEnvelope $event): void
    {
        $this->queue->enqueue($event, $this->clock->now() + $this->lagMs);
    }
}
```

[`src/Consistency/QueuedProjectionListener.php` L9-L22](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/QueuedProjectionListener.php#L9-L22)

Очередь сортирует события по `due_at` и отдаёт только те, чей срок наступил:

```php
    public function enqueue(EventEnvelope $event, int $dueAt): void
    {
        $this->entries[] = ['event' => $event, 'due_at' => $dueAt];
        usort($this->entries, static fn (array $a, array $b): int => $a['due_at'] <=> $b['due_at']);
        $this->counters->increment('projectionQueued');
    }

    /**
     * @return list<EventEnvelope>
     */
    public function due(int $now): array
    {
        $due = [];
        $remaining = [];

        foreach ($this->entries as $entry) {
            if ($entry['due_at'] > $now) {
                $remaining[] = $entry;

                continue;
            }

            $due[] = $entry['event'];
        }

        $this->entries = $remaining;

        return $due;
    }
```

[`src/Consistency/ProjectionQueue.php` L23-L51](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/ProjectionQueue.php#L23-L51)

`due_at` — логическое время, а не wall-clock. В лаборатории `projectionLagMs` по
умолчанию 50, и событие становится видимым через 50 логических миллисекунд после
публикации. Это не `sleep(0.05)`: реальное время выполнения не меняется.
Моделируется окно, а не стоимость ожидания.

Очередь живёт в памяти процесса. Это важное ограничение, к которому мы вернёмся:
outbox гарантирует, что событие не потеряется до публикации, но не гарантирует,
что оно переживёт падение процесса после публикации и до применения.

### 2.4. Drain двигает логическое время

`ProjectionRunner::drain()` разбирает очередь до конца. Для каждого события он
переставляет логические часы на `due_at` и вызывает проектор:

```php
    public function drain(): int
    {
        $applied = 0;

        while (!$this->queue->isEmpty()) {
            $next = $this->queue->next();

            if ($next === null) {
                break;
            }

            $this->clock->set($next['due_at']);
            $this->projector->handle($next['event']);
            $this->queue->removeNext();
            ++$applied;
        }

        $this->counters->increment('projectionDrainCalls');
        $this->counters->add('projectionDrained', $applied);

        return $applied;
    }
```

[`src/Consistency/ProjectionRunner.php` L40-L61](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/ProjectionRunner.php#L40-L61)

Слово «drain» здесь означает «сделать проекцию догнанной в модели», а не
«дождаться реального consumer lag». В production очередь была бы внешней, а
runner — фоновым процессом с собственным offset. В лаборатории drain синхронный
и детерминированный: после него очередь пуста, а проекция содержит все события.

### 2.5. Проекция — это delta, а не копия

`Projection::apply()` обновляет доступность на величину из события и делает
upsert статуса поездки:

```php
        foreach ($lines as $line) {
            $delta = $this->delta($event->type, $line);

            if ($delta === 0) {
                continue;
            }

            $type = ResourceType::from($line['type']);
            $updated = $this->db->execute(
                'UPDATE availability_view
                 SET available = available + :delta, version = version + 1, updated_at = :now
                 WHERE resource_type = :type AND resource_id = :id',
                [
                    'delta' => $delta,
                    'now' => $this->clock->now(),
                    'type' => $type->value,
                    'id' => $line['id'],
                ],
            );

            if ($updated === 0) {
                throw new \RuntimeException(sprintf(
                    'Projection has no seed for %s %s',
                    $type->value,
                    $line['id'],
                ));
            }

            $this->counters->increment('projectionAvailabilityUpdates');
        }

        $this->db->execute(
            'INSERT INTO trip_view (booking_id, status, traveler_id, amount_cents, version, updated_at)
             VALUES (:booking_id, :status, :traveler_id, :amount, 1, :now)
             ON CONFLICT(booking_id) DO UPDATE SET
                status = excluded.status,
                amount_cents = excluded.amount_cents,
                version = trip_view.version + 1,
                updated_at = excluded.updated_at',
            [
                'booking_id' => $bookingId,
                'status' => $status,
                'traveler_id' => $travelerId,
                'amount' => $amount,
                'now' => $this->clock->now(),
            ],
        );

        $this->counters->increment('projectionApplied');
```

[`src/Consistency/Projection.php` L60-L108](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/Projection.php#L60-L108)

Смысл delta задаётся отдельно:

```php
    private function delta(string $eventType, array $line): int
    {
        if ($line['state'] !== 'committed') {
            return 0;
        }

        return $eventType === 'booking.confirmed' ? -$line['units'] : $line['units'];
    }
```

[`src/Consistency/Projection.php` L140-L147](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/src/Consistency/Projection.php#L140-L147)

Только `committed`-строки меняют доступность. Подтверждённая бронь вычитает
единицы, отмена возвращает их. Если бронь отменена до подтверждения, её строки
имеют состояние `released`, и delta равна нулю: проекция не должна возвращать
то, что никогда не вычитала. Это защита от двойного восстановления.

У проекции есть `version`, но нет compare-and-swap:
`available = available + :delta` не проверяет ожидаемую версию. Для одного
последовательного consumer'а этого достаточно. Для out-of-order доставки — нет.

---

## 3. Эксперимент

```bash
make experiment ARGS="consistency:projection-lag"
```

По умолчанию: 20 броней, 100 чтений, lag 50 мс, capacity 30. Сначала все брони
создаются и outbox разбирается, затем 100 раз читается статус первой брони,
затем очередь проекции разбирается, затем те же 100 чтений повторяются. Вот
реальный вывод:

```text
Experiment: Eventual consistency
bookings=20 reads=100 lag=50 capacity=30

case       stale before stale after   queued    lag ms  applied  source    view
strong                0           0        0         0        0      10      10
eventual            100           0       20        50       20      10      10
```

**`strong`.** Ноль устаревших чтений до и после. Очередь пуста, drain применяет
0 событий: проекция уже применена внутри `dispatchOutbox()`. `applied = 0` в
этой колонке означает «применено при drain», а не «не применено вообще».
`source = view = 10`: все 20 броней заняли по единице из 30.

**`eventual`.** После dispatch 20 событий лежат в очереди. Все 100 чтений до
drain возвращают старое состояние: `stale before = 100`. Drain двигает
логическое время на 50 мс и применяет 20 событий. После drain те же 100 чтений
возвращают новое состояние: `stale after = 0`. `source = view = 10`: источник и
проекция сошлись.

Сто чтений из ста — это не «вероятность устаревания 100%». Это цикл без
продвижения логического времени: все 100 чтений происходят в один и тот же
момент модели. Эксперимент доказывает существование окна и его направление, а не
его длительность в секундах. Если между чтениями advance-нуть часы на 50 мс,
часть чтений увидит новое состояние. Лаборатория специально не делает этого,
чтобы окно было видно целиком.

`lag ms = 50` — это ровно сконфигурированный lag. Drain переставляет часы на
`due_at` первого события, поэтому наблюдённая задержка равна 50. В реальной
системе она зависела бы от backlog, скорости consumer'а и конкуренции за
ресурсы. Здесь измеряется модель, а не production lag.

### Сценарий целиком

`make scenario` использует eventual-проекцию с lag 50 и двумя бронями. Вот
фрагмент реального вывода:

```text
  t=9 booking trip-0001: confirmed after 8 ms, compensation attempts 0
  t=26 booking trip-0002: cancelled after 16 ms, compensation attempts 4 (Injected failure at payment.authorize (before effect))
  t=26 outbox has 2 pending event(s); the eventual projection still reports booking trip-0001 as absent
  t=26 outbox dispatch delivered 2 of 2 event(s), 0 pending
  t=76 projection drained 2 event(s); booking trip-0001 is now confirmed
```

До dispatch проекция не знает даже о подтверждённой брони. После dispatch
событие уже в очереди, но всё ещё не применено. Только на t=76, после drain,
`trip_view` содержит подтверждение. Сводка сценария: 2 брони, 1 подтверждена, 1
отменена, 4 попытки компенсации, 2 события доставлены, 2 применены, 2 устаревших
чтения, источник и проекция по 3 единицы, 0 pending после.

Два устаревших чтения в сценарии — это ровно два разных вопроса: «есть ли
подтверждённая поездка?» и «какая доступность рейса?». Оба возвращают старое
состояние, пока очередь не разобрана.

---

## 4. Бенчмарк

```bash
make benchmark ARGS="07-consistency"
```

300 полных roundtrip'ов: бронь, dispatch и drain. В eventual-случае drain
выполняется в том же измеряемом вызове, поэтому 50 мс — логические, а не
реальное ожидание.

| benchmark                        | итераций | медиана |     p95 | ops/sec | медиана на roundtrip |
| -------------------------------- | -------: | ------: | ------: | ------: | -------------------: |
| `consistency.strong_roundtrip`   |      300 | 551.688 | 555.924 |     544 |             1 839 µs |
| `consistency.eventual_roundtrip` |      300 | 527.824 | 744.556 |     568 |             1 759 µs |

Eventual-случай оказался на 4,3% быстрее по медиане. Это не победа eventual
consistency. Оба случая делают одну и ту же работу: применяют событие до конца
измеряемого roundtrip'а. Eventual лишь переставляет применение за очередь.
Разница медиан ниже шума контейнера, а p95 у eventual равен 744.556 мс — на 41%
выше его медианы. При пяти повторениях p95 — это максимум, а не гладкий
перцентиль. Читать это как «eventual быстрее» нельзя.

Настоящая цена eventual consistency не в CPU. Она в окне, которое бенчмарк не
измеряет: пока событие в очереди, чтение видит старое значение. Бенчмарк
специально разбирает очередь в каждом вызове, иначе он мерил бы не операцию, а
`sleep`. Если вам нужна цена ожидания, её надо мерить отдельно: сколько
пользовательских запросов готовы ждать конвергенции и сколько устаревших ответов
допускает продукт.

---

## 5. Что ломается и что не измерено

**Read-your-writes нарушается.** После успешной брони немедленное чтение
проекции может вернуть `null`. В эксперименте это 100 из 100 чтений, в сценарии
— 2 из 2 проверок. Это не редкий сбой, а штатное поведение окна. Продукт должен
выбрать: читать из write-модели, ждать версию, показывать `pending` или явно
предупреждать о задержке.

**Очередь проекции не персистентна.** Outbox защищает путь «коммит →
публикация». После публикации событие попадает в `ProjectionQueue` в памяти.
Если процесс упадёт после `markPublished()`, но до drain, outbox будет считать
событие опубликованным, а проекция его никогда не увидит. В лаборатории падения
в этом окне не инжектируются. В реальной системе нужен durable consumer offset
или отдельный outbox для проекции.

**Нет проверки порядка.** `ProcessedEventStore` защищает от повторного
применения события, но не от применения событий в неправильном порядке.
`Projection::apply()` не проверяет ожидаемую версию и не хранит sequence. Если
отмена придёт раньше подтверждения, `trip_view` может остаться в `cancelled`, а
доступность — в неверном состоянии. В текущем flow отмена всегда идёт после
неудачной брони, а не после подтверждённой, поэтому сценарий не воспроизводит
эту проблему.

**Нет rebuild.** Если проекция разошлась с источником, в лаборатории нет способа
её пересобрать. `Projection::seed()` задаёт начальную доступность, но не
проигрывает события заново. В production нужен snapshot + replay или полная
перестройка из outbox.

**Нет backpressure.** `ProjectionQueue` — массив без лимита. 20 событий
безопасны, миллион — нет. В отличие от `JobQueue` из главы 2, здесь нет
ограничения и явного отказа. Это ещё одна очередь, которая может вырасти в
память.

**Strong не бесплатен.** В strong-режиме проектор работает внутри publish. Если
проекция упадёт, dispatcher получит исключение, outbox останется pending и
повторит публикацию. Это может привести к дубликату, который поглотит
идемпотентный проектор. Strong убирает окно, но связывает доступность read-model
с доступностью публикации.

**Нет реальной репликации и clock skew.** В модели один процесс и логические
часы. Нет сетевых задержек, разных часов у узлов, partition, consumer groups и
rebalancing. Всё, что зависит от этих механизмов, остаётся за границей.

**Проекция — не источник истины.** `trip_view` и `availability_view` нельзя
использовать для решений, которые требуют актуального состояния: авторизации
платежа, проверки capacity, подтверждения брони. В лаборатории источником истины
остаются сервисы и их базы; проекция только читается.

---

## 6. Как это проверено

Тест фиксирует и окно, и конвергенцию:

```php
    public function testEventualProjectionStaysStaleUntilTheRunnerDrains(): void
    {
        $lab = $this->lab(new LabConfig(
            databaseDirectory: $this->directory,
            seed: 'test',
            consistency: \ArchitectureDecisionLab\Domain\ConsistencyMode::Eventual,
            projectionLagMs: 50,
        ));
        $lab->seed('f1', 'h1', 'c1', capacity: 4);

        $result = $lab->book($this->trip());
        $lab->dispatchOutbox();

        self::assertSame(1, $lab->projectionRunner->pending());
        self::assertNull($lab->projection->tripStatus($result->bookingId));
        self::assertSame(4, $lab->projectedAvailability(ResourceType::Flight, 'f1'));

        $applied = $lab->drainProjection();

        self::assertSame(1, $applied);
        self::assertSame('confirmed', $lab->projection->tripStatus($result->bookingId));
        self::assertSame(3, $lab->projectedAvailability(ResourceType::Flight, 'f1'));
    }
```

[`tests/Consistency/OutboxAndProjectionTest.php` L95-L117](https://github.com/Researcher86/architecture-decision-lab/blob/v1.0.0/tests/Consistency/OutboxAndProjectionTest.php#L95-L117)

До drain: событие в очереди (1), статуса нет (`null`), доступность ещё 4. После
drain: применено 1 событие, статус `confirmed`, доступность 3. Тест проверяет
именно промежуточное состояние, а не только финал. Без него реализация, которая
применяет событие сразу, прошла бы проверку конвергенции и потеряла бы главное
свойство eventual-режима.

Тест на duplicate в главе 5 проверяет другую сторону: повторная доставка не
применяет событие дважды. Вместе эти тесты задают контракт: событие может прийти
позже и может прийти дважды, но проекция обязана сойтись к одному результату.

Полный прогон на момент тега: 74 теста, 3 468 assertions, PHPStan level 8 без
ошибок, PHP-CS-Fixer без изменений.

---

## 7. Что запомнить

- Eventual consistency — это окно, а не только финальное состояние. Пока событие
  в очереди, чтение видит старое значение.
- Read-your-writes не появляется автоматически. После успешной записи проекция
  может ещё не знать о ней.
- Strong и eventual отличаются моментом применения, а не объёмом работы. Поэтому
  CPU-бенчмарк может не показать разницу.
- Устаревшие чтения — это цена, которую платит продукт. Её нужно измерять в
  пользовательских операциях, а не только в миллисекундах benchmark'а.
- Outbox гарантирует доставку до публикации, но не применение. Очередь проекции
  должна быть durable, если вы обещаете converged read model.
- Идемпотентность защищает от дубликатов, но не от неправильного порядка. Нужны
  версии или sequence.
- Проекция — read-model, а не источник истины. Решения о capacity, платеже и
  подтверждении брони принимаются по write-модели.
- «Eventually» — это не «никогда» и не «сразу». Это конкретное окно, которое
  надо назвать, измерить и объяснить пользователю.

---

## 8. Упражнения

1. Запустите `consistency:projection-lag --lag=500 --reads=1000`. Изменится ли
   число устаревших чтений? Почему оно зависит от продвижения логического
   времени, а не от количества чтений?
2. Сделайте очередь проекции durable: сохраняйте событие и offset в SQLite. Как
   восстановиться после падения между `markPublished()` и применением?
3. Добавьте в `Projection::apply()` проверку ожидаемой версии. Что должно
   произойти при событии с меньшей версией: ошибка, skip или retry?
4. Спроектируйте read-your-writes для сценария бронирования. Сравните три
   варианта: чтение из write-модели, ожидание версии проекции и показ `pending`.
   Какой из них честнее для пользователя?
5. Запустите сценарий с `--projection-lag-ms=0`. Остаётся ли он eventual? Что
   показывают два устаревших чтения?
6. Постройте rebuild проекции из outbox. Что нужно сбросить, какие события
   проиграть и как проверить, что результат совпал с источником?

---

## Вместо заключения

Семь глав — это семь решений одной системы. Они не независимы:

- форма развёртывания тянет за собой владение базой;
- владение базой убирает общую транзакцию и требует саги или 2PC;
- отсутствие общей транзакции требует outbox для событий;
- outbox делает доставку асинхронной и открывает окно eventual consistency;
- обработка может быть sync или async, но async добавляет очередь и второй
  момент истины;
- форма хранения меняет цену точечного чтения и запроса по чужому полю.

Ни одно из этих решений не бесплатно. Локальная транзакция платит связностью.
Сага — компенсациями и видимыми промежуточными состояниями. 2PC — удержаниями и
блокирующим окном. Outbox — дубликатами и идемпотентностью. Eventual consistency
— устаревшими чтениями. Даже «просто SQL» платит вторым запросом на точечное
чтение агрегата.

Поэтому вопрос «какая архитектура лучше» в этой лаборатории не имеет ответа.
Есть вопрос «какой отказ вы готовы пережить и чем готовы за него платить». Код,
тесты и измерения находятся в
[`Researcher86/architecture-decision-lab`](https://github.com/Researcher86/architecture-decision-lab)
на теге
[`v1.0.0`](https://github.com/Researcher86/architecture-decision-lab/releases/tag/v1.0.0).
Сценарий запускается одной командой `make scenario`, главы проверяются
`check_book.py`, а все числа в книге взяты из реальных прогонов, а не из оценок.
