# Глава 2. URL Shortener: ёмкость, коллизии и идемпотентность

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: rate limiter (глава 1). Следующая глава: notification
> service (глава 3).

Короткая ссылка кажется простой задачей: взять число, перевести его в base62 и
вернуть семь символов. Настоящие вопросы начинаются позже. Сколько ссылок
поместится в семь символов? Что делать, если два генератора выдали один код? Как
не создать вторую строку, когда пользователь дважды отправил одну и ту же URL?
Где хранится соответствие «арендатор + URL → ссылка» и сколько оно стоит?

В этой главе короткий код — это не косметика URL, а идентификатор с ограниченной
ёмкостью. Мы посмотрим, как sequential, random и hashed генераторы ведут себя
при росте заполнения, почему случайный генератор упирается в день рождения
раньше теоретической ёмкости и почему идемпотентность — это второй индекс, а не
бесплатное свойство.

---

## 1. Проблема

Семь символов base62 дают `62^7 = 3 521 614 606 208` комбинаций — около 3.5
триллиона. Для сервиса, который никогда не создаст столько ссылок, этого
достаточно. Но «достаточно» — не то же самое, что «безопасно при случайной
выдаче». Если каждый новый код выбирается равномерно из всего пространства, то
вероятность совпадения растёт как квадрат числа уже занятых кодов. Это задача о
дне рождения: при 20 000 ссылок в пространстве из 238 328 кодов заполнение
составляет 8.4%, а неожиданных совпадений уже сотни.

Sequential-генератор обходит проблему координацией: один счётчик выдаёт 0, 1, 2,
… и не повторяется, пока не кончится пространство. За это платят
предсказуемостью и зависимостью от одного писателя. Random-генератор не требует
координации, но требует проверки и повторной попытки. Hash-генератор делает код
функцией от арендатора и URL: одинаковый вход даёт одинаковый выход без чтения
базы, но усечение хеша снова создаёт коллизии.

Второй вопрос — идемпотентность. Пользователь может отправить одну и ту же URL
дважды: из-за двойного клика, retry после таймаута или просто потому, что
фронтенд не заблокировал кнопку. Если сервис каждый раз создаёт новую строку,
база растёт быстрее, чем число уникальных ссылок, а клиент получает разные коды
для одного ресурса. Нужен индекс по `(tenant_id, long_url)`, который вернёт
существующую ссылку до выделения нового кода.

Третий вопрос — цена. Второй индекс ускоряет повторную отправку, но занимает
память. Лаборатория измеряет и объекты, и оба индекса, чтобы утверждение
«идемпотентность дешевле» не осталось лозунгом.

---

## 2. Устройство

### 2.1. Base62 и ёмкость

Алфавит — цифры, заглавные и строчные латинские буквы. Кодирование идёт от
младшего разряда, затем результат дополняется нулями слева до фиксированной
длины. Декодер отвергает символы вне алфавита и переполнение целого.

```php
    public static function encode(int $value, int $length = 0): string
    {
        if ($value < 0) {
            throw new InvalidArgumentException('Cannot encode a negative value');
        }

        if ($length < 0) {
            throw new InvalidArgumentException('Length cannot be negative');
        }

        $encoded = '';

        do {
            $encoded = self::ALPHABET[$value % 62] . $encoded;
            $value = intdiv($value, 62);
        } while ($value > 0);

        if ($length === 0) {
            return $encoded;
        }

        if (strlen($encoded) > $length) {
            throw new InvalidArgumentException(sprintf(
                'Value %s does not fit into %d base62 characters',
                $encoded,
                $length,
            ));
        }

        return str_pad($encoded, $length, '0', STR_PAD_LEFT);
    }
```

[`src/Shortener/Base62.php` L22-L52](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/Base62.php#L22-L52)

`capacity()` — это не отдельная таблица, а арифметика с проверкой переполнения.
Именно она не даёт объявить код длиной, которую PHP не сможет представить целым.

```php
    public static function capacity(int $length): int
    {
        if ($length < 1) {
            throw new InvalidArgumentException('Code length must be at least 1');
        }

        $capacity = 1;

        for ($i = 0; $i < $length; ++$i) {
            if ($capacity > intdiv(PHP_INT_MAX, 62)) {
                throw new InvalidArgumentException(sprintf('%d base62 characters exceed the integer range', $length));
            }

            $capacity *= 62;
        }

        return $capacity;
    }
```

[`src/Shortener/Base62.php` L79-L96](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/Base62.php#L79-L96)

Семь символов — это не «магическое число» из статьи. Это компромисс между длиной
URL и запасом ёмкости. В лаборатории длина по умолчанию равна семи, но
эксперименты используют два и три символа, чтобы край пространства был достижим
за миллисекунды, а не за годы.

### 2.2. Три генератора

Sequential-генератор — контроль. Он начинает с нуля (или с заданного значения) и
не зависит от tenant или URL. Random-генератор берёт равномерное число из
пространства. Hash-генератор смешивает tenant, URL и номер попытки.

```php
/**
 * A random identifier drawn from the code space.
 *
 * There is no coordination and no obvious order, but two writers can draw the
 * same value. The shortener must check the repository and retry; the
 * `shorten:collisions` experiment shows how the birthday bound catches up
 * with a short code.
 */
final class RandomIdGenerator implements IdGenerator
{
    public function __construct(
        private readonly int $space,
        private readonly DeterministicRandom $random,
    ) {
        if ($this->space < 1) {
            throw new InvalidArgumentException('Identifier space must be at least 1');
        }
    }

    public function next(string $tenantId, string $longUrl, int $attempt = 0): int
    {
        return $this->random->intBetween(0, $this->space - 1);
    }
}
```

[`src/Shortener/RandomIdGenerator.php` L10-L33](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/RandomIdGenerator.php#L10-L33)

Обратите внимание: `$attempt` принимается, но не используется. Случайному
генератору не нужно менять вход — следующая попытка и так даёт новое число.
Hash-генератору он нужен обязательно.

```php
/**
 * An identifier derived from tenant and URL.
 *
 * The first attempt for a link is a pure function of tenant and URL, so the
 * same input maps to the same value without a lookup. Different URLs can still
 * collide after the hash is truncated to the code space; the attempt number is
 * mixed into the input so the shortener's retry loop can actually escape a
 * collision instead of hashing to the same taken code twenty times.
 */
final class HashIdGenerator implements IdGenerator
{
    public function __construct(
        private readonly int $space,
    ) {
        if ($this->space < 1) {
            throw new InvalidArgumentException('Identifier space must be at least 1');
        }
    }

    public function next(string $tenantId, string $longUrl, int $attempt = 0): int
    {
        return Hashers::hash64($tenantId . "\0" . $longUrl . "\0" . $attempt) % $this->space;
    }
}
```

[`src/Shortener/HashIdGenerator.php` L10-L33](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/HashIdGenerator.php#L10-L33)

Разделитель `"\0"` не даёт склеиться разным парам в одну строку: `("a", "bc")` и
`("ab", "c")` должны давать разные входы. Номер попытки входит в хеш, поэтому
повторный вызов после коллизии действительно меняет кандидата. Без этого
hash-генератор двадцать раз возвращал бы один и тот же занятый код.

### 2.3. Сервис: сначала идемпотентность, потом код

`UrlShortener::shorten()` сначала ищет существующую ссылку по паре
`(tenantId, longUrl)`. Только если её нет, он выделяет код. Это порядок важен:
если сначала выделить код, а потом обнаружить дубликат, код придётся
«возвращать» или оставлять дыру в пространстве.

```php
    public function shorten(
        string $tenantId,
        string $longUrl,
        string $title = '',
        array $tags = [],
        int $createdAtMs = 0,
        ?string $alias = null,
    ): ShortenResult {
        $tenantId = trim($tenantId);
        $longUrl = trim($longUrl);

        if ($tenantId === '') {
            throw new InvalidArgumentException('Tenant id cannot be empty');
        }

        if (!$this->isHttpUrl($longUrl)) {
            throw new InvalidArgumentException(sprintf('Not an HTTP(S) URL: %s', $longUrl));
        }

        $this->counters->add('shortenAttempts');
        $existing = $this->repository->findByUrl($tenantId, $longUrl);

        if ($existing !== null) {
            $this->counters->add('idempotentHits');

            return new ShortenResult($existing, false, 0);
        }

        $collisions = 0;

        if ($alias !== null) {
            if (preg_match('/^[0-9A-Za-z]{4,32}$/', $alias) !== 1) {
                throw new InvalidArgumentException('Alias must be 4-32 base62 characters');
            }

            if ($this->repository->findByCode($alias) !== null) {
                throw new DuplicateCode($alias);
            }

            $code = $alias;
        } else {
            $code = $this->generateCode($tenantId, $longUrl, $collisions);
        }
```

[`src/Shortener/UrlShortener.php` L48-L90](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/UrlShortener.php#L48-L90)

Пользовательский alias проверяется отдельно: он должен быть 4–32 символа base62
и не совпадать с существующим кодом. Это не «красивая строка» без ограничений —
иначе alias мог бы сломать разбор URL или занять чужой код.

### 2.4. Retry budget

Генерация кода — цикл с ограниченным числом попыток. Каждая занятая попытка
увеличивает счётчик коллизий. Если бюджет кончился, сервис выбрасывает
`CodeSpaceExhausted`, а не зависает в бесконечном поиске.

```php
    private function generateCode(string $tenantId, string $longUrl, int &$collisions): string
    {
        for ($attempt = 0; $attempt < $this->maxAttempts; ++$attempt) {
            $id = $this->ids->next($tenantId, $longUrl, $attempt);
            $this->counters->add('generatedIds');

            try {
                $code = Base62::encode($id, $this->codeLength);
            } catch (InvalidArgumentException) {
                throw new CodeSpaceExhausted(sprintf(
                    'Identifier %d does not fit into %d base62 characters',
                    $id,
                    $this->codeLength,
                ));
            }

            if ($this->repository->findByCode($code) === null) {
                return $code;
            }

            ++$collisions;
            $this->counters->add('collisions');
        }

        throw new CodeSpaceExhausted(sprintf(
            'No free code after %d attempts; the code space is too full for this generator',
            $this->maxAttempts,
        ));
    }
```

[`src/Shortener/UrlShortener.php` L133-L161](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/UrlShortener.php#L133-L161)

Бюджет — часть контракта. При заполнении 90% пространства вероятность случайно
попасть в свободный код падает, и фиксированные двадцать попыток могут не найти
место задолго до теоретической ёмкости. Это видно в эксперименте
`shorten:space`: random-генератор остановился на 2 770 из 3 844 кодов, хотя
пространство ещё не было заполнено.

### 2.5. Два индекса

Репозиторий хранит одну и ту же ссылку дважды: по коду и по паре
`(tenantId, longUrl)`. Ключ второго индекса склеивается через `"\0"`, чтобы
границы частей не размывались.

```php
    public function findByUrl(string $tenantId, string $longUrl): ?ShortLink
    {
        $this->counters->add('urlReads');

        return $this->byUrl[$this->urlKey($tenantId, $longUrl)] ?? null;
    }

    public function save(ShortLink $link): void
    {
        if (isset($this->byCode[$link->code])) {
            throw new DuplicateCode($link->code);
        }

        $this->byCode[$link->code] = $link;
        $this->byUrl[$this->urlKey($link->tenantId, $link->longUrl)] = $link;
        $this->counters->add('writes');
    }
```

[`src/Shortener/InMemoryLinkRepository.php` L46-L62](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Shortener/InMemoryLinkRepository.php#L46-L62)

`save()` проверяет только `byCode`. Если два потока одновременно прошли
`findByUrl()` и оба дошли до `save()`, второй перезапишет `byUrl` и создаст
вторую строку с другим кодом. В лаборатории поток один, поэтому гонки нет; в
production нужен уникальный индекс в базе или транзакция. Книга не выдаёт
однопоточную реализацию за конкурентную.

---

## 3. Эксперимент

### 3.1. Коллизии при 8.4% заполнения

`shorten:collisions` создаёт 20 000 ссылок в трёхсимвольном пространстве (238
328 кодов). Sequential — контроль, random и hash — испытуемые. Бюджет попыток
— 20.

```bash
make experiment ARGS="shorten:collisions"
```

| генератор  | создано | коллизий | заполнение | birthday-оценка |
| ---------- | ------: | -------: | ---------: | --------------: |
| sequential |  20 000 |        0 |       8.4% |               — |
| random     |  20 000 |      878 |       8.4% |             839 |
| hash       |  20 000 |      881 |       8.4% |             839 |

Sequential не столкнулся ни с одной коллизией, потому что один счётчик владеет
всем пространством. Random и hash заплатили 878 и 881 повторных попыток. Оценка
дня рождения — 839 — близка к обоим результатам; расхождение объясняется
конечной выборкой и тем, что hash не идеально равномерен.

Коллизия здесь не ошибка. Это нормальная стоимость случайной выдачи, которую
сервис обязан обработать. Ошибка — считать, что семи символов достаточно, и не
иметь цикла повторной попытки.

### 3.2. Край пространства

`shorten:space` сначала печатает арифметику ёмкости, затем пытается создать 5
000 ссылок в двухсимвольном пространстве из 3 844 кодов.

```bash
make experiment ARGS="shorten:space"
```

| длина |    ёмкость `62^n` |
| ----: | ----------------: |
|     1 |                62 |
|     2 |             3 844 |
|     3 |           238 328 |
|     4 |        14 776 336 |
|     5 |       916 132 832 |
|     6 |    56 800 235 584 |
|     7 | 3 521 614 606 208 |

Реальный прогон:

| генератор  | создано | заполнение | коллизий | исчерпано на попытке |
| ---------- | ------: | ---------: | -------: | -------------------: |
| sequential |   3 844 |     100.0% |        0 |                3 845 |
| random     |   2 770 |      72.1% |    2 223 |                2 771 |
| hash       |   2 794 |      72.7% |    2 200 |                2 795 |

Sequential дошёл до последнего кода и остановился ровно на следующем. Random и
hash остановились раньше: при 72% заполнения двадцать попыток подряд всё чаще
попадают в занятые коды. Ёмкость — это верхняя граница пространства, а не
практическая точка заполнения для генератора с фиксированным бюджетом.

Тот же эффект возникнет и на семи символах, но пространство в 91 636 раз больше,
поэтому до него практически не доходит. Лаборатория использует маленькое
пространство, чтобы край был достижим в одном прогоне, а не чтобы предсказать
поведение продакшена в абсолютных числах.

### 3.3. Идемпотентность

`shorten:idempotency` отправляет 50 000 запросов по Zipf-распределению на 5 000
URL и три арендатора.

```bash
make experiment ARGS="shorten:idempotency"
```

| метрика                          | значение |
| -------------------------------- | -------: |
| отправок                         |   50 000 |
| уникальных строк `(tenant, URL)` |    7 172 |
| созданных ссылок                 |    7 172 |
| идемпотентных попаданий          |   42 828 |
| усиление строк без проверки      |    6.97x |
| записей в репозиторий            |    7 172 |
| чтений URL-индекса               |   50 000 |

Идемпотентность убрала 42 828 потенциальных строк, но не сделала повторную
отправку бесплатной: каждый запрос всё равно читает URL-индекс. Экономия — это
второй код, вторая строка и вторая запись, а не весь путь целиком.

Обратите внимание на 6.97x. Это не «сжатие в семь раз» для любого потока. Это
следствие конкретного Zipf-распределения с alpha 1.1: несколько ссылок
популярны, хвост длинный. При равномерном потоке усиление было бы ближе к
отношению запросов к уникальным URL.

### 3.4. Память

`shorten:memory` хранит 20 000 ссылок в трёх схемах: только объекты, индекс по
коду и оба индекса репозитория.

```bash
make experiment ARGS="shorten:memory"
```

| схема             |   всего | байт на ссылку |
| ----------------- | ------: | -------------: |
| только объекты    | 5.8 MiB |          303.7 |
| индекс по коду    | 6.3 MiB |          329.5 |
| код + URL-индекс  | 9.1 MiB |          475.8 |
| только URL-индекс | 2.8 MiB |          146.3 |

Второй индекс стоит 146.3 байта на ссылку — почти половину стоимости объекта.
Идемпотентность покупается памятью, а не берётся из воздуха. В базе данных
единицей будет страница и запись индекса, а не хеш-бакет PHP; но направление
затрат сохраняется.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="url-shortener --repetitions=3"
```

| операция               | итерации | медиана, мс |    ops/sec |
| ---------------------- | -------: | ----------: | ---------: |
| base62 encode 7        |  100 000 |      29.650 |  3 372 643 |
| base62 decode 7        |  100 000 |      30.834 |  3 243 160 |
| sequential id          |  100 000 |       5.077 | 19 695 217 |
| random id              |  100 000 |      31.609 |  3 163 627 |
| hash id                |  100 000 |      24.529 |  4 076 786 |
| shorten new link       |    5 000 |       8.089 |    618 155 |
| shorten idempotent hit |   50 000 |      36.392 |  1 373 930 |
| resolve hit            |  100 000 |      28.697 |  3 484 680 |
| resolve miss           |  100 000 |      25.754 |  3 882 848 |

Sequential-генератор в шесть раз быстрее random и в пять раз быстрее hash на 100
000 операций, потому что это инкремент счётчика. Random платит за PRNG, hash —
за хеширование строки. Разница между encode и decode меньше, чем разброс между
запусками.

Создание новой ссылки (8.089 мс на 5 000) включает выделение кода, построение
объекта и две записи в индексы. Идемпотентное попадание (36.392 мс на 50 000)
обходится без записи, но всё равно ищет по URL. Разрешение по коду (28.697 мс на
100 000) дешевле создания, потому что это одно чтение.

Цифры снова описывают конкретный контейнер. Они не доказывают, что hash лучше
random в production: hash зависит от качества хеша и распределения tenant/URL, а
random — от качества PRNG и конкурентности.

---

## 5. Что ломается

**Ёмкость ≠ заполнение.** Семь символов дают 3.5 триллиона комбинаций, но
случайный генератор с бюджетом двадцати попыток не заполнит их полностью. При
проектировании надо считать не только `62^n`, но и ожидаемое число коллизий при
планируемом объёме.

**Retry budget.** Слишком маленький бюджет исчерпывает пространство раньше
времени; слишком большой превращает коллизию в долгий цикл. Бюджет должен быть
явной настройкой, а не случайным числом в коде.

**Гонка идемпотентности.** Проверка «нашёл — создал» не атомарна. Два
параллельных запроса могут оба не найти ссылку и оба сохранить её. В лаборатории
один поток; в production нужен уникальный индекс `(tenant_id, long_url)` и
обработка конфликта вставки.

**Нормализация URL.** Сервис делает `trim()`, но не канонизирует URL: `http` и
`https`, trailing slash, порядок query-параметров и фрагмент считаются разными
строками. Это осознанное ограничение. Идемпотентность работает для точного
совпадения, а не для «того же ресурса».

**Область tenant.** Один и тот же URL у двух арендаторов даёт две ссылки, потому
что ключ включает tenant. Это правильно для мультитенантного продукта, но
означает, что глобальной дедупликации нет.

**Alias.** Пользовательский alias глобально уникален по коду, а не по паре
tenant + alias. Если продукту нужны одинаковые alias у разных арендаторов,
текущая схема этого не разрешает.

**Память.** Два индекса стоят примерно 476 байт на ссылку в PHP-массивах. При
миллионах ссылок это уже гигабайты. В реальной системе индексы живут в базе или
в key-value хранилище, и цена считается в страницах и IOPS, а не в байтах
аллокатора.

**Срок жизни.** В лаборатории ссылки не истекают и не удаляются. Нет TTL, нет
сборки мусора, нет переиспользования кодов. В production это отдельные
механизмы, которые влияют на ёмкость и на безопасность.

---

## 6. Как это проверено

- `tests/Shortener/Base62Test.php` проверяет round-trip, фиксированную длину,
  лексикографический порядок, отказ при переполнении и неверных символах.
- `tests/Shortener/GeneratorsTest.php` проверяет детерминизм random и hash,
  границы пространства, разделение tenant и повторный хеш на следующей попытке.
- `tests/Shortener/InMemoryLinkRepositoryTest.php` проверяет оба индекса,
  tenant-scoped поиск по URL, счётчики и отказ при дубликате кода.
- `tests/Shortener/UrlShortenerTest.php` покрывает alias, коллизии, исчерпание
  бюджета, идемпотентность, неверный ввод и разрешение ссылок.
- Эксперименты `shorten:collisions`, `shorten:space`, `shorten:idempotency` и
  `shorten:memory` дают числа, которые можно перепроверить одной командой.

К моменту завершения главы набор содержал 143 теста; финальный набор проекта —
396 тестов и 19 064 assertions. PHPStan level 8 и PHP-CS-Fixer входят в
`make check`.

---

## 7. Что запомнить

1. Base62 — это кодирование, а не генерация. Ёмкость считается как `62^n`, но
   практическая заполняемость зависит от генератора и бюджета попыток.
2. Sequential не коллизионен, пока один счётчик владеет пространством; random и
   hash платят цену дня рождения.
3. Коллизия — штатное событие. Сервис должен уметь повторить попытку и явно
   исчерпать бюджет.
4. Hash-генератор должен смешивать номер попытки, иначе он не сможет выйти из
   коллизии.
5. Идемпотентность — это второй индекс по `(tenant, URL)`. Она экономит строки,
   но стоит памяти и чтения на каждом запросе.
6. Проверка и вставка должны быть атомарны в конкурентной системе. Однопоточная
   лаборатория этого не доказывает.
7. Нормализация URL — отдельное решение. Без неё «та же ссылка» означает «точно
   та же строка».

---

## 8. Упражнения

1. Запустите `shorten:collisions` с `--code-length=4` и `--links=100000`. Во
   сколько раз упадёт число коллизий? Сойдётся ли оно с формулой дня рождения?
2. Уменьшите `--max-attempts` до 5 и повторите `shorten:space`. На каком
   заполнении random остановится теперь?
3. Добавьте в `shorten:idempotency` четвёртый арендатор. Как изменится число
   строк? Почему не втрое?
4. Предложите канонизацию URL для `http/https`, trailing slash и query-строк.
   Какие случаи она сломает?
5. Реализуйте уникальный индекс `(tenant, URL)` в SQLite и сравните стоимость
   вставки с PHP-массивом. Что станет единицей измерения?
6. Придумайте схему alias, уникальных в пределах арендатора, не ломая глобальную
   уникальность кода.

---

Дальше — Notification Service. Короткая ссылка создаёт событие; следующая глава
решает, кому и по каким каналам его отправить, как подавить дубликаты и что
делать с тихими часами.
