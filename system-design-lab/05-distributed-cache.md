# Глава 5. Distributed Cache: согласованное хэширование и цена отказа

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: job queue (глава 4). Следующая глава: webhook delivery
> platform (глава 6).

Кэш кажется простым: положить значение по ключу и забрать его обратно. Пока кэш
один, этого достаточно. Как только узлов несколько, появляются три вопроса,
которые уже нельзя решить одной структурой данных. На каком узле живёт ключ? Что
происходит с отображением, когда узел добавляется или исчезает? Что видит
клиент, когда узел падает?

В лаборатории distributed cache — это стратегия размещения, несколько
независимых LRU-хранилищ и кластер, который читает primary, затем реплики и
чинит primary при replica hit. Здесь нет сети, TTL, инвалидации и фонового
rebalancing. Есть только то, что можно измерить: перемещения ключей, hit ratio,
поведение при отказе и цена репликации.

---

## 1. Проблема

Пусть есть восемь узлов и ключ `link-42`. Нужно детерминированно выбрать узел,
который будет отвечать за ключ. Самое простое — взять хэш ключа по модулю числа
узлов. Пока узлов восемь, отображение стабильно и равномерно. Когда узлов
становится девять, меняется делитель у каждого ключа: почти все ключи получают
нового владельца. Если за этим стоит реальный кэш, почти всё содержимое
превращается в промахи.

Consistent hashing решает именно эту проблему: при изменении состава двигается
только доля ключей, принадлежавшая изменившемуся участку кольца. В идеале для
одного нового узла это `1/(N+1)`. Rendezvous hashing даёт похожую границу без
кольца, но платит линейным перебором узлов на каждый ключ.

Вторая проблема — перекос. Равномерное число ключей на узле ещё не означает
равномерную нагрузку. Если запросы распределены по Zipf, горячие ключи могут
собрать на одном узле заметно больше трафика, чем на остальных. Ёмкость и
latency кластера определяет самый занятый узел, а не средний.

Третья проблема — отказ. Кэш — не база данных: узел может потерять всё
содержимое, и это нормальное поведение, а не катастрофа. Вопрос в том, сколько
промахов это добавит и что успеют сделать реплики. Репликация повышает
доступность, но при фиксированном объёме памяти отнимает ёмкость у уникальных
ключей. Поэтому hit ratio и устойчивость к отказу — это один компромисс, а не
два независимых улучшения.

---

## 2. Устройство

### 2.1. Контракт размещения

Все стратегии реализуют один интерфейс. `locate()` возвращает узлы в порядке
предпочтения: первый — primary, остальные — реплики. Именно этот контракт
позволяет сравнивать modulo, ring и rendezvous на одном кластере.

```php
interface HashStrategy
{
    /**
     * @return list<string> At most $replicas distinct node names.
     */
    public function locate(string $key, int $replicas = 1): array;

    /**
     * @return list<string>
     */
    public function nodes(): array;

    public function addNode(string $node): void;

    public function removeNode(string $node): void;
}
```

[`src/DistributedCache/HashStrategy.php` L14-L29](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/HashStrategy.php#L14-L29)

### 2.2. Modulo как базовая линия

Modulo-стратегия — это `hash64(key) % count(nodes)`. Она дешева и для
равномерного потока ключей даёт хороший баланс. Её слабое место проявляется не в
отдельном запросе, а при изменении числа узлов.

```php
    public function locate(string $key, int $replicas = 1): array
    {
        if ($replicas < 1) {
            throw new InvalidArgumentException('Replica count must be at least 1');
        }

        $count = count($this->nodes);

        if ($count === 0) {
            return [];
        }

        $start = Hashers::hash64($key) % $count;
        $result = [];

        for ($offset = 0; $offset < $count && count($result) < $replicas; ++$offset) {
            $result[] = $this->nodes[($start + $offset) % $count];
        }

        return $result;
    }
```

[`src/DistributedCache/ModuloHash.php` L48-L68](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/ModuloHash.php#L48-L68)

Реплики здесь — следующие узлы по кругу. Это простое соглашение, но оно не
делает modulo устойчивым к изменению состава: при переходе от восьми узлов к
девяти меняется сам делитель, поэтому ожидаемо переезжает `8/9` ключей.

### 2.3. Кольцо и виртуальные узлы

`HashRing` размещает каждый физический узел в нескольких точках 60-битного
пространства. По умолчанию это 150 виртуальных узлов. Токен — это
`hash64(node#index)`; коллизия разрешается линейным пробингом следующей позиции.
Больше виртуальных узлов означает более ровные дуги, но больше памяти и чуть
более дорогой поиск.

```php
    public function addNode(string $node): void
    {
        $node = trim($node);

        if ($node === '') {
            throw new InvalidArgumentException('Node name cannot be empty');
        }

        if (isset($this->tokens[$node])) {
            throw new InvalidArgumentException(sprintf('Node %s already exists', $node));
        }

        $tokens = [];

        for ($index = 0; $index < $this->virtualNodes; ++$index) {
            $position = Hashers::hash64($node . '#' . $index);

            while (isset($this->owners[$position])) {
                $position = ($position + 1) % self::SPACE;
            }

            $this->owners[$position] = $node;
            $this->positions[] = $position;
            $tokens[] = $position;
        }

        sort($this->positions);
        $this->tokens[$node] = $tokens;
    }
```

[`src/DistributedCache/HashRing.php` L43-L71](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/HashRing.php#L43-L71)

Поиск идёт бинарным поиском по отсортированным позициям: находится первый токен
не меньше хэша ключа, затем обход продолжается по часовой стрелке. Обход
пропускает повторных владельцев, пока не наберётся нужное число различных узлов.

```php
    public function locate(string $key, int $replicas = 1): array
    {
        if ($replicas < 1) {
            throw new InvalidArgumentException('Replica count must be at least 1');
        }

        $count = count($this->positions);

        if ($count === 0) {
            return [];
        }

        $hash = Hashers::hash64($key);
        $low = 0;
        $high = $count;

        while ($low < $high) {
            $middle = intdiv($low + $high, 2);

            if ($this->positions[$middle] < $hash) {
                $low = $middle + 1;
            } else {
                $high = $middle;
            }
        }

        $start = $low % $count;
        $result = [];
        $seen = [];

        for ($offset = 0; $offset < $count && count($result) < $replicas; ++$offset) {
            $position = $this->positions[($start + $offset) % $count];
            $node = $this->owners[$position];

            if (isset($seen[$node])) {
                continue;
            }

            $seen[$node] = true;
            $result[] = $node;
        }

        return $result;
    }
```

[`src/DistributedCache/HashRing.php` L90-L133](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/HashRing.php#L90-L133)

Кольцо двигает только ключи, чей ближайший токен сменился. Это и есть обещанная
граница `1/(N+1)` для одного нового узла. Важно, что здесь двигается только
отображение: данные на новый узел никто не копирует, поэтому его первые чтения
всё равно будут промахами.

### 2.4. Rendezvous как альтернатива

Rendezvous hashing не строит кольцо. Каждый узел получает оценку
`hash64(key + "\0" + node)`, и побеждают `replicas` старших оценок. При
добавлении узла меняются только те ключи, для которых новый узел оказался лучше,
поэтому ожидаемая доля перемещений снова около `1/(N+1)`.

```php
    public function locate(string $key, int $replicas = 1): array
    {
        if ($replicas < 1) {
            throw new InvalidArgumentException('Replica count must be at least 1');
        }

        /** @var list<array{node: string, score: int}> $best */
        $best = [];

        foreach ($this->nodes as $node) {
            $score = Hashers::hash64($key . "\0" . $node);
            $position = count($best);

            while ($position > 0 && $best[$position - 1]['score'] < $score) {
                --$position;
            }

            if ($position >= $replicas) {
                continue;
            }

            array_splice($best, $position, 0, [['node' => $node, 'score' => $score]]);

            if (count($best) > $replicas) {
                array_pop($best);
            }
        }

        return array_map(static fn (array $entry): string => $entry['node'], $best);
    }
```

[`src/DistributedCache/RendezvousHash.php` L49-L78](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/RendezvousHash.php#L49-L78)

Цена — `O(nodes)` на каждый поиск вместо `O(log V)` у кольца. В маленьком
кластере это незаметно, в большом — становится доминирующей стоимостью
размещения.

### 2.5. LRU на каждом узле

Узел хранит ограниченное число записей и вытесняет least recently used. Здесь
нет глобального LRU: у каждого узла свой порядок recency. Это принципиально,
потому что узлы не общаются и не знают, какие ключи горячие на соседях.

```php
    public function get(string $key): mixed
    {
        if (!array_key_exists($key, $this->entries)) {
            $this->counters->add('lruMisses');

            return null;
        }

        $value = $this->entries[$key];
        unset($this->entries[$key]);
        $this->entries[$key] = $value;
        $this->counters->add('lruHits');

        return $value;
    }
```

[`src/DistributedCache/LruStore.php` L39-L53](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/LruStore.php#L39-L53)

При попадании ключ переставляется в конец массива. Первый ключ массива — least
recently used; именно он удаляется при переполнении.

```php
    public function put(string $key, mixed $value): void
    {
        if ($value === null) {
            throw new InvalidArgumentException('Null is not a cacheable value');
        }

        if (array_key_exists($key, $this->entries)) {
            unset($this->entries[$key]);
            $this->counters->add('replacements');
        }

        $this->entries[$key] = $value;
        $this->counters->add('writes');

        while (count($this->entries) > $this->capacity) {
            foreach ($this->entries as $oldest => $value) {
                unset($this->entries[$oldest]);
                $this->counters->add('evictions');

                break;
            }
        }
    }
```

[`src/DistributedCache/LruStore.php` L55-L77](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/LruStore.php#L55-L77)

Операции с массивом дают ожидаемое `O(1)` на попадание и вставку. Null не
считается кэшируемым значением, поэтому `get()` может использовать null как
однозначный признак промаха.

### 2.6. Чтение из кластера и read repair

`CacheCluster` сначала идёт к primary. Если primary недоступен, он пропускается.
Если ключ найден на реплике, кластер возвращает replica hit и, когда primary
жив, записывает значение обратно в primary. Это и есть best-effort read repair.

```php
    public function get(string $key): CacheLookup
    {
        $this->counters->add('reads');
        $targets = $this->placement->locate($key, $this->replicationFactor);
        $primary = $targets[0] ?? null;

        foreach ($targets as $index => $node) {
            if (isset($this->down[$node])) {
                $this->counters->add('skippedDownNodes');

                continue;
            }

            $store = $this->stores[$node];

            if (!$store->has($key)) {
                continue;
            }

            $value = $store->get($key);

            if ($index === 0) {
                $this->counters->add('primaryHits');

                return CacheLookup::hit(CacheSource::Primary, $node, $value);
            }

            $this->counters->add('replicaHits');

            if ($primary !== null && !isset($this->down[$primary])) {
                $this->stores[$primary]->put($key, $value);
                $this->counters->add('readRepairs');
            }

            return CacheLookup::hit(CacheSource::Replica, $node, $value);
        }

        $this->counters->add('misses');

        return CacheLookup::miss();
    }
```

[`src/DistributedCache/CacheCluster.php` L67-L107](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/CacheCluster.php#L67-L107)

Запись идёт во все доступные целевые узлы. Если primary недоступен, но записана
реплика, кластер считает запись degraded: копий меньше, чем replication factor.
Если недоступны все целевые узлы, запись не происходит и инкрементируется
`writeFailures`. Отдельного фонового восстановления нет.

### 2.7. Отказ и восстановление

По умолчанию отказ узла означает потерю данных. Это не ошибка реализации, а
модель кэша: узел мог перезагрузиться с пустой памятью. Восстановление снимает
флаг недоступности, но не возвращает записи.

```php
    /**
     * @return int The number of entries lost with the node.
     */
    public function failNode(string $node, bool $loseData = true): int
    {
        $this->assertNode($node);

        if (isset($this->down[$node])) {
            return 0;
        }

        $this->down[$node] = true;
        $this->counters->add('nodeFailures');
        $lost = 0;

        if ($loseData) {
            $lost = $this->stores[$node]->clear();
            $this->counters->add('lostEntries', $lost);
            $this->counters->add('dataLossFailures');
        }

        return $lost;
    }

    public function recoverNode(string $node): void
    {
        $this->assertNode($node);

        if (!isset($this->down[$node])) {
            return;
        }

        unset($this->down[$node]);
        $this->counters->add('nodeRecoveries');
    }
```

[`src/DistributedCache/CacheCluster.php` L149-L183](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/DistributedCache/CacheCluster.php#L149-L183)

При RF=1 ключи с упавшего узла становятся промахами до тех пор, пока их снова не
положат в кэш. При RF=2 их может отдать реплика. Но это работает только для
ключей, у которых реплика действительно жива и не вытеснила значение. Кэш не
даёт гарантии, что копия есть всегда.

---

## 3. Эксперимент

### 3.1. Перемещение ключей

`cache:remap` размещает 20 000 ключей на восьми узлах, добавляет девятый и
считает, у скольких ключей изменился primary.

```bash
make experiment ARGS="cache:remap"
```

| стратегия        | перемещено |  доля | ожидание |
| ---------------- | ---------: | ----: | -------: |
| modulo           |     17 810 | 89.0% |    88.9% |
| ring, 150 vnodes |      2 141 | 10.7% |    11.1% |
| rendezvous       |      2 190 | 10.9% |    11.1% |

Modulo переставил почти всё: изменился делитель, а вместе с ним и владелец почти
каждого ключа. Кольцо и rendezvous уложились в ожидаемые `1/9`. Разница между
ring и rendezvous в этом прогоне — 49 ключей, то есть статистический шум для
одного изменения состава, а не преимущество одной стратегии.

### 3.2. Hit ratio при фиксированной памяти

`cache:hit-ratio` подаёт 100 000 запросов по Zipf на 20 000 ключей при общем
объёме 5 000 записей. Все варианты стартуют холодными и видят один и тот же
поток.

```bash
make experiment ARGS="cache:hit-ratio"
```

| режим      | узлов |  RF | на узел | попадания | промахи | hit ratio | evictions |
| ---------- | ----: | --: | ------: | --------: | ------: | --------: | --------: |
| single LRU |     1 |   1 |   5 000 |    79 529 |  20 471 |     79.5% |    15 471 |
| sharded    |     4 |   1 |   1 250 |    79 463 |  20 537 |     79.5% |    15 537 |
| sharded 2x |     8 |   1 |     625 |    79 416 |  20 584 |     79.4% |    15 584 |
| replicated |     4 |   2 |   1 250 |    73 394 |  26 606 |     73.4% |    48 397 |

При равномерном размещении и одинаковом суммарном объёме разбиение одного LRU на
четыре или восемь частей почти не изменило aggregate hit ratio: каждый шард
получил свою долю ключей и свою долю памяти. Это результат конкретного
эксперимента, а не закон: при сильном перекосе или неравных шардах маленький
шард может вытеснять горячие ключи, которые большой LRU удержал бы.

Репликация при той же памяти заметно другая. Каждая запись занимает две копии,
поэтому уникальных ключей помещается меньше. Hit ratio падает с 79.5% до 73.4%,
а evictions растут с 15 537 до 48 397. Это цена, которую эксперимент с отказом
должен окупить.

### 3.3. Отказ узла

`cache:failure` прогревает два кластера на одном потоке, сбрасывает счётчики,
измеряет фазу до отказа, валит `cache-0` с потерей данных, измеряет фазу отказа,
восстанавливает узел и измеряет фазу после. В каждой фазе 20 000 запросов; до
неё было 50 000 прогревающих запросов. Узлов четыре, по 1 250 записей.

```bash
make experiment ARGS="cache:failure"
```

|  RF | фаза                 | попадания | промахи | hit ratio | primary | replica | read repair |
| --: | -------------------- | --------: | ------: | --------: | ------: | ------: | ----------: |
|   1 | до отказа            |    16 196 |   3 804 |     81.0% |  16 196 |       0 |           0 |
|   1 | во время отказа      |    11 042 |   8 958 |     55.2% |  11 042 |       0 |           0 |
|   1 | после восстановления |    15 791 |   4 209 |     79.0% |  15 791 |       0 |           0 |
|   2 | до отказа            |    14 799 |   5 201 |     74.0% |  14 771 |      28 |          28 |
|   2 | во время отказа      |    14 709 |   5 291 |     73.5% |   9 930 |   4 779 |          26 |
|   2 | после восстановления |    14 772 |   5 228 |     73.9% |  14 558 |     214 |         214 |

Оба кластера потеряли по 1 250 записей — всё, что было на `cache-0`. При RF=1
hit ratio упал на 25.8 процентного пункта: ключи с упавшего узла больше некому
отдать. При RF=2 падение составило около 0.5 пункта: реплики обслужили 4 779
чтений. После восстановления 214 чтений нашли пустой primary и починили его из
реплики.

Небольшое число replica hits до отказа — 28 — объясняется независимыми порядками
LRU на узлах: primary мог вытеснить ключ, который реплика ещё держала. Read
repair — это стратегия повторного наполнения, а не гарантия согласованности.
Значение на реплике может быть старее, а восстановление реплики из primary
вообще не выполняется.

### 3.4. Баланс ключей и трафика

`cache:balance` отправляет 100 000 Zipf-запросов на восемь узлов и отдельно
считает, как распределены 20 000 ключей и как распределён трафик.

```bash
make experiment ARGS="cache:balance"
```

| стратегия        | самый занятый | max доля ключей | max доля трафика | min доля трафика | CV трафика |
| ---------------- | ------------- | --------------: | ---------------: | ---------------: | ---------: |
| modulo           | cache-2       |           12.8% |            20.3% |             8.7% |      0.269 |
| ring, 1 vnode    | cache-1       |           25.1% |            27.7% |             0.0% |      0.630 |
| ring, 150 vnodes | cache-0       |           13.7% |            19.2% |             8.7% |      0.272 |
| rendezvous       | cache-3       |           12.8% |            20.1% |             8.6% |      0.303 |

Один токен на узел оставил размеры дуг на волю случая: один узел получил 25.1%
ключей, а другой не получил ни одного и обслуживал 0% трафика. Виртуальные узлы
сгладили дуги. Но даже при ровном числе ключей Zipf собрал около 19–20% трафика
на самом занятом узле. Планировать ёмкость по количеству ключей недостаточно:
горячие ключи концентрируют запросы.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="distributed-cache --repetitions=3"
```

| операция          | итерации | медиана, мс |   ops/sec |
| ----------------- | -------: | ----------: | --------: |
| modulo locate     |  100 000 |      39.951 | 2 503 051 |
| ring locate       |  100 000 |      76.653 | 1 304 581 |
| ring locate rf3   |  100 000 |      89.946 | 1 111 784 |
| rendezvous locate |  100 000 |     592.274 |   168 841 |
| lru get hit       |  100 000 |      19.292 | 5 183 574 |
| lru put evict     |   20 000 |      56.745 |   352 452 |
| cluster get rf1   |  100 000 |     134.343 |   744 365 |
| cluster put rf2   |   20 000 |      28.535 |   700 894 |

Ring в этом прогоне примерно в 1.9 раза медленнее modulo на 100 000 поисков, а
rendezvous — примерно в 7.7 раза медленнее ring. Причина видна из кода: ring
делает бинарный поиск по 3 000 позициям виртуальных узлов, rendezvous перебирает
все узлы для каждого ключа.

Cluster get добавляет к LRU-попаданию вызов размещения, поиск узла и обновление
счётчиков, поэтому 134.343 мс на 100 000 чтений заметно больше 19.292 мс
локального попадания. Cluster put с RF=2 пишет две копии; его 28.535 мс на 20
000 операций — это стоимость двух записей и счётчиков, а не одной.

Числа описывают конкретную машину, PHP 8.5.11, OPcache CLI off, JIT off и Zend
MM. Это относительные ориентиры, а не универсальные константы.

---

## 5. Что ломается

**Consistent hashing двигает отображение, а не данные.** Новый узел получает
свою долю ключей, но его хранилище пустое. Первые чтения — промахи, и кто-то
должен загрузить значения из источника. Кольцо сокращает перестановку ключей, но
не делает добавление узла бесплатным.

**Modulo-лавина.** При изменении числа узлов меняется делитель у всех ключей.
Эксперимент показывает 89.0% перемещений против 10.7% у кольца. Это не
«небольшая неоптимальность», а почти полный сброс кэша.

**Перекос трафика.** Равномерное число ключей не означает равномерную нагрузку.
Zipf-поток оставил на самом занятом узле 19–20% запросов при 12.8–13.7% ключей.
Ёмкость, connections и latency нужно планировать по трафику.

**Репликация стоит памяти.** При фиксированном бюджете вторая копия уменьшает
число уникальных ключей. В эксперименте hit ratio упал с 79.5% до 73.4%, а
evictions выросли более чем в три раза. Репликация покупает устойчивость к
отказу, а не бесплатную надёжность.

**RF не гарантирует чтение.** Реплика может быть недоступна, вытеснить ключ или
быть пустой после собственного восстановления. При отказе двух узлов картина
зависит от того, какие копии уцелели. Лаборатория не обещает, что replication
factor означает «ключ всегда найдётся».

**Read repair — не консистентность.** Кластер чинит primary из реплики, но не
чинит реплику из primary, не хранит версии и не разрешает конфликты. Реплика
может остаться старой или пустой. Это repopulation strategy, а не модель
согласованности.

**Per-node LRU — не глобальный LRU.** У каждого узла свой порядок вытеснения.
Ключ, который глобально горячий, может быть вытеснен на одном узле и остаться на
другом. Отсюда replica hits без отказа: копии расходятся по recency.

**Кэш — не источник истины.** В лаборатории нет TTL, инвалидации, negative
caching, stampede protection и request coalescing. Промах — нормальное событие,
но одновременные промахи по горячему ключу могут всей толпой уйти в источник.
Этот слой нужно проектировать отдельно.

**Один процесс.** Нет сети, сериализации, реальной конкурентности и сетевых
задержек. Бенчмарк измеряет CPU-стоимость структур, а не latency кластера.
Память PHP-массивов тоже не равна памяти production-кэша.

---

## 6. Как это проверено

- `tests/DistributedCache/HashRingTest.php` проверяет стабильность размещения,
  различные реплики, границы, добавление и удаление узлов, а также то, что
  виртуальные узлы размазывают кольцо.
- `tests/DistributedCache/ModuloHashTest.php` фиксирует базовую линию и
  показывает, что добавление узла перемещает почти все ключи.
- `tests/DistributedCache/RendezvousHashTest.php` проверяет независимость от
  порядка узлов, различные реплики и долю перемещений около `1/(N+1)`.
- `tests/DistributedCache/LruStoreTest.php` покрывает попадания, промахи,
  вытеснение, перезапись, `clear()` и capacity.
- `tests/DistributedCache/CacheClusterTest.php` проверяет primary/replica hits,
  отказ, восстановление, read repair, degraded writes и отказ всех целей.
- Эксперименты `cache:remap`, `cache:hit-ratio`, `cache:failure` и
  `cache:balance` дают независимые счётчики для каждой гарантии.

К моменту завершения главы набор содержал 273 теста; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` включает PHPUnit, PHPStan level 8 и
PHP-CS-Fixer.

---

## 7. Что запомнить

1. Consistent hashing ограничивает перемещение ключей примерно `1/(N+1)`, но
   новый узел всё равно холодный.
2. Ring ищет за `O(log V)`, rendezvous — за `O(nodes)`. Виртуальные узлы — это
   компромисс между памятью и равномерностью дуг.
3. Ровное число ключей не означает ровный трафик. Zipf-популярность может
   перегрузить один узел.
4. Репликация стоит ёмкости и hit ratio, но покупает доступность при отказе.
5. Упавший кэш-узел теряет данные. Восстановление начинается с пустого
   хранилища.
6. Read repair — это повторное наполнение, а не гарантия согласованности.
7. Кэш не источник истины. Промахи, stampede и инвалидация остаются задачами
   системы вокруг кэша.

---

## 8. Упражнения

1. Запустите `cache:remap` для перехода с четырёх узлов на пять. Сравните долю
   перемещений с `1/5` и объясните расхождение.
2. Запустите `cache:balance` с `--vnodes=1` и `--vnodes=150`. Как меняются
   максимальная доля трафика и CV? Почему один узел может получить 0%?
3. Увеличьте общий объём в `cache:hit-ratio` до 10 000 записей. Сократится ли
   разрыв между RF=1 и RF=2? Почему?
4. Повторите `cache:failure`, вызвав `failNode()` с `loseData=false`. Что
   произойдёт с hit ratio и почему это не модель реального падения?
5. Свалите два узла при RF=2. Какие ключи всё ещё доступны, а какие стали
   промахами? Сформулируйте границу гарантии.
6. Добавьте TTL или request coalescing. Какие счётчики понадобятся, чтобы
   доказать эффект, не прибегая к wall-clock time?

---

Дальше — Webhook Delivery Platform. Кэш показал, что данные можно потерять и
восстановить; следующая глава показывает, как доставить внешнее событие, когда
получатель отвечает медленно, повторяет ответ и иногда исчезает.
