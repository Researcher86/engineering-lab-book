# Глава 7. Search Engine: inverted index, BM25 и шарды

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: webhook delivery platform (глава 6). Это последняя глава
> книги; дальше — интеграционный сценарий `make scenario`.

Поиск начинается не с перебора документов, а с индекса. Если на каждый запрос
просматривать весь корпус, стоимость растёт вместе с корпусом. Inverted index
меняет задачу: он хранит для каждого терма список документов, в которых этот
терм встречается. Запрос превращается в объединение posting-листов, а не в
чтение всех документов.

Но индекс — это не только ускорение. У него есть цена по памяти, стоимость
обновления, отдельная стоимость для частых термов и проблема шардирования.
Последняя особенно неприятна: шарды сохраняют общее число postings, но каждый
считает BM25 по своим локальным статистикам. Формально это те же документы и те
же термы, а ранжирование уже другое.

В лаборатории поиск — это in-process inverted index, BM25, ленивый префиксный
словарь и кластер из нескольких независимых индексов. Здесь нет stemming,
stop-word list, phrase search, обновлений, удалений, репликации, сети и segment
merging. Именно поэтому измерения можно объяснить до конца.

---

## 1. Проблема

Пусть есть 5 000 документов и запрос `topic0000`. Документ содержит заголовок и
тело; нужно вернуть десять лучших результатов. Полный перебор стоит
`O(документов × термов в запросе)`. Inverted index даёт список документов для
каждого терма, и стоимость запроса начинает зависеть от document frequency, а не
от размера корпуса.

У этой зависимости есть тёмная сторона. Терм, который встречается в 2 000
документах из 2 000, стоит 2 000 postings ещё до того, как начнётся
ранжирование. Редкий терм с document frequency 17 стоит 17 postings. Разница в
стоимости — два порядка, и её определяет не длина запроса, а распределение
термов в корпусе.

BM25 добавляет вторую сложность: score зависит от IDF, длины документа и средней
длины. IDF наказывает частые термы, length normalization наказывает длинные
документы. Это меняет порядок по сравнению с сырой частотой термина. Но те же
статистики делают score локальным: если каждый шард считает IDF по своему
document frequency и нормализует длину по своей средней, то score документа с
одного шарда не обязан быть сравним со score с другого.

Третья проблема — prefix completion. Autocomplete должен быстро вернуть термы,
начинающиеся с префикса. Для этого нужен отсортированный словарь и binary
search. Если словарь сортируется на каждой записи, запись становится дорогой.
Если сортировать лениво, первая выборка после записи платит `O(T log T)`, а
следующие — только `O(log T + результатов)`. Это компромисс между latency записи
и latency чтения.

---

## 2. Устройство

### 2.1. Analyzer

Tokenizer приводит текст к нижнему регистру и разбивает его по всему, что не
является буквой или цифрой. Это намеренно простой анализатор: без stemming, без
stop words и без позиций термов.

```php
    public function tokenize(string $text): array
    {
        $text = mb_strtolower($text, 'UTF-8');
        $parts = preg_split('/[^\p{L}\p{N}]+/u', $text, -1, PREG_SPLIT_NO_EMPTY);

        if ($parts === false) {
            throw new InvalidArgumentException('The tokenizer could not split the text');
        }

        return $parts;
    }
```

[`src/Search/Tokenizer.php` L21-L31](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/Tokenizer.php#L21-L31)

`link`, `Link` и `LINK` дадут один терм, а `link-42` — два: `link` и `42`.
Стемминга нет, поэтому `links` и `link` — разные термы. Это ограничение
лаборатории, а не свойство поиска вообще.

### 2.2. Inverted index

`InvertedIndex` хранит posting-и, длины документов и счётчик токенов. Сам
документ не хранится: поиск возвращает id и score, а за полями документа должен
ходить отдельный forward index.

```php
    public function addDocument(SearchDocument $document): void
    {
        if (isset($this->documentIds[$document->id])) {
            throw new InvalidArgumentException(sprintf('Document %s is already indexed', $document->id));
        }

        $tokens = $this->tokenizer->tokenize($document->title . ' ' . $document->body);
        $length = count($tokens);
        $this->documentIds[$document->id] = true;
        $this->lengths[$document->id] = $length;
        $this->tokenCount += $length;

        $frequencies = array_count_values($tokens);

        foreach ($frequencies as $term => $frequency) {
            $term = (string) $term;
            $this->prefix->add($term);

            if (!isset($this->postings[$term][$document->id])) {
                ++$this->postingPairs;
            }

            $this->postings[$term][$document->id] = $frequency;
        }
    }
```

[`src/Search/InvertedIndex.php` L41-L65](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/InvertedIndex.php#L41-L65)

Posting — это пара «терм → документ» с частотой термина в документе. Один
документ может добавить много postings, но каждый терм учитывается один раз.
Повторная вставка того же id отклоняется: обновления и удаления в лаборатории не
реализованы.

### 2.3. BM25

`Bm25Ranker` считает IDF по числу документов и document frequency, затем
нормализует частоту термина по длине документа. Параметры по умолчанию:
`k1 = 1.2`, `b = 0.75`.

```php
    public function rank(InvertedIndex $index, array $tokens, int $topK): RankedResults
    {
        if ($topK < 1) {
            throw new InvalidArgumentException('Top-k must be at least 1');
        }

        $tokens = array_values(array_unique($tokens));
        $documents = $index->documentCount();
        $averageLength = $index->averageDocumentLength();

        if ($tokens === [] || $documents === 0) {
            return new RankedResults([], 0, 0);
        }

        $scores = [];
        $postingsScanned = 0;

        foreach ($tokens as $term) {
            $postings = $index->postings($term);
            $documentFrequency = count($postings);

            if ($documentFrequency === 0) {
                continue;
            }

            $postingsScanned += $documentFrequency;
            $idf = log(1.0 + (($documents - $documentFrequency + 0.5) / ($documentFrequency + 0.5)));

            foreach ($postings as $docId => $frequency) {
                $docId = (string) $docId;
                $length = $index->documentLength($docId);
                $lengthRatio = $averageLength > 0.0 ? $length / $averageLength : 0.0;
                $denominator = $frequency + $this->k1 * (1.0 - $this->b + $this->b * $lengthRatio);
                $score = $idf * ($frequency * ($this->k1 + 1.0)) / $denominator;
                $scores[$docId] = ($scores[$docId] ?? 0.0) + $score;
            }
        }

        $docIds = array_keys($scores);
        usort($docIds, static function (int|string $left, int|string $right) use ($scores): int {
            $left = (string) $left;
            $right = (string) $right;
            $comparison = $scores[$right] <=> $scores[$left];

            return $comparison !== 0 ? $comparison : strcmp($left, $right);
        });
        $hits = [];

        foreach (array_slice($docIds, 0, $topK) as $docId) {
            $hits[] = new SearchHit((string) $docId, $scores[$docId]);
        }

        return new RankedResults($hits, $postingsScanned, count($scores));
    }
```

[`src/Search/Bm25Ranker.php` L35-L88](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/Bm25Ranker.php#L35-L88)

`postingsScanned` считает документы, просмотренные в posting-листах, а
`candidatesScored` — число различных документов, получивших score. Для одного
терма это одно и то же; для смешанного запроса кандидаты объединяются, поэтому
`candidates` может быть меньше суммы postings.

### 2.4. Prefix completion

`PrefixIndex` хранит термы в hash set. Отсортированный список строится лениво:
первый запрос после записи сортирует словарь, последующие только ищут нижнюю
границу префикса бинарным поиском.

```php
    public function complete(string $prefix, int $limit = 10): array
    {
        if ($limit < 1) {
            throw new InvalidArgumentException('Completion limit must be at least 1');
        }

        $prefix = mb_strtolower($prefix, 'UTF-8');
        $terms = $this->sortedTerms();
        $start = $this->lowerBound($terms, $prefix);
        $result = [];

        for ($index = $start; $index < count($terms) && count($result) < $limit; ++$index) {
            $term = $terms[$index];

            if (!str_starts_with($term, $prefix)) {
                break;
            }

            $result[] = $term;
        }

        return $result;
    }
```

[`src/Search/PrefixIndex.php` L46-L68](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/PrefixIndex.php#L46-L68)

Сортировка происходит только при первой необходимости. Любая новая запись
сбрасывает `sorted` в `null`, поэтому следующий запрос снова платит за
пересортировку.

```php
    private function sortedTerms(): array
    {
        if ($this->sorted === null) {
            $terms = array_map(
                static fn (int|string $term): string => (string) $term,
                array_keys($this->terms),
            );
            sort($terms, SORT_STRING);
            $this->sorted = $terms;
            ++$this->rebuilds;
        }

        return $this->sorted;
    }
```

[`src/Search/PrefixIndex.php` L105-L118](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/PrefixIndex.php#L105-L118)

`countWithPrefix()` устроен иначе: он вытягивает весь диапазон совпадений, а не
только `limit`. Это `O(matches)`, а не путь autocomplete. В книге это важно не
путать: completion с лимитом дешёвый, точный подсчёт — нет.

### 2.5. Маршрутизация документа

`SearchCluster` выбирает шард по хэшу document id. Документ принадлежит ровно
одному шарду; репликации нет.

```php
    public function addDocument(SearchDocument $document): void
    {
        $this->shards[$this->shardFor($document->id)]->addDocument($document);
        $this->counters->add('documentsIndexed');
    }
```

[`src/Search/SearchCluster.php` L56-L60](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/SearchCluster.php#L56-L60)

Хэш id, а не терма, означает, что все термы одного документа лежат на одном
шарде. Это упрощает запись, но не даёт локальности по термам: запрос всё равно
идёт во все шарды.

### 2.6. Fan-out и merge

Запрос токенизируется один раз. Каждый шард считает свой top-k по локальным
статистикам. Кластер сливает списки по score и document id и обрезает результат
до `topK`.

```php
    public function search(string $query, int $topK = 10): SearchResponse
    {
        if ($topK < 1) {
            throw new InvalidArgumentException('Top-k must be at least 1');
        }

        $tokens = $this->tokenizer->tokenize($query);
        $this->counters->add('queries');

        if ($tokens === []) {
            $this->counters->add('emptyQueries');

            return new SearchResponse($query, [], 0, 0, 0);
        }

        $hits = [];
        $postingsScanned = 0;
        $candidatesScored = 0;

        foreach ($this->shards as $shard) {
            $ranked = $this->ranker->rank($shard, $tokens, $topK);
            $postingsScanned += $ranked->postingsScanned;
            $candidatesScored += $ranked->candidatesScored;

            foreach ($ranked->hits as $hit) {
                $hits[] = $hit;
            }
        }

        usort($hits, static function (SearchHit $left, SearchHit $right): int {
            $comparison = $right->score <=> $left->score;

            return $comparison !== 0 ? $comparison : strcmp($left->docId, $right->docId);
        });
        $results = array_slice($hits, 0, $topK);
        $this->counters->add('shardsQueried', $this->shardCount);
        $this->counters->add('postingsScanned', $postingsScanned);
        $this->counters->add('candidatesScored', $candidatesScored);
        $this->counters->add('resultsReturned', count($results));

        return new SearchResponse(
            query: $query,
            results: $results,
            shardsQueried: $this->shardCount,
            postingsScanned: $postingsScanned,
            candidatesScored: $candidatesScored,
        );
    }
```

[`src/Search/SearchCluster.php` L70-L117](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/SearchCluster.php#L70-L117)

Взятие top-k с каждого шарда достаточно для глобального top-k только при
сравнимых score. Если score локальные, документ, не попавший в локальный top-k,
может заслуживать места в глобальном. Лаборатория это не скрывает: эксперимент с
шардами измеряет, насколько результат уходит от глобального индекса.

### 2.7. Prefix fan-out

Префиксный запрос идёт во все шарды. Каждый шард возвращает свой диапазон,
кластер объединяет термы, убирает дубликаты и сортирует результат.

```php
    private function prefixTerms(string $prefix, int $perShardLimit): array
    {
        $matches = [];

        foreach ($this->shards as $shard) {
            foreach ($shard->complete($prefix, $perShardLimit) as $term) {
                $matches[$term] = true;
            }
        }

        $terms = array_keys($matches);
        sort($terms, SORT_STRING);

        return $terms;
    }
```

[`src/Search/SearchCluster.php` L223-L237](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Search/SearchCluster.php#L223-L237)

Для обычного completion каждый шард получает тот же `limit`, поэтому глобальные
первые `limit` термов не теряются. Для точного подсчёта кластер передаёт
`PHP_INT_MAX` и вытягивает все совпадения; это уже не autocomplete-путь.

---

## 3. Эксперимент

### 3.1. Размер индекса

`search:index` индексирует 5 000 документов со словарём 20 000 термов и примерно
80 body-термов на документ.

```bash
make experiment ARGS="search:index"
```

| метрика                        |  значение |
| ------------------------------ | --------: |
| документов                     |     5 000 |
| токенов                        |   412 539 |
| токенов на документ            |      82.5 |
| postings (term-document pairs) |   274 611 |
| postings на документ           |      54.9 |
| термов в словаре               |    22 528 |
| в среднем postings на терм     |      12.2 |
| входной текст                  |   3.9 MiB |
| PHP-память: корпус             |   8.1 MiB |
| PHP-память: индекс             |  25.1 MiB |
| PHP-память: всего              |  33.2 MiB |
| PHP-память: индекс на документ | 5 273.8 B |

Пять самых частых термов:

| терм      | документов |   доля |
| --------- | ---------: | -----: |
| link      |      5 000 | 100.0% |
| word00001 |      4 998 | 100.0% |
| word00002 |      4 948 |  99.0% |
| word00003 |      4 756 |  95.1% |
| word00004 |      4 497 |  89.9% |

`link` есть в каждом документе, поэтому его posting-лист содержит 5 000 записей.
Это не «плохой» терм, а нормальная часть корпуса: чем чаще терм, тем дороже
любой запрос с ним. Память измерена как PHP allocator bytes, а не RSS; индексный
delta считается поверх живого корпуса.

### 3.2. Стоимость запроса и ранжирование

`search:ranking` строит индекс на 2 000 документов и выполняет по 200 запросов
трёх форм: редкий topic-терм, частый словарный терм и оба терма вместе.

```bash
make experiment ARGS="search:ranking"
```

| форма          | postings | кандидаты | возвращено | кандидатов в top-10 | TF overlap | b=0 overlap |
| -------------- | -------: | --------: | ---------: | ------------------: | ---------: | ----------: |
| редкий topic   |     20.3 |      20.3 |       10.0 |               49.2% |      52.2% |       52.2% |
| частый терм    |    772.1 |     772.1 |       10.0 |                1.3% |      49.1% |       49.1% |
| topic + частый |    792.4 |     784.4 |       10.0 |                1.3% |      59.7% |       64.9% |

Разница в стоимости огромна: редкий терм просматривает около 20 postings, частый
— около 772. В top-10 из 772 кандидатов выживает 1.3%; это нормально для
широкого запроса, но объясняет, почему broad term дорог ещё до сортировки.

В примере `topic0000` имел document frequency 17, `word00001` — 2 000, а
смешанный запрос дал объединение кандидатов размера 2 000. `TF overlap` и
`b=0 overlap` сравнивают BM25 с ранжированием по сырой частоте и с BM25 без
length normalization. Для одного терма порядок совпадает, потому что оба
варианта сортируют по частоте. Для смешанного запроса IDF меняет вклад термов,
поэтому overlap падает до 59.7% и 64.9%. Это разница ранжирования, а не оценка
релевантности: в синтетическом корпусе один topic-сигнал.

### 3.3. Prefix completion

`search:prefix` строит один индекс и кластер из четырёх шардов на 2 000
документов и завершает префикс `word00`.

```bash
make experiment ARGS="search:prefix"
```

| метрика                           | один индекс |   четыре шарда |
| --------------------------------- | ----------: | -------------: |
| записей в словаре                 |      14 864 | 26 953 (сумма) |
| различных термов с префиксом      |         999 |            999 |
| rebuild словаря на первом запросе |           1 |              4 |
| rebuild словаря на втором запросе |           0 |              0 |
| результатов completion            |          10 |             10 |

Оба варианта вернули одни и те же десять термов. Сумма словарей шардов больше,
потому что терм, лежащий на нескольких шардах, считается несколько раз. После
записи первый запрос пересобирает словарь: один раз для одного индекса и четыре
раза для четырёх шардов. Второй запрос уже только бинарно ищет диапазон.

### 3.4. Шарды и деградация ранжирования

`search:shards` индексирует одни и те же 2 000 документов глобально и по шардам,
затем выполняет по 100 редких и 100 смешанных запросов на каждую конфигурацию.
Один шард — контроль: он должен совпасть с глобальным индексом.

```bash
make experiment ARGS="search:shards"
```

| шардов | документов на шард | postings/запрос | кандидатов/запрос | запросов к шардам |
| -----: | -----------------: | --------------: | ----------------: | ----------------: |
|      1 |      2 000 / 2 000 |           384.0 |             380.3 |                 1 |
|      2 |        991 / 1 009 |           384.0 |             380.3 |                 2 |
|      4 |          481 / 513 |           384.0 |             380.3 |                 4 |
|      8 |          236 / 273 |           384.0 |             380.3 |                 8 |

Fan-out не изменил число просмотренных postings: каждый шард просматривает свою
долю, а сумма равна глобальному числу. Изменилось число запросов к шардам и
ранжирование.

| шардов | rare recall@10 | mixed recall@10 | rare top-1 | mixed top-1 |
| -----: | -------------: | --------------: | ---------: | ----------: |
|      1 |         100.0% |          100.0% |     100.0% |      100.0% |
|      2 |          79.5% |           84.0% |      43.0% |       67.0% |
|      4 |          67.4% |           75.2% |      22.0% |       44.0% |
|      8 |          64.8% |           69.3% |      18.0% |       29.0% |

Один шард совпал с глобальным индексом — это контроль. С ростом числа шардов
recall@10 падает: каждый шард считает IDF по своему числу документов,
нормализует длину по своей средней и сортирует свой локальный набор. Merge
сравнивает эти score без нормализации, поэтому глобальный порядок меняется.

Это не ошибка merge: локальные top-k действительно лучшие внутри шардов. Но
«лучший внутри шарда» не означает «сравнимый с лучшим на другом шарде».
Production-кластеру нужны глобальные статистики, нормализация score или
двухфазный retrieval. Лаборатория измеряет проблему, а не прячет её.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="search-engine --repetitions=3"
```

| операция                | итерации | медиана, мс |   ops/sec |
| ----------------------- | -------: | ----------: | --------: |
| tokenize                |  100 000 |      46.538 | 2 148 762 |
| index document          |    2 000 |       3.936 |   508 109 |
| bm25 rare term          |   20 000 |     193.888 |   103 152 |
| bm25 common term        |    2 000 |   3 955.336 |       506 |
| bm25 mixed terms        |    2 000 |   4 001.695 |       500 |
| prefix complete         |  100 000 |     147.132 |   679 663 |
| cluster search 4 rare   |   10 000 |     143.548 |    69 663 |
| cluster search 4 common |    1 000 |   1 607.033 |       622 |
| cluster index document  |    2 000 |       4.796 |   416 996 |

Common-term запрос примерно в 20 раз медленнее rare-term: он просматривает почти
весь индекс, а не около двадцати postings. Mixed-запрос близок к common, потому
что частый терм доминирует в стоимости.

Сравнивать cluster и single-index нужно на одну операцию, а не на размер батча.
В этом прогоне cluster rare стоит около 14.4 мкс против 9.7 мкс у single-index
rare: fan-out и merge добавляют примерно 1.5x. Cluster common, наоборот, около
1.61 мс против 1.98 мс: каждый шард сортирует меньший локальный набор, а
postings те же. Это не ускорение поиска вообще, а следствие того, что сортировка
разбита на части.

Index document и cluster index document растут во время прогона, поэтому их
memory delta относится к бенчмарку, а не к фиксированному размеру индекса. Числа
описывают конкретную машину, PHP 8.5.11, OPcache CLI off, JIT off и Zend MM.

---

## 5. Что ломается

**Broad term дорог до ранжирования.** Document frequency 772 означает 772
postings, которые нужно прочитать и оценить. Никакая оптимизация сортировки не
уменьшит эту работу. Нужны early termination, block-max WAND или аналогичные
стратегии, которых в лаборатории нет.

**BM25 зависит от статистик.** IDF, средняя длина и document frequency — это
свойства корпуса. На шарде они локальные. Одинаковый документ получает разный
score в зависимости от того, на каком шарде он лежит. Merge без нормализации
даёт drift, который эксперимент измеряет.

**Top-k с шарда — не глобальный top-k.** Если score сравнимы, этого достаточно.
Если нет, документ вне локального top-k может быть лучшим глобально. Нужны
глобальные статистики, нормализация или двухфазный retrieval.

**Prefix dictionary инвалидируется записью.** Первый запрос после записи платит
`O(T log T)`. При потоке записей словарь может пересобираться постоянно. Нужны
immutable segments, инкрементальные структуры или debounce, которых здесь нет.

**Сумма словарей шардов не равна глобальному словарю.** Терм, лежащий на
нескольких шардах, считается несколько раз. Это нормально для fan-out, но нельзя
использовать как точное число уникальных термов.

**Нет stemming и stop words.** `links` и `link` — разные термы; частые служебные
слова не удаляются. Это меняет и словарь, и postings, и ranking. Лаборатория
этого не моделирует.

**Нет phrase search и позиций.** Posting хранит частоту, но не позиции. Запрос
из нескольких термов — это сумма score, а не проверка порядка слов. Фразовый
поиск потребовал бы позиционных списков.

**Нет обновлений и удалений.** Повторный id отклоняется; удалить документ
нельзя. В реальном движке это tombstones, segments и merge, которые меняют
стоимость и конкурентность.

**Нет репликации и сети.** Шарды — это объекты в одном PHP-процессе. Падение
процесса теряет весь индекс. Нет сетевых задержек, retry, partial failure и
распределённого консенсуса. Бенчмарк измеряет CPU-стоимость, а не кластер.

**Нет segment merging и сжатия.** Индекс целиком живёт в PHP-массивах. Память
PHP allocator не равна RSS и не учитывает overhead production-хранилища. On-disk
index, compression и stored fields остаются за рамками.

**Synthetic relevance.** Корпус содержит один topic-сигнал и Zipf-тело. Overlap
и recall — это сравнение ранжирований, а не оценка качества поиска. Настоящая
релевантность требует размеченных данных и метрик вроде nDCG.

---

## 6. Как это проверено

- `tests/Search/TokenizerTest.php` проверяет регистр, Unicode, пунктуацию, цифры
  и отсутствие stemming.
- `tests/Search/InvertedIndexTest.php` проверяет postings, длины, среднюю длину,
  повторный id и пустой документ.
- `tests/Search/Bm25RankerTest.php` фиксирует формулу, влияние частоты и длины,
  tie-break по id, top-k и обработку неизвестных термов.
- `tests/Search/PrefixIndexTest.php` проверяет сортировку, лимит, регистр,
  дедупликацию, ленивый rebuild и точный подсчёт диапазона.
- `tests/Search/SearchClusterTest.php` проверяет маршрутизацию по id, fan-out,
  merge, префиксы, сумму словарей и границы.
- `tests/Search/CorpusTest.php` проверяет детерминированность корпуса.
- Эксперименты `search:index`, `search:ranking`, `search:prefix` и
  `search:shards` дают независимые счётчики для памяти, стоимости и качества
  ранжирования.

К моменту завершения главы набор содержал 385 тестов; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` включает PHPUnit, PHPStan level 8 и
PHP-CS-Fixer.

---

## 7. Что запомнить

1. Inverted index меняет стоимость запроса: она зависит от document frequency, а
   не от размера корпуса.
2. Частый терм дорог до ранжирования. Broad term — это стоимость postings, а не
   стоимость сортировки.
3. BM25 использует IDF и длину документа, поэтому score зависит от статистик
   корпуса.
4. Prefix dictionary сортируется лениво: запись инвалидирует список, первый
   запрос после записи платит за rebuild.
5. Шардирование сохраняет общее число postings, но умножает число запросов к
   шардам.
6. Локальные BM25-статистики делают score несравнимыми; merge без нормализации
   меняет глобальный ranking.
7. Лаборатория не реализует stemming, stop words, phrase search, обновления,
   удаления, репликацию, сеть и segment merging.

---

## 8. Упражнения

1. Запустите `search:index` с `--docs=10000`. Как изменятся память на документ и
   число postings? Останется ли средняя длина прежней?
2. Запустите `search:ranking` с `--top-k=50`. Как изменится доля кандидатов,
   попадающих в top-k, для частого терма?
3. Сравните `search:prefix` с `--shards=1` и `--shards=8`. Во сколько раз
   вырастет число rebuild на первом запросе? Совпадут ли completion-результаты?
4. Запустите `search:shards` с `--shards=1`. Почему recall@10 должен быть 100%?
   Что это доказывает про остальные конфигурации?
5. Добавьте глобальные document frequency в `Bm25Ranker` для шардов. Какие
   счётчики изменятся и какое rank drift останется?
6. Предложите удаление документа без полной перестройки индекса. Какие структуры
   и компромиссы понадобятся?

---

На этом главы заканчиваются. `make scenario` собирает rate limiter, short links,
cache, search, notifications, webhooks и queue в один детерминированный поток и
проверяет 35 инвариантов на реальном прогоне. Книга описывает те же компоненты,
которые этот сценарий использует.
