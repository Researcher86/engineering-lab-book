# Глава 16. SQL: от текста к плану выполнения

> Проект:
> [`php-mini-database`](https://github.com/Researcher86/php-mini-database)
> (релиз `v1.1.0`, коммит `9adde5d`). Нужно знать: страницу, кучу и B-дерево (→
> глава 15) — выбранное здесь индексное сканирование читает ровно то дерево.
> Пригодятся дальше: транзакции и изоляция (→ глава 17), WAL, fsync и
> восстановление (→ глава 18), анализ текста: Unicode, токены, стемминг (→ глава
> 19), инвертированный индекс (→ глава 20), `kill -9` как проверка (→ глава 26).

Глава 15 закончилась тем, что база научилась отвечать на два вопроса: «дай
строку по адресу» и «найди строки по значению». Оба ответа были возможны, потому
что уже было понятно, _что_ искать. Эта глава — о том, как из строки текста
становится понятно вообще всё.

## 1. Проблема

SQL приходит текстом: байт за байтом, с кавычками, комментариями и пробелами.
Между этим текстом и страницами из главы 15 лежит цепочка преобразований, и у
каждой её ступени своя хрупкость.

**Хрупкость первая: текст лжёт о своей структуре.** Кавычка в SQL — не декор:
одинарная открывает строковый литерал, двойная — идентификатор, а удвоенная
кавычка внутри литерала (`'O''Brien'`) — это не закрытие, а экранирование.
Счётчик символов, который это игнорирует, споткнётся и примет середину литерала
за его конец. Ещё хуже — отказ без позиции: «синтаксическая ошибка» в
четырёхсотстрочном скрипте ничего не говорит.

**Хрупкость вторая: исполнение «по мере разбора» не оставляет места выбору.**
Если `SELECT` разбирается сразу в последовательность вызовов чтения, то первая
же итерация прочитала таблицу последовательным обходом прежде, чем кто-то
заметил существующий индекс. Чтобы выбрать способ доступа, нужен момент, когда
запрос уже понятен, но ещё не тронут, — то есть промежуточное представление.
План.

**Хрупкость третья: NULL — не отсутствие значения, а третье значение логики.**
Сравнение с NULL даёт не `false`, а «неизвестно», и каждое место, проверяющее
условие, должно само решать, что делать с неизвестным. Размазанное по коду такое
решение где-нибудь да выльется в классическую пару: строка исчезла из выборки,
но прошла `CHECK`, — или наоборот.

**Хрупкость четвёртая: чтение и запись в одном проходе несовместимы.** `UPDATE`,
пишущий по мере чтения, может прочитать собственную запись: строка, выросшая при
обновлении (глава 15), переезжает в конец файла — ровно туда, где сканер ещё не
был. Второе посещение той же строки даёт неверный результат без единой ошибки.

`php-mini-database` отвечает на все четыре разом: текст разбирается одним
проходом с позициями в байтах, разобранный запрос превращается в **план** —
данные, а не в последовательность действий, — условия проверяются в **двух**
заранее отведённых местах, а мутации из конвейера чтения исключены конструкцией.

## 2. Устройство

### Пять шагов, разделённых данными

```text
"SELECT name FROM users WHERE age > 18"
        │  Lexer::tokenize()          один проход по байтам
        ▼
list<Token>                            типы, текст, позиция каждого токена
        │  Parser::parseOne()         рекурсивный спуск по приоритетам
        ▼
SelectStatement                        дерево Statement-объектов
        │  Planner                    извлечение порядка операций
        ▼
LogicalPlan                            Scan → Filter → Project → Limit
        │  Optimizer                  4 правила, фиксированный порядок
        ▼
LogicalPlan                            тот же, но Scan с индексом
        │  Executor::compile()        match по типу узла
        ▼
Operator                               генераторы, пулл
        │  foreach
        ▼
Row, Row, Row ...
```

Каждая стрелка — это данные, а не звонок. Лексер отдаёт список токенов целиком;
парсер — дерево; планировщик — массив узлов; оптимизатор переписывает узлы в
новый массив. Любой из этих моментов можно остановить и посмотреть, и ровно это
делает `EXPLAIN`: печатает план между оптимизатором и компиляцией, по одной
строке на узел, с отступом в два пробела на уровень (так предписано в
`docs/sql.md`).

Планировщик получает на вход только `SelectStatement` с `FROM`; запрос без
таблицы (`SELECT 1 + 1`) до него не доходит — он вычисляется напрямую,
планировать нечего.

### План — данные

`LogicalPlan` — интерфейс всего на две функции: `describe()` и `children()`.
Этого хватает всем трём потребителям: `EXPLAIN` рекурсивно обходит дерево и
печатает, правила оптимизатора рекурсивно его переписывают,
`Executor::compile()` рекурсивно собирает исполняемые операторы. Никто из трёх
не знает о существовании остальных.

Планировщик при этом ничего не решает о способе доступа: каждый `Scan`
начинается с `index: null`, и это не лень, а разделение ответственности —
деревья главы 15 не должны попадать в план по пути разбора.

### Четыре правила и их порядок

`Optimizer` применяет четыре правила строго по очереди, без поиска по стоимости:
каждое правило видит план целиком и возвращает новый там, где его шаблон совпал.

1. **`ConstantFolding`** — сворачивает подвыражения из литералов:
   `WHERE 2 + 3 = 5 AND age > 18` теряет `2 + 3 = 5`.
2. **`PredicatePushdown`** — опускает каждый конъюнкт `WHERE` выше `Join` на ту
   сторону, которая может ответить на него сама.
3. **`IndexSelection`** — по equality-предикату подбирает индекс для `Scan`.
4. **`JoinReordering`** — перестраивает цепочку `INNER JOIN` по числу страниц.

Порядок закреплён докблоком `Optimizer`: свёртка первой, чтобы все позднейшие
правила видели упрощённые выражения; спуск предиката перед выбором индекса,
чтобы конъюнкт, спущенный на сторону соединения, успел совпасть с индексом этой
стороны; переупорядочивание последним, поскольку ни одно из первых трёх не
меняет оценку числа строк соединения. `DECISIONS.md` описывает философию выбора
индекса так: «A wrong or missed opportunity here only costs a slower scan, never
a wrong answer». Пропустить возможность — медленнее; ошибиться — недопустимо.

Здесь же — единственный обнаруженный в документации расхождение с кодом:
`docs/architecture.md` до сих пор говорит «currently one rule,
`Rule\JoinReordering`», хотя правил четыре. Код и `docs/sql.md` верны, устарел
обзорный документ.

### Лень: всё работает только когда тянут

Операторы чтения — это генераторы PHP, встроенные друг в друга. Никто не
запускает обход: `foreach` по внешнему оператору тянет ровно столько строк,
сколько ему нужно, и ровно столько же прочитает каждый нижний уровень. `LIMIT 2`
над фильтром останавливает скан после второй подходящей строки, и все строки
после неё не читаются вовсе — это наблюдение эксперимента 3.

### Два ворота и мутации мимо конвейера

Трёхзначная логика живёт в двух местах, и это места с зеркальными правилами.
`Evaluator::isTrue()` — ворота `WHERE` и `HAVING`: строка проходит только при
определённом `true`. `Evaluator::isFalse()` — ворота `CHECK`: строка отклоняется
только при определённом `false`. NULL проходит `CHECK` и не проходит `WHERE` —
одна и та же логика, два направления.

И наконец, мутации конвейером не ходят вовсе. `Operator` — интерфейс только для
чтения; `INSERT`, `UPDATE` и `DELETE` читают кучу напрямую, и `UPDATE` сначала
собирает все совпавшие строки, и только потом пишет. Почему именно так — раздел
3, последний блок кода, а проверка — эксперимент 4.

## 3. Код

### Лексер: один проход, позиции в байтах

`Lexer::tokenize()` возвращает **весь** список токенов разом, и у каждого
записана его позиция в исходных байтах. Цикл `run()` тривиален: пропустить
пробелы и комментарии, проверить конец, вызвать `next()`.

```php
public static function tokenize(string $source): array
{
    return (new self($source))->run();
}

/** @return list<Token> */
private function run(): array
{
    $tokens = [];

    while (true) {
        $this->skipWhitespaceAndComments();

        if ($this->atEnd()) {
            $tokens[] = new Token(TokenType::EOF, '', $this->position);

            return $tokens;
        }

        $tokens[] = $this->next();
    }
}
```

[`src/Sql/Lexer.php#L41-L62`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Lexer.php#L41-L62)

**Перевод комментариев:**
- `run()` (`@return`): возвращает полный список токенов `Token` в порядке
  разбора.

Разбор первого символа — `match` по четырём случаям, и ни одного `if`-цепочки:
кавычки, цифра, начало идентификатора, всё остальное — оператор.

```php
private function next(): Token
{
    $char = $this->source[$this->position];

    return match (true) {
        $char === '"' => $this->quotedIdentifier(),
        $char === "'" => $this->stringLiteral(),
        $this->isDigit($char) => $this->number(),
        $this->isIdentifierStart($char) => $this->identifierOrKeyword(),
        default => $this->operator(),
    };
}
```

[`src/Sql/Lexer.php#L64-L75`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Lexer.php#L64-L75)

Ключевое свойство токена: его `text` — это уже **значение**, а не написание.
Удвоенная кавычка схлопывается лексером (`delimited()` ниже), и парсер никогда
не перечитывает исходник. Отказ об одном и том же срабатывает в двух типах
литералов — строковом и идентификаторе — потому что оба сводятся к одному методу
с параметром-кавычкой:

```php
private function delimited(string $quote, string $kind): string
{
    $start = $this->position;
    $this->position++; // opening quote

    $text = '';

    while (true) {
        if ($this->atEnd()) {
            throw ParserException::at($this->source, $start, sprintf('Unterminated %s.', $kind));
        }

        $char = $this->source[$this->position];

        if ($char !== $quote) {
            $text .= $char;
            $this->position++;
            continue;
        }

        // A doubled quote is an escaped literal quote; a lone one ends
        // the token.
        if ($this->peek(1) === $quote) {
            $text .= $quote;
            $this->position += 2;
            continue;
        }

        $this->position++;

        return $text;
    }
}
```

[`src/Sql/Lexer.php#L100-L132`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Lexer.php#L100-L132)

**Перевод комментариев:**
- `// opening quote` — открывающая кавычка.
- `// A doubled quote is an escaped literal quote; a lone one ends` — удвоенная
  кавычка — это экранированная литеральная кавычка; одиночная завершает токен.

Обратите внимание на `$start`: позиция, на которую указывает отказ об незакрытой
строке, — позиция **открывающей** кавычки, а не того места, где текст кончился.
Эксперимент 1 показывает обе позиции рядом.

### Парсер: разбирает или объясняет отказ

Точка входа, требующая ровно одного выражения:

```php
public static function parseOne(string $sql): Statement
{
    $statements = self::parse($sql);

    if (count($statements) !== 1) {
        throw new ParserException(sprintf('Expected exactly one statement, found %d.', count($statements)), 1, 1);
    }

    return $statements[0];
}
```

[`src/Sql/Parser.php#L130-L139`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Parser.php#L130-L139)

Приоритеты — рекурсивный спуск, по одному методу на уровень. Каждый уровень
разбирает свой тип связки левым циклом, поэтому цепочки `a AND b AND c`
собираются влево, а `OR` оказывается самым низким — ровно как предписано в
`docs/sql.md`: «precedence climbs multiplication over addition over comparison
over `NOT` over `AND` over `OR`». Одноимённый `notExpression()` зеркалит `NOT` в
глубину, что и даёт право унарному оператору «прилипать» к правому operand'у:

```php
private function expression(): Expression
{
    return $this->orExpression();
}

private function orExpression(): Expression
{
    $left = $this->andExpression();

    while ($this->match(TokenType::OR)) {
        $left = new BinaryOp($left, BinaryOperator::OR, $this->andExpression());
    }

    return $left;
}

private function andExpression(): Expression
{
    $left = $this->notExpression();

    while ($this->match(TokenType::AND)) {
        $left = new BinaryOp($left, BinaryOperator::AND, $this->notExpression());
    }

    return $left;
}

private function notExpression(): Expression
{
    if ($this->match(TokenType::NOT)) {
        return new UnaryOp(UnaryOperator::NOT, $this->notExpression());
    }

    return $this->comparisonExpression();
}
```

[`src/Sql/Parser.php#L747-L781`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Parser.php#L747-L781)

Отказ парсера несёт в себе и позицию. `ParserException` хранит строку и столбец
отдельно от сообщения и переводит байтовую позицию в пару «строка : столбец»
лениво — только когда исключение собираются бросить:

```php
public function __construct(
    string $message,
    public readonly int $sourceLine,
    public readonly int $sourceColumn,
) {
    parent::__construct(sprintf('%s (line %d, column %d)', $message, $sourceLine, $sourceColumn));
}

public static function at(string $source, int $position, string $message): self
{
    [$line, $column] = self::lineAndColumn($source, $position);

    return new self($message, $line, $column);
}

/**
 * @return array{0: int, 1: int} 1-based line, then 1-based column
 */
private static function lineAndColumn(string $source, int $position): array
{
    $before = substr($source, 0, $position);
    $line = substr_count($before, "\n") + 1;
    $lastNewline = strrpos($before, "\n");
    $column = $position - ($lastNewline === false ? -1 : $lastNewline);

    return [$line, $column];
}
```

[`src/Exception/ParserException.php#L19-L45`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Exception/ParserException.php#L19-L45)

**Перевод комментариев:**
- `lineAndColumn()` (PHPDoc): возвращает `[строка, столбец]`, оба считаются с 1:
  сначала строка, затем столбец.

Подсчёт столбца — это один `substr` и один `strrpos`, и на каждый отказ один
раз. Дешевле хранить позицию и не считать, чем считать на каждый токен.

### План — данные: интерфейс на две функции

```php
interface LogicalPlan
{
    /** One line describing this node alone, without its children. */
    public function describe(): string;

    /** @return list<LogicalPlan> */
    public function children(): array;
}
```

[`src/Sql/Planner/LogicalPlan.php#L27-L34`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Planner/LogicalPlan.php#L27-L34)

**Перевод комментариев:**
- `describe()` (PHPDoc): одна строка, описывающая только этот узел, без его
  детей.
- `children()` (`@return`): возвращает список дочерних `LogicalPlan`.

### Планировщик: извлечение, а не изобретение

Докблок `Planner` — редкий случай, когда намерение класса сформулировано в коде
точнее, чем в обзорной документации:

```php
/**
 * Turns a `SelectStatement` with a `FROM` clause into a `LogicalPlan` —
 * what to read and in what order to do the rest, before anything decides
 * *how* (`Sql\Optimizer\Optimizer`) or runs it (`Execution\Executor`).
 *
 * This is a direct extraction of what `Execution\Executor` built ad hoc for
 * a `SELECT` through Phase 8: the shape of the tree below is exactly
 * `executeSelectFromTable()`/`executeSelectFromJoin()`/`finishSelect()`'s
 * old pipeline order, just as data instead of as a sequence of `Operator`
 * constructions. Nothing here picks an access method or a join
 * algorithm — every `Scan` starts with `index: null` and every `Join`
 * with `hash: null`, exactly the defaults `Optimizer`'s rules exist to
 * fill in.
 *
 * A `SELECT` with no `FROM` at all never reaches this class:
 * `Execution\Executor::executeSelectWithoutFrom()` evaluates its select
 * list directly, since there is no table to plan a read against.
 */
```

[`src/Sql/Planner/Planner.php#L32-L49`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Planner/Planner.php#L32-L49)

**Перевод комментариев:**
- `Planner` (PHPDoc): превращает `SelectStatement` с секцией `FROM` в
  `LogicalPlan` — что читать и в каком порядке делать остальное, до того как
  что-либо решит, *как* (`Sql\Optimizer\Optimizer`), или выполнит это
  (`Execution\Executor`). Это прямое извлечение того, что `Execution\Executor`
  собирал на лету для `SELECT` до фазы 8: форма дерева ниже — в точности старый
  порядок конвейера `executeSelectFromTable()`
  /`executeSelectFromJoin()`/`finishSelect()`, только в виде данных, а не
  последовательности конструкций `Operator`. Здесь ничего не выбирает способ
  доступа или алгоритм соединения — каждый `Scan` начинается с `index: null`, а
  каждый `Join` с `hash: null`, ровно те значения по умолчанию, которые
  существуют, чтобы их заполняли правила `Optimizer`. `SELECT` вообще без `FROM`
  никогда не доходит до этого класса:
  `Execution\Executor::executeSelectWithoutFrom()` вычисляет свой список выборки
  напрямую, поскольку планировать чтение не по чему.

Порядок операций — функция `tail()`, и в нём видно обе расстановки сортировки:
перед выборкой столбцов для простого запроса и после агрегации для
сгруппированного. Обе нужны: `Sort` должен видеть и столбцы, которых ещё нет в
выходных (`Project` ещё не выполнен), и группы, которых ещё нет в строках
(`Aggregate` уже выполнил обе — и `docs/sql.md` называет обе расстановки
необходимыми: «Both placements are necessary…»):

```php
private function tail(LogicalPlan $plan, SelectStatement $statement, array $items, array $labels): LogicalPlan
{
    if ($statement->where !== null) {
        $plan = new Filter($plan, $statement->where);
    }

    if ($statement->groupBy !== [] || $statement->having !== null || $this->containsAggregate($items)) {
        $plan = new Aggregate($plan, $statement->groupBy, $items, $labels, $statement->having);

        if ($statement->orderBy !== []) {
            $plan = new Sort($plan, $statement->orderBy);
        }
    } else {
        if ($statement->orderBy !== []) {
            $plan = new Sort($plan, $statement->orderBy);
        }

        $plan = new Project($plan, $items, $labels);
    }

    if ($statement->distinct) {
        $plan = new Distinct($plan);
    }

    return new Limit($plan, $statement->limit, $statement->offset ?? 0);
}
```

[`src/Sql/Planner/Planner.php#L120-L145`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Planner/Planner.php#L120-L145)

Верхний узел — всегда `Limit`, даже когда `LIMIT` не написан: у плана есть дно,
и это дно называется «все строки». Тест `testEveryPlanEndsInALimitNode`
проверяет это для каждого плана в каждом тесте планировщика.

### Оптимизатор: цепочка, а не поиск

`Optimizer` — 46 строк: список правил и цикл. Никакого дерева вариантов, никаких
оценок стоимости — каждый шаг предсказуем и читается сверху вниз:

```php
/**
 * Rewrites a `LogicalPlan` into the one `Execution\Executor` actually runs,
 * by applying a fixed sequence of `Rule`s — there is no cost-based search
 * over alternative rewrites, each rule always fires wherever its shape
 * matches. The order is deliberate: `ConstantFolding` first, so every later
 * rule sees already-simplified expressions; `PredicatePushdown` before
 * `IndexSelection`, so a conjunct pushed onto a join side is still there to
 * match against that side's indexes; `JoinReordering` last, since neither
 * of the other two changes a join's row-count estimate.
 */
final readonly class Optimizer
{
    /** @var list<Rule> */
    private array $rules;

    public function __construct()
    {
        $this->rules = [
            new ConstantFolding(),
            new PredicatePushdown(),
            new IndexSelection(),
            new JoinReordering(),
        ];
    }

    public function optimize(LogicalPlan $plan, PlanContext $context): LogicalPlan
    {
        foreach ($this->rules as $rule) {
            $plan = $rule->apply($plan, $context);
        }

        return $plan;
    }
}
```

[`src/Sql/Optimizer/Optimizer.php#L13-L46`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Optimizer/Optimizer.php#L13-L46)

**Перевод комментариев:**
- `Optimizer` (PHPDoc): переписывает `LogicalPlan` в тот единственный, который
  реально исполняет `Execution\Executor`, применяя фиксированную
  последовательность `Rule` — поиска по стоимости среди альтернативных
  переписываний нет, каждое правило срабатывает всегда, когда совпадает его
  форма. Порядок намеренный: `ConstantFolding` первым, чтобы все последующие
  правила видели уже упрощённые выражения; `PredicatePushdown` до
  `IndexSelection`, чтобы конъюнкт, спущенный на сторону соединения, всё ещё был
  там для сопоставления с индексами этой стороны; `JoinReordering` последним,
  поскольку ни одно из двух других не меняет оценку числа строк соединения.
- `$rules` (`@var`): фиксированный список правил оптимизатора в порядке
  применения.

### Выбор индекса: узкое правило вместо широкой эвристики

Правило видит только «фильтр над сканом» и спрашивает три вещи: является ли
источник `Scan`, не выбран ли уже индекс, и есть ли в предикате
`столбец <операция> константа` по столбцу, на котором индекс существует. Ответ
«нет» на любой из трёх — предикат остаётся как был:

```php
private function applyFilter(Filter $plan, PlanContext $context): LogicalPlan
{
    $source = $this->apply($plan->source, $context);

    if (!$source instanceof Scan || $source->index !== null) {
        return new Filter($source, $plan->predicate);
    }

    $predicate = $this->indexablePredicate($plan->predicate);
    $indexName = $predicate === null ? null : $source->table->singleColumnIndexNameFor($predicate['column']);

    if ($predicate === null || $indexName === null) {
        return new Filter($source, $plan->predicate);
    }

    $index = new IndexAccess($indexName, $predicate['column'], $predicate['op'], $predicate['value']);

    return new Filter($source->withIndex($index), $plan->predicate);
}

/** @return array{column: string, op: 'eq'|'lt'|'lte'|'gt'|'gte', value: Expression}|null */
private function indexablePredicate(Expression $where): ?array
{
    if (!$where instanceof BinaryOp) {
        return null;
    }

    if ($where->operator === BinaryOperator::AND) {
        return $this->indexablePredicate($where->left) ?? $this->indexablePredicate($where->right);
    }

    $op = match ($where->operator) {
        BinaryOperator::EQUAL => 'eq',
        BinaryOperator::LESS_THAN => 'lt',
        BinaryOperator::LESS_THAN_OR_EQUAL => 'lte',
        BinaryOperator::GREATER_THAN => 'gt',
        BinaryOperator::GREATER_THAN_OR_EQUAL => 'gte',
        default => null,
    };

    if ($op === null || !$where->left instanceof ColumnRef || !$this->isConstant($where->right)) {
        return null;
    }

    return ['column' => $where->left->column, 'op' => $op, 'value' => $where->right];
}

private function isConstant(Expression $expression): bool
{
    return $expression instanceof Literal || $expression instanceof Placeholder;
}
```

[`src/Sql/Optimizer/Rule/IndexSelection.php#L59-L109`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Optimizer/Rule/IndexSelection.php#L59-L109)

**Перевод комментариев:**
- `indexablePredicate()` (`@return`): возвращает `column` /`op`/`value` для
  предиката, пригодного для индекса, или `null`.

Два решения заслуживают внимания. Первое: `Filter` **остаётся** над `Scan` с
индексом — индекс сужает читаемое, а полный предикат перепроверяет каждую
строку, которую индекс отдал. Тест `testTheFilterIsKeptEvenOnceAnIndexIsChosen`
следит, чтобы сужение так и не превратилось в замену. Второе: `isConstant()`
принимает и `Placeholder` — параметр `?` ещё не имеет значения, но уже
достаточно известен, чтобы по нему выбрать индекс.

### Спуск предиката: корректность прежде узости

Докблок `PredicatePushdown` — самый подробный в оптимизаторе, и его центральное
место — не оптимизация, а запрет: спускать предикат на `WHERE`-сторону
`LEFT JOIN` нельзя, потому что это меняет ответы:

```php
/**
 * Moves each top-level `AND` conjunct of a `WHERE` sitting above a `Join`
 * down onto whichever side alone can answer it, so that side's `Scan`
 * (and, once `IndexSelection` runs, its index) sees a narrower predicate
 * before the join ever runs — a `HashJoin`/`NestedLoopJoin` then does less
 * work on that side, and `IndexSelection` gets a chance it did not have
 * before this rule existed: `Execution\Executor` never used an `IndexScan`
 * for a join side prior to Phase 9, since nothing before this ran a per-
 * side predicate through it.
 *
 * Two restrictions are not narrowing choices but correctness requirements.
 * First, a conjunct only pushes past a side that is never the outer join's
 * nullable one: pushing a `WHERE` predicate onto a `LEFT JOIN`'s right side
 * (or a `RIGHT JOIN`'s left) would filter rows out *before* the join has a
 * chance to null-pad them for a left row with no match, changing which
 * rows the query returns — a well-known pitfall of pushdown past outer
 * joins, not a simplification specific to this project. Second, a conjunct
 * that references an unqualified column, or spans both sides, is left
 * exactly where it was (the residual `Filter` above the join): resolving
 * an unqualified reference to a specific side needs the runtime
 * ambiguity-checking `Execution\Expression\QualifiedRowContext` already
 * does, which this rule does not reproduce statically — see DECISIONS.md.
 */
```

[`src/Sql/Optimizer/Rule/PredicatePushdown.php#L33-L55`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Optimizer/Rule/PredicatePushdown.php#L33-L55)

**Перевод комментариев:**
- `PredicatePushdown` (PHPDoc): спускает каждый конъюнкт верхнего уровня `AND`
  из `WHERE` над `Join` на ту сторону, которая одна может на него ответить,
  чтобы `Scan` этой стороны (а после запуска `IndexSelection` — его индекс)
  увидел более узкий предикат ещё до выполнения соединения: тогда `HashJoin`
  /`NestedLoopJoin` делает на этой стороне меньше работы, а `IndexSelection`
  получает шанс, которого у него не было до появления этого правила —
  `Execution\Executor` вообще не использовал `IndexScan` для стороны соединения
  до фазы 9, потому что ничто раньше не пропускало предикат отдельной стороны
  через него. Два ограничения — не выбор в пользу узости, а требования
  корректности. Во-первых, конъюнкт спускается только через сторону, которая
  никогда не является nullable-стороной внешнего соединения: спуск предиката
  `WHERE` на правую сторону `LEFT JOIN` (или левую `RIGHT JOIN`) отфильтровал бы
  строки *до* того, как соединение успеет дополнить их `NULL` для левой строки
  без совпадения, и изменил бы набор возвращаемых строк — известная ловушка
  спуска предикатов через внешние соединения, а не упрощение, специфичное для
  этого проекта. Во-вторых, конъюнкт, ссылающийся на неквалифицированный столбец
  или затрагивающий обе стороны, остаётся ровно там, где был (остаточный
  `Filter` над соединением): разрешение неквалифицированной ссылки до конкретной
  стороны требует проверки неоднозначности во время выполнения, которую уже
  делает `Execution\Expression\QualifiedRowContext`, а это правило её статически
  не воспроизводит — см. DECISIONS.md.

Эксперимент 2 показывает обе стороны: конъюнкт спускается на сторону
`INNER JOIN` и получает индекс, а конъюнкт над `LEFT JOIN` оставляет фильтр на
месте — и тот же самый `age > 18` в двух запросах выглядит по-разному.

### EXPLAIN: план как дерево строк

Всё, что нужно для `EXPLAIN`, — это `describe()` на узле. Скан печатает себя
двумя строками кода, различающими наличие индекса:

```php
public function describe(): string
{
    if ($this->index === null) {
        return sprintf('SeqScan %s', $this->reference());
    }

    return sprintf(
        'IndexScan %s USING %s (%s %s %s)',
        $this->reference(),
        $this->index->indexName,
        $this->index->column,
        $this->operatorSymbol($this->index->operator),
        ExpressionPrinter::forExplain()->print($this->index->value),
    );
}

public function children(): array
{
    return [];
}
```

[`src/Sql/Planner/Plan/Scan.php#L41-L60`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Sql/Planner/Plan/Scan.php#L41-L60)

Исполнитель `EXPLAIN` — рекурсия в восемь строк: план строится, но не
компилируется, и печатается по одному строковому представлению на узел:

```php
private function executeExplain(ExplainStatement $statement): QueryResult
{
    if ($statement->statement->from === null) {
        throw new ExecutionException('EXPLAIN requires a FROM clause.');
    }

    $lines = [];
    $this->explainLines($this->plan($statement->statement)->plan, 0, $lines);

    return new QueryResult(['plan'], array_map(static fn (string $line): Row => new Row(['plan' => $line]), $lines));
}

/** @param list<string> $lines */
private function explainLines(LogicalPlan $plan, int $depth, array &$lines): void
{
    $lines[] = str_repeat('  ', $depth) . $plan->describe();

    foreach ($plan->children() as $child) {
        $this->explainLines($child, $depth + 1, $lines);
    }
}
```

[`src/Execution/Executor.php#L425-L445`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L425-L445)

Отступ `str_repeat('  ', $depth)` — тот самый двухпробельный из `docs/sql.md`.
Результат `EXPLAIN` — обычный `QueryResult` с одним столбцом `plan`, поэтому его
можно сортировать и фильтровать теми же средствами, что и данные.

### Компиляция: match по типу узла

`compile()` — единственное место, где план становится кодом. Один `match`,
рекурсия вниз по каждому ребру и жёсткая отказ в `default` — неизвестный узел не
может быть пропущен, он обязан быть обработан явно:

```php
private function compile(LogicalPlan $plan, bool $isJoin, array $parameters, Closure $contextFor): Operator
{
    return match (true) {
        $plan instanceof PlanScan => $this->compileScan($plan, $isJoin, $parameters),
        $plan instanceof PlanFilter => new Filter(
            $this->compile($plan->source, $isJoin, $parameters, $contextFor),
            $plan->predicate,
            $this->evaluator,
            $contextFor,
        ),
        $plan instanceof PlanJoin => $this->compileJoin($plan, $isJoin, $parameters, $contextFor),
        $plan instanceof PlanAggregate => new Aggregate(
            $this->compile($plan->source, $isJoin, $parameters, $contextFor),
            $plan->groupBy,
            $plan->items,
            $plan->labels,
            $plan->having,
            $this->evaluator,
            $contextFor,
        ),
        $plan instanceof PlanSort => new Sort(
            $this->compile($plan->source, $isJoin, $parameters, $contextFor),
            $plan->orderBy,
            $this->evaluator,
            $plan->source instanceof PlanAggregate ? static fn (Row $row): EvaluationContext => new RowContext($row) : $contextFor,
        ),
        $plan instanceof PlanProject => new Project(
            $this->compile($plan->source, $isJoin, $parameters, $contextFor),
            $plan->items,
            $plan->labels,
            $this->evaluator,
            $contextFor,
        ),
        $plan instanceof PlanDistinct => new Distinct($this->compile($plan->source, $isJoin, $parameters, $contextFor)),
        $plan instanceof PlanLimit => new Limit($this->compile($plan->source, $isJoin, $parameters, $contextFor), $plan->limit, $plan->offset),
        default => throw new ExecutionException(sprintf('Cannot compile plan node %s.', $plan::class)),
    };
}
```

[`src/Execution/Executor.php#L288-L325`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L288-L325)

`$contextFor` — замыкание, которое для каждой строки строит её контекст
вычисления: `RowContext` для одной таблицы, `QualifiedRowContext` для
соединения. Операторы ниже по конвейеру не знают, каким запросом они порождены.

### Операторы: пулл-цепочка

Интерфейс чтения — три строки контракта:

```php
/**
 * A step in the read path a `SELECT` is built from: something that yields
 * rows, usually by pulling from another `Operator` underneath it. Being an
 * `IteratorAggregate` is what lets one compose with `foreach` directly and
 * lets a plain generator satisfy the interface with no wrapper class.
 *
 * This is a *read-only* pipeline. `INSERT`, `UPDATE` and `DELETE` do not
 * flow through it — see DECISIONS.md for why mutation is kept out.
 *
 * @extends IteratorAggregate<mixed, Row>
 */
interface Operator extends IteratorAggregate
{
    /** @return Generator<mixed, Row> */
    public function getIterator(): Generator;
}
```

[`src/Execution/Operator/Operator.php#L11-L26`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/Operator.php#L11-L26)

**Перевод комментариев:**
- `Operator` (PHPDoc): шаг пути чтения, из которого строится `SELECT`: нечто,
  выдающее строки, обычно вытягивая их из другого `Operator` под ним. Именно
  `IteratorAggregate` позволяет составлять шаги напрямую через `foreach` и
  удовлетворять интерфейсу обычным генератором без класса-обёртки. Это
  *read-only* конвейер. `INSERT`, `UPDATE` и `DELETE` через него не идут — см.
  DECISIONS.md о том, почему мутации держатся в стороне.
- `getIterator()` (`@return`): возвращает генератор строк `Row`.

Последовательный скан — единственная реализация, которая знает о `HeapFile`, и
она отдаёт `RecordId` в качестве ключа генератора, потому что кто-то выше может
захотеть адрес строки:

```php
/**
 * Reads every live row of a table, in whatever physical order `HeapFile`
 * stores them — the only access path until `IndexScan` exists (Phase 6).
 * The generator's keys are the rows' `RecordId`s, carried through in case a
 * caller above this one needs them (nothing in the Phase 5 pipeline does;
 * `UPDATE`/`DELETE` read the heap file directly instead — see
 * DECISIONS.md).
 */
final readonly class SeqScan implements Operator
{
    public function __construct(
        private Table $table,
        private HeapFile $heap,
    ) {
    }

    public function getIterator(): Generator
    {
        /** @var RecordId $id */
        foreach ($this->heap->scan() as $id => $record) {
            yield $id => $this->table->deserializeRow($record);
        }
    }
}
```

[`src/Execution/Operator/SeqScan.php#L12-L35`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/SeqScan.php#L12-L35)

**Перевод комментариев:**
- `SeqScan` (PHPDoc): читает каждую живую строку таблицы в том физическом
  порядке, в котором их хранит `HeapFile`, — единственный путь доступа, пока не
  появится `IndexScan` (фаза 6). Ключи генератора — это `RecordId` строк,
  передаваемые на случай, если вызывающему выше они понадобятся (в конвейере
  фазы 5 этого не делает ничто; `UPDATE` /`DELETE` вместо этого читают кучу
  напрямую — см. DECISIONS.md).
- `$id` (`@var`): `RecordId` текущей строки, ключ генератора `scan()`.

Фильтр — единственное ворото чтения, и решение о пропуске строки принимает ровно
один вызов — `isTrue()`, о котором раздел следующий:

```php
/**
 * Yields only the rows of its source for which `$predicate` evaluates to a
 * definite `true` — `Evaluator::isTrue()` is what decides that, so a `NULL`
 * (unknown) result excludes a row exactly like `false` does, per SQL's
 * three-valued logic.
 *
 * `$contextFor` builds the `EvaluationContext` a given row is checked
 * against — a plain `RowContext` for a single table, a `QualifiedRowContext`
 * once a `JOIN` is involved (Phase 7) — so this class stays ignorant of
 * which kind of query produced the row it was handed.
 */
final readonly class Filter implements Operator
{
    /** @param Closure(Row): EvaluationContext $contextFor */
    public function __construct(
        private Operator $source,
        private Expression $predicate,
        private Evaluator $evaluator,
        private Closure $contextFor,
    ) {
    }

    public function getIterator(): Generator
    {
        foreach ($this->source as $id => $row) {
            if ($this->evaluator->isTrue($this->evaluator->evaluate($this->predicate, ($this->contextFor)($row)))) {
                yield $id => $row;
            }
        }
    }
}
```

[`src/Execution/Operator/Filter.php#L14-L44`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/Filter.php#L14-L44)

**Перевод комментариев:**
- `Filter` (PHPDoc): выдаёт только те строки источника, для которых `$predicate`
  вычисляется в определённое `true` — это решает `Evaluator::isTrue()`, поэтому
  результат `NULL` (неизвестно) исключает строку так же, как `false`, по
  трёхзначной логике SQL. `$contextFor` строит `EvaluationContext`, с которым
  проверяется данная строка: обычный `RowContext` для одной таблицы,
  `QualifiedRowContext`, когда участвует `JOIN` (фаза 7), — так этот класс
  остаётся в неведении о том, какой запрос породил переданную ему строку.
- `$contextFor` (`@param`): замыкание, строящее контекст вычисления для строки.

`Limit` — оператор, который умеет **останавливаться**: `return` из генератора
закрывает весь стек ниже. Именно эта остановка делает `LIMIT 2` над пятью
строками четырьмя прочитанными строками:

```php
    public function getIterator(): Generator
    {
        if ($this->limit === 0) {
            return;
        }

        $skipped = 0;
        $taken = 0;

        foreach ($this->source as $id => $row) {
            if ($skipped < $this->offset) {
                $skipped++;
                continue;
            }

            yield $id => $row;
            $taken++;

            if ($this->limit !== null && $taken >= $this->limit) {
                return;
            }
        }
    }
}
```

[`src/Execution/Operator/Limit.php#L23-L46`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/Limit.php#L23-L46)

### Трёхзначная логика: два ворота с зеркальными правилами

Одно значение, две функции, и у каждой — комментарий, объясняющий, что
SQL-стандарт требует **противоположного** поведения от NULL в двух позициях:

```php
/**
 * Whether an evaluated value counts as satisfying a `WHERE`/`HAVING`
 * condition: only a definite `true` does. `NULL` (unknown) and `false`
 * both exclude the row, which is what three-valued logic means in a
 * filter position.
 */
public function isTrue(mixed $value): bool
{
    return $this->toBoolean($value) === true;
}

/**
 * Whether an evaluated value violates a `CHECK` constraint: only a
 * definite `false` does. `NULL` passes a `CHECK` — the opposite of what
 * it does in a `WHERE`/`HAVING` position — because standard SQL only
 * ever rejects a row a `CHECK` expression can *prove* wrong; a
 * constraint it cannot evaluate at all (a comparison against a `NULL`
 * column, say) is not grounds to refuse the row.
 */
public function isFalse(mixed $value): bool
{
    return $this->toBoolean($value) === false;
}
```

[`src/Execution/Expression/Evaluator.php#L74-L96`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Expression/Evaluator.php#L74-L96)

**Перевод комментариев:**
- `isTrue()` (PHPDoc): считается ли вычисленное значение удовлетворяющим условию
  `WHERE` /`HAVING`: только определённое `true`. `NULL` (неизвестно) и `false`
  оба исключают строку — вот что означает трёхзначная логика в позиции фильтра.
- `isFalse()` (PHPDoc): нарушает ли вычисленное значение ограничение `CHECK`:
  только определённое `false`. `NULL` проходит `CHECK` — противоположность
  поведению в позиции `WHERE` /`HAVING` — потому что стандартный SQL отвергает
  строку, только когда выражение `CHECK` может *доказать* её неправильность;
  ограничение, которое вообще не удаётся вычислить (скажем, сравнение со
  столбцом `NULL`), не основание отказывать строке.

Обе сводятся к одному `toBoolean()`, который возвращает `null` для неизвестного.
Трёхзначные таблицы `AND`/`OR` получаются как побочный эффект короткого
замыкания: `FALSE AND NULL` — это `FALSE`, потому что левый операнд уже решает
вопрос, и правый даже не вычисляется:

```php
/**
 * AND/OR short-circuit on the operand that already decides the answer,
 * which is also what makes their three-valued truth tables come out
 * right: `FALSE AND NULL` is `FALSE`, not `NULL`, because the left side
 * alone already settles it.
 */
private function logical(BinaryOp $expression, EvaluationContext $context): ?bool
{
    $left = $this->toBoolean($this->evaluate($expression->left, $context));

    if ($expression->operator === BinaryOperator::AND && $left === false) {
        return false;
    }
    if ($expression->operator === BinaryOperator::OR && $left === true) {
        return true;
    }

    $right = $this->toBoolean($this->evaluate($expression->right, $context));

    if ($expression->operator === BinaryOperator::AND) {
        if ($right === false) {
            return false;
        }

        return $left === true && $right === true ? true : null;
    }

    if ($right === true) {
        return true;
    }

    return $left === false && $right === false ? false : null;
}
```

[`src/Execution/Expression/Evaluator.php#L140-L172`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Expression/Evaluator.php#L140-L172)

**Перевод комментариев:**
- `logical()` (PHPDoc): `AND` /`OR` замыкаются на операнде, который уже решает
  ответ; это же обеспечивает правильность их трёхзначных таблиц истинности:
  `FALSE AND NULL` — это `FALSE`, а не `NULL`, потому что левая часть уже всё
  решила.

Отсюда следует свойство, которое ловит людей в реальных СУБД:
`WHERE (age > 18) OR (age <= 18)` при `age = NULL` не пропускает строку. Обе
стороны — неизвестно, «неизвестно ИЛИ неизвестно» — неизвестно, а ворота `WHERE`
пропускают только определённое `true`. Единственное исключение для NULL —
операторы `IS NULL`/`IS NOT NULL`.

### Агрегаты подставляются, а не считаются в Evaluator

`Evaluator` не знает о группировках. Когда `Aggregate` собрал строки группы, он
проходит по выражению `HAVING`/`SELECT` и **заменяет** каждый вызов аггрегата
готовым литералом:

```php
private function substituteAggregates(Expression $expression, array $rows): Expression
{
    if ($expression instanceof FunctionCall && in_array(strtoupper($expression->name), self::AGGREGATE_FUNCTIONS, true)) {
        return new Literal($this->computeAggregate($expression, $rows));
    }

    if ($expression instanceof BinaryOp) {
        return new BinaryOp(
            $this->substituteAggregates($expression->left, $rows),
            $expression->operator,
            $this->substituteAggregates($expression->right, $rows),
        );
    }

    if ($expression instanceof UnaryOp) {
        return new UnaryOp($expression->operator, $this->substituteAggregates($expression->operand, $rows));
    }

    return $expression;
}
```

[`src/Execution/Operator/Aggregate.php#L131-L150`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Operator/Aggregate.php#L131-L150)

Подстановка вместо передачи контекста — это разделение, которое обходится
дешевле: `Evaluator` не меняется вовсе, `HAVING COUNT(*) > 1` работает без
специального кода, а «unknown function» по-прежнему получает отказ при
исполнении, не при разборе (`docs/sql.md`: «an unrecognised function throws at
execution, not at parse time»).

### UPDATE: сначала прочитать всё, потом писать

Последний блок кода главы — и его единственная тема: почему мутация не ходит
через операторы. Комментарий в `executeUpdate()` — самый длинный в цитируемом
коде, и он описывает ловушку, которую эксперимент 4 воспроизводит буквально:
выросшая строка переехала в конец файла, в ту самую страницу, до которой скан
ещё не дошёл:

```php
private function executeUpdate(UpdateStatement $statement, array $parameters): int
{
    $table = $this->database->table($statement->table);
    $heap = $this->database->heapFile($statement->table);

    return $this->withTransaction(function () use ($statement, $table, $heap, $parameters): int {
        $txId = $this->currentTransactionId();

        // Collected - and uniqueness-checked - before anything is written:
        // a row that grows past its page moves, and HeapFile::insert()
        // places the moved copy at the end of the file - possibly onto a
        // page this same scan has not reached yet, which would visit and
        // update it a second time. See DECISIONS.md. Checking every match
        // up front, before any write, also means a uniqueness violation
        // anywhere in the statement leaves every row untouched rather than
        // leaving earlier matches already changed. Locks are taken here
        // too, as each candidate is found, rather than only once it is
        // written.
        $writes = [];

        foreach ($heap->scan() as $id => $record) {
            $oldRow = $table->deserializeRow($record);
            $context = new RowContext($oldRow, $statement->table, parameters: $parameters);

            if (!$this->satisfies($statement->where, $context)) {
                continue;
            }

            $this->locks->acquireRowLock($table->name, $id, $txId, LockMode::EXCLUSIVE);

            $values = $oldRow->toArray();

            foreach ($statement->assignments as $assignment) {
                if (!$table->hasColumn($assignment->column)) {
                    throw new ExecutionException(sprintf('Table "%s" has no column "%s".', $statement->table, $assignment->column));
                }

                $values[$assignment->column] = $this->evaluator->evaluate($assignment->value, $context);
            }

            $newRow = $table->resolveRow(new Row($values));
            $this->constraints->assertCheckConstraints($table, $newRow);
            $this->constraints->assertForeignKeysOnWrite($table, $newRow);
            $this->indexMaintainer->assertUniqueForUpdate($table, $newRow, $id);

            $writes[] = ['id' => $id, 'oldRow' => $oldRow, 'newRow' => $newRow];
        }

        foreach ($writes as $write) {
            // Checked (and, for CASCADE/SET NULL, acted on) before this
            // row's own update is applied - see cascadeBeforeUpdate()'s
            // docblock for why the order matters.
            $this->cascadeBeforeUpdate($table, $write['oldRow'], $write['newRow'], $txId);
            $this->physicallyUpdateRow($table, $write['id'], $write['oldRow'], $write['newRow']);
        }

        return count($writes);
    });
}
```

[`src/Execution/Executor.php#L557-L615`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Execution/Executor.php#L557-L615)

**Перевод комментариев:**
- `// Collected - and uniqueness-checked - before anything is written:` — всё
  собрано и проверено на уникальность до первой записи: строка, выросшая за
  пределы страницы, переезжает, а `HeapFile::insert()` кладёт перемещённую копию
  в конец файла — возможно, на страницу, до которой этот же скан ещё не дошёл, и
  тогда он посетил бы и обновил её второй раз. См. DECISIONS.md. Проверка всех
  совпадений заранее, до любой записи, также означает, что нарушение
  уникальности в любом месте утверждения оставляет все строки нетронутыми, а не
  меняет ранее найденные. Блокировки берутся здесь же, по мере находки каждого
  кандидата, а не только когда он записан.
- `// Checked (and, for CASCADE/SET NULL, acted on) before this` — проверяется
  (а для `CASCADE` /`SET NULL` — выполняется) до применения собственного
  обновления этой строки; почему порядок важен, см. в докблоке
  `cascadeBeforeUpdate()`.

Два цикла вместо одного решают три задачи сразу: строку нельзя посетить дважды
(Halloween problem), нарушение уникальности в середине `UPDATE` не должно
оставить половину таблицы изменённой, а блокировки строк должны быть взяты до
того, как данные прочитаны дальше — тем же проходом, что и чтение. Плата —
удержание всех кандидатов в памяти: столько байт, сколько весит `WHERE`-выборка
целиком.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-database
make build install
```

Четыре скрипта ниже кладутся в `/tmp` на хосте и копируются в контейнер, где
проект смонтирован в `/app`. Вывод — реальный, снятый при написании главы. Все
четыре детерминированы: замеров времени нет ни в одном, и повторный прогон
каждого даёт те же байты.

### 1. Лексер: текст в, токены и отказы с позициями

Первый вопрос главы: что такое «разобрать текст» на самом деле. Скрипт печатает
каждый токен одного запроса с его позицией, затем три отказа — незакрытая
строка, синтаксическая ошибка и два выражения в одном вызове:

```bash
docker compose cp /tmp/sql-tokens.php php:/tmp/sql-tokens.php
docker compose exec php php /tmp/sql-tokens.php
```

```php
<?php

declare(strict_types=1);

/**
 * Text in, tokens out — and the errors a real lexer has to raise.
 *
 * The Lexer is a single left-to-right pass over the SQL text that produces
 * the *whole* token list up front, every token carrying its byte position
 * in the source. Two properties of that list are visible here:
 *
 *   - a token's text is already the VALUE, not the raw spelling: a string
 *     literal's '' escapes are collapsed by the lexer, so the parser never
 *     re-reads a token's spelling;
 *   - keywords get their own token type instead of a generic KEYWORD —
 *     "is the next token FROM?" reads as a case match, not as data.
 *
 * Then the error side: an unterminated string literal is caught at the
 * position of its OPENING quote, with line and column computed from the
 * byte offset the lexer recorded.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/sql-tokens.php php:/tmp/sql-tokens.php
 *   docker compose exec php php /tmp/sql-tokens.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\ParserException;
use PhpMiniDatabase\Sql\Lexer;
use PhpMiniDatabase\Sql\Parser;

// ---------------------------------------------------------------------
// 1. One SELECT, every token it produces, with its position in the text.
// ---------------------------------------------------------------------

$sql = <<<'SQL'
SELECT "user".name, age
  FROM "user"
 WHERE name = 'O''Brien' -- the doubled quote is already collapsed
   AND age >= 21;
SQL;

printf("source: %s\n", str_replace("\n", '\n', $sql));

foreach (Lexer::tokenize($sql) as $i => $token) {
    printf(
        "%2d  %-16s %-14s @%d\n",
        $i,
        $token->type->name,
        "'" . $token->text . "'",
        $token->position,
    );
}

// ---------------------------------------------------------------------
// 2. An unterminated string: the position reported is the OPENING quote.
// ---------------------------------------------------------------------

try {
    Parser::parseOne("SELECT name\n  FROM users\n WHERE name = 'O'Brien'");
} catch (ParserException $e) {
    printf("unterminated literal: %s\n", $e->getMessage());
}

// ---------------------------------------------------------------------
// 3. A syntax error: line and column come from the byte position, too.
// ---------------------------------------------------------------------

try {
    Parser::parseOne("SELECT name\nFROM users\nWHERE age >> 21");
} catch (ParserException $e) {
    printf("syntax error: %s\n", $e->getMessage());
}

// ---------------------------------------------------------------------
// 4. Two statements, and an empty statement, are both refused.
// ---------------------------------------------------------------------

try {
    Parser::parseOne('SELECT 1; DROP TABLE users');
} catch (ParserException $e) {
    printf("two statements: %s\n", $e->getMessage());
}
```

**Перевод комментариев:**
- `sql-tokens.php` (PHPDoc): текст на входе, токены на выходе — и ошибки,
  которые настоящий лексер обязан поднимать. `Lexer` — один проход по тексту SQL
  слева направо, выдающий *весь* список токенов сразу, каждый токен несёт свою
  байтовую позицию в исходнике. Здесь видны два свойства списка: текст токена —
  это уже ЗНАЧЕНИЕ, а не исходное написание: `''`-экранирование в строковом
  литерале схлопывается лексером, так что парсер никогда не перечитывает
  написание токена; ключевые слова получают собственный тип токена вместо общего
  `KEYWORD` — «следующий токен `FROM`? » читается как вариант `case`, а не как
  данные. Затем сторона ошибок: незавершённый строковый литерал ловится на
  позиции своей ОТКРЫВАЮЩЕЙ кавычки, а строка и столбец вычисляются из байтового
  смещения, записанного лексером.
- `// 1. One SELECT, every token it produces, with its position in the text.` —
  один `SELECT` и все его токены с позициями в тексте.
- `// 2. An unterminated string: the position reported is the OPENING quote.` —
  незавершённая строка: сообщённая позиция — ОТКРЫВАЮЩАЯ кавычка.
- `// 3. A syntax error: line and column come from the byte position, too.` —
  синтаксическая ошибка: строка и столбец тоже берутся из байтовой позиции.
- `// 4. Two statements, and an empty statement, are both refused.` — два
  утверждения и пустое утверждение оба отвергаются.

```text
source: SELECT "user".name, age\n  FROM "user"\n WHERE name = 'O''Brien' -- the doubled quote is already collapsed\n   AND age >= 21;
 0  SELECT           'SELECT'       @0
 1  IDENTIFIER       'user'         @7
 2  DOT              '.'            @13
 3  IDENTIFIER       'name'         @14
 4  COMMA            ','            @18
 5  IDENTIFIER       'age'          @20
 6  FROM             'FROM'         @26
 7  IDENTIFIER       'user'         @31
 8  WHERE            'WHERE'        @39
 9  IDENTIFIER       'name'         @45
10  EQ               '='            @50
11  STRING           'O'Brien'      @52
12  AND              'AND'          @108
13  IDENTIFIER       'age'          @112
14  GTE              '>='           @116
15  NUMBER           '21'           @119
16  SEMICOLON        ';'            @121
17  EOF              ''             @122
unterminated literal: Unterminated string literal. (line 3, column 23)
syntax error: Expected an expression, found ">". (line 3, column 12)
two statements: Expected exactly one statement, found 2. (line 1, column 1)
```

**Восемнадцать токенов**, включая завершающий `EOF` на позиции 122 — длине всего
запроса. Позиции не «примерные»: `STRING` начинается на байте 52, то есть ровно
на открывающей кавычке, а её текст — `O'Brien`: удвоенная кавычка уже схлопнута,
парсер её больше не увидит. Обратите внимание на комментарий в конце третьей
строки: `-- ...` съеден вместе с пробелами, `AND` стоит на позиции 108, а не
там, где стоял в тексте.

Три отказа читаются как учебник позиционирования. Незакрытая строка указывает на
`(line 3, column 23)` — это **открывающая** кавычка литерала `'O'Brien'`
(позиция, записанная `delimited()` в переменную `$start`). Синтаксическая ошибка
указывает на `(line 3, column 12)` — второй символ `>>`: парсер уже прочитал
первый `>` как оператор сравнения и теперь ждёт выражение, а не оператор. И
`parseOne` отказывает подсчётом, а не молчанием: «found 2» — это ровно то, что
проверяет `testATrailingSemicolonIsOptional` и его соседи в `ParserErrorTest`.

Вопрос, на который отвечает этот прогон: сколько стоит позиция в каждом токене?
Один целый `int` — и весь раздел 3 показал, что этот `int` покупает сообщения об
ошибках, которые можно чинить по номеру строки, а не по догадкам.

### 2. Планы: четыре запроса и четыре решения оптимизатора

Второй вопрос: что оптимизатор делает на самом деле, а не на словах. Скрипт
строит две таблицы — маленькую с индексом и большую без, — а затем печатает
`EXPLAIN` пяти запросов, каждый из которых заставляет правило принять или **не**
принять решение:

```bash
docker compose cp /tmp/explain-plans.php php:/tmp/explain-plans.php
docker compose exec php php /tmp/explain-plans.php
```

```php
<?php

declare(strict_types=1);

/**
 * The same query text, four different plans — and the rules that chose.
 *
 * EXPLAIN prints one line per node of the plan Planner built and
 * Optimizer rewrote. Nothing here is a cost model: the optimizer is a
 * fixed sequence of four rules, and each rule's decision is visible in
 * the output:
 *
 *   - IndexSelection    SeqScan -> IndexScan when WHERE has `col <op>
 *                       constant` and the column is indexed. The Filter
 *                       stays above the index — the index narrows, the
 *                       filter re-validates every row it gets.
 *   - PredicatePushdown a join's WHERE conjuncts move onto the side that
 *                       can answer them, but never onto the outer join's
 *                       nullable side — that one would change answers.
 *   - JoinReordering    the smaller table (by page count) becomes the
 *                       hash-built side of an INNER equi-join.
 *   - ConstantFolding   literal-only subexpressions collapse.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/explain-plans.php php:/tmp/explain-plans.php
 *   docker compose exec php php /tmp/explain-plans.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;

$directory = sys_get_temp_dir() . '/ch16-explain-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$executor = new Executor($database);

$explain = static function (Executor $executor, string $sql): void {
    printf("EXPLAIN %s\n", str_replace("\n", ' ', $sql));

    foreach ($executor->run('EXPLAIN ' . $sql)->rows as $row) {
        printf("  %s\n", $row->get('plan'));
    }

    echo "\n";
};

// ---------------------------------------------------------------------
// A table with an index, and one big enough to span several pages —
// JoinReordering estimates "smaller" by page count, so the sizes have to
// actually differ.
// ---------------------------------------------------------------------

$executor->run('CREATE TABLE users (id INT PRIMARY KEY, name VARCHAR(50), age INT)');
$executor->run('CREATE TABLE orders (id INT PRIMARY KEY, user_id INT, total INT)');

$executor->run("INSERT INTO users (id, name, age) VALUES (1, 'alice', 30), (2, 'bob', 17), (3, 'carol', 35)");

$rows = [];

for ($i = 1; $i <= 500; $i++) {
    $rows[] = sprintf('(%d, %d, %d)', $i, ($i % 3) + 1, $i * 10);
}

$executor->run('INSERT INTO orders (id, user_id, total) VALUES ' . implode(', ', $rows));

// ---------------------------------------------------------------------
// 1. IndexSelection: the only plan change a CREATE INDEX makes.
// ---------------------------------------------------------------------

$explain($executor, "SELECT name FROM users WHERE age = 30");

$executor->run('CREATE INDEX idx_users_age ON users (age)');

$explain($executor, "SELECT name FROM users WHERE age = 30");

// The Filter never leaves: the index narrows the rows read, the full
// WHERE re-validates each of them. Same answer, fewer rows touched.
$before = array_map(
    static fn ($row): string => $row->get('name'),
    iterator_to_array($executor->run("SELECT name FROM users WHERE age = 30 AND name = 'alice'")->rows),
);

$explain($executor, "SELECT name FROM users WHERE age = 30 AND name = 'alice'");

printf("rows: %s\n\n", implode(', ', $before));

// ---------------------------------------------------------------------
// 2. JoinReordering: orders spans several pages, users is one — the
//    smaller side is built into the hash table, wherever the text put it.
// ---------------------------------------------------------------------

$explain($executor, "SELECT u.name, o.total FROM users u JOIN orders o ON o.user_id = u.id WHERE o.total > 100");

printf("orders spans %d pages, users spans %d\n\n", $database->heapFile('orders')->pageCount(), $database->heapFile('users')->pageCount());

// ---------------------------------------------------------------------
// 3. PredicatePushdown: an INNER join takes both conjuncts; a LEFT
//    join's WHERE may not touch its nullable side.
// ---------------------------------------------------------------------

$explain($executor, "SELECT u.name, o.total FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.total > 100 AND u.age > 18");

// ---------------------------------------------------------------------
// 4. ConstantFolding: 2 + 3 = 5 is decided by the optimizer, not the
//    executor.
// ---------------------------------------------------------------------

$explain($executor, "SELECT name FROM users WHERE 2 + 3 = 5 AND age > 18");

$database->close();
```

**Перевод комментариев:**
- `explain-plans.php` (PHPDoc): один и тот же текст запроса, четыре разных плана
  — и правила, которые их выбрали. `EXPLAIN` печатает одну строку на узел плана,
  который построил `Planner` и переписал `Optimizer`. Ничего здесь не является
  моделью стоимости: оптимизатор — фиксированная последовательность из четырёх
  правил, и решение каждого видно в выводе: `IndexSelection` превращает
  `SeqScan` в `IndexScan`, когда в `WHERE` есть `col <op> constant`, а столбец
  проиндексирован (при этом `Filter` остаётся над индексом: индекс сужает,
  фильтр перепроверяет каждую полученную строку); `PredicatePushdown` переносит
  конъюнкты `WHERE` соединения на сторону, которая может на них ответить, но
  никогда на nullable-сторону внешнего соединения — это изменило бы ответы;
  `JoinReordering` делает меньшую таблицу (по числу страниц) стороной, по
  которой строится хеш, во `INNER` equi-join; `ConstantFolding` схлопывает
  подвыражения только из литералов.
- `// A table with an index, and one big enough to span several pages —` —
  таблица с индексом и достаточно большая, чтобы занять несколько страниц:
  `JoinReordering` оценивает «меньше» по числу страниц, поэтому размеры должны
  действительно различаться.
- `// 1. IndexSelection: the only plan change a CREATE INDEX makes.` —
  `IndexSelection`: единственное изменение плана, которое даёт `CREATE INDEX`.
- `// The Filter never leaves: the index narrows the rows read, the full` —
  `Filter` никогда не уходит: индекс сужает читаемые строки, а полный `WHERE`
  перепроверяет каждую из них. Тот же ответ, меньше затронутых строк.
- `// 2. JoinReordering: orders spans several pages, users is one — the` —
  `JoinReordering`: `orders` занимает несколько страниц, `users` — одну; меньшая
  сторона строится в хеш-таблицу, где бы в тексте она ни стояла.
- `// 3. PredicatePushdown: an INNER join takes both conjuncts; a LEFT` —
  `PredicatePushdown`: `INNER` join принимает оба конъюнкта; `WHERE` у `LEFT`
  join не может затрагивать его nullable-сторону.
- `// 4. ConstantFolding: 2 + 3 = 5 is decided by the optimizer, not the` —
  `ConstantFolding`: `2 + 3 = 5` решает оптимизатор, а не исполнитель.

```text
EXPLAIN SELECT name FROM users WHERE age = 30
  Limit ALL OFFSET 0
    Project (name)
      Filter (age = 30)
        SeqScan users

EXPLAIN SELECT name FROM users WHERE age = 30
  Limit ALL OFFSET 0
    Project (name)
      Filter (age = 30)
        IndexScan users USING idx_users_age (age = 30)

EXPLAIN SELECT name FROM users WHERE age = 30 AND name = 'alice'
  Limit ALL OFFSET 0
    Project (name)
      Filter (age = 30 AND name = 'alice')
        IndexScan users USING idx_users_age (age = 30)

rows: alice

EXPLAIN SELECT u.name, o.total FROM users u JOIN orders o ON o.user_id = u.id WHERE o.total > 100
  Limit ALL OFFSET 0
    Project (name, total)
      HashJoin INNER (o.user_id = u.id)
        Filter (o.total > 100)
          SeqScan o
        SeqScan u

orders spans 2 pages, users spans 1

EXPLAIN SELECT u.name, o.total FROM users u LEFT JOIN orders o ON o.user_id = u.id WHERE o.total > 100 AND u.age > 18
  Limit ALL OFFSET 0
    Project (name, total)
      Filter (o.total > 100)
        NestedLoopJoin LEFT (o.user_id = u.id)
          Filter (u.age > 18)
            IndexScan u USING idx_users_age (age > 18)
          SeqScan o

EXPLAIN SELECT name FROM users WHERE 2 + 3 = 5 AND age > 18
  Limit ALL OFFSET 0
    Project (name)
      Filter (TRUE AND age > 18)
        IndexScan users USING idx_users_age (age > 18)

```

Пять планов — пять наблюдений.

**Одно `CREATE INDEX` меняет ровно одну строку**: `SeqScan users` в
`IndexScan users USING idx_users_age (age = 30)`. Всё остальное — та же
`Filter`, тот же `Project`, тот же `Limit ALL OFFSET 0` (план без `LIMIT`
печатает «ALL»). Изменился способ чтения, не изменился ответ.

**`Filter` остаётся и с индексом.** Третий план: предикат
`age = 30 AND name = 'alice'` целиком висит над `IndexScan`, хотя индексу
достаточно его левой половины. Так и задумано — индекс сужает читаемое, фильтр
перепроверяет; строка `rows: alice` подтверждает, что ответ тот же, что дал бы
полный обход. Тест `testTheFilterIsKeptEvenOnceAnIndexIsChosen` закрепляет это
правилом.

**Переупорядочивание соединения видно по страницам.** Текст запроса говорит
«users первый, orders второй», а план строит хеш по `users`: `orders` занимает 2
страницы, `users` — 1, и меньшая сторона становится «строительной». Правило
смотрит на `pageCount()`, а не на текст запроса.

**`LEFT JOIN` не получает ни спуска предиката, ни `HashJoin`.** Тот же
`age > 18` в шестом плане остался `Filter`'ом над стороной соединения — и,
поскольку спущен не был, `IndexSelection` увидел его через `PredicatePushdown` и
всё-таки выбрал индекс для стороны `u`: два правила подряд работают на один
узел. А `o.total > 100` остался наверху, над `NestedLoopJoin LEFT`: на
nullable-сторону он **не мог** спуститься — это изменило бы, какие строки
возвращает запрос. `HashJoin` для `LEFT JOIN` недоступен (`HashJoin` — «An
`INNER JOIN` restricted to one shape» по докблоку класса), поэтому соединение
осталось вложенным циклом.

**Свёртка констант работает без данных**: `2 + 3 = 5` стало `TRUE`. Обратите
внимание, что `TRUE AND age > 18` **не свернулось до конца** — `ConstantFolding`
не выкидывает `TRUE` из конъюнкта, а только схлопывает литеральные подвыражения.
Это осознанная узость: правило меняет форму, не трогая ответ, а удаление `TRUE`
— уже решение о том, что «истина И X» равно «X», — и для `NULL` оно верно, но
для проекта важнее, чтобы каждое правило делало ровно одно.

### 3. Пулл и NULL: лень, которую можно посчитать, и логику, которую нельзя обойти

Третий вопрос: работает ли конвейер на самом деле «только когда тянут», и как
NULL обходится через все ворота. Скрипт оборачивает `SeqScan` счётчиком,
собирает `Limit(Filter(scan))` вручную и печатает, сколько строк прочитано;
затем гоняет три выборки с NULL и проверяет `CHECK` с обеих сторон:

```bash
docker compose cp /tmp/pull-and-nulls.php php:/tmp/pull-and-nulls.php
docker compose exec php php /tmp/pull-and-nulls.php
```

```php
<?php

declare(strict_types=1);

/**
 * The operator pipeline is a pull chain, and every condition in it is
 * three-valued.
 *
 * Two properties of Execution\Operator\* are visible here.
 *
 * Laziness: operators are generators; nothing runs until someone pulls,
 * and each operator pulls from the one below only as far as it needs.
 * A counting wrapper around a SeqScan shows that `LIMIT 2` over a filter
 * stops the scan after the fourth of five rows — the fifth is never read.
 *
 * Three-valued logic: `age > 18` is not false for a NULL age, it is
 * UNKNOWN, and both gates in the engine treat unknown the same way from
 * opposite sides — WHERE keeps a row only when the predicate is TRUE,
 * a CHECK constraint rejects a row only when it is FALSE. So NULL passes
 * CHECK but disappears from WHERE, including under NOT: NOT unknown is
 * unknown, not true.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/pull-and-nulls.php php:/tmp/pull-and-nulls.php
 *   docker compose exec php php /tmp/pull-and-nulls.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Execution\Expression\EvaluationContext;
use PhpMiniDatabase\Execution\Expression\Evaluator;
use PhpMiniDatabase\Execution\Expression\RowContext;
use PhpMiniDatabase\Execution\Operator\Filter;
use PhpMiniDatabase\Execution\Operator\Limit;
use PhpMiniDatabase\Execution\Operator\Operator;
use PhpMiniDatabase\Execution\Operator\SeqScan;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;
use PhpMiniDatabase\Sql\Parser;

/** Counts every row pulled through it — the meter for the pull chain. */
final class CountingScan implements Operator
{
    public int $pulled = 0;

    public function __construct(private readonly Operator $source)
    {
    }

    public function getIterator(): \Generator
    {
        foreach ($this->source as $id => $row) {
            $this->pulled++;

            yield $id => $row;
        }
    }
}

$directory = sys_get_temp_dir() . '/ch16-pull-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$executor = new Executor($database);

$executor->run('CREATE TABLE t (id INT PRIMARY KEY, name VARCHAR(20), age INT)');
$executor->run("INSERT INTO t (id, name, age) VALUES (1, 'alice', 30), (2, 'bob', 17), (3, 'carol', NULL), (4, 'dave', 42), (5, 'erin', 25)");

// ---------------------------------------------------------------------
// 1. LIMIT 2 over a filter: the scan is pulled exactly until the second
//    match exists — four of five rows, and the fifth is never deserialized.
// ---------------------------------------------------------------------

$scan = new CountingScan(new SeqScan($database->table('t'), $database->heapFile('t')));

$pipeline = new Limit(
    new Filter(
        $scan,
        Parser::parseExpression('age > 18'),
        new Evaluator(),
        static fn (Row $row): EvaluationContext => new RowContext($row, 't'),
    ),
    2,
);

foreach ($pipeline as $row) {
    printf("row: %s\n", $row->get('name'));
}

printf("pulled through the scan: %d of 5 rows\n\n", $scan->pulled);

// ---------------------------------------------------------------------
// 2. WHERE keeps only TRUE. UNKNOWN — any comparison with NULL — is
//    dropped, and NOT does not resurrect it: NOT unknown is unknown.
// ---------------------------------------------------------------------

$names = static function (Executor $executor, string $sql): string {
    $rows = iterator_to_array($executor->run($sql)->rows);

    return $rows === [] ? '(no rows)' : implode(', ', array_map(static fn (Row $row): string => $row->get('name'), $rows));
};

printf("age > 18            -> %s\n", $names($executor, 'SELECT name FROM t WHERE age > 18'));
printf("NOT (age > 18)      -> %s\n", $names($executor, 'SELECT name FROM t WHERE NOT (age > 18)'));
printf("age IS NULL         -> %s\n", $names($executor, 'SELECT name FROM t WHERE age IS NULL'));
printf("(age > 18) OR (<=18) -> %s\n", $names($executor, 'SELECT name FROM t WHERE (age > 18) OR (age <= 18)'));

// ---------------------------------------------------------------------
// 3. CHECK is the same three-valued logic from the other side: it only
//    rejects a definite FALSE, so NULL goes in — and then vanishes from
//    every WHERE above.
// ---------------------------------------------------------------------

$executor->run('CREATE TABLE c (id INT PRIMARY KEY, age INT CHECK (age > 18))');
$executor->run('INSERT INTO c (id, age) VALUES (1, NULL)');

printf("CHECK (age > 18) accepted NULL: %d row(s)\n", count(iterator_to_array($executor->run('SELECT id FROM c')->rows)));

try {
    $executor->run('INSERT INTO c (id, age) VALUES (2, 17)');
} catch (\PhpMiniDatabase\Exception\ConstraintViolationException $e) {
    printf("CHECK (age > 18) rejected 17: %s\n", $e->getMessage());
}

$database->close();
```

**Перевод комментариев:**
- `pull-and-nulls.php` (PHPDoc): конвейер операторов — это цепочка вытягивания,
  и каждое условие в нём трёхзначное. Здесь видны два свойства
  `Execution\Operator\*`. Ленивость: операторы — генераторы; ничего не
  выполняется, пока кто-то не потянет, и каждый оператор тянет из нижнего ровно
  столько, сколько нужно. Счётная обёртка вокруг `SeqScan` показывает, что
  `LIMIT 2` над фильтром останавливает скан после четвёртой из пяти строк —
  пятая вообще не читается. Трёхзначная логика: `age > 18` для `NULL`-возраста
  не `false`, а `UNKNOWN`, и оба ворот в движке обрабатывают неизвестное
  одинаково с противоположных сторон — `WHERE` сохраняет строку, только когда
  предикат `TRUE`, а ограничение `CHECK` отвергает строку, только когда он
  `FALSE`. Поэтому `NULL` проходит `CHECK`, но исчезает из `WHERE`, в том числе
  под `NOT`: `NOT unknown` — это unknown, а не true.
- `CountingScan` (PHPDoc): считает каждую строку, вытянутую через него, —
  счётчик для цепочки вытягивания.
- `// 1. LIMIT 2 over a filter: the scan is pulled exactly until the second` —
  `LIMIT 2` над фильтром: скан вытягивается ровно до появления второго
  совпадения — четыре из пяти строк, пятая даже не десериализуется.
- `// 2. WHERE keeps only TRUE. UNKNOWN — any comparison with NULL — is` —
  `WHERE` сохраняет только `TRUE`. `UNKNOWN` — любое сравнение с `NULL` —
  отбрасывается, и `NOT` не воскрешает его: `NOT unknown` — unknown.
- `// 3. CHECK is the same three-valued logic from the other side: it only` —
  `CHECK` — та же трёхзначная логика с другой стороны: он отвергает только
  определённое `FALSE`, поэтому `NULL` проходит — а затем исчезает из любого
  `WHERE` выше.

```text
row: alice
row: dave
pulled through the scan: 4 of 5 rows

age > 18            -> alice, dave, erin
NOT (age > 18)      -> bob
age IS NULL         -> carol
(age > 18) OR (<=18) -> alice, bob, dave, erin
CHECK (age > 18) accepted NULL: 1 row(s)
CHECK (age > 18) rejected 17: Check constraint "ck_c_age" on "c" failed.
```

**Четыре прочитанные строки из пяти** — и это не оценка, а счётчик: `pulled`
увеличивается в обёртке вокруг `SeqScan`, и к моменту, когда `Limit` получил
вторую подходящую строку, скан прочитал ровно четыре: alice (подошла), bob
(нет), carol (нет, NULL), dave (подошла) — и `return` из `getIterator()` закрыл
всё вниз по стеку. Пятая строка, erin, не десериализована вовсе.

Дальше — NULL во всех позициях, и каждая строка вывода проверяет одно
назначенное свойство. `age > 18` пропускает три строки из пяти: carol пропала,
потому что «неизвестно» не является «определённым true». `NOT (age > 18)`
возвращает только bob: NOT не воскрешает неизвестное, `NOT unknown = unknown`.
Полнота покрытия `(age > 18) OR (age <= 18)` даёт четыре строки, не пять — carol
отсутствует, потому что обе половины дисъюнкта для неё неизвестны; знакомый урок
трёхзначной логики, который в двухзначной системе был бы невозможен. `IS NULL`
находит ровно carol — единственное сравнение, которое определённо работает с
NULL.

И наконец зеркальная половина: та же таблица `c` с тем же `CHECK (age > 18)`
**принимает** NULL — `isFalse()` видит «неизвестно» и не считает это доказанным
нарушением, — но отказывает 17 ровным сообщением
`Check constraint "ck_c_age" on "c" failed.` Одна и та же трёхзначная логика,
два противоположных ворот, и ни одной строчки специального кода для NULL в
`Filter` или в ограничителе.

### 4. UPDATE: ловушка Halloween problem, построенная нарочно

Последний вопрос: зачем `UPDATE` не ходит через операторы. Скрипт строит ровно
ту ситуацию, которую описывает комментарий в `executeUpdate()`: четыре строки
заполняют почти всю страницу, обновление растит каждую строку на 100 байтов, и
вторая строка **переезжает** в конец файла. Счётчик `n = n + 1` — детектор:
строка, посещённая дважды, вышла бы с `n = 2`:

```bash
docker compose cp /tmp/update-collect.php php:/tmp/update-collect.php
docker compose exec php php /tmp/update-collect.php
```

```php
<?php

declare(strict_types=1);

/**
 * UPDATE that grows rows: every matching row is read before any is
 * written — the fix for the Halloween problem.
 *
 * A `HeapFile::update()` that grows a row past its page's free space
 * deletes the old copy and appends the new one at the END of the file —
 * a page a scan that is still running has not reached yet. A naive
 * scan-and-mutate UPDATE would then find the moved row again and apply
 * SET to it a second time. This script builds exactly that trap:
 *
 *   - four rows fill most of one 8 KiB page;
 *   - `UPDATE t SET n = n + 1, note = <bigger>` grows every row past the
 *     page's free space: the first row still fits the ~120 spare bytes,
 *     the second does not — it moves to the file's end, and the ~2000
 *     bytes it frees let the last two grow in place;
 *   - `n = n + 1` is the tell: a row visited twice would come out with
 *     n = 2, a row visited once with n = 1.
 *
 * The heap layout before and after is printed from a direct
 * HeapFile::scan() — the moved row's new RecordId is the page the same
 * scan would have walked into next.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/update-collect.php php:/tmp/update-collect.php
 *   docker compose exec php php /tmp/update-collect.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Execution\Executor;
use PhpMiniDatabase\Schema\Database;
use PhpMiniDatabase\Schema\Row;

$directory = sys_get_temp_dir() . '/ch16-update-' . getmypid();

register_shutdown_function(static function () use ($directory): void {
    if (!is_dir($directory)) {
        return;
    }

    $remove = static function (string $path) use (&$remove): void {
        if (is_dir($path)) {
            foreach (scandir($path) ?: [] as $entry) {
                if ($entry !== '.' && $entry !== '..') {
                    $remove($path . '/' . $entry);
                }
            }

            rmdir($path);

            return;
        }

        unlink($path);
    };

    $remove($directory);
});

$database = Database::open($directory);
$executor = new Executor($database);

$layout = static function (Database $database): string {
    $lines = [];

    foreach ($database->heapFile('t')->scan() as $id => $record) {
        $row = $database->table('t')->deserializeRow($record);
        $lines[] = sprintf(
            '  %s  id=%d n=%d note=%d bytes',
            $id,
            $row->get('id'),
            $row->get('n'),
            strlen((string) $row->get('note')),
        );
    }

    return implode("\n", $lines);
};

// Four rows that together fill most of one page: 4 x (1 + 4 + 4 + 2 + 2000)
// bytes of record plus four slot entries leave roughly 120 bytes free.
$small = str_repeat('x', 2000);
$big = str_repeat('y', 2100);

$executor->run('CREATE TABLE t (id INT, n INT, note VARCHAR(4000))');
$executor->run("INSERT INTO t (id, n, note) VALUES (1, 0, '$small'), (2, 0, '$small'), (3, 0, '$small'), (4, 0, '$small')");

printf("before: %d page(s)\n%s\n\n", $database->heapFile('t')->pageCount(), $layout($database));

// Growing each row by 100 bytes passes what page 0 has left: HeapFile
// deletes the old copy and appends the new one at the file's end.
$affected = $executor->run("UPDATE t SET n = n + 1, note = '$big' WHERE id <= 4");

printf("affected: %d\n", $affected);
printf("after: %d page(s)\n%s\n\n", $database->heapFile('t')->pageCount(), $layout($database));

// Every n is 1, not 2: the scan that found the rows finished before the
// first write, so no moved row could be found again by it.
$rows = array_map(
    static fn (Row $row): string => sprintf('id=%d n=%d', $row->get('id'), $row->get('n')),
    iterator_to_array($executor->run('SELECT id, n FROM t ORDER BY id')->rows),
);

printf("each row incremented once: %s\n", implode(', ', $rows));

// And the table still holds exactly the four rows it started with.
printf("rows after a no-op UPDATE: %d\n", $executor->run('UPDATE t SET id = id'));

$database->close();
```

**Перевод комментариев:**
- `update-collect.php` (PHPDoc): `UPDATE`, который растит строки: каждая
  совпавшая строка читается до записи любой из них — исправление Halloween
  problem. `HeapFile::update()`, который растит строку за пределы свободного
  места страницы, удаляет старую копию и дописывает новую в КОНЕЦ файла — на
  страницу, до которой ещё не дошёл работающий скан. Наивный `UPDATE`,
  сканирующий и мутирующий одновременно, нашёл бы переехавшую строку снова и
  применил `SET` к ней второй раз. Этот скрипт строит ровно такую ловушку:
  четыре строки заполняют большую часть одной страницы на 8 КиБ;
  `UPDATE t SET n = n + 1, note = <bigger>` растит каждую строку за пределы
  свободного места страницы: первая ещё влезает в ~120 свободных байт, вторая
  нет — она переезжает в конец файла, а ~2000 байт, которые она освобождает,
  позволяют последним двум вырасти на месте; `n = n + 1` — индикатор: строка,
  посещённая дважды, вышла бы с `n = 2`, посещённая один раз — с `n = 1`.
  Размещение кучи до и после печатается прямым `HeapFile::scan()` — новый
  `RecordId` переехавшей строки указывает на страницу, в которую тот же скан
  вошёл бы следующей.
- `// Four rows that together fill most of one page: 4 x (1 + 4 + 4 + 2 + 2000)`
  — четыре строки вместе заполняют большую часть одной страницы: 4 × (1 + 4 + 4
  + 2 + 2000) байт записи плюс четыре слота оставляют примерно 120 свободных
  байт.
- `// Growing each row by 100 bytes passes what page 0 has left: HeapFile` —
  рост каждой строки на 100 байт превышает остаток страницы 0: `HeapFile`
  удаляет старую копию и дописывает новую в конец файла.
- `// Every n is 1, not 2: the scan that found the rows finished before the` —
  каждое `n` равно 1, а не 2: скан, нашедший строки, закончился до первой
  записи, поэтому ни одна переехавшая строка не могла быть найдена им снова.
- `// And the table still holds exactly the four rows it started with.` — и
  таблица по-прежнему содержит ровно те четыре строки, с которых началась.

```text
before: 1 page(s)
  0:0  id=1 n=0 note=2000 bytes
  0:1  id=2 n=0 note=2000 bytes
  0:2  id=3 n=0 note=2000 bytes
  0:3  id=4 n=0 note=2000 bytes

affected: 4
after: 2 page(s)
  0:0  id=1 n=1 note=2100 bytes
  0:2  id=3 n=1 note=2100 bytes
  0:3  id=4 n=1 note=2100 bytes
  1:0  id=2 n=1 note=2100 bytes

each row incremented once: id=1 n=1, id=2 n=1, id=3 n=1, id=4 n=1
rows after a no-op UPDATE: 4
```

Разбор — по адресам. До обновления все четыре строки лежат на странице 0 в
слотах 0–3. После обновления страниц стало две, и **id=2 стоит `1:0`** — на
странице 1, слоте 0: первая строка ещё влезла в ~120 свободных байтов, вторая
переполнила страницу, удалилась и была добавлена `HeapFile::insert()` в конец
файла. Ровно туда, где скан, пишущий по мере чтения, оказался бы следующим. А
строки id=3 и id=4, которые переехавшая строка освободила ~2000 байт, остались
на месте — `0:2` и `0:3`, — подтвердив правило вставки из главы 15: новый хвост
файла, а не первая полупустая страница.

Проверка детектором: `each row incremented once` — все четыре строки с `n = 1`.
Даже у **переехавшей** строки, которую наивный скан нашёл бы вторично на новом
месте. Сбор всех кандидатов в `$writes` до первой записи — и это не оптимизация,
а единственная корректная семантика.

И заключительная проверка на согласованность: `UPDATE t SET id = id` меняет
каждую строку «на себя», но `affected` равен 4 и ни одна строка не задублирована
— таблица по-прежнему содержит ровно четыре строки. Примечательно, что этот
no-op проходит через уникальность `id` без отказа:
`IndexMaintainer::assertUniqueForUpdate()` сравнивает новую строку с её же
старым `RecordId` и пропускает совпадение.

Этот эксперимент — единственный из четырёх, который не просто наблюдает, а
воспроизводит отказ: `n = 2` у одной из строк — это и был бы Halloween problem.
Ловушка здесь не сработала, потому что защита — два цикла вместо одного — стоит
в коде до того, как ситуация возникла.

## 5. Отказы

| Что ломается                                  | Чем обнаруживается                                                                                                 | Что происходит                                                        |
| --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------- |
| Незакрытый строковый литерал                  | `Lexer::delimited()` → `Unterminated string literal.` (эксперимент 1: line 3, column 23)                           | позиция — **открывающая** кавычка, а не конец файла                   |
| Неожиданный символ в выражении                | `Expected an expression, found ">".` (line 3, column 12)                                                           | отказ указывает на второй символ `>>`: первый уже принят за оператор  |
| Скрипт из двух выражений в `parseOne()`       | `Expected exactly one statement, found 2.`                                                                         | пустые выражения между `;` считаются, а не игнорируются               |
| `EXPLAIN` запроса без `FROM`                  | `ExecutionException`: `EXPLAIN requires a FROM clause.`                                                            | плана не существует — отказ, а не «пустой план»                       |
| Подзапрос                                     | `Subqueries are not supported yet.` (`Evaluator`)                                                                  | именованный пробел: отказ вместо тихо неверного результата            |
| Производная таблица (`SELECT` в `FROM`)       | `Derived tables are not supported yet.` (`Planner`, `Executor`)                                                    | та же политика: названное неподдерживаемое ≠ ошибочно выполненное     |
| `SELECT *` в соединении                       | `SELECT * is not supported for JOIN queries yet; list the columns explicitly.` (`Planner`)                         | амбигуитет столбцов запрещён на уровне плана, а не runtime'ом         |
| Неизвестная функция                           | бросает при **исполнении**, не при разборе (`docs/sql.md`)                                                         | `SELECT nosuch(1)` проходит парсер и падает на первом вызове          |
| NULL в условии `WHERE`/`HAVING`               | строка исчезает; `NOT` не возвращает её (эксперимент 3)                                                            | «неизвестно» ≠ «истина»; только `IS NULL` работает с NULL определённо |
| NULL в `CHECK`                                | проходит вставка (эксперимент 3), `17` — `Check constraint "ck_c_age" on "c" failed.`                              | зеркальные ворота: `CHECK` отклоняет только доказанный `false`        |
| `(age > 18) OR (age <= 18)` при `age = NULL`  | не «полнота», а 4 строки из 5 (эксперимент 3)                                                                      | трёхзначная логика: дисъюнкт неизвестных неизвестен                   |
| `UPDATE`, растящий строку за пределы страницы | `n` стало бы 2 у посещённой дважды строки (эксперимент 4 — ловушка не сработала)                                   | Halloween problem: сбор всех кандидатов до первой записи              |
| Нарушение уникальности внутри `UPDATE`        | `ConstraintViolationException`                                                                                     | ни одна строка не изменена: проверки идут в первой фазе, до записи    |
| Неизвестный узел в `compile()`                | `Cannot compile plan node %s.`                                                                                     | новый `LogicalPlan` без ветки компиляции — отказ, а не пропуск        |
| Несогласованные типы в выражении              | нет отказа: запрос выполняется (`docs/sql.md`: «an inconsistent query runs, it does not get a SQL-standard error») | именованный компромисс: сравнение типов — не задача этого слоя        |
| Несколько индексов на одном столбце подряд    | второй `CREATE INDEX` на тот же столбец                                                                            | `singleColumnIndexNameFor()` детерминированно выбирает один           |

Из шестнадцати строк тринадцать — это отказы с **сообщением**, и большинство из
них видели в экспериментах 1, 3 и 4. Три последние строки — честно перечисленные
ограничения: проект называет их именами в `docs/sql.md` («a named gap, not
silently wrong results») вместо того, чтобы делать вид, что их нет.

## 6. Как это проверено

`tests/Unit/Sql/` — 162 теста, 694 утверждения. Разбор текста проверяется прежде
всего **позициями и отказами**: `LexerTest` (20 тестов) содержит
`testTokenPositionsPointAtTheirStart`,
`testStringLiteralUnescapesDoubledQuotes`, `testAnUnterminatedStringThrows`,
`testAnUnexpectedCharacterThrows`,
`testAQuotedIdentifierIsNeverMatchedAsAKeyword` (двойная кавычка отдаёт
идентификатор, даже если внутри написано ключевое слово) и
`testEmptySourceIsJustEof`. `ParserErrorTest` (12 тестов) проверяет координаты
отказа — `testTheExceptionReportsALineAndColumn` — и грамматические края,
например `testATrailingSemicolonIsOptional`. Остальные файлы разбора:
`ParserSelectTest` (15), `ParserExpressionTest` (20), `ParserDdlTest` (24),
`ParserDmlTest` (9), `ParserExplainTest` (2), `ParserTransactionTest` (13),
`StatementSplitterTest` (8) и `ExpressionPrinterTest` (5 тестов на 97
утверждений — печать выражений проверяется на побайтовое совпадение).

Планы и оптимизатор — `PlannerTest` (12 тестов), `ConstantFoldingTest` (4),
`IndexSelectionTest` (6), `PredicatePushdownTest` (6), `JoinReorderingTest` (6).
Ключевые проверки соответствуют эксперименту 2 один в один:
`testAPlainSelectPlansAsScanThenProjectThenLimit`,
`testEveryPlanEndsInALimitNode`, `testNoScanStartsWithAnIndexChosen`
(планировщик **никогда** не выбирает индекс сам — это работа оптимизатора),
`testTheFilterIsKeptEvenOnceAnIndexIsChosen`,
`testAComparisonAgainstAnUnindexedColumnLeavesTheScanAlone`,
`testALogicalCombinationOfLiteralsCollapses`, `testAPlaceholderIsNeverFolded`
(параметр `?` не является константой для свёртки),
`testAConjunctPushedOntoAJoinSideCanStillPickThatSidesIndex` (порядок правил из
докблока `Optimizer` — проверенный инвариант) и отказы:
`testADerivedTableIsRejected`, `testAJoinRejectsSelectStar`.

`tests/Unit/Execution/` — 249 тестов, 721 утверждение. Сканирование и фильтры:
`ExecutorIndexTest` (20), `ExecutorJoinTest` (10), `ExecutorGroupByTest` (9);
сами операторы — 55 тестов в `Operator/*`. `ExecutorExplainTest` — все 8 тестов
напечатаны в эксперименте 2 планами: `SeqScan` до индекса, `IndexScan` после,
`Filter` который остаётся, `HashJoin` для `INNER` и `NestedLoopJoin` для `LEFT`.
Трёхзначная логика — `EvaluatorTest`, 35 тестов на 164 утверждения, включая обе
таблицы `AND`/`OR` со всеми комбинациями `true`/`false`/`null`. Ограничители —
`ExecutorConstraintTest` (17) и `ConstraintEnforcerTest` (7); таблицы растают —
`ExecutorAlterTableTest` (23), `TableBuilderTest` (12); индексы поддерживаются —
`IndexMaintainerTest` (7).

Сборка всех кандидатов `UPDATE` до записи закреплена тестом
`testGrowingARowDuringUpdateDoesNotVisitItTwice` — прямая защита ловушки
эксперимента 4, — а целостность после обновления — `ExecutorTransactionTest` (21
тест), где проверяется и то, что нарушение уникальности не оставляет изменённой
половины таблицы.

Сквозная проверка — `tests/Integration/SqlEndToEndTest.php`: 5 тестов, 20
утверждений, запросы через HTTP-уровень против живой базы.

Прогоны при написании главы (Docker, PHP 8.5.11): `tests/Unit/Sql` и
`tests/Unit/Execution` — OK, 411 тестов, 1415 утверждений вместе;
`SqlEndToEndTest` — OK, 5 тестов, 20 утверждений; полный набор `make test` — OK,
**1082 теста, 3338 утверждений**.

## 7. Цифры

Размеры кода главы (все числа — из файлов релиза `v1.1.0`):

| Файл                                     | Строк     | Что делает                                                 |
| ---------------------------------------- | --------- | ---------------------------------------------------------- |
| `src/Sql/Lexer.php`                      | 288       | один проход по байтам, токены с позициями                  |
| `src/Sql/Parser.php`                     | 1137      | рекурсивный спуск, 15 видов запросов (`statements`)        |
| `src/Sql/TokenType.php`                  | 227       | 93 варианта токенов, каждое ключевое слово — отдельный тип |
| `src/Sql/Planner/Planner.php`            | 214       | `Statement` → `LogicalPlan`                                |
| `src/Sql/Optimizer/Optimizer.php`        | 46        | цепочка из 4 правил                                        |
| `src/Execution/Executor.php`             | 1261      | компиляция плана и `SELECT`/`INSERT`/`UPDATE`/`DELETE`     |
| `src/Execution/Expression/Evaluator.php` | 319       | вычисление выражений, обе воротные функции                 |
| `src/Sql/ExpressionPrinter.php`          | 133       | обратная печать выражений для `EXPLAIN`                    |
| `src/Sql/Planner/Plan/`                  | 10 файлов | 8 реализаций `LogicalPlan` + абстрактный базис             |

Операторов сравнения в `BinaryOperator` и `UnaryOperator` — 12; типов данных,
перечисленных в `docs/sql.md`, — 8 (`INT`, `BIGINT`, `BOOL`, `VARCHAR`,
`DECIMAL`, `DATE`, `DATETIME`, `BLOB`); агрегатов — 5 (`COUNT`, `SUM`, `AVG`,
`MIN`, `MAX`); скалярных функций — 6 (`COALESCE`, `UPPER`, `LOWER`, `LENGTH`,
`ABS`, `CONCAT`) плюс `CURRENT_TIMESTAMP` и `CURRENT_DATE`. `CURRENT_TIME` не
поддерживается — осознанный пробел в `docs/sql.md`.

Живые прогоны этой главы (Docker, PHP 8.5.11; все четыре скрипта
детерминированы, повторный прогон каждого даёт те же байты):

| Что                            | Значение                                                                                                                                                    |
| ------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sql-tokens`                   | 18 токенов (EOF @122); `STRING` `'O'Brien'` @52; три отказа: (3, 23), (3, 12), «found 2»                                                                    |
| `explain-plans`                | `SeqScan` → `IndexScan` при том же `Filter`; `orders` — 2 страницы, `users` — 1; `LEFT JOIN` остаётся `NestedLoopJoin` + `Filter` наверху                   |
| `pull-and-nulls`               | 4 из 5 строк прочитано при `LIMIT 2`; `age > 18` → 3 строки; `NOT` → только bob; полнота `OR` → 4 строки; `IS NULL` → carol; `CHECK` принял NULL, отверг 17 |
| `update-collect`               | 1 → 2 страницы; `id=2` переехал в `1:0`; все `n = 1`; no-op `UPDATE` → 4                                                                                    |
| Тесты `tests/Unit/Sql`         | 162 теста, 694 утверждения                                                                                                                                  |
| Тесты `tests/Unit/Execution`   | 249 тестов, 721 утверждение                                                                                                                                 |
| `SqlEndToEndTest`              | 5 тестов, 20 утверждений                                                                                                                                    |
| Весь набор `php-mini-database` | 1082 теста, 3338 утверждений                                                                                                                                |

Документированные характеристики — из `docs/sql.md` и `README.md`: PHPStan level
8 без ошибок, покрытие строк около 90%, поддерживаемые выражения описаны с
указанием приоритетов, а неподдерживаемые — перечислены явно. Обзорный
`docs/architecture.md` упоминает одно правило при четырёх фактических —
единственное расхождение документации с кодом, обнаруженное при написании главы.

## 8. Упражнения

1. **Посчитайте.** Запрос `SELECT name FROM users WHERE age > 18` после
   `CREATE INDEX` даёт план из четырёх узлов, а соединение из эксперимента 2 —
   из шести. Сколько строк вернёт `EXPLAIN` для `LEFT JOIN`-запроса из
   эксперимента 2 и какой будет отступ у самого глубокого узла? (Ответ: 7 строк;
   8 пробелов.)
2. **Объясните.** Почему `Filter` остаётся над `IndexScan`, а не заменяется им?
   Что конкретно сломалось бы в третьем плане эксперимента 2, если бы
   `IndexSelection` **удаляла** предикат после выбора индекса?
3. **Найдите ошибку.** `PredicatePushdown` не опускает конъюнкты на
   nullable-сторону `LEFT JOIN`. Опишите конкретную таблицу из трёх строк и
   запрос, для которых спуск `WHERE o.total > 100` на сторону `orders` изменил
   бы результат — то есть сделал бы его строже, чем требует SQL.
4. **Спроектируйте.** Правило `LimitPushdown`: `Limit` над `Join` и над `Sort`.
   Для какого из двух расположений опускание `LIMIT n` вниз сохраняет ответ, а
   для какого — нет? Почему `Limit` над `Sort` никогда нельзя опустить к скану?
5. **Посчитайте.** `ConstantFolding` свернула `2 + 3 = 5` в `TRUE`, но оставила
   `TRUE AND age > 18`. Сколько токенов сэкономила бы свёртка `TRUE AND` до
   `age > 18` — и почему в `IndexSelection` результат не изменился бы в обоих
   случаях?
6. **Разберите отказ.** `WHERE age >> 21` даёт «found ">"» на столбце 12, хотя
   первый `>` стоит в столбце 11. Опишите по шагам, что именно сделал парсер до
   момента отказа — и почему сообщение указывает на второй символ, а не на
   первый.
7. **Сравните.** `executeUpdate()` держит все кандидаты в `$writes` до записи.
   Назовите минимальный размер `WHERE`-выборки, при котором эта стратегия станет
   проблемой, и предложите механизм, который сохраняет и защиту от Halloween
   problem, и ограниченную память. (Подсказка: диапазоны `RecordId` и отложенный
   повторный проход.)

## Что дальше

- **Глава 17** — транзакции и изоляция: блокировки строк, взятые
  `executeUpdate()` в том же проходе, что и чтение, становятся предметом
  изоляции, а `withTransaction()` — границей, за которой ни один из двух циклов
  не видит чужих данных.
- **Глава 18** — WAL, fsync и восстановление: страница, растущая при обновлении,
  теперь пишется через журнал, и «строка переехала» означает ещё и «запись в WAL
  была первой».
- **Глава 19** — анализ текста: Unicode, токены, стемминг — другой лексер,
  другая задача, но тот же принцип: один проход, позиции в байтах и отказы с
  координатами.
- **Глава 20** — инвертированный индекс и BM25 в `php-mini-search`: тот же
  принцип «индекс выбирается по предикату», но живущий в памяти и считающий
  релевантность, а не только адреса.
- **Глава 26** — как проверять тесты: почему зелёный тест на `UPDATE` ещё не
  доказывает, что он способен упасть.
