# Глава 19. Анализ текста: Unicode, токены, стемминг

> Проект: [`php-mini-search`](https://github.com/Researcher86/php-mini-search)
> (релиз `v1.0.0`, коммит `7ecddc0`). Нужно знать: журнал как единственное
> надёжное состояние и то, что индекс строится из него при каждом старте (→
> глава 18). Пригодится дальше: инвертированный индекс и BM25 (→ глава 20),
> версии, надгробия и переиндексация (→ глава 21).
>
> Код этой главы — `src/Analysis/`: `Analyzer` на 96 строк и два рукописных
> порта Snowball на 239 и 354 строки. Тестов на них больше, чем кода: 92 434
> пары «слово → основа» из официальных словарей.

Поиск начинается не с индекса, а с вопроса: **какие две строки считаются одним и
тем же словом?** Человек, набравший `кабель usb`, и документ с заголовком
`Кабели USB-C` не имеют ни одного общего байта — кроме идеи, что это одно и то
же. Всё остальное в поисковой системе (постинги, BM25, выдача) работает уже с
тем, что получилось здесь. Поэтому глава про анализ текста стоит перед главой
про индекс, а не после неё.

## 1. Проблема

Поиск по словам — это проверка равенства. Значит, всё, что люди пишут
по-разному, должно стать одинаковым **до** сравнения, и ровно одинаковым с обеих
сторон: в документе и в запросе. Любая асимметрия — это не «неточный поиск», а
тихий отказ: документ есть, а запрос его не находит.

Различий, которые надо свести, пять, и они разной природы.

**Регистр и орфография.** `Кабель`, `кабель`, `КАБЕЛЬ` — одно слово; `ёжик` и
`ежик` — тоже, потому что в русской типографике `ё` пишут необязательно. Это
складывается двумя строками — `mb_strtolower()` и заменой `ё` на `е`, — но
только если сделать это **до** стемминга: правила Snowball рассчитаны на нижний
регистр, а `ё` стеммер складывает в `е` сам, уже внутри `stem()`.

**Unicode-формы.** Одна и та же буква может быть записана разными
последовательностями байтов: `й` — это `U+0439` или `и` + `U+0306`; `ﬁ` —
лигатура `U+FB01` или `f` + `i`; `ＵＳＢ` — полноширинные символы
`U+FF35 U+FF33 U+FF22`. Визуально это одно и то же, побайтно — разное. Browsers
и IME выдают разные формы для одного и того же текста, а копипаст из PDF —
третью. Лечится нормализацией, и здесь важно, какой именно: NFKC, а не NFC.

**Апострофы.** `Levi's`, `O’Neill`, `д’Артаньян` — апостроф может быть ASCII
(`'`), типографским (`’`, U+2019) или модификатором буквы (`ʼ`, U+02BC), и NFKC
их **не** сводит. Разбить по апострофу — получить термы `o` и `neill` и никогда
не найти `oneill`; оставить его внутри токена — тоже не найти, потому что в
поисковую строку апостроф почти никто не печатает.

**Границы токенов.** `USB-C` — это `usb` и `c`, а `usb3` — один токен. `3.1` —
это `3` и `1`, а не версия. Дефис, точка, запятая, эмодзи — всё это разделители,
и это осознанное решение, а не следствие регулярного выражения: альтернатива
(держать `usb-c` и `usb-c` вместе) потребовала бы словаря того, какие дефисы
значимы.

**Морфология.** Русский существительное имеет около дюжины форм: `кабель`,
`кабеля`, `кабели`, `кабелей`, `кабелем`, `кабелю`. При семантике «в документе
должны быть все термы запроса» (AND, → глава 20) запрос `сокет` не находит
документ со словом `сокетов` — и это не край, а обычный случай. Отсюда стемминг:
свести словоформы к общей основе.

Но у стемминга есть цена, и она определяет всю архитектуру вокруг него. **Он
меняет каждый терм.** Стоит поменять правило — и индекс, построенный старыми
правилами, больше не соответствует запросам, построенным новыми. В
`php-mini-search` это не катастрофа, потому что индекс производный: журнал
хранит исходный текст, при старте текст анализируется заново (→ глава 18). Смена
анализатора — это перезапуск, а не миграция. В системе, где индекс —
единственная копия данных, та же операция была бы переиндексацией всего корпуса.

И ещё одна цена, самая практичная: стемминг дорог. По замерам проекта
(`docs/BENCHMARKS.md`) анализ стоил 12 µs на токен, из которых **96% приходилось
на стемминг**, а 90% токенов в корпусе повторяются. Это наблюдение породило
последнее решение главы — кеш основ.

## 2. Устройство

### Пять шагов, один для документов и запросов

```text
text
 │
 │  1. NFKC            ﬁ → fi, ＵＳＢ → USB, и + U+0306 → й
 │  2. lowercase       Кабель → кабель
 │     ё → е           ёжик → ежик
 │  3. apostrophes     O’Neill → oneill, Levi's → levis
 │     (inside a word) 'quoted' → 'quoted' (edge: separator)
 │  4. tokens          maximal runs of \p{L} and \p{N}
 │
 ├── only Cyrillic?      → Snowball Russian
 ├── only ASCII a–z?     → Snowball English (Porter2)
 └── anything else       → unchanged: usb3, café, 東京, 3
 │
terms
duplicates kept — that is the term frequency
```

Симметрия — не следствие аккуратности, а контракт:

```text
title / body ──┐
               ├── Analyzer::terms() ──→ terms ──→ index (tf) / query (set)
query string ──┘
```

В `Corpus` это буквально две строки в двух местах: `put()` анализирует заголовок
и тело, `search()` — запрос, и оба зовут один и тот же объект `Analyzer`. Запрос
дополнительно проходит `array_unique()`, потому что запрос — это множество
термов, а документ — их последовательность: частоту терма в документе считают по
числу вхождений.

### Почему NFKC, а не NFC

NFC и NFKC обе приводят `и + U+0306` к `й`, но расходятся на **совместимых**
символах. NFKC дополнительно раскладывает лигатуры, полноширинные формы,
надстрочные индексы и типографские варианты. Для поиска это то, что нужно: `ﬁle`
и `file` — одно слово для читателя, `ＵＳＢ` и `USB` — тоже. Обратная сторона:
NFKC меняет и то, что человек, возможно, хотел различить (`²` → `2`, `①` → `1`),
но в поисковом индексе эти различия не несут смысла.

Нормализация — единственный шаг, который может **отказать**: если вход не
валидный UTF-8, `Normalizer::normalize()` вернёт `false`, и анализатор бросит
`InvalidArgumentException('Text is not valid UTF-8.')`. Это единственная ошибка,
которую `terms()` вообще способен вернуть.

### Что считается токеном

`[\p{L}\p{N}]+` — максимальные последовательности букв и цифр. Всё остальное
разделяет. Из этого следуют следствия, которые лучше знать заранее:

- `USB-C` → `usb`, `c`; `3.1` → `3`, `1`; `2 × 0` → `2`, `0`;
- `usb3` → `usb3` (один токен, и он не стеммится — см. ниже);
- `🔥 кабель 🔥` → `кабель` (эмодзи — разделители);
- `東京` → `東京` (один токен: CJK-сегментации нет);
- пустая строка и строка из пунктуации → `[]`.

Апостроф обрабатывается **до** токенизации, отдельным регулярным выражением с
просмотром назад и вперёд: удаляется только тот, что стоит между двумя буквами.
Поэтому `'quoted'` сохраняет кавычки как разделители (`quot`), а `O’Neill`
становится `oneill`. Именно этот порядок — сначала удалить внутренний апостроф,
потом резать на токены — делает `д’Артаньян` одним словом, а не двумя.

### Маршрутизация по письменности

После токенизации каждый токен уходит в один из трёх путей:

- `^\p{Cyrillic}+$` — русский стеммер;
- `^[a-z]+$` — английский стеммер (Porter2);
- всё остальное — **как есть**.

Третий путь — не лень, а граница применимости: Porter2 определён только для
`a`–`z`, а русские правила — для кириллицы. Токен `café` не английский (в нём
`é`), не русский и не цифра — он остаётся `café`, и запрос `cafe` его не найдёт.
Токен `usb3` остаётся `usb3`, поэтому `usb 3` его не найдёт. Это
задокументированные ограничения, и эксперимент 2 показывает оба.

### Русский стеммер: только окончания, только в RV

Порт `algorithms/russian.sbl` устроен вокруг двух понятий. **RV** — часть слова
после первой гласной; **R2** — область, полученная применением того же правила
дважды. Всё, что левее RV, не трогается никогда. Но и внутри RV окончание
удаляется не всегда: у части таблиц есть дополнительное условие. Поэтому `сон`
остаётся `сон`: кандидат `-н` входит в таблицу глагольных окончаний, которая
требует перед ним `а` или `я`, — а в `сон` перед `н` стоит `о`. У `дартаньян`
условие выполнено, и конечный `-н` уходит.

Внутри RV работает `among` из Snowball: окончания перебираются **от длинного к
короткому**, и как только найдено самое длинное совпадение, применяется только
его правило. Если условие этого правила не выполнено, более короткое окончание
**не** пробуется — именно так ведёт себя `among`, и именно это чаще всего ломают
при ручном порте.

Шаги алгоритма:

1. деепричастие (совершенного вида) — или, если его нет, возвратное `-ся/-сь`,
   затем прилагательное (с возможным причастием перед ним), затем глагол, затем
   существительное;
2. конечная `-и`;
3. словообразовательное `-ост(ь)`, но только если оно лежит в R2;
4. превосходная степень (`-ейш`), затем `-нн → -н`, затем конечный `ь`.

Позиции считаются **в символах**, а суффиксы режутся **в байтах**. Это выглядит
как несогласованность, но ею не является: полный UTF-8-суффикс может совпасть
только на границе символа, поэтому обе меры дают одно и то же место разреза.
Такой порт проще читать рядом с `.sbl`, где позиции тоже в символах.

### Английский стеммер: Porter2, а не Porter

Порт `english.sbl` — это Porter2. Отличие не косметическое: `generously` →
`generous`, `dying` → `die`, а `inning` и `proceed` не трогаются вовсе;
оригинальный алгоритм 1980 года даёт на этих словах другой результат.

Приём, который проходит через весь код, — маркер `Y`. В прелюдии согласная `y`
заменяется на заглавную `Y` (в начале слова и после гласной), и дальше все
проверки «гласная/согласная» работают с обычным набором `aeiouy`, не разбирая
случаев `say` и `cry` отдельно. В постлюдии `Y` возвращается в `y`.

Области R1/R2 считаются так же, как в русском, но с оговоркой: несколько
префиксов (`gener`, `commun`, `organ`, …) задают R1 явно, чтобы `general` и
`generous` попадали в одну область и давали одну основу. Плюс таблица исключений
(`skis` → `ski`, `news` → `news`, `atlas` → `atlas`), которую Porter2 требует
дословно.

### Кеш: почему он вообще появился

Замеры проекта: анализ 10 000 документов занимал 29.9 s при цели 10 s, один
`PUT` — 24.4 ms. При этом 5 миллионов токенов корпуса давали всего 27 379
различных слов: 90% токенов — повторы. Стемминг — чистая функция от токена,
поэтому её результат можно запомнить.

Кеш — это `array<string, string>` на 200 000 записей. При переполнении он **не**
вытесняет одну запись, а очищается целиком: это несколько строк кода вместо
реализации LRU, и на практике «несколько основ посчитаются заново». Результат от
кеша не зависит по построению — токен всегда даёт один и тот же терм, — и это
проверяется теми же 92 434 парами.

Что дал кеш по документации: анализ корпуса — с ~60 s до 1.5 s, replay журнала
при старте — с 29.9 s до 5.0 s, `PUT` — с 24.4 ms до 8.2 ms (p50), загрузка — с
339 до 985 документов в секунду. Кеш занимает около 4 MB на синтетическом
корпусе.

### Чего здесь сознательно нет

- **Никакой нормализации диакритики.** `café` и `cafe` — разные термы.
  Складывать `é` в `e` значило бы менять смысл в языках, где диакритика
  различает слова.
- **Никакой сегментации CJK.** `東京` — один токен; для японского и китайского
  нужен словарь, а не регулярное выражение.
- **Никаких стоп-слов.** При AND редкие слова и так не мешают, а выбрасывание
  частых ломает фразы и подсчёт длины документа.
- **Никаких синонимов и транслитерации.** `usb` не найдёт `юсб`.
- **Никаких позиций термов.** Индекс хранит частоты, а не места (→ глава 20),
  поэтому фразовый поиск здесь не поддержан.

## 3. Код

### Один анализатор на документы и запросы

Контракт из §2 буквально: `put()` анализирует заголовок и тело, `search()`
анализирует запрос — одним и тем же `Analyzer`. Различие ровно одно: у документа
список термов сохраняется целиком, у запроса он превращается в множество.

```php
    return $this->staleResult($id);
}

$terms = ['title' => $this->analyzer->terms($title), 'body' => $this->analyzer->terms($body)];
```

[`src/Corpus/Corpus.php#L98-L101`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L98-L101)

```php
$terms = array_values(array_unique($this->analyzer->terms($query)));
if ($terms === []) {
    throw new QueryRejected(QueryRejected::EMPTY_QUERY, 'The query has no terms after analysis.');
}
```

[`src/Corpus/Corpus.php#L161-L164`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L161-L164)

Каждый терм запроса обязан найтись в документе — это и есть AND, о котором будет
глава 20. Пока важно другое: и документ, и запрос прошли через **один** вызов
`terms()`, поэтому сравнивать их можно побайтно. `array_unique()` нужен только
потому, что документ хранит частоты, а запросу частота ни к чему.

### Контракт целиком — в докблоке

Докблок `Analyzer` — лучшее описание системы, которое есть в проекте: он
перечисляет все пять шагов и называет цену последнего (кеша) в цифрах.

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
  и с которыми сравниваются запросы. Документы и запросы проходят через один
  конвейер, и в этом весь контракт. `Кабели USB-C` в заголовке и `кабель usb` в
  строке поиска встречаются только потому, что здесь оба становятся `кабел`,
  `usb` и `c`: 1) Unicode NFKC, чтобы `й` был одним символом, лигатура `ﬁ` —
  `fi`, а полноширинный `ＵＳＢ` — `USB`; 2) нижний регистр, затем `ё` → `е`,
  потому что в русском тексте их используют взаимозаменяемо; 3) апостроф между
  двумя буквами удаляется — `O’Neill` → `oneill` — чтобы запрос, набранный без
  него, всё равно находился; 4) токены — максимальные последовательности букв и
  цифр; всё остальное их разделяет; 5) токен из кириллических букв идёт в
  русский стеммер, токен из ASCII-латиницы — в английский, а любой другой
  (цифры, смешанные письменности, латиница с диакритикой) остаётся как есть.
  Повторы сохраняются: частота терма в поле — это сколько раз он вышел отсюда.
  Основы кешируются: токен → терм. Стемминг составлял 96% стоимости анализа (12
  µs на токен), а 90% токенов повторяются, как и слова в тексте. С кешем анализ
  10 000 документов занял ~1.5 s вместо ~60 s (`docs/BENCHMARKS.md`). Кеш
  ограничен: когда в нём `CACHE_ENTRIES` токенов, он очищается и начинает
  заново, что стоит нескольких основ и держит память ровной при любом словаре.

### Пять шагов и один кеш, в двадцати строках

Вся глава — это следующие три блока. Первый — константа и конструктор: два
стеммера со значениями по умолчанию, так что в тестах и скриптах
`new Analyzer()` — это всё, что нужно.

```php
final class Analyzer
{
    public const int CACHE_ENTRIES = 200_000;

    /** @var array<string, string> token → term */
    private array $cache = [];

    public function __construct(
        private readonly RussianStemmer $russian = new RussianStemmer(),
        private readonly EnglishStemmer $english = new EnglishStemmer(),
    ) {
    }
```

[`src/Analysis/Analyzer.php#L41-L52`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L41-L52)

**Перевод комментариев:**
- `$cache` (`@var`): кеш токен → терм.

Второй — сам конвейер. Обратите внимание на порядок: нормализация, регистр,
апострофы, токены, стемминг. Апострофы **до** токенизации — то, что делает
`O’Neill` одним токеном; регистр и `ё` **до** стемминга — то, на что рассчитаны
правила Snowball.

```php
/**
 * @return list<string>
 */
public function terms(string $text): array
{
    $normalized = Normalizer::normalize($text, Normalizer::FORM_KC);
    if (!is_string($normalized)) {
        throw new InvalidArgumentException('Text is not valid UTF-8.');
    }

    $lowered = str_replace('ё', 'е', mb_strtolower($normalized, 'UTF-8'));
    $joined = preg_replace("/(?<=\\p{L})['’ʼ](?=\\p{L})/u", '', $lowered) ?? $lowered;

    preg_match_all('/[\p{L}\p{N}]+/u', $joined, $matches);

    return array_map($this->stem(...), $matches[0]);
}
```

[`src/Analysis/Analyzer.php#L54-L70`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L54-L70)

Третий — кеш и маршрутизация. Кеш проверяется и обновляется в одном месте,
поэтому все три пути (русский, английский, «как есть») кешируются одинаково.

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
```

[`src/Analysis/Analyzer.php#L72-L82`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L72-L82)

```php
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

[`src/Analysis/Analyzer.php#L84-L95`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Analyzer.php#L84-L95)

`stemUncached()` заодно объясняет, почему токен `café` не стеммится: он не
проходит ни `^\p{Cyrillic}+$`, ни `^[a-z]+$`. Обратите внимание, что `usb3` не
попадает и в английский путь: цифра `3` делает токен не-`a`–`z`, хотя начинается
он с латиницы.

### Русский стеммер

Докблок называет источник (конкретный коммит `snowball`), лицензию и самое
неочевидное свойство алгоритма: **сравниваются байты, а позиции считаются в
символах**.

```php
/**
 * The Snowball Russian stemmer, ported by hand from `algorithms/russian.sbl`
 * at snowballstem/snowball commit 14a4f24e4cf64f1003d1205edc0713b8111a18a8.
 *
 * Stemming is what lets the query `сокет` find a document that says
 * `сокетов`: both become `сокет`. A Russian noun has about a dozen forms, and
 * with AND semantics a query that misses one form misses the document.
 *
 * The algorithm only ever removes endings, and only inside RV — the part of
 * the word after its first vowel. Everything before RV is never looked at,
 * so a short word like `сон` keeps its stem whole. Endings are tried longest
 * first, exactly like Snowball's `among`: once the longest matching ending is
 * found, only its rule is applied, and a shorter ending is never tried
 * instead, even when that rule's condition fails.
 *
 * Input must be lowercase. `ё` is folded to `е` here as upstream does, though
 * the analyzer has already done it.
 *
 * Positions (RV, R2) are counted in characters, while suffixes are compared
 * and cut as UTF-8 bytes: a complete UTF-8 suffix can only match on a
 * character boundary, so the two never disagree.
 *
 * Copyright (c) 2001, Dr Martin Porter; (c) 2004,2005, Richard Boulton;
 * (c) 2013, Yoshiki Shibukawa; (c) 2006-2025, Olly Betts. Ported under the
 * 3-clause BSD license in COPYING next to this file.
 */
```

[`src/Analysis/Snowball/RussianStemmer.php#L7-L32`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/RussianStemmer.php#L7-L32)

**Перевод комментариев:**
- `RussianStemmer` (PHPDoc): стеммер Snowball для русского, перенесённый вручную
  из `algorithms/russian.sbl` на коммите
  `14a4f24e4cf64f1003d1205edc0713b8111a18a8` репозитория snowballstem/snowball.
  Стемминг — это то, что позволяет запросу `сокет` найти документ со словом
  `сокетов`: оба становятся `сокет`. У русского существительного около дюжины
  форм, и при семантике AND запрос, упустивший одну форму, упускает документ.
  Алгоритм только удаляет окончания и только внутри RV — части слова после его
  первой гласной. Всё, что левее RV, не рассматривается никогда, поэтому
  короткое слово вроде `сон` сохраняет основу целиком. Окончания перебираются от
  длинного к короткому, точно как `among` в Snowball: как только найдено самое
  длинное совпадение, применяется только его правило, и более короткое окончание
  не пробуется, даже если условие того правила не выполнено. Вход должен быть в
  нижнем регистре. `ё` здесь складывается в `е`, как и в оригинале, хотя
  анализатор уже сделал это. Позиции (RV, R2) считаются в символах, а суффиксы
  сравниваются и режутся как UTF-8-байты: полный UTF-8-суффикс может совпасть
  только на границе символа, поэтому эти два подхода никогда не расходятся.
  Copyright (c) 2001, Dr Martin Porter; (c) 2004,2005, Richard Boulton; (c)
  2013, Yoshiki Shibukawa; (c) 2006-2025, Olly Betts. Порт под лицензией
  3-clause BSD в COPYING рядом с этим файлом.

Данные алгоритма — это набор гласных и одиннадцать таблиц окончаний. Здесь
видно, почему порт читается «рядом с `.sbl`»: таблицы выписаны ровно в том
порядке, в котором они появляются в исходном алгоритме.

```php
private const string VOWELS = 'аеиоуыэюя';

/** Endings removed only after `а` or `я`, which stays. */
private const array GERUND_AFTER_A = ['в', 'вши', 'вшись'];

private const array GERUND = ['ив', 'ивши', 'ившись', 'ыв', 'ывши', 'ывшись'];

private const array ADJECTIVE = [
    'ее', 'ие', 'ые', 'ое', 'ими', 'ыми', 'ей', 'ий', 'ый', 'ой', 'ем', 'им',
    'ым', 'ом', 'его', 'ого', 'ему', 'ому', 'их', 'ых', 'ую', 'юю', 'ая',
    'яя', 'ою', 'ею',
];

private const array PARTICIPLE_AFTER_A = ['ем', 'нн', 'вш', 'ющ', 'щ'];

private const array PARTICIPLE = ['ивш', 'ывш', 'ующ'];

private const array REFLEXIVE = ['ся', 'сь'];

private const array VERB_AFTER_A = [
    'ла', 'на', 'ете', 'йте', 'ли', 'й', 'л', 'ем', 'н', 'ло', 'но', 'ет',
    'ют', 'ны', 'ть', 'ешь', 'нно',
];

private const array VERB = [
    'ила', 'ыла', 'ена', 'ейте', 'уйте', 'ите', 'или', 'ыли', 'ей', 'уй',
    'ил', 'ыл', 'им', 'ым', 'ен', 'ило', 'ыло', 'ено', 'ят', 'ует', 'уют',
    'ит', 'ыт', 'ены', 'ить', 'ыть', 'ишь', 'ую', 'ю',
];

private const array NOUN = [
    'а', 'ев', 'ов', 'ие', 'ье', 'е', 'иями', 'ями', 'ами', 'еи', 'ии', 'и',
    'ией', 'ей', 'ой', 'ий', 'й', 'иям', 'ям', 'ием', 'ем', 'ам', 'ом', 'о',
    'у', 'ах', 'иях', 'ях', 'ы', 'ь', 'ию', 'ью', 'ю', 'ия', 'ья', 'я',
];

private const array DERIVATIONAL = ['ост', 'ость'];

private const array SUPERLATIVE = ['ейш', 'ейше'];
```

[`src/Analysis/Snowball/RussianStemmer.php#L35-L73`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/RussianStemmer.php#L35-L73)

**Перевод комментариев:**
- `GERUND_AFTER_A` (PHPDoc): окончания, которые удаляются только после `а` или
  `я`; сама гласная остаётся.

Дальше — четыре шага. Деепричастие и прилагательное обрабатываются **вложенно**:
если найдено прилагательное окончание, сразу за ним ищется причастие, и только
потом алгоритм спускается к глаголу и существительному.

```php
public function stem(string $word): string
{
    $word = str_replace('ё', 'е', $word);
    [$rv, $r2] = $this->regions($word);

    // Step 1: a perfective gerund, or else an optional reflexive ending
    // followed by an adjectival, verb or noun ending.
    if (!$this->removeEnding($word, $rv, self::GERUND_AFTER_A, self::GERUND)) {
        $this->removeEnding($word, $rv, [], self::REFLEXIVE);

        if (!$this->removeAdjectival($word, $rv)
            && !$this->removeEnding($word, $rv, self::VERB_AFTER_A, self::VERB)
        ) {
            $this->removeEnding($word, $rv, [], self::NOUN);
        }
    }

    // Step 2: a final и (the noun ending -ию is treated as the verb ending -ю).
    $this->removeEnding($word, $rv, [], ['и']);

    // Step 3: the derivational ending -ост(ь), only when it lies in R2.
    $derivational = $this->longestEnding($word, $rv, self::DERIVATIONAL);
    if ($derivational !== null && $this->start($word, $derivational) >= $r2) {
        $word = $this->cut($word, $derivational);
    }

    // Step 4: a superlative ending (then нн → н), or нн → н, or a final ь.
    $this->tidyUp($word, $rv);

    return $word;
}
```

[`src/Analysis/Snowball/RussianStemmer.php#L75-L105`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/RussianStemmer.php#L75-L105)

**Перевод комментариев:**
- `// Step 1: a perfective gerund, or else an optional reflexive...` — шаг 1:
  деепричастие совершенного вида, иначе — необязательное возвратное окончание,
  за которым идёт окончание прилагательного, глагола или существительного.
- `// Step 2: a final и (the noun ending -ию is treated as the verb ending -ю).`
  — шаг 2: конечная `и` (окончание существительного `-ию` трактуется как
  окончание глагола `-ю`).
- `// Step 3: the derivational ending -ост(ь), only when it lies in R2.` —
  третий шаг: словообразовательное окончание `-ост(ь)`, только когда оно
  находится в R2.
- `// Step 4: a superlative ending (then нн → н), or нн → н, or a final ь.` —
  шаг 4: окончание превосходной степени (затем `нн` → `н`), либо `нн` → `н`,
  либо конечная `ь`.

Области RV и R2 считаются одним общим приёмом `gopast`: «двигаться, пока не
найдёшь символ нужного вида, и остановиться сразу за ним». Если нужный символ не
найден, область считается пустой и начинается с конца слова — поэтому короткие
слова неприкосновенны.

```php
/**
 * RV starts after the first vowel. R1 starts after the first non-vowel
 * that follows a vowel, and R2 is the R1 of R1. A region that does not
 * exist starts at the end of the word.
 *
 * @return array{int, int} [RV, R2] as character offsets
 */
private function regions(string $word): array
{
    $chars = mb_str_split($word);
    $length = count($chars);
    $isVowel = fn (int $i): bool => str_contains(self::VOWELS, $chars[$i]);

    // Each step is one Snowball `gopast`: move to just past the first
    // character of the wanted kind, or give up at the end of the word.
    $gopast = function (int $from, bool $vowel) use ($length, $isVowel): ?int {
        for ($i = $from; $i < $length; $i++) {
            if ($isVowel($i) === $vowel) {
                return $i + 1;
            }
        }

        return null;
    };

    $rv = $gopast(0, true);
    if ($rv === null) {
        return [$length, $length];
    }

    $r1 = $gopast($rv, false);
    $afterVowel = $r1 === null ? null : $gopast($r1, true);
    $r2 = $afterVowel === null ? null : $gopast($afterVowel, false);

    return [$rv, $r2 ?? $length];
}
```

[`src/Analysis/Snowball/RussianStemmer.php#L107-L142`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/RussianStemmer.php#L107-L142)

**Перевод комментариев:**
- `regions()` (PHPDoc): RV начинается после первой гласной. R1 начинается после
  первой согласной, которая идёт за гласной, а R2 — это R1 от R1. Область,
  которой нет, начинается с конца слова. `@return`: `[RV, R2]` как смещения в
  символах.
- `// Each step is one Snowball gopast: move to just past the first` — каждый
  шаг — один `gopast` из Snowball: сдвинуться сразу за первый символ нужного
  вида или сдаться в конце слова.

А вот и `among`: из всех совпадений берётся **самое длинное**, и дальше работает
только его правило. Если для окончания из списка `afterA` перед ним нет `а` или
`я`, функция возвращает `false` — и более короткое окончание не рассматривается.

```php
/**
 * Removes the longest matching ending inside RV. An ending from the first
 * list is removed only when `а` or `я` (also inside RV) precedes it; if
 * that check fails, nothing is removed.
 *
 * @param list<string> $afterA endings that need a preceding а or я
 * @param list<string> $plain endings removed unconditionally
 */
private function removeEnding(string &$word, int $rv, array $afterA, array $plain): bool
{
    $ending = $this->longestEnding($word, $rv, [...$afterA, ...$plain]);
    if ($ending === null) {
        return false;
    }

    if (in_array($ending, $afterA, true)) {
        $before = $this->start($word, $ending) - 1;
        if ($before < $rv || !in_array(mb_substr($word, $before, 1), ['а', 'я'], true)) {
            return false;
        }
    }

    $word = $this->cut($word, $ending);

    return true;
}
```

[`src/Analysis/Snowball/RussianStemmer.php#L144-L169`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/RussianStemmer.php#L144-L169)

**Перевод комментариев:**
- `removeEnding()` (PHPDoc): удаляет самое длинное совпадающее окончание внутри
  RV. Окончание из первого списка удаляется только когда перед ним стоит `а` или
  `я` (тоже внутри RV); если эта проверка не прошла, ничего не удаляется.
  `@param`: `$afterA` — окончания, которым нужна предшествующая `а` или `я`;
  `$plain` — окончания, удаляемые безусловно.

### Английский стеммер

Докблок сразу называет версию алгоритма и объясняет приём с `Y`, который иначе
выглядит случайной деталью реализации.

```php
/**
 * The Snowball English (Porter2) stemmer, ported by hand from
 * `algorithms/english.sbl` at snowballstem/snowball commit
 * 14a4f24e4cf64f1003d1205edc0713b8111a18a8.
 *
 * Porter2, not the original 1980 Porter algorithm: `generously` → `generous`,
 * `dying` → `die`, and words such as `inning` or `proceed` are left alone.
 *
 * The algorithm works on lowercase ASCII letters and the apostrophe, byte by
 * byte. One trick runs through all of it: the prelude marks a consonant `y`
 * as `Y`, so the vowel checks can tell `say` (consonant) from `cry` (vowel)
 * without looking twice; the postlude turns `Y` back into `y`.
 *
 * R1 starts after the first non-vowel that follows a vowel (or after one of
 * a few listed prefixes, so `generous` and `general` share a region); R2 is
 * the R1 of R1. Most endings are removed only when they lie in R1 or R2.
 * Endings are matched longest first, and when the longest ending's condition
 * fails, no shorter ending is tried: that is how Snowball's `among` works.
 *
 * Copyright (c) 2001, Dr Martin Porter; (c) 2004,2005, Richard Boulton;
 * (c) 2013, Yoshiki Shibukawa; (c) 2006-2025, Olly Betts. Ported under the
 * 3-clause BSD license in COPYING next to this file.
 */
```

[`src/Analysis/Snowball/EnglishStemmer.php#L7-L29`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L7-L29)

**Перевод комментариев:**
- `EnglishStemmer` (PHPDoc): стеммер Snowball для английского (Porter2),
  перенесённый вручную из `algorithms/english.sbl` на коммите
  `14a4f24e4cf64f1003d1205edc0713b8111a18a8` репозитория snowballstem/snowball.
  Это Porter2, а не исходный алгоритм Портера 1980 года: `generously` →
  `generous`, `dying` → `die`, а слова вроде `inning` или `proceed` не
  трогаются. Алгоритм работает со строчными ASCII-буквами и апострофом побайтно.
  Через весь код идёт один приём: прелюдия помечает согласную `y` как `Y`, чтобы
  проверки гласных отличали `say` (согласная) от `cry` (гласная), не глядя
  дважды; постлюдия возвращает `Y` в `y`. R1 начинается после первой согласной,
  идущей за гласной (или после одного из перечисленных префиксов, чтобы
  `generous` и `general` делили область); R2 — это R1 от R1. Большинство
  окончаний удаляется только в R1 или R2. Окончания сравниваются от длинного к
  короткому, и если условие самого длинного не выполнено, более короткое не
  пробуется: так работает `among` в Snowball. Copyright (c) 2001, Dr Martin
  Porter; (c) 2004,2005, Richard Boulton; (c) 2013, Yoshiki Shibukawa; (c)
  2006-2025, Olly Betts. Порт под лицензией 3-clause BSD в COPYING рядом с этим
  файлом.

Исключения и префиксы, задающие R1, — та часть Porter2, которую нельзя вывести
из правил и приходится переносить посимвольно.

```php
/** Words that are stemmed, or kept, as a whole. */
private const array EXCEPTIONS = [
    'skis' => 'ski', 'skies' => 'sky',
    'idly' => 'idl', 'gently' => 'gentl', 'ugly' => 'ugli', 'early' => 'earli',
    'only' => 'onli', 'singly' => 'singl',
    'sky' => 'sky', 'news' => 'news', 'howe' => 'howe',
    'atlas' => 'atlas', 'cosmos' => 'cosmos', 'bias' => 'bias', 'andes' => 'andes',
];

/** Prefixes after which R1 starts, so related words keep one stem. */
private const array R1_PREFIXES = ['gener', 'commun', 'arsen', 'past', 'univers', 'later', 'emerg', 'organ', 'inter'];
```

[`src/Analysis/Snowball/EnglishStemmer.php#L34-L44`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L34-L44)

**Перевод комментариев:**
- `EXCEPTIONS` (PHPDoc): слова, которые стеммятся или сохраняются целиком.
- `R1_PREFIXES` (PHPDoc): префиксы, после которых начинается R1, чтобы
  родственные слова сохраняли одну основу.

Таблицы шагов 2–4: замена («tional» → «tion»), удаление («ness» → пусто) и
проверка области (шаг 4 удаляет окончание только в R2). Список `DOUBLES` нужен
для правила «удвоенная согласная теряет одну букву».

```php
private const array STEP2 = [
    'tional' => 'tion', 'enci' => 'ence', 'anci' => 'ance', 'abli' => 'able',
    'entli' => 'ent', 'izer' => 'ize', 'ization' => 'ize', 'ational' => 'ate',
    'ation' => 'ate', 'ator' => 'ate', 'alism' => 'al', 'aliti' => 'al',
    'alli' => 'al', 'fulness' => 'ful', 'ousli' => 'ous', 'ousness' => 'ous',
    'iveness' => 'ive', 'iviti' => 'ive', 'biliti' => 'ble', 'bli' => 'ble',
    'ogist' => 'og', 'ogi' => 'og', 'fulli' => 'ful', 'lessli' => 'less',
    'li' => '',
];

private const array STEP3 = [
    'tional' => 'tion', 'ational' => 'ate', 'alize' => 'al', 'icate' => 'ic',
    'iciti' => 'ic', 'ical' => 'ic', 'ful' => '', 'ness' => '', 'ative' => '',
];

private const array STEP4 = [
    'al', 'ance', 'ence', 'er', 'ic', 'able', 'ible', 'ant', 'ement', 'ment',
    'ent', 'ism', 'ate', 'iti', 'ous', 'ive', 'ize', 'ion',
];

/** Letters that may precede a removable -li. */
private const string VALID_LI = 'cdeghkmnrt';

private const array DOUBLES = ['bb', 'dd', 'ff', 'gg', 'mm', 'nn', 'pp', 'rr', 'tt'];
```

[`src/Analysis/Snowball/EnglishStemmer.php#L46-L69`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L46-L69)

**Перевод комментариев:**
- `VALID_LI` (PHPDoc): буквы, которые могут стоять перед удаляемым `-li`.

Сам `stem()` — это прелюдия, вычисление областей и семь шагов подряд.
Единственный шаг, стоящий особняком, — проверка `strlen($word) < 3`: слишком
короткие слова не трогаются до всякой обработки.

```php
public function stem(string $word): string
{
    if (isset(self::EXCEPTIONS[$word])) {
        return self::EXCEPTIONS[$word];
    }

    if (strlen($word) < 3) {
        return $word;
    }

    [$word, $yFound] = $this->prelude($word);
    [$r1, $r2] = $this->regions($word);

    $word = $this->step1a($word);
    $word = $this->step1b($word, $r1);
    $word = $this->step1c($word);
    $word = $this->step2($word, $r1);
    $word = $this->step3($word, $r1, $r2);
    $word = $this->step4($word, $r2);
    $word = $this->step5($word, $r1, $r2);

    return $yFound ? str_replace('Y', 'y', $word) : $word;
}
```

[`src/Analysis/Snowball/EnglishStemmer.php#L71-L93`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L71-L93)

Прелюдия помечает согласную `y` заглавной `Y` — в начале слова и после гласной.
Именно поэтому дальнейшим шагам достаточно простого набора `aeiouy`: `Y` в него
не входит и ведёт себя как согласная.

```php
/**
 * Drops a leading apostrophe and marks every consonant y as Y: an initial
 * y, and a y right after a vowel.
 *
 * @return array{string, bool} the word, and whether any Y was marked
 */
private function prelude(string $word): array
{
    if (str_starts_with($word, "'")) {
        $word = substr($word, 1);
    }

    $yFound = false;
    for ($i = 0, $length = strlen($word); $i < $length; $i++) {
        if ($word[$i] === 'y' && ($i === 0 || $this->isVowel($word[$i - 1]))) {
            $word[$i] = 'Y';
            $yFound = true;
        }
    }

    return [$word, $yFound];
}
```

[`src/Analysis/Snowball/EnglishStemmer.php#L95-L116`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L95-L116)

**Перевод комментариев:**
- `prelude()` (PHPDoc): отбрасывает ведущий апостроф и помечает каждую согласную
  `y` как `Y`: начальную `y` и `y` сразу после гласной. `@return`: слово и
  признак, была ли помечена хоть одна `Y`.

Области R1/R2 и вспомогательная функция, которая ищет «первую согласную после
гласной». Явные префиксы задают R1 раньше общего правила; это часть Porter2,
которую нельзя вывести из алгоритма и приходится переносить дословно — как и
таблицу исключений выше.

```php
/**
 * @return array{int, int} [R1, R2] as byte offsets; a missing region starts at the end
 */
private function regions(string $word): array
{
    $length = strlen($word);

    $r1 = null;
    foreach (self::R1_PREFIXES as $prefix) {
        if (str_starts_with($word, $prefix)) {
            $r1 = strlen($prefix);
            break;
        }
    }
    $r1 ??= $this->afterVowelThenConsonant($word, 0);
    if ($r1 === null) {
        return [$length, $length];
    }

    return [$r1, $this->afterVowelThenConsonant($word, $r1) ?? $length];
}

/** The offset just past the first non-vowel that follows a vowel, searching from $from. */
private function afterVowelThenConsonant(string $word, int $from): ?int
{
    $length = strlen($word);
    $i = $from;

    while ($i < $length && !$this->isVowel($word[$i])) {
        $i++;
    }
    while ($i < $length && $this->isVowel($word[$i])) {
        $i++;
    }

    return $i < $length ? $i + 1 : null;
}
```

[`src/Analysis/Snowball/EnglishStemmer.php#L118-L154`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Analysis/Snowball/EnglishStemmer.php#L118-L154)

**Перевод комментариев:**
- `regions()` (PHPDoc): `@return`: `[R1, R2]` как байтовые смещения;
  отсутствующая область начинается с конца.
- `afterVowelThenConsonant()` (PHPDoc): смещение сразу за первой согласной,
  идущей после гласной, начиная поиск с `$from`.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-search
make build install
```

Все четыре эксперимента выполняются в контейнере `php-mini-search`, проект
смонтирован в `/app`. Замеров времени в скриптах нет, поэтому все выводы
детерминированы: каждый прогон повторялся дважды и совпал байт-в-байт.

### 1. Одна фраза, пять шагов

Скрипт повторяет четыре операции `Analyzer::terms()` по отдельности
(нормализация, регистр, апострофы, токены) и печатает результат каждого шага, а
затем — то, что вернул сам анализатор, то есть ещё и стемминг. Промежуточные
значения наружу не торчат, поэтому четыре строки конвейера в скрипте выписаны
ровно так, как они стоят в `terms()`.

```bash
docker compose cp /tmp/ch19-pipeline.php php:/tmp/ch19-pipeline.php
docker compose exec php php /tmp/ch19-pipeline.php
```

```php
<?php

declare(strict_types=1);

/**
 * One phrase, five stages: what the analyzer does to text, step by step.
 *
 * `Analyzer::terms()` is one method, so the intermediate values are not
 * exposed anywhere; the four lines below repeat exactly the four operations
 * it performs (NFKC, lowercase with ё → е, apostrophe removal, the token
 * regex), and `terms()` then does the fifth, stemming. Printing each stage
 * is the point: most surprises in search quality come from stage one or two,
 * long before any index is involved.
 *
 * The last two cases are printed as code points, because that is where NFKC
 * earns its place: `й` typed as `и` plus a combining breve is one character
 * after normalization, and full-width `ＵＳＢ` is plain ASCII.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch19-pipeline.php php:/tmp/ch19-pipeline.php
 *   docker compose exec php php /tmp/ch19-pipeline.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;

$analyzer = new Analyzer();

/**
 * @return list<string>
 */
$codePoints = static function (string $text): array {
    $points = [];

    foreach (mb_str_split($text) as $character) {
        $points[] = sprintf('U+%04X', mb_ord($character));
    }

    return $points;
};

/**
 * @return list<string>
 */
$stages = static function (string $text): array {
    $normalized = Normalizer::normalize($text, Normalizer::FORM_KC);
    if (!is_string($normalized)) {
        throw new RuntimeException('Text is not valid UTF-8.');
    }

    $lowered = str_replace('ё', 'е', mb_strtolower($normalized, 'UTF-8'));
    $joined = preg_replace("/(?<=\\p{L})['’ʼ](?=\\p{L})/u", '', $lowered) ?? $lowered;

    preg_match_all('/[\p{L}\p{N}]+/u', $joined, $matches);

    return [$normalized, $lowered, $joined, $matches[0]];
};

$cases = [
    'Кабели USB-C',
    'ﬁle ＵＳＢ',
    "бели\u{0306}",
    "зеле\u{0308}ный",
    "O’Neill д’Артаньян Levi's",
    "'quoted'",
    'USB-C 3.1, 2 × 0',
    '🔥 кабель 🔥',
];

foreach ($cases as $case) {
    [$normalized, $lowered, $joined, $tokens] = $stages($case);
    $terms = $analyzer->terms($case);

    printf("input:     %s\n", $case);
    printf("NFKC:      %s\n", $normalized);
    printf("lower, ё:  %s\n", $lowered);
    printf("no apos:   %s\n", $joined);
    printf("tokens:    %s\n", $tokens === [] ? '(none)' : implode(' | ', $tokens));
    printf("terms:     %s\n", $terms === [] ? '(none)' : implode(' | ', $terms));

    if ($normalized !== $case) {
        printf("codepoints: %s\n", implode(' ', $codePoints($normalized)));
    }

    printf("\n");
}
```

**Перевод комментариев:**
- `ch19-pipeline.php` (PHPDoc): одна фраза, пять стадий: что анализатор делает с
  текстом, шаг за шагом. `Analyzer::terms()` — один метод, поэтому промежуточные
  значения нигде не видны; четыре строки ниже повторяют ровно четыре операции,
  которые он выполняет (NFKC, нижний регистр с `ё` → `е`, удаление апострофов,
  регулярное выражение для токенов), а `terms()` затем делает пятую — стемминг.
  Печать каждой стадии и есть смысл: большинство сюрпризов в качестве поиска
  приходит из первой или второй стадии, задолго до участия индекса. Последние
  два случая печатаются как кодовые точки, потому что именно там NFKC
  оправдывает себя: `й`, набранная как `и` плюс комбинирующая бревис, после
  нормализации — один символ, а полноширинный `ＵＳＢ` — обычный ASCII. Запуск в
  контейнере, где проект смонтирован в `/app`: `docker compose cp ...` и
  `docker compose exec ...`.

```text
input:     Кабели USB-C
NFKC:      Кабели USB-C
lower, ё:  кабели usb-c
no apos:   кабели usb-c
tokens:    кабели | usb | c
terms:     кабел | usb | c

input:     ﬁle ＵＳＢ
NFKC:      file USB
lower, ё:  file usb
no apos:   file usb
tokens:    file | usb
terms:     file | usb
codepoints: U+0066 U+0069 U+006C U+0065 U+0020 U+0055 U+0053 U+0042

input:     белй
NFKC:      белй
lower, ё:  белй
no apos:   белй
tokens:    белй
terms:     бел
codepoints: U+0431 U+0435 U+043B U+0439

input:     зелёный
NFKC:      зелёный
lower, ё:  зеленый
no apos:   зеленый
tokens:    зеленый
terms:     зелен
codepoints: U+0437 U+0435 U+043B U+0451 U+043D U+044B U+0439

input:     O’Neill д’Артаньян Levi's
NFKC:      O’Neill д’Артаньян Levi's
lower, ё:  o’neill д’артаньян levi's
no apos:   oneill дартаньян levis
tokens:    oneill | дартаньян | levis
terms:     oneil | дартанья | levi

input:     'quoted'
NFKC:      'quoted'
lower, ё:  'quoted'
no apos:   'quoted'
tokens:    quoted
terms:     quot

input:     USB-C 3.1, 2 × 0
NFKC:      USB-C 3.1, 2 × 0
lower, ё:  usb-c 3.1, 2 × 0
no apos:   usb-c 3.1, 2 × 0
tokens:    usb | c | 3 | 1 | 2 | 0
terms:     usb | c | 3 | 1 | 2 | 0

input:     🔥 кабель 🔥
NFKC:      🔥 кабель 🔥
lower, ё:  🔥 кабель 🔥
no apos:   🔥 кабель 🔥
tokens:    кабель
terms:     кабел
```

Пятьдесят девять строк — и почти каждое решение главы видно на конкретном
примере.

`Кабели USB-C` не меняется на NFKC, но после регистра и токенизации распадается
на `кабели | usb | c`, а после стемминга — на `кабел | usb | c`. Дефис здесь —
разделитель, а не часть слова.

`ﬁle ＵＳＢ` — это два символа, которые в байтах не имеют ничего общего с `file`
и `USB`, но NFKC превращает их именно в `file USB`. Строка `codepoints`
показывает, что после нормализации там настоящие ASCII `U+0066 U+0069 …`.

`бели` + `U+0306` — самый наглядный случай: NFKC собирает `и` и комбинирующую
бревис в один символ `й` (`U+0439`), и в строке кодовых точек остаётся четыре
символа вместо пяти. Терм `бел` получается уже стеммингом: `белй` → `бел`.

`зеле` + `U+0308` + `ный` нормализуется в `зелёный` с настоящей `ё` (`U+0451`),
а следующий шаг складывает её в `е`. Поэтому `зелёный` и `зеленый` — один терм
`зелен`.

Апострофы: `O’Neill` (типографский `’`), `д’Артаньян` (он же) и `Levi's` (ASCII)
после шага 3 становятся `oneill`, `дартаньян`, `levis`; термы — `oneil`,
`дартанья`, `levi`. Обратите внимание на `дартанья`: конечный `-н` удалён на
шаге 1 — он входит в таблицу глагольных окончаний, которая требует перед собой
`а` или `я`, а в `дартаньян` перед `н` стоит `я`. Для фамилии это выглядит
странно, но это ровно то, что делает `russian.sbl`, и то, чему соответствует
официальный словарь.

`'quoted'` теряет апострофы как разделители и даёт `quot`; цифры
`USB-C 3.1, 2 × 0` — шесть отдельных токенов `usb | c | 3 | 1 | 2 | 0`; эмодзи
вокруг `кабель` просто исчезают.

### 2. Документ и запрос встречаются в термах

Здесь проверяется главный контракт: две разные строки должны дать пересекающиеся
термы — или не дать, и тогда это должно быть задокументированным ограничением, а
не сюрпризом.

```bash
docker compose cp /tmp/ch19-doc-query.php php:/tmp/ch19-doc-query.php
docker compose exec php php /tmp/ch19-doc-query.php
```

```php
<?php

declare(strict_types=1);

/**
 * Why documents and queries go through one pipeline: the same text has to
 * come out as the same terms on both sides, or the search never matches.
 *
 * Each pair below is a document title and a query a person might type. The
 * script prints the terms of each, and whether the sets meet. Two pairs are
 * deliberately negative: `café` and `cafe` do *not* meet, because accented
 * Latin is left alone (Porter2 is defined for a–z only), and `usb3` and
 * `usb 3` do not meet either, because one is a single token and the other
 * is two.
 *
 * The query side calls `array_unique()` exactly as `Corpus::search()` does:
 * a query is a set of terms, while a document keeps every occurrence, since
 * that is what term frequency is.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch19-doc-query.php php:/tmp/ch19-doc-query.php
 *   docker compose exec php php /tmp/ch19-doc-query.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;

$analyzer = new Analyzer();

$pairs = [
    'case and punctuation' => ['Кабели USB-C', 'кабель usb'],
    'ё vs е' => ['Ёжик зелёный', 'ежик зеленый'],
    'apostrophe typed, not typed' => ['O’Neill', 'oneill'],
    'apostrophe, other form' => ['O’Neill', "o'neill"],
    'morphology' => ['сокетов', 'сокет'],
    'morphology, English' => ['connected cables', 'connect cable'],
    'accented Latin (left alone)' => ['café', 'cafe'],
    'token boundary' => ['usb3', 'usb 3'],
];

/**
 * Pads to a character count, not a byte count: `%-20s` in printf() counts
 * bytes, and Cyrillic words are two bytes a letter.
 */
$pad = static fn (string $text, int $to): string => $text . str_repeat(' ', max(0, $to - mb_strlen($text)));

foreach ($pairs as $label => [$document, $query]) {
    $documentTerms = $analyzer->terms($document);
    $queryTerms = array_values(array_unique($analyzer->terms($query)));

    $meet = array_intersect($queryTerms, $documentTerms) === $queryTerms;

    printf("%s\n", $label);
    printf("  document: %s -> %s\n", $pad($document, 20), implode(' | ', $documentTerms));
    printf("  query:    %s -> %s\n", $pad($query, 20), implode(' | ', $queryTerms));
    printf("  every query term is in the document: %s\n\n", $meet ? 'yes' : 'no');
}
```

**Перевод комментариев:**
- `ch19-doc-query.php` (PHPDoc): почему документы и запросы идут через один
  конвейер: один и тот же текст должен давать одинаковые термы с обеих сторон,
  иначе поиск никогда не найдёт совпадения. Каждая пара ниже — это заголовок
  документа и запрос, который человек мог бы набрать. Скрипт печатает термы
  каждого и то, пересекаются ли множества. Две пары намеренно отрицательные:
  `café` и `cafe` *не* пересекаются, потому что латиница с диакритикой остаётся
  как есть (Porter2 определён только для a–z), а `usb3` и `usb 3` тоже не
  пересекаются, потому что первое — один токен, а второе — два. Сторона запроса
  вызывает `array_unique()` точно как `Corpus::search()`: запрос — это множество
  термов, а документ хранит каждое вхождение, потому что именно это и есть
  частота терма. Запуск в контейнере, где проект смонтирован в `/app`:
  `docker compose cp ...` и `docker compose exec ...`.
- `$pad` (PHPDoc): дополняет до числа символов, а не байтов: `%-20s` в
  `printf()` считает байты, а кириллическая буква занимает два байта.

```text
case and punctuation
  document: Кабели USB-C         -> кабел | usb | c
  query:    кабель usb           -> кабел | usb
  every query term is in the document: yes

ё vs е
  document: Ёжик зелёный         -> ежик | зелен
  query:    ежик зеленый         -> ежик | зелен
  every query term is in the document: yes

apostrophe typed, not typed
  document: O’Neill              -> oneil
  query:    oneill               -> oneil
  every query term is in the document: yes

apostrophe, other form
  document: O’Neill              -> oneil
  query:    o'neill              -> oneil
  every query term is in the document: yes

morphology
  document: сокетов              -> сокет
  query:    сокет                -> сокет
  every query term is in the document: yes

morphology, English
  document: connected cables     -> connect | cabl
  query:    connect cable        -> connect | cabl
  every query term is in the document: yes

accented Latin (left alone)
  document: café                 -> café
  query:    cafe                 -> cafe
  every query term is in the document: no

token boundary
  document: usb3                 -> usb3
  query:    usb 3                -> usb | 3
  every query term is in the document: no
```

Сорок строк, шесть пар из восьми совпадают — и обе несовпадающие пары названы в
скрипте заранее.

`Кабели USB-C` и `кабель usb`: у документа три терма, у запроса два, и оба есть
в документе. `Ёжик зелёный` и `ежик зеленый` дают одинаковые `ежик | зелен`.
`O’Neill` и `oneill`, `O’Neill` и `o'neill` — один терм `oneil` во всех трёх
вариантах написания, включая запрос без апострофа.

Морфология работает в обе стороны и в обоих языках: `сокетов` и `сокет` дают
`сокет`, `connected cables` и `connect cable` — `connect | cabl`.

А вот `café` против `cafe` и `usb3` против `usb 3` не совпадают, и это не баг:
`é` не входит в `a`–`z`, поэтому токен не идёт в Porter2, а `usb3` — один токен,
тогда как `usb 3` — два. Оба ограничения задокументированы, и оба — цена
простоты, а не недосмотр.

### 3. Два стеммера на семьях слов

Скрипт вызывает стеммеры напрямую, а последнюю группу — через `Analyzer`, чтобы
показать, какие тексты до стеммеров вообще не доходят.

```bash
docker compose cp /tmp/ch19-stems.php php:/tmp/ch19-stems.php
docker compose exec php php /tmp/ch19-stems.php
```

```php
<?php

declare(strict_types=1);

/**
 * The two stemmers, on families of real words and on the cases where they
 * deliberately do nothing.
 *
 * The Russian column shows why AND without morphology would be unusable: a
 * noun has about a dozen forms, and every one of them has to reach the same
 * term. The English column shows Porter2's exceptions and its treatment of
 * `y`, `dying` and `inning` — the three places where the 1980 Porter
 * algorithm and Porter2 disagree, and the port has to agree with Porter2.
 *
 * The last group is the important one for the contract: a token that is not
 * a run of Cyrillic letters or of ASCII a–z is not stemmed at all. That is
 * not an oversight — Porter2 is defined for a–z only — but it does mean
 * `café` and `cafe` are two different terms.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch19-stems.php php:/tmp/ch19-stems.php
 *   docker compose exec php php /tmp/ch19-stems.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;
use PhpMiniSearch\Analysis\Snowball\EnglishStemmer;
use PhpMiniSearch\Analysis\Snowball\RussianStemmer;

$russian = new RussianStemmer();
$english = new EnglishStemmer();
$analyzer = new Analyzer();

/**
 * Pads to a character count, not a byte count: `%-14s` in printf() counts
 * bytes, and Cyrillic words are two bytes a letter.
 */
$pad = static fn (string $word, int $to): string => $word . str_repeat(' ', max(0, $to - mb_strlen($word)));

$table = static function (string $heading, array $words, callable $stem) use ($pad): void {
    printf("%s\n", $heading);

    foreach ($words as $word) {
        printf("  %s -> %s\n", $pad($word, 14), $stem($word));
    }

    printf("\n");
};

$table('Russian: one noun, many forms', [
    'кабель', 'кабеля', 'кабели', 'кабелей', 'кабелем', 'кабелю',
], $russian->stem(...));

$table('Russian: another family, and a short word', [
    'сокет', 'сокетов', 'сокеты', 'сокетом', 'сон', 'ёж', 'ежик',
], $russian->stem(...));

$table('Russian: adjective, participle, superlative', [
    'красивый', 'работающий', 'новейший', 'русский',
], $russian->stem(...));

$table('English: one verb, many forms', [
    'connect', 'connected', 'connecting', 'connection', 'connections',
], $english->stem(...));

$table('English: the cases Porter and Porter2 disagree on', [
    'generously', 'dying', 'inning', 'proceed', 'skis',
], $english->stem(...));

printf("Kept as they are — the analyzer never sends these to a stemmer:\n");

foreach (['café', 'usb3', '東京', '3', 'USB-C'] as $text) {
    printf("  %s -> %s\n", $pad($text, 14), implode(' | ', $analyzer->terms($text)));
}
```

**Перевод комментариев:**
- `ch19-stems.php` (PHPDoc): два стеммера на семьях реальных слов и на случаях,
  где они намеренно ничего не делают. Русская колонка показывает, почему AND без
  морфологии был бы непригоден: у существительного около дюжины форм, и каждая
  должна прийти к одному терму. Английская колонка показывает исключения Porter2
  и его обращение с `y`, `dying` и `inning` — три места, где алгоритм Портера
  1980 года и Porter2 расходятся, а порт должен согласовываться с Porter2.
  Последняя группа важна для контракта: токен, который не является
  последовательностью кириллических букв или ASCII a–z, не стеммится вовсе. Это
  не недосмотр — Porter2 определён только для a–z, — но это значит, что `café` и
  `cafe` — два разных терма. Запуск в контейнере, где проект смонтирован в
  `/app`: `docker compose cp ...` и `docker compose exec ...`.
- `$pad` (PHPDoc): дополняет до числа символов, а не байтов: `%-14s` в
  `printf()` считает байты, а кириллическая буква занимает два байта.

```text
Russian: one noun, many forms
  кабель         -> кабел
  кабеля         -> кабел
  кабели         -> кабел
  кабелей        -> кабел
  кабелем        -> кабел
  кабелю         -> кабел

Russian: another family, and a short word
  сокет          -> сокет
  сокетов        -> сокет
  сокеты         -> сокет
  сокетом        -> сокет
  сон            -> сон
  ёж             -> еж
  ежик           -> ежик

Russian: adjective, participle, superlative
  красивый       -> красив
  работающий     -> работа
  новейший       -> нов
  русский        -> русск

English: one verb, many forms
  connect        -> connect
  connected      -> connect
  connecting     -> connect
  connection     -> connect
  connections    -> connect

English: the cases Porter and Porter2 disagree on
  generously     -> generous
  dying          -> die
  inning         -> inning
  proceed        -> proceed
  skis           -> ski

Kept as they are — the analyzer never sends these to a stemmer:
  café           -> café
  usb3           -> usb3
  東京             -> 東京
  3              -> 3
  USB-C          -> usb | c
```

Сорок три строки. Русская семья `кабель` сходится в `кабел` во всех шести формах
— это и есть ответ на вопрос, зачем нужен стемминг при AND. Семья `сокет` — в
`сокет`. При этом `сон` остаётся `сон`: кандидат `-н` в таблице есть, но таблица
требует перед ним `а` или `я`, а перед ним стоит `о`.

`ёж` → `еж` (стеммер сам складывает `ё` на случай прямого вызова), но `ежик`
остаётся `ежик`: конечная `к` не окончание, а шаг 2 удаляет только конечную `и`.

Прилагательное, причастие и превосходная степень: `красивый` → `красив`,
`работающий` → `работа` (сначала удалено `-ий`, затем `-ющ`), `новейший` → `нов`
(удалено `-ейш`), `русский` → `русск`.

Английская семья `connect` сходится в `connect` из пяти форм. Слова, на которых
Porter 1980 и Porter2 расходятся, ведут себя как в Porter2: `generously` →
`generous`, `dying` → `die`, `inning` и `proceed` не трогаются, `skis` берётся
из таблицы исключений.

Последняя группа — то, что не стеммится вовсе: `café`, `usb3`, `東京`, `3`.
`USB-C` в этой группе распадается на `usb | c` ещё до стеммеров.

### 4. Кеш и его предел

Кеш приватен и наружу не выставлен, поэтому скрипт читает его размер, привязывая
замыкание к объекту: `Closure::call()` даёт доступ к `$this->cache`, не меняя
класс и не используя reflection. Дальше — 200 000 различных шестибуквенных слов,
все из `a`–`z`, то есть все через английский стеммер.

```bash
docker compose cp /tmp/ch19-cache.php php:/tmp/ch19-cache.php
docker compose exec php php /tmp/ch19-cache.php
```

```php
<?php

declare(strict_types=1);

/**
 * The stem cache, and its bound, from the inside.
 *
 * The cache is private and has no accessor, because nothing outside the
 * analyzer needs it. Reading its size is still the only way to see the two
 * properties the docblock claims: one entry per distinct token (so repeats
 * are free), and a hard bound of `CACHE_ENTRIES` after which the cache is
 * emptied rather than grown. The size is read by binding a closure to the
 * analyzer — no reflection, and no change to the class.
 *
 * The bulk part feeds 200,000 distinct six-letter words, which all go
 * through the English stemmer. At 200,000 entries the next token triggers
 * the reset, so the cache falls back to a single entry — and the stem it
 * returns is still the same one an empty cache would return.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch19-cache.php php:/tmp/ch19-cache.php
 *   docker compose exec php php /tmp/ch19-cache.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Analysis\Analyzer;

$size = static fn (Analyzer $analyzer): int => (function (): int {
    return count($this->cache);
})->call($analyzer);

/**
 * 26^6 distinct words: aaaaaa, aaaaab, … — all ASCII letters, so every one
 * of them reaches the English stemmer.
 */
$word = static function (int $index): string {
    $letters = 'abcdefghijklmnopqrstuvwxyz';
    $word = '';

    for ($position = 0; $position < 6; $position++) {
        $word = $letters[$index % 26] . $word;
        $index = intdiv($index, 26);
    }

    return $word;
};

$analyzer = new Analyzer();

$repeated = $analyzer->terms('кабель кабели кабелей кабель');
printf(
    "repeats: %d terms, %d distinct tokens, %d cache entries\n",
    count($repeated),
    count(array_unique(['кабель', 'кабели', 'кабелей', 'кабель'])),
    $size($analyzer),
);

printf("CACHE_ENTRIES: %d\n", Analyzer::CACHE_ENTRIES);

$limit = Analyzer::CACHE_ENTRIES;

// A separate analyzer, so the repeats above do not shift the count.
$bulk = new Analyzer();

for ($index = 0; $index < $limit - 1; $index++) {
    $bulk->terms($word($index));
}

printf("after %d distinct words: %d cache entries\n", $limit - 1, $size($bulk));

$bulk->terms($word($limit - 1));
printf("after %d distinct words: %d cache entries\n", $limit, $size($bulk));

$overflow = 'connections';
$stems = $bulk->terms($overflow);
printf("after %d distinct words: %d cache entries\n", $limit + 1, $size($bulk));
printf("the word that emptied it: %s -> %s\n", $overflow, implode(' | ', $stems));

$fresh = new Analyzer();
$cached = $bulk->terms('Кабели USB-C и O’Neill');
$uncached = $fresh->terms('Кабели USB-C и O’Neill');
printf("cached result:   %s\n", implode(' | ', $cached));
printf("fresh result:    %s\n", implode(' | ', $uncached));
printf("identical: %s\n", $cached === $uncached ? 'yes' : 'no');
```

**Перевод комментариев:**
- `ch19-cache.php` (PHPDoc): кеш основ и его предел изнутри. Кеш приватный и без
  аксессора, потому что снаружи анализатора он никому не нужен. Но чтение его
  размера — единственный способ увидеть два свойства, обещанных докблоком: одна
  запись на различный токен (поэтому повторы бесплатны) и жёсткий предел
  `CACHE_ENTRIES`, после которого кеш очищается, а не растёт. Размер читается
  привязкой замыкания к анализатору — без reflection и без изменений в классе.
  Основная часть подаёт 200 000 различных шестибуквенных слов, и все они идут
  через английский стеммер. На 200 000 записей следующий токен запускает сброс,
  поэтому кеш падает до одной записи — и основа, которую он возвращает, всё та
  же, что вернул бы пустой кеш. Запуск в контейнере, где проект смонтирован в
  `/app`: `docker compose cp ...` и `docker compose exec ...`.
- `$word` (PHPDoc): 26^6 различных слов: `aaaaaa`, `aaaaab`, … — все из
  ASCII-букв, поэтому каждое доходит до английского стеммера.
- `// A separate analyzer, so the repeats above do not shift the count.` —
  отдельный анализатор, чтобы повторы выше не сдвигали счётчик.

```text
repeats: 4 terms, 3 distinct tokens, 3 cache entries
CACHE_ENTRIES: 200000
after 199999 distinct words: 199999 cache entries
after 200000 distinct words: 200000 cache entries
after 200001 distinct words: 1 cache entries
the word that emptied it: connections -> connect
cached result:   кабел | usb | c | и | oneil
fresh result:    кабел | usb | c | и | oneil
identical: yes
```

Девять строк, и в них видно обе заявленные свойства.

Первая строка: четыре терма из трёх различных токенов — три записи в кеше.
Повтор токена не создаёт новую запись, но и не схлопывает термы: `terms()`
возвращает все вхождения, потому что частота считается по ним.

Дальше счётчик растёт ровно на единицу за слово: 199 999, затем 200 000. На
двухсоттысячной записи кеш полон, и следующий токен — `connections` — сначала
очищает его целиком, а потом добавляет одну запись: в выводе `1`. Стемминг при
этом не ломается: `connections` → `connect`.

Последние строки — проверка, что кеш не может изменить результат: тот же текст
через анализатор после очистки и через совершенно новый анализатор даёт
одинаковые термы. Это и есть инвариант, который позволяет кешу быть настолько
простым.

## 5. Отказы

| Что ломается                            | Чем обнаруживается                                      | Что происходит                                                                                                |
| --------------------------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| Регистр не сложен                       | `Кабель` и `кабель` — разные термы                      | документ не находится; лечится шагом 2 (`mb_strtolower`)                                                      |
| `ё` не сложена в `е`                    | `ёжик` и `ежик` — разные термы                          | то же; шаг 2 делает `str_replace('ё', 'е', …)` **до** стемминга                                               |
| NFC вместо NFKC                         | `ﬁle` и `file`, `ＵＳＢ` и `USB` не совпадают           | лигатуры и полноширинные формы остаются разными термами                                                       |
| Невалидный UTF-8                        | `Normalizer::normalize()` вернул `false`                | `InvalidArgumentException('Text is not valid UTF-8.')` — единственная ошибка `terms()`                        |
| Апостроф разбит на токены               | `O’Neill` → `o`, `neill`; в словаре мусорный терм `o`   | запрос `oneill` не находит документ (разобрано в `docs/DECISIONS.md`)                                         |
| Апостроф на краю слова                  | `'quoted'` → `quot`                                     | кавычка остаётся разделителем, а не частью токена                                                             |
| Дефис и точка                           | `USB-C` → `usb`, `c`; `3.1` → `3`, `1`                  | версия и составное слово распадаются; это выбранная семантика                                                 |
| `usb3` против `usb 3`                   | разные термы (`usb3` и `usb` + `3`)                     | запрос не находит документ (эксперимент 2)                                                                    |
| `café` против `cafe`                    | `é` не входит в `a`–`z`, Porter2 не вызывается          | термы разные; диакритика сознательно не складывается                                                          |
| Текст на CJK                            | `東京` — один токен                                     | сегментации нет; для японского и китайского нужен словарь                                                     |
| Смешанный токен                         | `usb3` не проходит ни кириллический, ни английский путь | остаётся как есть, не стеммится                                                                               |
| Стеммер получил верхний регистр         | правила Snowball рассчитаны на нижний                   | `Analyzer` опускает регистр до стемминга; прямой вызов стеммера — ответственность вызывающего                 |
| `ё` при прямом вызове русского стеммера | `str_replace('ё', 'е')` внутри `stem()`                 | защита от расхождения с анализатором, который уже сложил `ё`                                                  |
| Смена анализатора                       | меняется каждый терм                                    | индекс перестраивается из журнала при следующем старте; миграции нет, потому что журнал хранит исходный текст |
| Кеш заполнен                            | `count($this->cache) >= CACHE_ENTRIES`                  | кеш очищается целиком; несколько основ считаются заново; результат не меняется                                |
| Пустой запрос после анализа             | `Corpus::search()` видит `$terms === []`                | `QueryRejected::EMPTY_QUERY`: `The query has no terms after analysis.`                                        |
| Запрос из эмодзи и пунктуации           | `terms()` возвращает `[]`                               | тот же `EMPTY_QUERY` — до индекса запрос не доходит                                                           |

## 6. Как это проверено

`tests/Analysis/AnalyzerTest.php` — 10 методов, 18 случаев (два набора данных:
четыре совместимые формы и шесть вариантов апострофа). Каждый тест — это одно
решение главы:

- `testEmptyAndPunctuationOnlyTextHasNoTerms`;
- `testRepeatedWordsKeepEveryOccurrence` — `кабель, кабели, кабелей` → `кабел`,
  `кабел`, `кабел`, и все три вхождения остаются;
- `testCaseDoesNotMatter`, `testYoIsTheSameLetterAsYe`;
- `testCompatibilityFormsMatchWhatPeopleType` — `ＵＳＢ`, `ﬁle`, `и + U+0306`,
  `е + U+0308`, каждое против того, что человек набрал бы руками;
- `testNumbersAreTermsAndAreNotStemmed` — `USB-C 3.1, 2 × 0` →
  `usb | c | 3 | 1 | 2 | 0`;
- `testEachScriptGoesToItsOwnStemmerAndMixedTokensAreKept` —
  `кабели cables usb3 café 東京` → `кабел | cabl | usb3 | café | 東京`;
- `testAnApostropheInsideAWordDoesNotMatter` — шесть вариантов, включая
  модификатор `ʼ` (`U+02BC`), который NFKC не складывает с `'`;
- `testAnApostropheAtTheEdgeOfAWordSeparates`;
- `testInvalidUtf8IsRejected` — байты `\xC3\x28`.

`tests/Analysis/Snowball/SnowballVocabularyTest.php` — два теста, по одному на
язык, но за ними 92 434 пары из официальных словарей: **49 785** русских и **42
649** английских (`tests/Fixtures/snowball`, 2.1 MB). Это тот случай, когда
«один тест на слово» был бы хуже: 92 тысячи кейсов в data provider провели бы
больше времени в PHPUnit, чем в стеммере. Поэтому тест проходит словарь целиком
и при расхождении печатает первые двадцать несовпадений — этого достаточно,
чтобы найти сломанное правило.

Обе стороны прибиты к конкретным коммитам, и это часть контракта: алгоритмы —
`snowballstem/snowball` на `14a4f24`, словари — `snowballstem/snowball-data` на
`a0ec0d0`, оба под BSD-3 (текст лицензии лежит в `COPYING` рядом со стеммерами).
Алгоритм меняется (в 2025-10 добавили префикс `inter`), поэтому «взять свежий
Snowball» и «взять свежие словари» — две разные операции, и рассинхронизировать
их нельзя.

Прогоны при написании главы: `tests/Analysis` — OK, **20 тестов, 23
утверждения**; весь набор `php-mini-search` — OK, **180 тестов, 47 230
утверждений** (Docker, PHP 8.5.11). Отдельно `docs/BENCHMARKS.md` фиксирует, что
92 434 пары и сравнение с наивным оракулом проходят неизменными и после
включения кеша — то есть кеш не поменял ни одного терма.

## 7. Цифры

| Файл                                                 | Строк | Что делает                               |
| ---------------------------------------------------- | ----- | ---------------------------------------- |
| `src/Analysis/Analyzer.php`                          | 96    | пять шагов, маршрутизация, кеш           |
| `…/Snowball/RussianStemmer.php`                      | 239   | порт `russian.sbl`: RV/R2, четыре шага   |
| `…/Snowball/EnglishStemmer.php`                      | 354   | порт `english.sbl`: Porter2, семь шагов  |
| `src/Corpus/Corpus.php`                              | 405   | анализ документа и запроса в одном месте |
| `tests/Analysis/AnalyzerTest.php`                    | 109   | 18 случаев на конвейер                   |
| `tests/Analysis/Snowball/SnowballVocabularyTest.php` | 67    | 92 434 пары на два теста                 |

| Словари и замеры              | Значение                              |
| ----------------------------- | ------------------------------------- |
| Русских пар «слово → основа»  | 49 785                                |
| Английских пар                | 42 649                                |
| Всего пар                     | 92 434                                |
| Объём фикстур                 | 2.1 MB                                |
| Стоимость анализа             | 12 µs на токен                        |
| Доля стемминга в анализе      | 96%                                   |
| Доля повторяющихся токенов    | 90% (5 млн токенов, 27 379 различных) |
| Предел кеша                   | 200 000 записей                       |
| Память кеша                   | около 4 MB на синтетическом корпусе   |
| Анализ корпуса, до/после кеша | ~60 s → 1.5 s                         |
| Replay журнала при старте     | 29.9 s → 5.0 s                        |
| Один `PUT`, p50               | 24.4 ms → 8.2 ms                      |
| Загрузка корпуса              | 339 → 985 документов/с                |

Живые прогоны (все детерминированы, два прогона каждого совпали байт-в-байт):

| Что              | Значение                                                                    |
| ---------------- | --------------------------------------------------------------------------- |
| `ch19-pipeline`  | 59 строк; `ﬁle ＵＳＢ` → `file USB`; `и + U+0306` → `U+0439`                |
| `ch19-doc-query` | 40 строк; 6 пар из 8 совпали; обе несовпавшие названы заранее               |
| `ch19-stems`     | 43 строки; `кабель` в шести формах → `кабел`; `connect` в пяти → `connect`  |
| `ch19-cache`     | 9 строк; 199 999 → 200 000 → **1**; `connections` → `connect` после очистки |

## 8. Упражнения

1. **Проследите.** Проведите `Ёжик зелёный` по всем пяти шагам и выпишите
   промежуточные строки. На каком шаге исчезает различие с `ежик зеленый` и
   почему ни один из шагов после этого не может его вернуть?
2. **Объясните.** Почему `ﬁle` и `file` — один терм, а `café` и `cafe` — два? В
   ответе должны фигурировать два разных механизма: нормализация и маршрутизация
   по письменности.
3. **Разберите порядок.** Почему апостроф удаляется **до** токенизации, а не
   после? Что получилось бы из `д’Артаньян`, если поменять эти два шага местами?
4. **Найдите правило.** `дартаньян` → `дартанья`. Какая таблица русского
   стеммера содержит сработавшее окончание, какое дополнительное условие при
   этом выполнилось и почему для `сон` то же окончание не удаляется?
5. **Сравните.** Кеш при переполнении очищается целиком, а не вытесняет одну
   запись. Чем это лучше LRU по коду и по предсказуемости — и что стоит на очень
   большом словаре?
6. **Спроектируйте тест.** Как поймать асимметрию «документ и запрос
   анализируются по-разному», не перечисляя все пары вручную? Опишите свойство,
   которое должно выполняться для любого текста, и как его проверить на корпусе.
7. **Оцените цену.** Смена анализатора меняет каждый терм. Что именно позволяет
   `php-mini-search` пережить это перезапуском, а не миграцией, и какое одно
   изменение в хранении сделало бы такую смену недопустимой без полной
   переиндексации?

## Что дальше

- **Глава 20** — инвертированный индекс и BM25: что происходит с термами дальше
  — постинги, частоты, длины полей и оценка.
- **Глава 21** — версии, надгробия и переиндексация: почему смена анализатора
  здесь безопасна и что делает проект, когда журнал отложен в сторону.
- **Глава 22** — границы компонента: как этот анализатор и индекс встраиваются в
  платформу, не превращая поиск во вторую базу данных.
