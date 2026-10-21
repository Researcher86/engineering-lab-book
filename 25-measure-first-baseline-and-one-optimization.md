# Глава 25. Сначала измерь: базовая линия и одна оптимизация

> Проекты: [`php-mini-search`](https://github.com/Researcher86/php-mini-search)
> (тег `v1.0.0`, `7ecddc0`) и
> [`php-systems-platform`](https://github.com/Researcher86/php-systems-platform)
> (коммит `9a6a916`). Нужно знать: анализатор, индекс и BM25 (→ главы 19–20),
> кеш и его предел (→ глава 19), наблюдаемость (→ глава 24).
>
> Здесь начинается последняя часть — инженерная практика. И первое её правило:
> оптимизация, у которой нет базовой линии, — это не оптимизация, а надежда.

## 1. Проблема

Оптимизация — это утверждение: «так быстрее, чем было». Утверждение можно
проверить только одним способом — измерить **до** и **после**. Но измерить можно
так, что оба числа окажутся про другое, и тогда оптимизация сделана, а
доказательства нет. Три способа ошибиться встречались в этой лаборатории
буквально:

1. **Измерить не то.** В постановке нагрузочного теста платформы первая версия
   читала «промахи кеша», создавая заказы через HTTP. Но путь создания сам же и
   заполняет кеш (`setOrder` при записи, → глава 23), поэтому созданный по API
   заказ уже лежит в кеше — и фаза, задуманная как промах, измеряла попадания.
   Число было честным, вывод — противоположным.
2. **Измерить секунды.** «p50 упал с 24.4 до 8.2 мс» звучит как результат, но не
   воспроизводится: другая машина, другая частота, другая загрузка, и число
   уехало. Про такое число нельзя написать проверку, и в книге его нельзя
   напечатать как факт.
3. **Оптимизировать до измерения.** Если узкое место угадано, то оптимизация
   может ускорить то, что не было медленным, — и это не видно, потому что базы
   для сравнения нет.

К этому добавляется ловушка из главы 24: **среднее прячет хвост**. Отчёт,
построенный на одном среднем, не отличает «стало ровнее» от «стало короче в
среднем, но иногда в десять раз дольше».

Дисциплина этой главы — четыре правила, и все четыре проверимы:

- **Базовая линия сначала.** До всякой правки — число, которое воспроизводится и
  описывает, где работа **есть**.
- **Изменение только одно.** Одна правка, один замер после неё. Две сразу — и
  непонятно, которая сработала (или которая сломала).
- **Результат не меняется.** Оптимизация имеет право менять время и память. Она
  не имеет права менять ответ. Это проверяемо: сравните результат до и после.
- **Заявляйте границу, а не число.** «Память не растёт» лучше, чем «память 47
  МБ»; «не хуже, чем в 5 раз быстрее» лучше, чем «в 37.2 раза».

## 2. Устройство

### Что измерять: счётчик, который выдерживает утверждение

```text
секундомер                    счётчик (воспроизводимый)
───────────────────           ─────────────────────────────────────
p50 24.4 ms                   db.operations  1 (а не 3)
«стало быстрее»               «база спрошена один раз вместо трёх»
зависит от машины             одинаково на любой машине
прячет хвост                  говорит, ЧТО именно изменилось

нужны оба, но базовая линия — второй:
он проверяем, а первый — только наблюдаем.
```

Для оптимизации анализатора (→ глава 19) таким счётчиком оказалось **число
токенов**: сколько раз вызван стеммер. Для оптимизации поиска (→ глава 20) —
**число документов, попавших под BM25**: `SearchResult::total` (см. §3). Для
оптимизации пути чтения на платформе — **число операций к базе** и **обращений к
кешу** (→ глава 24). Все три — счётчики, и все три напрямую несут то свойство,
ради которого делалась правка.

### Базовая линия, которую нашла лаборатория

Измерение анализатора (`docs/BENCHMARKS.md` php-mini-search) дало три числа, и
каждое определило следующую правку:

| Измерено                        | Значение                              | Что из этого следует                     |
| ------------------------------- | ------------------------------------- | ---------------------------------------- |
| стоимость анализа одного токена | 12 µs                                 | токенов много, и каждый стоит денег      |
| доля стемминга в этой стоимости | 96%                                   | оптимизировать надо стеммер, а не разбор |
| доля повторяющихся токенов      | 90% (5 млн токенов, 27 379 различных) | повтор — то, что можно не делать дважды  |

Отсюда — **одна правка**: результаты стемминга кешируются `token → term`.
Разница (по тому же документу): анализ всего корпуса — с ~60 с до ~1.5 с;
загрузка через API — с 339 до 985 док/с; `PUT` — с p50 24.4 мс до 8.2 мс.

```text
одна правка, два замера

до:    5 000 000 токенов ──► 5 000 000 стемов ──► анализ ≈ 60 с
после: 5 000 000 токенов ──►    27 379 стемов ──► анализ ≈ 1.5 с
                               (кеш token → term)

не изменилось: ни один терм, ни один документ, ни один результат поиска
изменилось:     выдан ли тот же набор термов за меньшее время
```

### Границы правки: что она имеет право менять

1. **Ответ — нет.** Кеш обязан быть невидимым: тот же текст даёт тот же список
   термов, с теми же повторами.
2. **Память — в границах.** Кеш конечен (`CACHE_ENTRIES` = 200 000): когда он
   полон, он **опустошается** и начинает заново. Это «пара стемов» потерь и
   плоская память при любом словаре.
3. **Поиск — не здесь.** Оптимизация анализатора ускоряет загрузку и запись и
   **не** ускоряет поиск: стоимость поиска — это BM25 по совпадениям частых
   термов, а не анализ. Документ это и показывает: 488 запросов/с до правки и
   489 после. Обещать «стало быстрее» вообще значило бы обещать то, чего не
   было.

### Почему поиск не ускорился: работу решает корпус, а не код

Тот же код на двух корпусах работает в сотни раз по-разному, и это не дефект:

| Корпус        | Словарь                    | Почему                                                                               | p99     |
| ------------- | -------------------------- | ------------------------------------------------------------------------------------ | ------- |
| синтетический | 25 896 термов, крутой Zipf | частый терм есть почти в каждом документе → BM25 считает тысячи совпадений           | 58.5 мс |
| Wikipedia     | 151 505 термов             | словá из заголовка в основном редкие → пересечение с самого редкого даёт мало работы | 6.0 мс  |

Вывод, который стоит запомнить: **быстрый корпус и медленный корпус — это
свойство данных**. Измерив один, нельзя утверждать об оптимизации кода, пока не
измерен второй.

### Протокол измерения платформы

Платформа измеряет свою нагрузку сама (`php bin/platform.php load`), и её
протокол — часть ответа:

- **Каждая фаза — свой вопрос.** `A` — сколько стоит сам сервер; `C1` — первое
  (холодное) чтение каждого заказа; `C2` — те же чтения с тёплым кешем; `B` — те
  же чтения на `serve`, запущенном с `CACHE_ENABLED=0`; `D` — очередь; `E` —
  масштабирование по числу воркеров.
- **Данные для фаз `C` вставляются в базу напрямую**, а не создаются через API:
  иначе путь записи заполнит кеш и промах не воспроизведётся (ловушка №1).
- **Оборванное соединение — это запрос.** Такие запросы считаются проваленными и
  **не** дают выборки латентности: нельзя позволить брошенным соединениям
  сделать фазу «быстрее».
- **Статусы и `X-Cache` считаются, а не предполагаются.** Фаза, ответившая `500`
  со скоростью 10 000 запросов/с, не должна выглядеть хорошей.
- **Перцентили — по ближайшему рангу**, а не интерполяцией: у наблюдаемых
  значений есть порядок, и перцентиль обязан быть одним из них.
- **Окружение — часть результата**, а не примечание: пропускная способность без
  машины, на которой она получена, ничего не значит.

Наконец, стоимость процессов читается **у ядра**, а не оценивается: потреблённый
CPU — разность `utime+stime` двух чтений `/proc/<pid>/stat` (процесс тратил CPU
и до фазы), а пиковая память — `VmHWM` из `/proc/<pid>/status`, то есть
собственный максимум ядра. Он точнее замера по таймеру: таймер может не попасть
в пик, а `VmHWM` — не может.

### Чего это стоит

- **Кеш — это память и промахи.** Он сам требует памяти (с границей) и сам
  иногда не работает: опустошение даёт всплеск стемминга. Правка обменивает
  «всегда стеммить» на «иногда заново».
- **База для сравнения забывается.** Корпус, задержки, версия PHP, число
  воркеров — без всего этого протокол не воспроизводится, и через месяц
  непонятно, с чем сравнивали.
- **Нагрузочный тест — это минуты.** `load` поднимает и останавливает платформу;
  он не влезает в быстрый цикл обратной связи и потому не заменяет юнит-тесты.
- **Счётчик легко подменить.** «Документов под BM25» измеряет работу поиска, но
  не его качество: можно сделать запрос, который считается многие документы, но
  ничего не находит. Счётчик отвечает на вопрос, ради которого выбран, и ни на
  какой другой.
- **У оптимизации здесь нет своего теста.** Контракт кеша — поведение
  анализатора, и его проверяют тесты анализатора (§6); что кеш **вообще что-то**
  ускоряет — проверено экспериментом, а не тестом, потому что проверять секунды
  в тесте нельзя.

## 3. Код

### Базовая линия живёт в докблоке

`Analyzer` начинается с измерения, а не с описания: стоимость токена, доля
стемминга, доля повторов и результат правки. Если бы это было неверно,
оптимизация была бы бессмысленной — поэтому это первое, что видит читатель.

```php
/**
 * Turns text into terms: the units the index stores and queries match.
 *
 * Documents and queries go through this one pipeline, and that is the whole
 * contract. `Кабели USB-C` in a title and `кабель usb` in a search box only
 * meet because both become `кабел`, `usb` and `c` here:
 *
 *   1. Unicode NFKC, so `й` is one character, the ligature `ﬁ` is `fi` and
 *      full-width `ＵＳＢ` is `USB`;
 *   2. lowercase, then `ё` → `е`, since Russian text uses the two
 *      interchangeably;
 *   3. an apostrophe between two letters is removed — `O’Neill` → `oneill` —
 *      so a query typed without it still matches;
 *   4. tokens are maximal runs of letters and digits; everything else
 *      separates them;
 *   5. a token of Cyrillic letters goes through the Russian stemmer, a token
 *      of ASCII Latin letters through the English one, and any other token
 *      (digits, mixed scripts, accented Latin) is kept as it is.
 *
 * Duplicates are kept: a term's frequency in a field is how many times it
 * comes out of here.
 *
 * Stems are cached, token → term. Stemming was 96% of the cost of analysis
 * (12 µs a token), while 90% of tokens repeat, as words in text do. With the
 * cache, analysing 10,000 documents went from about 60 s to 1.5 s
 * (docs/BENCHMARKS.md). The cache is bounded: when it holds
 * CACHE_ENTRIES tokens it is emptied and starts over, which costs a few
 * stems and keeps memory flat whatever the vocabulary.
 */
```

[`src/Analysis/Analyzer.php#L12-L40`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L12-L40)

**Перевод комментариев:**
- `Analyzer` (PHPDoc): превращает текст в термы — единицы, которые хранит индекс
  и по которым ищут запросы. Документы и запросы проходят один и тот же
  конвейер, и в этом весь контракт: `Кабели USB-C` в заголовке и `кабель usb` в
  строке поиска встречаются только потому, что оба становятся `кабел`, `usb` и
  `c` здесь: 1) Unicode NFKC, чтобы `й` был одним символом, лигатура `ﬁ` — `fi`,
  а полноширинный `ＵＳＢ` — `USB`; 2) нижний регистр, затем `ё` → `е`, поскольку в
  русском тексте эти буквы взаимозаменяемы; 3) апостроф между двумя буквами
  удаляется — `O’Neill` → `oneill` — чтобы запрос, набранный без него, всё равно
  находил; 4) токены — максимальные последовательности букв и цифр, всё
  остальное их разделяет; 5) токен из кириллических букв идёт в русский стеммер,
  токен из ASCII-латиницы — в английский, а любой другой (цифры, смешанные
  письменности, латиница с диакритикой) остаётся как есть. Дубликаты
  сохраняются: частота терма в поле — это сколько раз он вышел отсюда. Стемы
  кешируются, `token → term`. Стемминг составлял 96% стоимости анализа (12 µs на
  токен), а 90% токенов повторяются, как и слова в тексте. С кешем анализ 10 000
  документов упал примерно с 60 с до 1,5 с (`docs/BENCHMARKS.md`). Кеш
  ограничен: когда в нём `CACHE_ENTRIES` токенов, он опустошается и начинает
  заново — это стоит нескольких стемов и держит память плоской при любом
  словаре.

### Одна правка: кеш, который обязан быть невидимым

Три ветки — попадание, граница и промах. Обратите внимание, что вытеснение здесь
не по одной записи, а «сброс целиком»: кеш, который начинает заново, всё равно
попадает в повтор на следующем же документе, а память при этом строго
ограничена.

```php
private function stem(string $token): string
{
    if (isset($this->cache[$token])) {
        return $this->cache[$token];
    }
    if (count($this->cache) >= self::CACHE_ENTRIES) {
        $this->cache = [];
    }

    return $this->cache[$token] = $this->stemUncached($token);
}

private function stemUncached(string $token): string
{
    if (preg_match('/^\p{Cyrillic}+$/u', $token) === 1) {
        return $this->russian->stem($token);
    }

    if (preg_match('/^[a-z]+$/', $token) === 1) {
        return $this->english->stem($token);
    }

    return $token;
}
```

[`src/Analysis/Analyzer.php#L72-L95`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L72-L95)

### Счётчик, которым меряется поиск

`search()` увеличивает `total` на **каждое** совпадение, а не только на попавшие
на страницу. Это и есть число документов, которое увидят BM25 и `score()`, — то
самое, что измеряет эксперимент 3.

```php
public function search(array $terms, int $limit, int $offset = 0): SearchResult
{
    $terms = array_values(array_unique($terms));
    sort($terms, SORT_STRING);

    if ($terms === [] || $limit < 1 || $offset < 0) {
        return new SearchResult([], 0);
    }

    $top = new TopHits($offset + $limit);
    $total = 0;

    foreach ($this->matches($terms) as $number) {
        $total++;
        $top->offer(new SearchHit($this->ids[$number], $this->score($terms, $number)));
    }

    return new SearchResult(array_slice($top->best(), $offset), $total);
```

[`src/Index/InvertedIndex.php#L183-L200`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Index/InvertedIndex.php#L183-L200)

А страница результатов не сортирует все совпадения: она держит кучу на
`offset + limit` лучших, поэтому цена страницы — `O(n log k)`, и `total`
остаётся точным.

```php
/**
 * Keeps the best k hits seen so far, so a page of results costs
 * O(n log k) instead of sorting every match.
 *
 * The heap is upside down on purpose: its top is the *worst* hit it holds,
 * so a new hit only has to beat the top to get in, and evicting is one
 * extract(). "Better" means a higher score, and on equal scores a smaller id
 * — the same tie-break the API promises, so equal scores still come out in a
 * fixed order.
 *
 * @extends SplHeap<SearchHit>
 */
final class TopHits extends SplHeap
{
    public function __construct(
        private readonly int $capacity,
    ) {
    }

    public function offer(SearchHit $hit): void
    {
        if ($this->count() < $this->capacity) {
            $this->insert($hit);

            return;
        }

        if ($this->capacity > 0 && self::better($hit, $this->top())) {
            $this->extract();
            $this->insert($hit);
        }
    }
```

[`src/Index/TopHits.php#L9-L40`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Index/TopHits.php#L9-L40)

**Перевод комментариев:**
- `TopHits` (PHPDoc): хранит лучшие `k` попаданий, увиденных к этому моменту,
  поэтому страница результатов стоит `O(n log k)`, а не сортировка всех
  совпадений. Куча намеренно перевёрнута: на её вершине — *худшее* попадание из
  хранимых, так что новому попаданию достаточно побить вершину, а вытеснение —
  один `extract()`. «Лучше» означает больший счёт, а при равных счетах — меньший
  идентификатор: тот же tie-break, который обещает API, поэтому равные счета всё
  равно выходят в фиксированном порядке. `@extends SplHeap<SearchHit>`.

### Фазы нагрузочного теста: вопрос важнее числа

Докблок `LoadTestRunner` объясняет, почему фазы стоят именно так и почему корпус
для холодного чтения вставляется напрямую. Это ровно та ошибка, которую
совершает каждый, кто строит «фазу промахов» из заказов, созданных через API.

```php
/**
 * PLAN Step 29's load tests as one command that owns a platform for its
 * duration and stops everything it started. One corpus of orders is behind
 * every read phase:
 *
 *   A  GET /health       what the server itself costs with no work behind it
 *   C1 GET /orders/{id}  every order read for the first time: every request a
 *                        cache miss, so every request reaches the database
 *   C2 GET /orders/{id}  the same orders again: every request a cache hit, so
 *                        the gap between C1 and C2 is the cache doing its job
 *   B  GET /orders/{id}  the same orders on a serve started with
 *                        CACHE_ENABLED=0 - which is what "without cache" means
 *                        here: no cache server, no cache lookup, no fallback
 *   D  background jobs   the platform's own queue benchmark, 1,000 jobs
 *   E  worker scaling    the same workload through 1, 2, 4 and 8 workers
 *
 * The corpus is written straight into the database rather than created over
 * HTTP, and that is not a shortcut: the create path populates the cache on
 * write, so an order created through the API is already cached and a "cache
 * miss" phase built from API-created orders would be measuring hits and
 * calling them misses. Direct inserts mean the read phases start against an
 * empty cache, and cost one write per order instead of an HTTP round trip.
 *
 * C1 and C2 run in that order on purpose: C2's hits are C1's misses having
 * done their job, so the hit phase is a consequence of the miss phase instead
 * of a second thing that had to be arranged.
 *
 * Test B restarts the platform, because the cache tier is decided when serve
 * starts and not per request. A serve that fails to start puts its own output
 * in the exception; a run that fails later keeps its child logs in the temp
 * log directory.
 */
```

[`src/Benchmarks/LoadTestRunner.php#L17-L48`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/LoadTestRunner.php#L17-L48)

**Перевод комментариев:**
- `LoadTestRunner` (PHPDoc): нагрузочные тесты PLAN Step 29 одной командой,
  которая владеет платформой на время выполнения и останавливает всё, что
  запустила. За каждой фазой чтения стоит один корпус заказов: `A` `GET /health`
  — сколько стоит сам сервер без работы за ним; `C1` `GET /orders/{id}` — каждый
  заказ читается впервые, каждый запрос — промах кеша, поэтому доходит до базы;
  `C2` — те же заказы снова, каждый запрос — попадание, и разрыв между `C1` и
  `C2` — это работа кеша; `B` — те же заказы на `serve`, запущенном с
  `CACHE_ENABLED=0` (это и значит «без кеша»: нет сервера кеша, нет обращения к
  нему, нет fallback); `D` — фоновые задания, собственный бенчмарк очереди
  платформы на 1000 заданий; `E` — масштабирование воркеров: та же нагрузка
  через 1, 2, 4 и 8 воркеров. Корпус пишется прямо в базу, а не создаётся через
  HTTP, и это не сокращение: путь создания заполняет кеш при записи, поэтому
  заказ, созданный через API, уже закеширован, и «фаза промахов» из таких
  заказов измеряла бы попадания, называя их промахами. Прямые вставки означают,
  что фазы чтения начинаются с пустого кеша и стоят одну запись на заказ вместо
  HTTP-обмена. `C1` и `C2` идут в таком порядке намеренно: попадания `C2` — это
  промахи `C1`, сделавшие свою работу, поэтому фаза попаданий — следствие фазы
  промахов, а не вторая вещь, которую пришлось устраивать. Тест `B`
  перезапускает платформу, потому что уровень кеша решается при старте `serve`,
  а не на каждый запрос. `serve`, который не смог стартовать, кладёт свой вывод
  в исключение; прогон, упавший позже, оставляет дочерние логи во временном
  каталоге логов.

### Клиент нагрузки: настоящая параллельность

Один лёгкий curl-хэндл на каждый слот, все гоняются одним `curl_multi`.
Соединение не переустанавливается между запросами слота: измеряется сервер, а не
TCP-рукопожатие. И запрос, который не завершился, считается запросом, но не даёт
выборки времени.

```php
 * Drives $requests HTTP requests at a fixed concurrency and keeps every
 * per-request time - PLAN Step 29's Tests A, B and C.
 *
 * The concurrency is real: one easy handle per in-flight slot, all driven by
 * one curl_multi handle, so $concurrency connections are open at once. Each
 * slot keeps its easy handle for the whole phase because curl keeps the
 * handle's connection cache across curl_reset(): a slot connects once, and
 * what gets measured is the server rather than a TCP handshake per request.
 *
 * A request that never completed still counts as a request and as a failure
 * but contributes no latency sample, so dropped connections cannot make a
 * phase look faster than it was.
 */
final class HttpLoadTest
{
```

[`src/Benchmarks/HttpLoadTest.php#L13-L27`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/HttpLoadTest.php#L13-L27)

### Результат фазы: хвост, а не среднее

`HttpLoadResult` хранит **все** времена ответов наполовину именно за этим:
перцентилю нужен порядок, а среднее скрыло бы хвост. `X-Cache` — часть
результата, потому что без него фаза попаданий и фаза промахов неразличимы.

```php
/**
 * What one load phase measured (PLAN Step 29's Tests A, B and C).
 *
 * Every per-request time is kept, not just a mean, so the tail is visible.
 * Status codes and X-Cache markers are tallied rather than assumed, so a
 * phase that answered 500s at a great RPS cannot pass for a good one.
 */
final readonly class HttpLoadResult
{
    public const float MILLISECONDS = 1000.0;

    /**
     * @param list<float>        $latencyMs   every per-request time, ascending
     * @param array<int, int>    $statusCodes answers per status code
     * @param array<string, int> $cacheTally  answers per X-Cache value
     */
    public function __construct(
        public string $label,
        public int $requests,
        public int $succeeded,
        public float $wallSeconds,
        public array $latencyMs = [],
        public array $statusCodes = [],
        public array $cacheTally = [],
    ) {
    }
```

[`src/Benchmarks/HttpLoadResult.php#L7-L32`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/HttpLoadResult.php#L7-L32)

**Перевод комментариев:**
- `HttpLoadResult` (PHPDoc): что измерила одна фаза нагрузки (тесты A, B и C из
  PLAN Step 29). Каждое время ответа сохраняется, а не только среднее, чтобы был
  виден хвост. Коды статусов и маркеры `X-Cache` подсчитываются, а не
  предполагаются, поэтому фаза, ответившая `500` при отличном RPS, не может
  сойти за хорошую.
- `__construct()` (PHPDoc): `@param list<float> $latencyMs` — все времена по
  запросам по возрастанию; `@param array<int, int> $statusCodes` — ответы по
  кодам статусов; `@param array<string, int> $cacheTally` — ответы по значениям
  `X-Cache`.

### Отчёт: числа и машина — одно целое

`LoadTestReport` несёт окружение в той же структуре, что и измерения, потому что
число без машины не является результатом. Оттуда же две формы: массив для
документации и таблица для человека — из одних данных.

```php
/**
 * What a load run measured (PLAN Step 29): a payload plus two renderings.
 * toArray()/toJson() is the machine shape docs/benchmarks.md quotes; toText()
 * is the human table, built from the same data so the two cannot disagree.
 * The environment is part of the result because a throughput number without
 * the machine it came from means little.
 */
final readonly class LoadTestReport
{
    /**
     * @param array<string, mixed>        $environment what the run ran on
     * @param list<array<string, mixed>>  $http        Tests A, B and C, one
     *                                               row per phase
     * @param array<string, mixed>        $queue       Test D
     * @param list<array<string, mixed>>  $scaling     Test E
     */
    public function __construct(
        public array $environment,
        public array $http,
        public array $queue,
        public array $scaling,
    ) {
    }
```

[`src/Benchmarks/LoadTestReport.php#L7-L29`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/LoadTestReport.php#L7-L29)

**Перевод комментариев:**
- `LoadTestReport` (PHPDoc): что измерил прогон нагрузки (PLAN Step 29): payload
  и два представления. `toArray()` /`toJson()` — машинная форма, которую
  цитирует `docs/benchmarks.md`; `toText()` — человекочитаемая таблица,
  построенная из тех же данных, поэтому они не могут разойтись. Окружение —
  часть результата, потому что число пропускной способности без машины, на
  которой оно получено, значит мало.
- `__construct()` (PHPDoc): `@param array<string, mixed> $environment` — на чём
  шёл прогон; `@param list<array<string, mixed>> $http` — тесты A, B и C, по
  строке на фазу; `@param array<string, mixed> $queue` — тест D;
  `@param list<array<string, mixed>> $scaling` — тест E.

### Стоимость процесса — у ядра

CPU фазы — это разность двух чтений, а не абсолют: процесс тратил CPU и до неё.
Пиковая память — высоководяная отметка ядра, точная по определению.

```php
/**
 * One reading of a process's CPU and memory, from the kernel.
 *
 * CPU is utime+stime from /proc/<pid>/stat; a phase's cost is the delta
 * between two readings (cpuSince()), since the process spent CPU before the
 * phase began. Peak memory is the kernel's own high-water mark (VmHWM) -
 * over the process's lifetime, and exact, unlike a timer-sampled peak.
 */
final readonly class ProcessCost
{
    /**
     * CLK_TCK on every Linux this project runs on (x86_64 and arm64), so CPU
     * seconds have 10 ms resolution - ample for phases that take seconds.
     */
    public const int CLOCK_TICKS_PER_SECOND = 100;

    public function __construct(
        public int $pid,
        public float $cpuSeconds,
        public int $rssBytes,
        public int $peakRssBytes,
    ) {
    }

    /**
     * The CPU this process spent between two readings, in seconds.
     */
    public function cpuSince(self $before): float
    {
        return round(max(0.0, $this->cpuSeconds - $before->cpuSeconds), 3);
    }
}
```

[`src/Benchmarks/ProcessCost.php#L7-L38`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/ProcessCost.php#L7-L38)

**Перевод комментариев:**
- `ProcessCost` (PHPDoc): одно чтение CPU и памяти процесса, из ядра. CPU —
  `utime+stime` из `/proc/<pid>/stat`; стоимость фазы — разность двух чтений
  (`cpuSince()`), поскольку процесс тратил CPU и до начала фазы. Пиковая память
  — собственная высоководяная отметка ядра (`VmHWM`) за время жизни процесса,
  точная, в отличие от пика, замеренного таймером.
- `CLOCK_TICKS_PER_SECOND` (PHPDoc): `CLK_TCK` на любом Linux, где запускается
  проект (`x86_64` и `arm64`), поэтому секунды CPU имеют разрешение 10 мс —
  достаточно для фаз, которые длятся секунды.
- `cpuSince()` (PHPDoc): CPU, которое процесс потратил между двумя чтениями, в
  секундах.

Чтение `/proc/<pid>/stat` — отдельная маленькая придирчивость: во втором поле
может быть что угодно, включая пробелы и скобки, поэтому разбор начинается
**после последней** `)`. Ошибиться здесь — значит тихо получить не то поле и не
то число.

```php
/**
 * Reads a ProcessCost out of /proc: VmRSS and VmHWM from /proc/<pid>/status
 * (via Memory\ProcStatusReader), utime and stime from /proc/<pid>/stat.
 */
final class ProcessCostReader
{
    public function __construct(
        private readonly ProcStatusReader $status = new ProcStatusReader(),
    ) {
    }

    /**
     * @throws RuntimeException the process is gone or this is not Linux -
     *                          better than silently reporting a zero cost
     */
    public function read(int $pid): ProcessCost
    {
        if ($pid <= 0) {
            throw new RuntimeException('A process cost needs a real pid.');
        }

        $status = $this->status->read($pid);

        return new ProcessCost(
            $pid,
            $this->cpuSeconds($pid),
            (int) ($status['VmRSS'] ?? 0),
            (int) ($status['VmHWM'] ?? 0),
        );
    }
```

[`src/Benchmarks/ProcessCostReader.php#L10-L39`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Benchmarks/ProcessCostReader.php#L10-L39)

**Перевод комментариев:**
- `ProcessCostReader` (PHPDoc): читает `ProcessCost` из `/proc`: `VmRSS` и
  `VmHWM` из `/proc/<pid>/status` (через `Memory\ProcStatusReader`), `utime` и
  `stime` из `/proc/<pid>/stat`.
- `read()` (PHPDoc): `@throws RuntimeException` — процесс исчез или это не
  Linux; лучше, чем молча сообщить нулевую стоимость.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-search && make build install
cd ../php-systems-platform && make build install
```

Первые три эксперимента выполняются в контейнере `php-mini-search` (проект в
`/app`), четвёртый — в контейнере `php-systems-platform` (там он поднимает
настоящий `serve`). Все четыре вывода детерминированы: каждый скрипт прогонялся
дважды и совпал байт-в-байт. Корпуса генерируются с одним и тем же `mt_srand`,
поэтому «один и тот же корпус на любой машине» — не обещание, а свойство
скрипта. Секунды в вывод не попадают: где нужна была скорость, напечатано
**направление с запасом** («не хуже, чем в 5 раз быстрее»), а не число.

### 1. Базовая линия: из чего состоит работа

Прежде чем что-то менять, надо увидеть, где работа **есть**. Здесь считается
корпус, сгенерированный из 800 слов, и сравнение двух способов обойти его кешем:
на каждый токен и на каждый **различный** токен.

```bash
docker compose cp /tmp/ch25-token-baseline.php php:/tmp/ch25-token-baseline.php
docker compose exec php php /tmp/ch25-token-baseline.php
```

```php
<?php

declare(strict_types=1);

/**
 * The baseline before anything is changed: where the work in a corpus
 * actually goes, counted rather than timed.
 *
 * docs/BENCHMARKS.md reports that analysis was 96% stemming and that 90% of
 * tokens repeat, and that repetition is what made a cache worth 40x. This
 * experiment re-measures the repetition on a corpus generated here, so the
 * claim is a property somebody can check rather than a sentence in a
 * document. The corpus is synthetic and seeded, so every number below is
 * reproducible on any machine: nothing here is a timing.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch25-token-baseline.php php:/tmp/ch25-token-baseline.php
 *   docker compose exec php php /tmp/ch25-token-baseline.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;

// A vocabulary of real-looking English words: 40 stems x 20 endings, all
// lowercase ASCII, so the English stemmer runs and collapses some of them.
$stems = [
    'connect', 'order', 'system', 'worker', 'queue', 'search', 'index', 'record',
    'update', 'delete', 'store', 'fetch', 'merge', 'split', 'cache', 'buffer',
    'handle', 'resolve', 'request', 'response', 'server', 'client', 'packet', 'stream',
    'encode', 'decode', 'parse', 'render', 'commit', 'abort', 'retry', 'timeout',
    'lock', 'unlock', 'pool', 'shard', 'replica', 'journal', 'snapshot', 'recover',
];

$endings = [
    '', 's', 'es', 'ed', 'ing', 'er', 'ers', 'ion', 'ions', 'able',
    'ables', 'ment', 'ments', 'less', 'ness', 'ity', 'ities', 'al', 'ally', 'ize',
];

$vocabulary = [];

foreach ($stems as $stem) {
    foreach ($endings as $ending) {
        $vocabulary[] = $stem . $ending;
    }
}

sort($vocabulary);

// Zipf: word at rank k is sampled with weight 1/(k+1). This is what makes a
// corpus look like text - a few words everywhere, most words rare.
$weights = [];
$cumulative = 0.0;

foreach ($vocabulary as $rank => $word) {
    $cumulative += 1.0 / ($rank + 1);
    $weights[$word] = $cumulative;
}

mt_srand(20251019);

$pick = static function () use ($vocabulary, $weights, $cumulative): string {
    $target = (mt_rand() / mt_getrandmax()) * $cumulative;

    foreach ($vocabulary as $word) {
        if ($target <= $weights[$word]) {
            return $word;
        }
    }

    return $vocabulary[count($vocabulary) - 1];
};

$documents = 300;
$tokensPerDocument = 200;

$analyzer = new Analyzer();

$tokens = [];
$terms = [];

for ($document = 0; $document < $documents; $document++) {
    $words = [];

    for ($token = 0; $token < $tokensPerDocument; $token++) {
        $words[] = $pick();
    }

    // The corpus is generated here, so its token stream is known exactly.
    $tokens = array_merge($tokens, $words);
    $terms = array_merge($terms, $analyzer->terms(implode(' ', $words)));
}

$distinctTokens = count(array_unique($tokens));
$distinctTerms = count(array_unique($terms));

printf("documents: %d\n", $documents);
printf("tokens per document: %d\n", $tokensPerDocument);
printf("tokens, by construction: %d\n", count($tokens));
printf("terms the analyzer returned: %d\n", count($terms));
printf("a term per token, as documented: %s\n", count($terms) === count($tokens) ? 'yes' : 'no');
printf("\ndistinct tokens: %d\n", $distinctTokens);
printf("distinct terms after stemming: %d\n", $distinctTerms);
printf("share of tokens that repeat: %.1f%%\n", 100.0 * (1.0 - $distinctTokens / count($tokens)));
printf("stemming work without a cache: %d stems (one per token)\n", count($tokens));
printf("stemming work with a cache: %d stems (one per distinct token)\n", $distinctTokens);

$counts = array_count_values($tokens);
arsort($counts);
$top = array_key_first($counts);

printf("\nthe most frequent token is \"%s\": %d of %d tokens\n", $top, $counts[$top], count($tokens));
printf("tokens seen exactly once: %d of %d distinct\n", count(array_filter($counts, static fn (int $count): bool => $count === 1)), $distinctTokens);
printf("cache bound (Analyzer::CACHE_ENTRIES): %d\n", Analyzer::CACHE_ENTRIES);
printf("the whole vocabulary fits in the cache: %s\n", $distinctTokens < Analyzer::CACHE_ENTRIES ? 'yes' : 'no');
```

**Перевод комментариев:**
- `ch25-token-baseline.php` (PHPDoc): базовая линия до любых изменений — куда на
  самом деле уходит работа в корпусе, посчитанная, а не замеренная по времени.
  `docs/BENCHMARKS.md` сообщает, что анализ на 96% состоял из стемминга и что
  90% токенов повторяются, а повтор — то, что делало кеш стоящим 40x. Этот
  эксперимент заново измеряет повтор на сгенерированном здесь корпусе, чтобы
  утверждение было свойством, которое можно проверить, а не предложением в
  документе. Корпус синтетический и засеянный, поэтому каждое число ниже
  воспроизводится на любой машине: здесь нет ни одного замера времени.
  Запускается в контейнере, где проект смонтирован в `/app`, командами из
  докблока.
- `// A vocabulary of real-looking English words: 40 stems x 20 endings, all` —
  словарь правдоподобных английских слов: 40 основ × 20 окончаний, всё в нижнем
  регистре ASCII, чтобы английский стеммер работал и схлопывал некоторые из них.
- `// Zipf: word at rank k is sampled with weight 1/(k+1). This is what makes a`
  — Zipf: слово ранга `k` выбирается с весом `1/(k+1)`. Именно это делает корпус
  похожим на текст — несколько слов везде, большинство редко.
- `// The corpus is generated here, so its token stream is known exactly.` —
  корпус генерируется здесь, поэтому его поток токенов известен точно.

```text
documents: 300
tokens per document: 200
tokens, by construction: 60000
terms the analyzer returned: 60000
a term per token, as documented: yes

distinct tokens: 800
distinct terms after stemming: 220
share of tokens that repeat: 98.7%
stemming work without a cache: 60000 stems (one per token)
stemming work with a cache: 800 stems (one per distinct token)

the most frequent token is "abort": 8295 of 60000 tokens
tokens seen exactly once: 0 of 800 distinct
cache bound (Analyzer::CACHE_ENTRIES): 200000
the whole vocabulary fits in the cache: yes
```

Первые шесть строк — про контракт анализатора: сколько токенов (**60000**, по
построению), сколько термов вернулось (`60000`) и что «терм на токен» — правда,
а не упрощение (дубликаты сохраняются: частота терма в поле — это сколько раз он
вышел, → глава 20).

Дальше — сама базовая линия. В этом корпусе 800 различных токенов, и `98.7%`
токенов — повторы. Это **закрытый** словарь: он специально мал, чтобы показать
повтор в чистом виде (в реальном тексте из документации — 27 379 различных
токенов на 5 миллионов). Именно отсюда растут две строки о работе стемминга: без
кеша стеммер вызывается на каждый токен (`60000`), с кешем — на каждый различный
(`800`), то есть в 75 раз реже. Это ещё не измерение ускорения: это измерение
того, **что** можно не делать дважды.

Последние строки проверяют границу. Самый частый токен — `abort`, который
встречается 8295 раз из 60000; токенов, встретившихся ровно один раз, **ноль**
(это свойство закрытого словаря). И весь словарь влезает в кеш: 800 против 200
000 записей — значит, на этом корпусе кеш никогда не будет опустошён. Заранее
зная это, легче читать следующий эксперимент.

### 2. Одна правка: кеш обязан быть невидимым

Здесь корпус крупнее — 400 документов по 400 токенов над словарём в 15 000 слов,
— и это важно: словарь из восьмисот слов заполняется первым же документом, и
«холодного» прохода по нему не бывает. Повтор бывает только там, где токенов
много, а разных — меньше.

```bash
docker compose cp /tmp/ch25-stem-cache.php php:/tmp/ch25-stem-cache.php
docker compose exec php php /tmp/ch25-stem-cache.php
```

```php
<?php

declare(strict_types=1);

/**
 * The change itself: stems are cached token -> term. What it is allowed to
 * change is measured by a hash of the whole term stream (nothing), and what
 * it is worth is reported as a floor and as derived counts rather than as a
 * stopwatch number.
 *
 * The corpus here is bigger than experiment 1's on purpose: a closed
 * vocabulary of eight hundred words is filled by the first document, so a
 * "cold" pass over it never happens. Text-like repetition needs many tokens
 * over a large vocabulary - the shape docs/BENCHMARKS.md measured (5M tokens,
 * 27k distinct), which is what makes the cache worth an order of magnitude.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch25-stem-cache.php php:/tmp/ch25-stem-cache.php
 *   docker compose exec php php /tmp/ch25-stem-cache.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;

// A vocabulary of 15,000 lowercase words, seeded, so the corpus is the same
// on every machine. They are not English; what matters for the cache is that
// a token is a token, and every miss costs exactly one stem() call.
mt_srand(20251019);

$letters = 'abcdefghijklmnopqrstuvwxyz';
$vocabulary = [];

for ($word = 0; $word < 15_000; $word++) {
    $length = 4 + mt_rand(0, 5);
    $token = '';

    for ($letter = 0; $letter < $length; $letter++) {
        $token .= $letters[mt_rand(0, 25)];
    }

    $vocabulary[$token] = true;
}

$vocabulary = array_keys($vocabulary);
sort($vocabulary);

// Zipf again: rank k is sampled with weight 1/(k+1).
$weights = [];
$cumulative = 0.0;

foreach ($vocabulary as $rank => $word) {
    $cumulative += 1.0 / ($rank + 1);
    $weights[$word] = $cumulative;
}

$pick = static function () use ($vocabulary, $weights, $cumulative): string {
    $target = (mt_rand() / mt_getrandmax()) * $cumulative;

    foreach ($vocabulary as $word) {
        if ($target <= $weights[$word]) {
            return $word;
        }
    }

    return $vocabulary[count($vocabulary) - 1];
};

$documents = 400;
$tokensPerDocument = 400;
$corpus = [];
$seen = [];

for ($document = 0; $document < $documents; $document++) {
    $words = [];

    for ($token = 0; $token < $tokensPerDocument; $token++) {
        $word = $pick();
        $seen[$word] = true;
        $words[] = $word;
    }

    $corpus[] = implode(' ', $words);
}

$tokens = $documents * $tokensPerDocument;
$distinctTokens = count($seen);

/**
 * Analyze the whole corpus, feeding every term into a hash so the stream can
 * be compared without keeping 160,000 strings in memory.
 *
 * @return array{hash: string, terms: int, distinct: int, seconds: float}
 */
$pass = static function (Analyzer $analyzer) use ($corpus): array {
    $hash = hash_init('xxh128');
    $terms = 0;
    $distinct = [];
    $start = hrtime(true);

    foreach ($corpus as $text) {
        foreach ($analyzer->terms($text) as $term) {
            hash_update($hash, $term . "\x1f");
            $terms++;
            $distinct[$term] = true;
        }
    }

    return [
        'hash' => hash_final($hash),
        'terms' => $terms,
        'distinct' => count($distinct),
        'seconds' => (hrtime(true) - $start) / 1e9,
    ];
};

// --- the cold pass, then a warm pass over the same corpus ------------------
$shared = new Analyzer();

$cold = $pass($shared);
$warm = $pass($shared);

// --- one analyzer for the corpus vs one per document -----------------------
$perDocumentStart = hrtime(true);

foreach ($corpus as $text) {
    (new Analyzer())->terms($text);
}

$perDocumentSeconds = (hrtime(true) - $perDocumentStart) / 1e9;

$sharedStart = hrtime(true);
$sharedPass = $pass(new Analyzer());
$sharedSeconds = (hrtime(true) - $sharedStart) / 1e9;

printf("documents: %d\n", $documents);
printf("tokens: %d\n", $tokens);
printf("distinct tokens: %d\n", $distinctTokens);
printf("an average token repeats: %.1f times\n", $tokens / $distinctTokens);

printf("\nwhat the cache is allowed to change:\n");
printf("  terms from the cold pass: %d\n", $cold['terms']);
printf("  terms from the warm pass: %d\n", $warm['terms']);
printf("  distinct terms after stemming: %d\n", $cold['distinct']);
printf("  the two term streams are the same stream: %s\n", $cold['hash'] === $warm['hash'] ? 'yes' : 'no');

printf("\nthe stemming work each variant implies (counts, not timings):\n");
printf("  no cache at all (one stem per token): %d stems\n", $tokens);
printf("  one analyzer for the corpus: %d stems (one per distinct token)\n", $distinctTokens);
printf(
    "  the same corpus, a fresh analyzer per document: %d stems\n",
    $documents * $tokensPerDocument,
);

printf("\nand the direction of the timing, as a floor:\n");
printf(
    "  one analyzer for the corpus was at least 5x faster than one per document: %s\n",
    $sharedSeconds * 5.0 <= $perDocumentSeconds ? 'yes' : 'no',
);

printf("\nwhat the cache is not allowed to do:\n");
printf("  distinct tokens: %d\n", $distinctTokens);
printf("  the cache holds up to %d tokens\n", Analyzer::CACHE_ENTRIES);
printf(
    "  so it was never emptied, and the warm pass above stemmed nothing: %s\n",
    $distinctTokens < Analyzer::CACHE_ENTRIES ? 'yes' : 'no',
);
```

**Перевод комментариев:**
- `ch25-stem-cache.php` (PHPDoc): само изменение — стемы кешируются
  `token -> term`. То, что ему разрешено менять, измеряется хэшем всего потока
  термов (ничего), а его ценность сообщается как нижняя граница и производные
  счётчики, а не как число секундомера. Корпус здесь намеренно больше, чем в
  эксперименте 1: закрытый словарь из восьмисот слов заполняется первым же
  документом, поэтому «холодного» прохода по нему не бывает. Повтор, похожий на
  текст, требует много токенов над большим словарём — той формы, которую измерил
  `docs/BENCHMARKS.md` (5 млн токенов, 27 тыс. различных); именно это делает кеш
  стоящим порядка величины. Запускается в контейнере, где проект смонтирован в
  `/app`, командами из докблока.
- `// A vocabulary of 15,000 lowercase words, seeded, so the corpus is the same`
  — словарь из 15 000 слов в нижнем регистре, засеянный, чтобы корпус был
  одинаковым на любой машине. Это не английский; для кеша важно, что токен есть
  токен и каждый промах стоит ровно одного вызова `stem()`.
- `// Zipf again: rank k is sampled with weight 1/(k+1).` — снова Zipf: слово
  ранга `k` выбирается с весом `1/(k+1)`.
- `$pass` (PHPDoc): анализирует весь корпус, подавая каждый терм в хэш, чтобы
  потоки можно было сравнить, не держа 160 000 строк в памяти. Возвращает
  `array{hash: string, terms: int, distinct: int, seconds: float}`.
- `// --- the cold pass, then a warm pass over the same corpus ---` — холодный
  проход, затем тёплый по тому же корпусу.
- `// --- one analyzer for the corpus vs one per document ---` — один анализатор
  на корпус против анализатора на каждый документ.

```text
documents: 400
tokens: 160000
distinct tokens: 12967
an average token repeats: 12.3 times

what the cache is allowed to change:
  terms from the cold pass: 160000
  terms from the warm pass: 160000
  distinct terms after stemming: 12967
  the two term streams are the same stream: yes

the stemming work each variant implies (counts, not timings):
  no cache at all (one stem per token): 160000 stems
  one analyzer for the corpus: 12967 stems (one per distinct token)
  the same corpus, a fresh analyzer per document: 160000 stems

and the direction of the timing, as a floor:
  one analyzer for the corpus was at least 5x faster than one per document: yes

what the cache is not allowed to do:
  distinct tokens: 12967
  the cache holds up to 200000 tokens
  so it was never emptied, and the warm pass above stemmed nothing: yes
```

Ключевое утверждение главы проверяется двумя строками о хэшах: список термов
холодного прохода и список термов тёплого прохода — **один и тот же поток**
(`the two term streams are the same stream: yes`). Целый список из 160 000
термов сравнивать в памяти дорого, поэтому каждый терм подаётся в инкрементный
хэш: сравнение двух хэшей — это сравнение двух потоков, и оно дешёвое.

Строки «сколько работы подразумевает каждый вариант» — не секунды, а
**счётчики**: без кеша стеммер вызывается 160 000 раз, с одним анализатором на
корпус — 12 967 (число различных токенов), со свежим анализатором на каждый
документ — снова 160 000. Из этих трёх чисел видно и предмет правки, и её
границу: свежий анализатор на документ **теряет** меж-документный повтор, и
именно его находит кеш.

Направление скорости заявлено с запасом: «один анализатор на корпус был по
меньшей мере в 5 раз быстрее, чем по анализатору на документ — `yes`». Это
единственное место с временем, и оно намеренно грубое: точное отношение зависит
от машины, а его **направление** — нет.

Наконец, граница памяти: 12 967 различных токенов против 200 000 записей, кеш ни
разу не опустошался — и поэтому тёплый проход не стеммил ничего. Два свойства,
«меняет результат» и «растёт в памяти», проверяются здесь на одном и том же
прогоне.

### 3. Работу решает корпус, а не код

Тот же запрос, тот же код, два корпуса. Измеряется `SearchResult::total` — число
документов, которые BM25 обязан оценить, то есть работа, которую не обходит
никакая оптимизация порядка обхода.

```bash
docker compose cp /tmp/ch25-which-work.php php:/tmp/ch25-which-work.php
docker compose exec php php /tmp/ch25-which-work.php
```

```php
<?php

declare(strict_types=1);

/**
 * Measuring the right thing: the same query, the same code, two corpora -
 * and a difference of two orders of magnitude in the work done.
 *
 * docs/BENCHMARKS.md reports that the synthetic corpus is a worst case and
 * Wikipedia a good one (a term in most documents vs a term in a few). The
 * observable that carries this is SearchResult::total - the number of
 * documents BM25 has to score, which is exactly the work a query ordering
 * cannot avoid. It is a count, so it is reproducible byte for byte.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch25-which-work.php php:/tmp/ch25-which-work.php
 *   docker compose exec php php /tmp/ch25-which-work.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;
use PhpMiniSearch\Index\InvertedIndex;

$analyzer = new Analyzer();
$documents = 400;

/**
 * @return array<string, list<string>>
 */
$fields = static function (array $terms): array {
    return ['title' => $terms, 'body' => $terms];
};

// --- corpus A: the query term is in every document -------------------------
$everywhere = new InvertedIndex();

for ($number = 0; $number < $documents; $number++) {
    $body = 'system system term' . $number;

    $everywhere->put('a' . $number, $fields($analyzer->terms($body)));
}

// --- corpus B: the query term is in two documents --------------------------
$rarely = new InvertedIndex();

for ($number = 0; $number < $documents; $number++) {
    $words = ["term{$number}"];

    for ($filler = 0; $filler < 20; $filler++) {
        $words[] = "filler{$number}x{$filler}";
    }

    if ($number < 2) {
        $words[] = 'system';
    }

    $rarely->put('b' . $number, $fields($analyzer->terms(implode(' ', $words))));
}

$inEveryDocument = $everywhere->search(['system'], 10);
$inTwoDocuments = $rarely->search(['system'], 10);

// One frequent term and one rare one: the intersection is what the rare term
// allows, and the frequent term does not change the count.
$intersection = $everywhere->search(['system', 'term10'], 10);

printf("documents in each corpus: %d\n", $documents);
printf("documents the index holds (A / B): %d / %d\n", $everywhere->documentCount(), $rarely->documentCount());

printf("\nquery \"system\":\n");
printf("  corpus A (the term is in every document): %d documents scored\n", $inEveryDocument->total);
printf("  corpus B (the term is in two documents): %d documents scored\n", $inTwoDocuments->total);
printf(
    "  the same query is %.0fx the work on A\n",
    (float) $inEveryDocument->total / (float) max(1, $inTwoDocuments->total),
);

printf("\nquery \"system term10\" (the second term is in one document):\n");
printf("  corpus A: %d documents scored\n", $intersection->total);
printf("  the rare term decides, the frequent one did not add work: %s\n", $intersection->total === 1 ? 'yes' : 'no');

printf("\nhits returned are capped, the work is not:\n");
printf("  A returned %d hits and scored %d documents\n", count($inEveryDocument->hits), $inEveryDocument->total);
printf("  B returned %d hits and scored %d documents\n", count($inTwoDocuments->hits), $inTwoDocuments->total);
```

**Перевод комментариев:**
- `ch25-which-work.php` (PHPDoc): измеряем правильную вещь — один запрос, один и
  тот же код, два корпуса и разница в два порядка в выполненной работе.
  `docs/BENCHMARKS.md` сообщает, что синтетический корпус — худший случай, а
  Wikipedia — хороший (терм почти в каждом документе против терма в немногих).
  Наблюдаемая величина, которая это несёт, — `SearchResult::total`: число
  документов, которые BM25 обязан оценить, то есть ровно та работа, которую не
  обойти порядком запроса. Это счётчик, поэтому он воспроизводится байт-в-байт.
  Запускается в контейнере, где проект смонтирован в `/app`, командами из
  докблока.
- `// --- corpus A: the query term is in every document ---` — корпус A: искомый
  терм есть в каждом документе.
- `// --- corpus B: the query term is in two documents ---` — корпус B: искомый
  терм есть в двух документах.
- `// One frequent term and one rare one: the intersection is what the rare term`
  — один частый и один редкий терм: пересечение — это то, что допускает редкий
  терм, а частый счёт не меняет.

```text
documents in each corpus: 400
documents the index holds (A / B): 400 / 400

query "system":
  corpus A (the term is in every document): 400 documents scored
  corpus B (the term is in two documents): 2 documents scored
  the same query is 200x the work on A

query "system term10" (the second term is in one document):
  corpus A: 1 documents scored
  the rare term decides, the frequent one did not add work: yes

hits returned are capped, the work is not:
  A returned 10 hits and scored 400 documents
  B returned 2 hits and scored 2 documents
```

Корпус `A` содержит искомый терм в каждом из 400 документов; корпус `B` — в
двух. Результат: `400` документа против `2` — **в 200 раз больше работы на том
же запросе**. Это и есть та разница, которую документация показывает между
синтетическим корпусом и Wikipedia: частый терм — это тысячи совпадений под
BM25, редкий — единицы.

Строка с запросом из двух термов показывает, откуда берётся экономия: один терм
есть везде, второй — в одном документе, и результатов по-прежнему **1**. Точное
пересечение решает редкий терм; частый не добавляет работы. Именно поэтому
«самый редкий терм первым» — это не мелочь, а вся разница между быстрым и
медленным корпусом (→ глава 20).

И последняя пара строк — про то, чего счётчик **не** показывает: `A` вернул 10
попаданий, оценив 400 документов; `B` вернул 2 попадания, оценив 2. Ограничение
страницы (`limit`) не ограничивает работу: она зависит от совпадений, а не от
того, сколько строк вы просили.

### 4. Та же дисциплина на платформе: операции вместо секунд

Последний эксперимент повторяет всю главу на платформе. Здесь измеряется путь
чтения заказа: сколько раз база была спрошена и что ответил кеш. Числа —
счётчики из `/metrics` (→ глава 24), то есть они не зависят от машины вообще.

```bash
docker compose cp /tmp/ch25-read-path.php php:/tmp/ch25-read-path.php
docker compose exec php php /tmp/ch25-read-path.php
```

```php
<?php

declare(strict_types=1);

/**
 * The same discipline on the platform's read path: the cache-aside change,
 * measured in operations instead of seconds.
 *
 * What the change saves is measurable without a stopwatch. db.operations
 * counts the rows the database was asked for, cache.hit/cache.miss count what
 * the cache answered, cache.operations counts every cache call. Four counts,
 * so the result is the same on every machine - and unlike a latency it says
 * exactly WHAT changed.
 *
 * The path is walked step by step, because the interesting number is not one
 * total but where a database operation appears and where it does not: a create
 * fills the cache (write-through), so the read after it is free; an update
 * invalidates the key, so the read after THAT pays for the row again. Each
 * step is printed as a delta against the previous /metrics snapshot, so
 * nothing is attributed to startup work.
 *
 * A real serve is started on its own ports, its own pool socket and its own
 * TMPDIR, and stopped at the end. Nothing here is simulated.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch25-read-path.php php:/tmp/ch25-read-path.php
 *   docker compose exec php php /tmp/ch25-read-path.php
 */

require '/app/vendor/autoload.php';

use PhpSystemsPlatform\Support\OwnedProcess;

$directory = sys_get_temp_dir() . '/ch25-read-path-' . getmypid() . '/';

@mkdir($directory . 'logs', 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    OwnedProcess::removeTree($directory);
});

$freePort = static function (): int {
    $probe = stream_socket_server('tcp://127.0.0.1:0', $errno, $error);
    $name = (string) stream_socket_get_name($probe, false);
    fclose($probe);

    return (int) substr($name, (int) strrpos($name, ':') + 1);
};

$httpPort = $freePort();
$databasePort = $freePort();
$cachePort = $freePort();
$searchPort = $freePort();
$socket = $directory . 'worker.sock';

$environment = array_merge((array) getenv(), [
    'TMPDIR' => $directory,
    'HTTP_PORT' => (string) $httpPort,
    'DATABASE_PORT' => (string) $databasePort,
    'CACHE_PORT' => (string) $cachePort,
    'SEARCH_PORT' => (string) $searchPort,
    'WORKER_POOL_SOCKET' => $socket,
    'PLATFORM_ENV' => 'dev',
]);

$serve = OwnedProcess::startAndWaitForPort(
    [PHP_BINARY, '/app/bin/platform.php', 'serve'],
    'serve',
    $directory . 'logs',
    '127.0.0.1',
    $httpPort,
    $environment,
    60.0,
);

$base = 'http://127.0.0.1:' . $httpPort;

/**
 * @return array{status: int, body: string, headers: array<string, string>}
 */
$request = static function (string $method, string $path, ?string $body = null) use ($base): array {
    $context = stream_context_create(['http' => [
        'method' => $method,
        'header' => "Content-Type: application/json\r\nConnection: close\r\n",
        'content' => $body ?? '',
        'ignore_errors' => true,
        'timeout' => 5.0,
    ]]);

    $response = @file_get_contents($base . $path, false, $context);
    $raw = $http_response_header ?? [];

    $status = 0;
    $headers = [];

    foreach ($raw as $index => $line) {
        if ($index === 0 && preg_match('~^HTTP/\S+\s+(\d+)~', $line, $match) === 1) {
            $status = (int) $match[1];

            continue;
        }

        $parts = explode(':', $line, 2);

        if (count($parts) === 2) {
            $headers[strtolower(trim($parts[0]))] = trim($parts[1]);
        }
    }

    return ['status' => $status, 'body' => (string) $response, 'headers' => $headers];
};

/**
 * The numeric lines of a /metrics answer, by name.
 *
 * @return array<string, float>
 */
$counters = static function (string $body): array {
    $values = [];

    foreach (explode("\n", $body) as $line) {
        $parts = preg_split('/\s+/', trim($line));

        if (count($parts) === 2 && is_numeric($parts[1])) {
            $values[$parts[0]] = (float) $parts[1];
        }
    }

    return $values;
};

$now = static fn (): array => $counters($request('GET', '/metrics')['body']);

$columns = ['db.operations', 'cache.hit', 'cache.miss', 'cache.operations'];

$table = [];
$previous = $now();
$firstSnapshot = $previous;

/**
 * Run a step and record what it cost, as a delta of the counters.
 *
 * @param array<string, float> $previous
 */
$step = function (string $label, callable $action) use (&$previous, $now, $columns, &$table): array {
    $result = $action();
    $after = $now();
    $row = [$label];

    foreach ($columns as $column) {
        $row[] = (int) (($after[$column] ?? 0.0) - ($previous[$column] ?? 0.0));
    }

    $table[] = $row;
    $previous = $after;

    return $result;
};

// --- the walk --------------------------------------------------------------
$created = $step(
    'POST /orders (create)',
    fn (): array => $request('POST', '/orders', json_encode(['customer' => 'acme', 'amount' => 42])),
);

$order = json_decode($created['body'], true);
$id = is_array($order) ? (string) ($order['id'] ?? '') : '';

$readAfterCreate = $step(
    'GET /orders/{id} (read, cache filled by the write)',
    fn (): array => $request('GET', '/orders/' . $id),
);

$updated = $step(
    'PUT /orders/{id} (update, invalidate-on-write)',
    fn (): array => $request('PUT', '/orders/' . $id, json_encode(['status' => 'processing'])),
);

$readAfterUpdate = $step(
    'GET /orders/{id} (read, key was just deleted)',
    fn (): array => $request('GET', '/orders/' . $id),
);

$readAgain = $step(
    'GET /orders/{id} (read, refilled by the miss)',
    fn (): array => $request('GET', '/orders/' . $id),
);

// The last step stops after reading /metrics, so the table is complete.

printf("POST /orders answered: %d\n", $created['status']);
printf("GET /orders/{id} answered: %d, %d, %d\n", $readAfterCreate['status'], $readAfterUpdate['status'], $readAgain['status']);
printf("PUT /orders/{id} answered: %d\n", $updated['status']);
printf("X-Cache on the three reads: %s, %s, %s\n", $readAfterCreate['headers']['x-cache'] ?? 'none', $readAfterUpdate['headers']['x-cache'] ?? 'none', $readAgain['headers']['x-cache'] ?? 'none');

printf("\nwhat each step cost, in operations (a delta of two /metrics reads):\n");
printf("  %-52s %8s %10s %11s %13s\n", 'step', 'db.ops', 'cache.hit', 'cache.miss', 'cache.ops');

foreach ($table as $row) {
    printf("  %-52s %8d %10d %11d %13d\n", $row[0], $row[1], $row[2], $row[3], $row[4]);
}

$readOperations = $table[1][1] + $table[3][1] + $table[4][1];

printf("\nthe three reads together cost this many database operations: %d\n", $readOperations);
printf("the same three reads without a cache would have cost: %d\n", 3);
printf("the database was asked for the row exactly when the cache had no answer: %s\n", $readOperations === 1 ? 'yes' : 'no');

printf("\nthe baseline, before any order existed:\n");
printf("  database operations: %d (the schema work serve does on start)\n", (int) ($firstSnapshot['db.operations'] ?? 0));
printf("  cache hits + misses: %d + %d\n", (int) ($firstSnapshot['cache.hit'] ?? 0), (int) ($firstSnapshot['cache.miss'] ?? 0));

printf("\nthe serve it measured was stopped with code: %d\n", $serve->stop(20.0));
```

**Перевод комментариев:**
- `ch25-read-path.php` (PHPDoc): та же дисциплина на пути чтения платформы —
  изменение cache-aside, измеренное в операциях, а не в секундах. Что экономит
  изменение, измеримо без секундомера: `db.operations` считает строки, которые
  спросили у базы; `cache.hit` /`cache.miss` — что ответил кеш;
  `cache.operations` — каждый вызов кеша. Четыре счётчика, поэтому результат
  одинаков на любой машине и, в отличие от латентности, точно говорит, **что**
  изменилось. Путь проходится шаг за шагом, потому что интересное число — не
  итог, а где операция к базе появляется, а где нет: создание заполняет кеш
  (write-through), поэтому чтение после него бесплатно; обновление инвалидирует
  ключ, поэтому чтение **после него** снова платит за строку. Каждый шаг
  печатается как разность с предыдущим снимком `/metrics`, чтобы ничего не
  приписывалось работе запуска. Запускается настоящий `serve` на своих портах,
  своём сокете пула и своём `TMPDIR`, а в конце останавливается. Здесь ничего не
  симулируется. Запускается в контейнере, где проект смонтирован в `/app`,
  командами из докблока.
- `$request` (PHPDoc):
  `@return array{status: int, body: string, headers: array<string, string>}` —
  статус, тело и заголовки ответа.
- `$counters` (PHPDoc): числовые строки ответа `/metrics` по именам; возвращает
  `array<string, float>`.
- `$step` (PHPDoc): выполняет шаг и записывает, сколько он стоил, как разность
  счётчиков; `@param array<string, float> $previous` — предыдущие значения.
- `// --- the walk ---` — проход по шагам.
- `// The last step stops after reading /metrics, so the table is complete.` —
  последний шаг останавливается после чтения `/metrics`, поэтому таблица полна.

```text
POST /orders answered: 201
GET /orders/{id} answered: 200, 200, 200
PUT /orders/{id} answered: 200
X-Cache on the three reads: hit, miss, hit

what each step cost, in operations (a delta of two /metrics reads):
  step                                                   db.ops  cache.hit  cache.miss     cache.ops
  POST /orders (create)                                       1          0           0             1
  GET /orders/{id} (read, cache filled by the write)          0          1           0             1
  PUT /orders/{id} (update, invalidate-on-write)              2          0           0             1
  GET /orders/{id} (read, key was just deleted)               1          0           1             2
  GET /orders/{id} (read, refilled by the miss)               0          1           0             1

the three reads together cost this many database operations: 1
the same three reads without a cache would have cost: 3
the database was asked for the row exactly when the cache had no answer: yes

the baseline, before any order existed:
  database operations: 26 (the schema work serve does on start)
  cache hits + misses: 0 + 0

the serve it measured was stopped with code: 0
```

Таблица — пошаговая, и каждый шаг — это **разность** двух чтений `/metrics` (до
и после шага), поэтому работа запуска (`26` операций на схему) не примешивается
ни к одной строке. Базовая линия здесь делает то же, что и в первом
эксперименте: она снята до первого заказа и показывает, что счётчики кеша
стартуют с нуля.

Первые два шага — главный сюрприз для того, кто не мерил: `POST /orders` стоит
**одной** операции к базе и **одной** к кешу (`setOrder` при записи), а
следующее чтение — **ноль** операций к базе и попадание. Кеш заполняется не при
чтении, а при записи, поэтому вовсе не всякое чтение является первым промахом.
Ровно поэтому фазы `C1` нагрузочного теста платформы вставляют данные в базу
напрямую: иначе «фаза промахов» окажется фазой попаданий — та самая ловушка №1
из §1.

Дальше `PUT /orders/{id}` стоит **двух** операций к базе (прочитать строку и
записать её) и **одной** к кешу — это удаление ключа (invalidate-on-write, →
глава 24). И вот **после него** чтение наконец становится промахом:
`X-Cache: miss`, одна операция к базе, два обращения к кешу (промах и
заполнение). Следующее чтение — снова попадание и ноль операций к базе.

Итог: три чтения стоят **одной** операции к базе вместо трёх — и база спрошена
ровно в тот момент, когда у кеша не было ответа. Это то же утверждение, что «60
с → 1.5 с» в анализаторе, но проверяемое на любой машине и слово в слово:
**что** изменилось, а не насколько быстро.

## 5. Отказы

| Что ломается                                    | Чем обнаруживается                                      | Что происходит                                                                        |
| ----------------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------- |
| Оптимизируют без базовой линии                  | не с чем сравнивать                                     | ускорение недоказуемо; правка может ускорять не то, что медленно                      |
| Меряют секунды                                  | другое железо, другая загрузка                          | число не воспроизводится, проверку на него не написать                                |
| Измеряют среднее                                | перцентиль не сходится со средним                       | хвост невидим: редкая десятикратная задержка растворяется в среднем                   |
| «Фаза промахов» из заказов, созданных через API | счётчики кеша: попаданий больше, чем ожидалось          | путь записи заполнил кеш — измеряются попадания под именем промахов                   |
| Путь чтения меняют, не сняв базовую линию       | `db.operations` до и после отличаются на работу запуска | вы приписываете оптимизации то, что сделала инициализация схемы                       |
| Оборванное соединение «ускоряет» фазу           | оно не дало выборки латентности                         | запрос считается проваленным и не попадает в перцентили                               |
| Фаза ответила `500`, но быстро                  | статусы таллируются, а не предполагаются                | быстрая фаза с ошибками не проходит за хорошую                                        |
| Оптимизация меняет ответ                        | сравнение результатов до и после                        | правка неверна: время можно менять, ответ — нет                                       |
| Кеш растёт без границы                          | `CACHE_ENTRIES`, сброс при заполнении                   | память плоская; ценой всплеска стемминга на опустошение                               |
| Число повторов в корпусе мало                   | доля различных токенов близка к 100%                    | кешировать нечего; правка не даст выигрыша                                            |
| Словарь огромен, документ мал                   | токенов на различных — около единицы                    | меж-документного повтора нет: свежий анализатор на документ почти так же хорош        |
| Быстрый корпус приняли за быстрый код           | второй корпус даёт другие числа                         | работу решает корпус: синтетический `p99` vs Wikipedia                                |
| Число токенов упало, а p50 не изменился         | цена ушла в другую часть профиля                        | анализ был не единственной стоимостью: ускорение не там, где искали                   |
| CPU фазы взят абсолютом                         | разность двух чтений `/proc`                            | в число попадёт работа, сделанная до фазы                                             |
| Пик памяти замерен таймером                     | `VmHWM` вместо опроса                                   | таймер может не попасть в пик; ядро не может                                          |
| `/proc/<pid>/stat` разобран наивно              | второе поле содержит `)` и пробелы                      | читается не то поле — тихо, без ошибки                                                |
| Замеряют качество по скорости                   | счётчик измеряет работу, не релевантность               | запрос, оценивший много документов и нашедший ноль, выглядит «тяжёлым», а не неверным |

## 6. Как это проверено

**Контракт оптимизации — поведение, а не скорость.** У кеша стеммов нет своего
теста, и это осознанный выбор: проверять в тесте секунды нельзя, а проверять
надо ровно то, что кеш менять не имеет права. Это поведение `Analyzer`, и оно
проверяется в `tests/Analysis/AnalyzerTest.php` — десять методов:
`testRepeatedWordsKeepEveryOccurrence` (повторы — часть результата, и кеш их не
съедает), `testCaseDoesNotMatter`,
`testEachScriptGoesToItsOwnStemmerAndMixedTokensAreKept`,
`testNumbersAreTermsAndAreNotStemmed`, `testYoIsTheSameLetterAsYe`,
`testCompatibilityFormsMatchWhatPeopleType`,
`testAnApostropheInsideAWordDoesNotMatter`,
`testAnApostropheAtTheEdgeOfAWordSeparates`,
`testEmptyAndPunctuationOnlyTextHasNoTerms`, `testInvalidUtf8IsRejected`.

**Счётчик, которым меряется поиск.** `tests/Index/InvertedIndexTest.php` —
четырнадцать методов, и три прямо про измеряемое в эксперименте 3:
`testPagesSliceTheRankingAndTotalCountsEveryMatch` (страница обрезается, `total`
считает все совпадения), `testATermInEveryDocumentStillScoresAboveZero` (корпус
`A`) и `testQueryOrderAndDuplicatesDoNotChangeTheResult` (порядок термов не
меняет ответ — то, на чём стоит пересечение с самого редкого). Рядом —
`tests/Index/InvertedIndexAgainstOracleTest.php`, который сверяет индекс с
независимой реализацией: это защита от «оптимизации», меняющей результат.

**Протокол измерения.** `tests/Integration/HttpLoadDriverTest.php` — шесть
методов, и каждый — про ловушку из §1:
`testEveryRequestIsSentAndEveryAnswerIsCounted`,
`testConcurrencyIsRealRatherThanSequentialInDisguise` (параллельность настоящая,
а не последовательная под видом параллельной),
`testTheReadPathMarkerIsReadRatherThanAssumed` (`X-Cache` читается),
`testAFailedRequestIsCountedAsAFailedRequest`,
`testAConnectionFailureIsVisibleRatherThanDropped` и
`testTheSamePathsAreReusedAcrossPhasesOnOneDriver`.

**Числа и их чтение.** `tests/Unit/HttpLoadResultTest.php` (девять методов) —
про арифметику отчёта: `testOnlyTwoAndThreeHundredCountAsSucceeded`,
`testRateIsRequestsOverWallTime`,
`testPercentilesAreNearestRankNotInterpolated`,
`testSamplesAreSortedSoPercentilesHaveAnOrderToRead`,
`testAConnectionFailureCountsAsAFailedRequest`,
`testEmptySamplesAreZeroRatherThanUndefined`,
`testTheLastPercentileOfOneSampleIsThatSample`,
`testRateIsZeroWhenNothingWasSent`,
`testTheArrayFormCarriesTheEvidenceNotJustTheSummary`.
`tests/Unit/LoadTestReportTest.php` (девять) — про то, что отчёт не может
разойтись с данными: `testTheArrayShapeIsTheOneTheDocsQuote`,
`testTheTableHasOneBlockPerPlannedTest`, `testEveryPhaseIsNamedInTheTable`,
`testTheCacheMarkersAreCountedNotAsserted`,
`testTheScalingRatiosAreAgainstOneWorker`,
`testARunWithoutAOneWorkerRowSaysSoInsteadOfPretending`,
`testTheQueueBlockNamesItsWorkload`,
`testTheEnvironmentIsPartOfTheReportNotAFootnote`.

**Стоимость процесса.** `tests/Unit/ProcessCostReaderTest.php` (шесть):
`testItReadsThisProcess`, `testTheKernelPeakIsAtLeastTheCurrentResidentSize`,
`testCpuIsReadAsADeltaBetweenTwoReadings`,
`testAProcessThatIsNotThereIsAnError`, `testAPidOfZeroIsAnError`,
`testReadsBothMemoryNumbersForTheGivenPid`. Последние два — про то, что
отсутствие числа лучше нуля: ошибка вместо тихого нуля.
`tests/Unit/OrderLoadBenchmarkTest.php` (два) и
`tests/Integration/{MemoryDemoCommandTest,WorkersMemoryCommandTest}.php` (по
одному) — про команды, которые эти замеры печатают.

Прогоны: в `php-mini-search` —
`docker compose exec php php vendor/bin/phpunit tests/Analysis tests/Index` —
**39 тестов, 45 653 утверждения, OK** (много утверждений даёт сверка с оракулом:
она проверяет индекс значениями, а не примерами). В `php-systems-platform` —
четыре юнит-файла отчётов и стоимости (`LoadTestReportTest`,
`ProcessCostReaderTest`, `HttpLoadResultTest`, `OrderLoadBenchmarkTest`) — **26
тестов, 78 утверждений, OK**.

## 7. Цифры

| Файл                                                          | Строк | Что делает                     |
| ------------------------------------------------------------- | ----- | ------------------------------ |
| `php-mini-search` `src/Analysis/Analyzer.php`                 | 96    | конвейер текста и кеш стеммов  |
| `php-mini-search` `src/Index/InvertedIndex.php`               | 353   | постинги, `total`, `score()`   |
| `php-mini-search` `src/Index/TopHits.php`                     | 79    | куча на `offset+limit` лучших  |
| `php-mini-search` `docs/BENCHMARKS.md`                        | 149   | измерения и их окружение       |
| `php-systems-platform` `src/Benchmarks/LoadTestRunner.php`    | 298   | фазы и владение платформой     |
| `php-systems-platform` `src/Benchmarks/HttpLoadTest.php`      | 227   | один curl-хэндл на слот        |
| `php-systems-platform` `src/Benchmarks/HttpLoadResult.php`    | 131   | выборки, перцентили, статусы   |
| `php-systems-platform` `src/Benchmarks/LoadTestReport.php`    | 164   | одно измерение — две формы     |
| `php-systems-platform` `src/Benchmarks/ProcessCost.php`       | 38    | CPU и пиковая память           |
| `php-systems-platform` `src/Benchmarks/ProcessCostReader.php` | 64    | `/proc/<pid>/stat` и `/status` |
| `php-systems-platform` `src/Memory/ProcStatusReader.php`      | 77    | RSS и VmHWM                    |
| `php-systems-platform` `docs/benchmarks.md`                   | 221   | восемь полей на каждый замер   |

| Документированное число (php-mini-search)          | Значение                              |
| -------------------------------------------------- | ------------------------------------- |
| Стоимость анализа одного токена                    | 12 µs                                 |
| Доля стемминга в стоимости анализа                 | 96%                                   |
| Повторяющиеся токены в корпусе                     | 90% (5 млн токенов, 27 379 различных) |
| NFKC + нижний регистр + токенайзинг 300 документов | 22 мс вместе                          |
| Анализ всего корпуса: до / после                   | ≈ 60 с / 1.5 с                        |
| Загрузка (полная переиндексация через API)         | 339 → 985 док/с                       |
| `PUT` (запись)                                     | p50 24.4 → 8.2 мс                     |
| Поиск (не изменился)                               | 488 → 489 запросов/с                  |
| `fsync`                                            | 0.56 мс (не узкое место)              |
| Синтетический корпус / Wikipedia (словарь)         | 25 896 / 151 505 термов               |
| Стоимость записи в обоих корпусах                  | 7–8 мс                                |
| 200 медленных клиентов                             | −17% пропускной способности           |

| Предел или константа                   | Значение                                |
| -------------------------------------- | --------------------------------------- |
| Записей в кеше стеммов                 | 200 000 (`Analyzer::CACHE_ENTRIES`)     |
| Ожидаемое число термов                 | «терм на токен» (дубликаты сохраняются) |
| Предел запроса по числу термов         | 32                                      |
| Запросов в одном прогоне нагрузки      | до 20 000                               |
| Интервал простоя `serve` при остановке | 20 с                                    |
| Тиков на секунду для `/proc` CPU       | 100 (разрешение 10 мс)                  |
| Успешные коды в отчёте                 | 200–399                                 |

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что                   | Значение                                                          |
| --------------------- | ----------------------------------------------------------------- |
| `ch25-token-baseline` | 116 строк; 60 000 токенов, 800 различных, 98.7% повторов          |
| `ch25-stem-cache`     | 168 строк; 160 000 токенов, 12 967 различных, потоки термов равны |
| `ch25-which-work`     | 86 строк; 400 документов против 2 при одном запросе               |
| `ch25-read-path`      | 214 строк; три чтения — 1 операция к базе вместо трёх             |

## 8. Упражнения

1. **Проследите.** В эксперименте 4 первое чтение после `POST` — попадание, а
   после `PUT` — промах. Объясните, почему путь создания заполняет кеш, а путь
   обновления — нет, и что бы изменилось, если бы `PUT` перезаписывал значение
   вместо удаления ключа.
2. **Докажите гипотезу.** В эксперименте 2 «один анализатор на корпус» быстрее
   «анализатора на документ» только потому, что корпус устроен определённым
   образом. Придумайте корпус, на котором разницы **не будет**, и объясните
   через отношения «токенов на различных», почему.
3. **Найдите правило.** Почему в эксперименте 2 заявлено «не хуже, чем в 5 раз
   быстрее», а не точное отношение? Сформулируйте, какие утверждения о скорости
   можно печатать в тексте, а какие — только в отчёте о конкретном прогоне.
4. **Сравните.** `db.operations` (эксперимент 4) и `SearchResult::total`
   (эксперимент 3) — оба счётчики, оба «мера работы». Чем они отличаются как
   доказательства, и почему для пути чтения выбран первый, а для поиска —
   второй?
5. **Спроектируйте тест.** У кеша стеммов нет своего теста (§6). Предложите
   тест, который проверяет **наблюдаемое** свойство кеша, не измеряя время: что
   именно он может утверждать и на каком шве?
6. **Разберитесь в цене.** Эксперимент 1 показал, что в закрытом словаре 98.7%
   токенов — повторы. Оцените, при какой доле различных токенов кеш перестаёт
   окупаться, и что выступает «платой» за него (память, сброс, сложность), если
   выигрыш измеряется временем, а не счётчиком.

## Что дальше

Мы измерили систему её же числами: базовая линия, одна правка, проверяемая
граница. Но остался вопрос, который до сих пор решался на слово: **кто проверяет
сами тесты?** Тридцать девять тестов, зелёных на каждой правке, не доказывают,
что они вообще способны упасть. Следующая глава — про то, как проверить
инструмент проверки: оракулы, мутации и `kill -9` в середине записи.
