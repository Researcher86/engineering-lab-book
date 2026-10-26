# Глава 6. Top-K в потоке: точная память и ограниченные оценки

> Проект: [`algorithms-lab`](https://github.com/Researcher86/algorithms-lab),
> тег [`v1.0.0`](https://github.com/Researcher86/algorithms-lab/releases/tag/v1.0.0).
> Предыдущая глава: [consistent hashing](05-consistent-hashing.md).
> Дальше: external sorting (глава 7).

«Top-K требует `O(K)` памяти» — одно из самых вредных упрощений в разговорах
о потоковой обработке. Куча действительно ограничена `K`, но точный ответ
требует счётчика для каждого встреченного ключа: иначе позднее появление
ключа нельзя сравнить с текущими лидерами. В этой главе сначала строится
точный `TopKHeap` с map на `O(distinct)`, затем — ограниченные summaries,
которые платят точностью и не умеют перечислять свои ключи.

Глава отвечает на три вопроса:

- сколько памяти стоит точный top-K на потоке из уникальных ключей;
- что именно гарантируют Count-Min, Space-Saving и Misra-Gries;
- почему capacity `K` не равна «гарантированно найду top-K».

---

## 1. Проблема

Поток приходит по одному элементу, и его нельзя перечитать. Нужно вернуть
`K` самых частых ключей. Если ключей мало, можно хранить map `ключ => счётчик`
и в конце выбрать top-K. Если ключей миллионы, map растёт вместе с числом
различных ключей `D`, даже если `K = 10`.

Точный ответ без map невозможен в общем случае: ключ, который встретится
позже, должен быть сопоставлен с текущими кандидатами. Поэтому точный
top-K — это `O(D + K)` памяти, а не `O(K)`. Куча нужна, чтобы не сортировать
весь map на каждом шаге; она не отменяет map.

Ограниченные структуры меняют контракт:

- **Count-Min sketch** оценивает частоту ключа, но не умеет находить
  кандидатов;
- **Space-Saving** хранит не больше `K` ключей и заменяет минимум;
- **Misra-Gries** хранит не больше `K` счётчиков и уменьшает все при
  переполнении.

У всех есть ошибка и вероятностные оговорки. Задача главы — измерить эту
ошибку, а не объявить один summary победителем.

---

## 2. Устройство

### 2.1. Точный TopKHeap

`TopKHeap` хранит min-heap из `K` записей и map `counts` для всех ключей.
При `observe(key)` счётчик увеличивается всегда. Если ключ уже в heap, его
запись обновляется и куча восстанавливается. Если ключ новый и heap не полон,
он добавляется. Если heap полон, новый ключ сравнивается с минимумом; при
победе минимум вытесняется.

```php
    public function observe(string $key, int $increment = 1): void
    {
        if ($increment < 1) {
            throw new InvalidArgumentException('Increment must be positive');
        }

        $count = ($this->counts[$key] ?? 0) + $increment;
        $this->counts[$key] = $count;

        if (isset($this->positions[$key])) {
            $index = $this->positions[$key];
            $this->heap[$index]->count = $count;
            $this->siftDown($index);
            $this->siftUp($index);

            return;
        }

        $entry = new TopKEntry($key, $count);

        if (count($this->heap) < $this->k) {
            $this->heap[] = $entry;
            $this->positions[$key] = count($this->heap) - 1;
            $this->siftUp(count($this->heap) - 1);

            return;
        }

        $worst = $this->heap[0];

        if (!$this->isBetter($entry, $worst)) {
            return;
        }

        unset($this->positions[$worst->key]);
        $this->heap[0] = $entry;
        $this->positions[$key] = 0;
        $this->siftDown(0);
    }
```

[`src/Stream/TopKHeap.php` L38-L76](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/Stream/TopKHeap.php#L38-L76)

Сравнение при равенстве идёт по ключу: меньший ключ считается лучшим. Это
делает результат детерминированным и объясняет, почему в отчёте порядок
стабилен. `top()` сортирует копию heap, поэтому не портит структуру.

`QuickSelect` решает ту же задачу иначе: он получает уже готовый map и
выбирает `K` элементов за среднее `O(D)`, худшее `O(D²)`. Это batch-алгоритм;
он не экономит map и не подходит для потока без полного map.

### 2.2. Count-Min sketch

`CountMinSketch` хранит `depth` строк по `width` счётчиков. `add(key)`
увеличивает по одному счётчику в каждой строке; `estimate(key)` берёт
минимум из `depth` значений. Коллизии могут только завысить оценку: счётчик,
в который попал ключ, увеличивают и другие ключи. Оценка никогда не бывает
ниже истинной частоты.

```php
    public function add(string $key, int $count = 1): void
    {
        if ($count < 1) {
            throw new InvalidArgumentException('Increment must be positive');
        }

        for ($row = 0; $row < $this->depth; ++$row) {
            $this->counters?->add('hashCalls');
            $this->counters?->add('probes');
            $index = Hashers::xxh128($row . ':' . $key) % $this->width;
            $this->table[$row][$index] += $count;
        }
    }

    public function estimate(string $key): int
    {
        $minimum = null;

        for ($row = 0; $row < $this->depth; ++$row) {
            $this->counters?->add('hashCalls');
            $this->counters?->add('probes');
            $index = Hashers::xxh128($row . ':' . $key) % $this->width;
            $value = $this->table[$row][$index];

            if ($minimum === null || $value < $minimum) {
                $this->counters?->add('comparisons');
                $minimum = $value;
            }
        }

        return $minimum ?? 0;
    }
```

[`src/Stream/CountMinSketch.php` L41-L72](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/Stream/CountMinSketch.php#L41-L72)

Count-Min не хранит ключи и не умеет их перечислять. Его нельзя использовать
как detector top-K: он может оценить частоту данного ключа, но не подскажет,
какие ключи вообще стоит оценивать. В эксперименте ему специально дают
истинные top-K ключи, чтобы измерить ошибку оценки.

### 2.3. Space-Saving и Misra-Gries

`SpaceSaving` хранит до `capacity` пар ключ-счётчик. Если ключ известен, счётчик
растёт. Если места нет, находится минимальный счётчик, удаляется, а новый
ключ получает `minimum + 1`. Это переоценка: новый ключ мог встретиться
меньше, но наследует минимум.

```php
    public function observe(string $key, int $count = 1): void
    {
        if ($count < 1) {
            throw new InvalidArgumentException('Increment must be positive');
        }

        if (isset($this->frequencies[$key])) {
            $this->frequencies[$key] += $count;

            return;
        }

        if (count($this->frequencies) < $this->capacity) {
            $this->frequencies[$key] = $count;

            return;
        }

        $minimumKey = null;
        $minimum = PHP_INT_MAX;

        foreach ($this->frequencies as $candidate => $value) {
            $this->counters?->add('comparisons');
            if ($value < $minimum || ($value === $minimum && ($minimumKey === null || $candidate < $minimumKey))) {
                $minimum = $value;
                $minimumKey = $candidate;
            }
        }

        if ($minimumKey === null) {
            throw new \LogicException('A full Space-Saving set must have a minimum key');
        }

        unset($this->frequencies[$minimumKey]);
        $this->frequencies[$key] = $minimum + $count;
    }
```

[`src/Stream/SpaceSaving.php` L33-L68](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/Stream/SpaceSaving.php#L33-L68)

`MisraGries` при переполнении уменьшает все счётчики на единицу и удаляет
нулевые; новый ключ на этом шаге не добавляется. Это недооценка, зато
счётчики никогда не превышают истинную частоту. Обе структуры дают
кандидатов, но их recall зависит от распределения и threshold `N/K`.

---

## 3. Эксперимент

Zipf-поток: 100 000 элементов, 10 000 возможных ключей, α = 1.1, seed
`book`; фактически встретилось 7 348 различных ключей.

```bash
make experiment ARGS="stream:topk --seed=book"
```

| K | distinct | heap update | heap memory | quickselect | quick memory | recall |
|---:|---:|---:|---:|---:|---:|---:|
| 10 | 7 348 | 22.910 ms | 344.0 KiB | 2.781 ms | 4.0 KiB | 100.0% |
| 100 | 7 348 | 23.259 ms | 338.2 KiB | 2.978 ms | 34.8 KiB | 100.0% |
| 1 000 | 7 348 | 31.691 ms | 506.0 KiB | 3.441 ms | 347.6 KiB | 100.0% |

Оба метода точные, поэтому recall 100%. Главное — память heap: она включает
map на все 7 348 ключей. При `K = 10` и `K = 100` значения близки, потому
что map доминирует; при `K = 1 000` добавляется около 170 KiB записей heap.
QuickSelect быстрее, но не включает стоимость построения map — в эксперименте
map уже готов.

Heavy hitters, capacity `K`:

| K | summary | entries | recall | precision | mean rank error |
|---:|---|---:|---:|---:|---:|
| 10 | space-saving | 10 | 50.0% | 50.0% | 2.40 |
| 10 | misra-gries | 8 | 30.0% | 37.5% | 3.10 |
| 100 | space-saving | 100 | 48.0% | 48.0% | 21.73 |
| 100 | misra-gries | 87 | 46.0% | 52.9% | 22.79 |
| 1 000 | space-saving | 1 000 | 55.9% | 55.9% | 198.01 |
| 1 000 | misra-gries | 989 | 56.4% | 57.0% | 200.77 |

Порог истинного top-K: 1 246 вхождений при `K = 10`, 96 при `K = 100` и 8
при `K = 1 000`. При `K = 10` порог — 1.25% потока, и recall 50% не
противоречит теории: summary capacity `K` не обязан сохранять всех
кандидатов в длинном хвосте с близкими частотами. Space-Saving
переоценивает, Misra-Gries недооценивает; поэтому precision у них может
отличаться от recall.

Оценка частот на истинных top-K ключах при равном бюджете `4K` счётчиков:

| K | estimator | units | mean error | max overestimate |
|---:|---|---:|---:|---:|
| 10 | count-min (w=10, d=4) | 40 cells | 5 554.60 | 7 574 |
| 10 | space-saving | 40 entries | 252.70 | 683 |
| 10 | misra-gries | 15 entries | 1 707.50 | 0 |
| 100 | count-min (w=100, d=4) | 400 cells | 353.77 | 920 |
| 100 | space-saving | 400 entries | 3.64 | 23 |
| 100 | misra-gries | 291 entries | 112.18 | 0 |
| 1 000 | count-min (w=1 000, d=4) | 4 000 cells | 14.52 | 72 |
| 1 000 | space-saving | 4 000 entries | 0.018 | 2 |
| 1 000 | misra-gries | 2 906 entries | 3.98 | 0 |

Равное число счётчиков — не равные байты: ячейка Count-Min — это integer в
строке, а запись summary владеет строкой ключа и счётчиком.

Поток только из уникальных ключей, 100 000 элементов, `K = 100`,
Count-Min width 2 048 / depth 4:

| structure | logical size | PHP memory | bytes per unit | sample |
|---|---:|---:|---:|---:|
| TopKHeap (exact) | 100 000 | 5 166.7 KiB | 52.91 B/item | 1 |
| CountMinSketch | 8 192 | 169.2 KiB | 21.15 B/cell | 36 |
| SpaceSaving | 100 | 18.8 KiB | 192.16 B/slot | 1 000 |
| MisraGries | 10 | 17.0 KiB | 173.60 B/slot | 1 |

Точный heap вырос до 5 166.7 KiB, хотя `K = 100`. Это и есть `O(D)`, а не
`O(K)`. Ограниченные структуры остались около своего размера. `sample` — не
рейтинг: у Space-Saving 1 000 — унаследованная переоценка, у Misra-Gries 1 —
недооценка.

Concept drift: 100 000 элементов из 200 ключей; первая половина — горячий
набор `phase-a` из 20 ключей, вторая — `phase-b` из 20 других; capacity 30.

| summary | entries | current-hot recall | stale phase-a keys in top | top count |
|---|---:|---:|---:|---:|
| space-saving | 30 | 100.0% | 10 | 3 820 |
| misra-gries | 30 | 100.0% | 10 | 1 194 |

Оба summary нашли текущий горячий набор, но сохранили и десять старых
ключей. Capacity — не окно и не decay: старые тяжёлые ключи не исчезают
сами.

Время, 50 000 элементов, 5 000 ключей:

| benchmark | iterations | median | ops/sec |
|---|---:|---:|---:|
| stream-topk-observe | 100 000 | 27.560 ms | 3 628 457 |
| stream-topk-batch | 20 | 58.371 ms | 342 |
| stream-quickselect-1000 | 100 | 268.911 ms | 371 |
| stream-count-min | 100 000 | 105.469 ms | 948 145 |
| stream-space-saving | 100 000 | 123.949 ms | 806 782 |
| stream-misra-gries | 100 000 | 11.892 ms | 8 409 338 |

Misra-Gries быстрее на этом потоке, потому что его набор может уменьшаться
после отмены, и он сканирует только при полном наборе и новом ключе. Это
свойство нагрузки, а не универсальный рейтинг.

---

## 4. Что ломается

**Точный top-K растёт с `D`.** 100 000 уникальных ключей при `K = 100` дали
5 166.7 KiB. В child-процессе с `memory_limit=8M` точный `TopKHeap` падает с
allowed-memory error, а Count-Min и Space-Saving завершаются. Это
исполняемая граница, а не лозунг.

**Count-Min не находит кандидатов.** Он оценивает частоту заданного ключа и
не умеет перечислять ключи. Использовать его как detector — значит
предполагать, что кандидаты уже откуда-то известны.

**Capacity `K` не гарантирует recall.** При `K = 10` Space-Saving нашёл 50%
истинного top-10, Misra-Gries — 30%. Это не ошибка кода: гарантии summary
обычно формулируются для частот выше `N/K`, а здесь порог — 1.25% потока, и
много близких частот.

**Равные счётчики — не равные байты.** 40 cells Count-Min и 40 entries
Space-Saving имеют разный allocator cost. Сравнивать нужно и ошибку, и
память, иначе вывод будет ложным.

**Drift.** Оба summary сохранили старые ключи. Для recency-sensitive задачи
нужно окно, decay или периодический сброс. Глава этого не строит и не
обещает.

**Поток материализуется.** Эксперименты replay-ят детерминированный список,
чтобы сравнить алгоритмы на одном входе. Production-пайплайн должен читать
generator и не держать полный map рядом с bounded summary.

---

## 5. Как это проверено

Тесты сравнивают `TopKHeap` с точной сортировкой map на случайных потоках,
проверяют ties по ключу, `K = 1`, `K > distinct`, повторные ключи и
уникальный поток. Для `QuickSelect` есть дифференциальный тест против
сортировки, включая границы и повторяющиеся частоты. Count-Min проверяется
на отсутствие недооценки; Space-Saving и Misra-Gries — на размер, сумму
счётчиков и поведение при переполнении.

`tests/Stream/BoundedMemoryTest.php` запускает child с `memory_limit=8M` и
150 000 уникальных ключей: точный heap падает, Count-Min и Space-Saving
завершаются. Это регрессия на bounded-memory контракт. `make check`
дополнительно запускает PHPStan и форматтер.

---

## 6. Что запомнить

- Точный top-K — это `O(D + K)` памяти; куча ограничена `K`, map — нет.
- QuickSelect — batch-альтернатива после построения map, а не потоковый
  алгоритм.
- Count-Min — оценщик, не detector; он не перечисляет ключи.
- Space-Saving и Misra-Gries дают кандидатов, но recall зависит от
  распределения и threshold.
- Capacity не равна recency: старые тяжёлые ключи переживают drift.
- Равное число счётчиков не означает равную память.

---

## 7. Упражнения

1. Реализуйте sliding-window top-K: храните два окна и вычитайте счётчики.
   Как изменится recall на drift-потоке?
2. Добавьте exponential decay к Space-Saving. Подберите коэффициент так,
   чтобы старые ключи уходили, а текущий горячий набор сохранялся.
3. Реализуйте mergeable Count-Min sketch для двух потоков. Как сложить
   таблицы и что произойдёт с ошибкой?
4. Откалибруйте Count-Min по `ε` и `δ`: выведите `width` и `depth` и
   проверьте ошибку на синтетическом потоке.
5. Повторите all-unique тест с `K = 10 000`. Где точный heap перестанет
   помещаться в 8M и почему?

---

## 8. Что дальше

Top-K ограничивает ответ, но не вход. Если вход не помещается в память,
нужен external sorting: читать чанками, сортировать, сбрасывать на диск и
сливать. Следующая глава показывает, как chunk и fan-in определяют число
проходов и почему 128 MiB чанка могут занять 291 MiB PHP-памяти.
