# Глава 1. Rate Limiter: как пропускать трафик, не пропуская всё

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Следующая глава: URL Shortener (глава 2).

Первое место, где система встречает нагрузку, — это решение о допуске. Пока
запрос не прошёл проверку, он не должен стоить ни обращения к базе, ни записи в
очередь, ни вызова внешнего API. Rate limiter выглядит простой задачей: «не
больше десяти запросов в секунду на арендатора». Сложность начинается, когда мы
спрашиваем, что именно считается секундой, где хранится счётчик и что происходит
на границе окна.

В лаборатории четыре политики получают один и тот же поток запросов. Token
bucket, fixed window, sliding log и sliding counter отличаются не только
точностью. Они по-разному расходуют память, по-разному ведут себя при всплеске и
по-разному врут. Эксперименты показывают все три различия, а не только число
отклонённых запросов.

---

## 1. Проблема

Пусть у арендатора есть бюджет «10 запросов на 1000 мс». Клиент отправляет
десять запросов в момент 999 мс и ещё десять в момент 1000 мс. Fixed window
считает это двумя разными окнами: `[0, 1000)` и `[1000, 2000)`. Оба раза счётчик
начинается с нуля, поэтому passing получают двадцать запросов, хотя между первым
и последним прошла всего одна миллисекунда. Это не ошибка реализации. Это
свойство модели окна.

Sliding log хранит каждый принятый запрос и на каждом шаге выбрасывает всё, что
старше `now - window`. Он отвечает точно: в любом интервале длиной 1000 мс
принято не больше десяти запросов. Цена — память, пропорциональная лимиту, а не
числу ключей. Sliding counter хранит два счётчика и интерполирует предыдущее
окно. Он дешевле по памяти, но его ответ — оценка, и у этой оценки есть своя
цена в ложных допусках и ложных отказах.

Token bucket вообще не мыслит окнами. У него есть ёмкость и скорость пополнения.
Клиент может мгновенно потратить всю ёмкость, а потом получает токены с заданной
скоростью. Это удобно для API, который принимает пакет запросов, и опасно для
сервиса, который рассчитывал на равномерный поток. Один и тот же лимит «10 в
секунду» описывает разные графики.

Наконец, лимитер — это не только алгоритм. Это контракт: по какому ключу
считаем, чьи часы используем, что возвращаем при отказе и сколько состояния
держим на каждый ключ. В лаборатории время передаётся явно, поэтому решение
воспроизводимо и не зависит от `sleep()`.

---

## 2. Устройство

### 2.1. Контракт и результат

Все политики реализуют один интерфейс. Время — параметр, а не скрытый вызов
`microtime()`.

```php
/**
 * A per-key admission policy.
 *
 * Time is an explicit parameter. A production limiter would read a clock and
 * share state through Redis or a gateway; this interface keeps the policy
 * testable and lets a simulation move time without sleeping.
 */
interface RateLimiter
{
    public function name(): string;

    public function attempt(string $key, int $nowMs): Decision;

    public function reset(string $key): void;

    public function trackedKeys(): int;

    public function counters(): Counters;
}
```

[`src/RateLimiter/RateLimiter.php` L9-L27](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/RateLimiter.php#L9-L27)

`Decision` отделяет разрешение от полезной нагрузки. `remaining` — это сколько
целых запросов ещё помещается в текущий бюджет, `retryAfterMs` — подсказка, а не
обещание: пока клиент ждёт, бюджет может потратить кто-то другой.

```php
/**
 * The answer to one admission attempt.
 *
 * `remaining` is the number of whole requests the caller may still make in the
 * current budget. `retryAfterMs` is zero when the request was allowed and a
 * positive hint when it was not; it is a hint, not a promise, because another
 * caller can consume the budget first.
 */
final readonly class Decision
{
    public function __construct(
        public bool $allowed,
        public int $remaining,
        public int $retryAfterMs,
        public int $limit,
    ) {
    }
```

[`src/RateLimiter/Decision.php` L7-L23](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/Decision.php#L7-L23)

### 2.2. Token bucket

Bucket хранит остаток в целых «нанотокенах», чтобы избежать дрейфа чисел с
плавающей точкой. При первом обращении ключ получает полную ёмкость. При
следующем сначала добавляются токены за прошедшее время, но не выше ёмкости.

```php
    public function attempt(string $key, int $nowMs): Decision
    {
        $this->counters->add('attempts');
        $state = $this->states[$key] ?? null;

        if ($state === null) {
            $state = new BucketState($this->capacityUnits, $nowMs);
            $this->states[$key] = $state;
            $this->counters->add('createdKeys');
        }

        $elapsed = max(0, $nowMs - $state->lastRefillMs);

        if ($elapsed > 0) {
            if ($state->units < $this->capacityUnits && $this->refillUnitsPerMs > 0) {
                $refill = $elapsed * $this->refillUnitsPerMs;
                $state->units = min($this->capacityUnits, $state->units + $refill);
                $this->counters->add('refills');
            }

            $state->lastRefillMs = max($state->lastRefillMs, $nowMs);
        }

        if ($state->units >= self::UNIT) {
            $state->units -= self::UNIT;
            $this->counters->add('allowed');

            return new Decision(
                allowed: true,
                remaining: intdiv($state->units, self::UNIT),
                retryAfterMs: 0,
                limit: $this->capacity,
            );
        }
```

[`src/RateLimiter/TokenBucket.php` L53-L86](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/TokenBucket.php#L53-L86)

`elapsed = max(0, ...)` — не косметика. Если время идемпотентно откатилось
назад, лимитер не должен начислять отрицательное пополнение. `lastRefillMs`
двигается только вперёд. Это делает поведение монотонным относительно
наблюдаемого времени.

### 2.3. Fixed window

Fixed window — самый дешёвый вариант: одно число и начало окна на ключ. Он же
самый уязвимый на границе.

```php
    public function attempt(string $key, int $nowMs): Decision
    {
        $this->counters->add('attempts');
        $state = $this->states[$key] ?? null;
        $windowStart = intdiv(max(0, $nowMs), $this->windowMs) * $this->windowMs;

        if ($state === null) {
            $state = new FixedWindowState($windowStart, 0);
            $this->states[$key] = $state;
            $this->counters->add('createdKeys');
        } elseif ($windowStart > $state->windowStartMs) {
            $state->windowStartMs = $windowStart;
            $state->count = 0;
            $this->counters->add('windowRollovers');
        }

        if ($state->count < $this->limit) {
            ++$state->count;
            $this->counters->add('allowed');

            return new Decision(
                allowed: true,
                remaining: $this->limit - $state->count,
                retryAfterMs: 0,
                limit: $this->limit,
            );
        }
```

[`src/RateLimiter/FixedWindowCounter.php` L46-L72](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/FixedWindowCounter.php#L46-L72)

Окно дискретно: `intdiv(nowMs, windowMs)`. В момент 1000 начинается новое окно,
и счётчик обнуляется, даже если девятьсот девяносто девять миллисекунд назад уже
было потрачено десять запросов. Именно это увидит эксперимент `rate:burst`.

### 2.4. Sliding log

Sliding log хранит кольцевой буфер принятых отметок времени. Перед решением он
выбрасывает всё, что вышло за окно. Точность здесь не приблизительная: это
оракул для остальных политик.

```php
    public function attempt(string $key, int $nowMs): Decision
    {
        $this->counters->add('attempts');
        $state = $this->states[$key] ?? null;

        if ($state === null) {
            $state = new SlidingWindowState($this->limit);
            $this->states[$key] = $state;
            $this->counters->add('createdKeys');
        }

        $this->prune($state, $nowMs);

        if ($state->count < $this->limit) {
            $index = ($state->head + $state->count) % $this->limit;
            $state->timestamps[$index] = $nowMs;
            ++$state->count;
            $this->counters->add('allowed');

            return new Decision(
                allowed: true,
                remaining: $this->limit - $state->count,
                retryAfterMs: 0,
                limit: $this->limit,
            );
        }

        $this->counters->add('denied');

        return new Decision(
            allowed: false,
            remaining: 0,
            retryAfterMs: max(0, $state->timestamps[$state->head] + $this->windowMs - $nowMs),
            limit: $this->limit,
        );
    }
```

[`src/RateLimiter/SlidingWindowLog.php` L46-L81](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/SlidingWindowLog.php#L46-L81)

Кольцевой буфер имеет фиксированный размер `limit`, поэтому вставка и удаление —
O(1), а память — O(limit) на ключ. Это важно: «точный» не значит «безграничный».
При лимите 10 000 на ключ счётчик съест гораздо больше памяти, чем при
лимите 10.

```php
    private function prune(SlidingWindowState $state, int $nowMs): void
    {
        while ($state->count > 0 && $state->timestamps[$state->head] <= $nowMs - $this->windowMs) {
            $state->head = ($state->head + 1) % $state->limit;
            --$state->count;
            $this->counters->add('expiredEntries');
        }
    }
```

[`src/RateLimiter/SlidingWindowLog.php` L98-L105](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/SlidingWindowLog.php#L98-L105)

Условие `<=` задаёт полуоткрытое окно: запрос ровно на границе уже не считается
принятым. Это осознанный выбор; главное, чтобы он был одинаковым в тестах,
эксперименте и документации.

### 2.5. Sliding counter

Sliding counter заменяет список двумя числами. Он оценивает, какая доля
предыдущего окна ещё «перекрывает» текущий момент, и складывает её с текущим
счётчиком.

```php
        $elapsedFraction = ($nowMs - $state->windowStartMs) / $this->windowMs;
        $weightedPrevious = $state->previousCount * (1.0 - $elapsedFraction);
        $estimated = $weightedPrevious + $state->currentCount;

        if ($estimated < $this->limit) {
            ++$state->currentCount;
            $this->counters->add('allowed');

            return new Decision(
                allowed: true,
                remaining: max(0, $this->limit - (int) ceil($weightedPrevious) - $state->currentCount),
                retryAfterMs: 0,
                limit: $this->limit,
            );
        }
```

[`src/RateLimiter/SlidingWindowCounter.php` L65-L79](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/RateLimiter/SlidingWindowCounter.php#L65-L79)

Оценка предполагает, что запросы в предыдущем окне распределены равномерно. Это
не факт о потоке, а допущение модели. Если десять запросов пришли в последнюю
миллисекунду окна, интерполяция ошибётся. Лаборатория не прячет ошибку: она
сравнивает каждое решение с точным логом и считает false allows и false denies.

---

## 3. Эксперимент

### 3.1. Всплеск на границе

`make experiment ARGS="rate:burst"` ставит по десять запросов в шесть моментов:
999, 1000, 1999, 2000, 2999 и 3000 мс. Лимит — 10, окно — 1000 мс, пополнение
bucket — 10 токенов в секунду.

```bash
make experiment ARGS="rate:burst"
```

| политика        | допущено | отклонено | максимум в любом окне |
| --------------- | -------: | --------: | --------------------: |
| token-bucket    |       30 |        30 |                    10 |
| fixed-window    |       40 |        20 |                    20 |
| sliding-log     |       30 |        30 |                    10 |
| sliding-counter |       30 |        30 |                    10 |

Fixed window допустил 40 запросов и достиг 20 в одном скользящем окне — ровно
двойной бюджет на границе. Остальные три политики удержали максимум на десяти.
При этом sliding counter и token bucket пришли к одинаковому итогу по-разному:
bucket копил токены между всплесками, counter взвешивал предыдущее окно.

Тридцать допущенных из шестидесяти — это не «половина трафика». Это следствие
расписания: после каждого всплеска бюджет исчерпан, а до следующего успевает
восстановиться ровно один. Эксперимент проверяет не пропускную способность, а
форму допуска.

### 3.2. Точность против оракула

`rate:accuracy` генерирует 2000 запросов со случайными интервалами 0–20 мс.
Точный sliding log служит оракулом. Для каждого запроса сравниваются решения
всех политик.

```bash
make experiment ARGS="rate:accuracy"
```

| политика        | допущено | ложных допусков | ложных отказов |
| --------------- | -------: | --------------: | -------------: |
| sliding-log     |      200 |               0 |              0 |
| fixed-window    |      200 |             151 |            151 |
| sliding-counter |      199 |             165 |            166 |

Fixed window допустил столько же запросов, сколько оракул, но 302 решения из
2000 были неверными: 151 раз он пропустил запрос, который оракул бы отклонил, и
151 раз отклонил тот, который оракул бы пропустил. Совпадение итогового числа
ничего не говорит о корректности. Это главный результат главы: считать надо не
только «сколько прошло», но и «сколько решений было правильным».

Sliding counter ошибся 331 раз и допустил на один запрос меньше. Он дешевле лога
по памяти, но его ошибка не исчезает от увеличения выборки — она следует из
модели интерполяции.

### 3.3. Память на ключ

`rate:memory` заводит 10 000 ключей и по десять попыток на каждый. Для каждой
политики измеряется прирост памяти PHP-аллокатора, а не RSS процесса.

```bash
make experiment ARGS="rate:memory"
```

| политика        |   всего | байт на ключ |
| --------------- | ------: | -----------: |
| token-bucket    | 1.5 MiB |        158.3 |
| fixed-window    | 1.4 MiB |        145.7 |
| sliding-log     | 5.3 MiB |        553.8 |
| sliding-counter | 1.6 MiB |        168.3 |

Sliding log стоит в 3.4–3.8 раза дороже остальных на ключ, потому что хранит
десять отметок времени. Разница между token bucket и sliding counter — 10 байт
на ключ; это цена двух счётчиков и начала окна. Fixed window дешевле всех, но
именно он показал двойной бюджет на границе.

Числа описывают PHP-аллокатор в контейнере, а не универсальную стоимость
структуры. В Redis или в шардированном счётчике единицей будет запись и сетевой
вызов, а не хеш-бакет.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="rate-limiter --repetitions=3"
```

| операция              | итерации | медиана, мс |   ops/sec |
| --------------------- | -------: | ----------: | --------: |
| unlimited attempt     |   20 000 |       4.786 | 4 178 746 |
| token bucket allow    |   20 000 |      10.705 | 1 868 344 |
| fixed window allow    |   20 000 |       9.780 | 2 045 033 |
| sliding log allow     |   20 000 |      10.602 | 1 886 385 |
| sliding counter allow |   20 000 |      10.656 | 1 876 958 |
| token bucket deny     |   20 000 |       6.105 | 3 276 093 |
| fixed window deny     |   20 000 |       6.403 | 3 123 373 |
| sliding log deny      |   20 000 |       6.778 | 2 950 922 |
| sliding counter deny  |   20 000 |       6.911 | 2 893 798 |

Пустой вызов `unlimited` стоит около 4.8 мс на 20 000 попыток. Любая реальная
политика добавляет 5–6 мс на создание состояния и проверку. Отказ дешевле
допуска, потому что не нужно обновлять счётчик или вставлять отметку. Разница
между политиками на этом масштабе меньше, чем разница между «есть состояние» и
«нет состояния».

Это не рейтинг алгоритмов. В контейнере на macOS виртуализация и планировщик
дают разброс, а PHP-аллокатор добавляет свой шум. Сравнивать имеет смысл порядок
величин и форму зависимости, а не миллисекунды между двумя соседними строками.

---

## 5. Что ломается

**Часы.** `nowMs` приходит снаружи. Если два инстанса видят разное время, token
bucket начислит разное пополнение, а окна разъедутся. В распределённой системе
нужен общий источник времени или логические часы; лаборатория этого не
моделирует.

**Ключ.** Лимит на арендатора, пользователя, IP или маршрут — это разные
системы. Если ключ включает только IP, за NAT один клиент расходует бюджет
других. Если только арендатора, один шумный пользователь блокирует всех. В
сценарии главы 8 ключ — `tenant-N`, потому что продукт мультитенантный.

**Граница окна.** Fixed window всегда будет пропускать двойной бюджет на стыке
окон. Уменьшение окна уменьшает абсолютный всплеск, но не убирает его. Точный
логический ответ требует либо лога, либо эквивалентной структуры.

**Память.** Sliding log при лимите 10 000 на ключ хранит до 10 000 отметок. При
миллионе активных ключей это уже не «маленький счётчик». Sliding counter
обменивает точность на константную память, и этот обмен надо называть явно.

**Всплеск token bucket.** Ёмкость — это разрешённый burst. Если downstream не
готов к нему, лимитер пропустит всплеск, который выглядит как нарушение
«сглаженного» лимита. Настройка capacity и refill — часть контракта с
зависимостью, а не только с клиентом.

**Оценка.** Sliding counter может и пропустить лишнее, и отказать зря.
`retryAfterMs` — подсказка; при конкуренции она устаревает. Клиент не должен
трактовать её как гарантию.

**Состояние.** В лаборатории карта живёт в памяти процесса. В production она
либо шардируется, либо реплицируется. Тогда появляются новые отказы: потеря
счётчика при падении узла, гонка при `INCR`, задержка репликации. Этих сценариев
здесь нет, и книга их не приписывает коду.

---

## 6. Как это проверено

- `tests/RateLimiter/RateLimiterTest.php` проверяет границы окна, пустые ключи,
  сброс, `trackedKeys()`, поведение при откате времени и точность скользящего
  лога против массива-оракула.
- Эксперимент `rate:accuracy` сравнивает каждое решение с точным логом, а не
  только итоговое число допусков.
- `rate:burst` фиксирует максимальное число принятых запросов в любом скользящем
  окне, поэтому двойной бюджет виден как число, а не как рассказ.
- `rate:memory` измеряет PHP-аллокатор в отдельном процессе и не выдаёт его за
  RSS.
- Бенчмарк запускает warm-up и повторения, печатает медиану, p95, min/max, MAD и
  окружение.

К моменту завершения главы набор содержал 74 теста; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` в контейнере включает PHPUnit,
PHPStan level 8 и PHP-CS-Fixer.

---

## 7. Что запомнить

1. Лимитер — это модель трафика, а не только счётчик. Fixed window, token bucket
   и sliding log описывают разные допустимые графики.
2. Совпадение числа допущенных запросов не доказывает корректность. Сравнивайте
   решения с оракулом и считайте ложные допуски и отказы.
3. Точность стоит памяти. Sliding log платит O(limit) на ключ; sliding counter
   платит точностью.
4. Время и ключ — часть контракта. Без явного времени и области действия лимитер
   невозможно ни протестировать, ни объяснить.
5. `retryAfterMs` — подсказка. Конкурентный клиент может занять бюджет первым.
6. В распределённой системе к алгоритму добавляются общие часы, общее состояние
   и сетевые отказы. Лаборатория измеряет только локальную политику.

---

## 8. Упражнения

1. Поставьте `limit=100`, `window=1000` и повторите `rate:memory`. Во сколько
   раз вырастет память sliding log? Останется ли token bucket константным?
2. Запустите `rate:accuracy` с `--requests=10000` и `--seed=...`. Уменьшится ли
   доля ошибок fixed window? Почему?
3. Добавьте пятый момент в `rate:burst` — 3999 и 4000 мс. Как изменится
   соотношение допусков?
4. Сделайте ключ составным: арендатор + маршрут. Какие новые вопросы о бюджете
   придётся решить?
5. Предложите, как перенести sliding counter в Redis, не потеряв атомарность
   между чтением и записью. Какие команды понадобятся?

---

Дальше — URL Shortener. Лимитер решает, кого пустить; следующая глава решает,
что вернуть допущенному запросу и как не создать одну и ту же ссылку дважды.
