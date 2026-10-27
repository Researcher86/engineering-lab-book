# Глава 3. Clean Architecture: правило зависимостей как исполняемая проверка

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: порты и адаптеры. Дальше: модульный монолит (глава 4).

Clean Architecture часто показывают как диаграмму концентрических кругов:
entities внутри, use cases вокруг, адаптеры снаружи. Диаграмма полезна, но её
легко повесить на стену и не выполнять. В коде остаются те же импорты, что и
раньше, только папки называются `Domain`, `Application`, `Infrastructure`.

В этой главе Clean Architecture — это одно правило: **зависимости направлены
внутрь**. Внутренний слой не импортирует внешний. Правило сформулировано как
код, который читает `use`-импорты, строит граф, находит нарушения и циклы и
считает метрики модулей. Checker не доказывает, что архитектура хороша. Он
доказывает, что объявленные направления соблюдаются, и делает нарушение
видимым в CI, а не в ретроспективе через полгода.

---

## 1. Проблема

Допустим, есть правило: «domain не зависит от инфраструктуры». Как его
проверить? Можно договориться на ревью. Можно написать в README. Можно
надеяться, что все помнят. Все три способа ломаются одинаково: один импорт
`use PDO;` в доменном классе — и правило перестало быть правдой, хотя README
остался прежним.

Нужен механизм, который:

1. читает исходники без запуска приложения;
2. строит зависимости между классами и модулями;
3. проверяет запрещённые направления;
4. находит циклы, потому что цикл — это тоже нарушение направления;
5. считает хотя бы грубые метрики, чтобы видеть форму графа;
6. выдаёт отчёт, пригодный для CI и для человека.

В лаборатории этот механизм — `ArchitectureAnalyzer`. Он не парсит PHP в
полноценное AST: для правила импортов достаточно токенов. Он не исполняет код и
не видит динамические имена классов. Это ограничение объявлено, а не спрятано.
Зато checker не тянет внешнюю зависимость и сам является частью измеряемого
кода.

---

## 2. Устройство

### 2.1. Сканер: что именно читается

`SourceScanner::parse()` получает исходник и превращает его в `SourceFile`:
путь, namespace, список импортов, список объявленных классов и число строк.
Сканер идёт по токенам и отслеживает глубину фигурных скобок.

```php
    public function parse(string $path, string $source): SourceFile
    {
        $tokens = token_get_all($source);
        $namespace = '';
        $imports = [];
        $classes = [];
        $depth = 0;
        $abstractPending = false;
        $count = count($tokens);

        for ($index = 0; $index < $count; ++$index) {
            $token = $tokens[$index];

            if (is_string($token)) {
                if ($token === '{') {
                    ++$depth;
                } elseif ($token === '}') {
                    --$depth;
                }

                continue;
            }
```

[`src/Architecture/SourceScanner.php` L58-L79](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/SourceScanner.php#L58-L79)

Глубина нужна, чтобы не принимать `use` внутри функции за импорт пространства
имён. PHP разрешает `use` для замыканий, и без проверки `$depth === 0` сканер
ловил бы их как зависимости.

Импорты читаются до `;` или `{`, затем обрезаются по ключевому слову `as` и
ведущему слешу:

```php
            if ($token[0] === T_USE && $depth === 0) {
                [$statement, $index] = $this->readStatement($tokens, $index + 1);
                $statement = trim($statement);

                if ($statement === '' || str_starts_with($statement, 'function ') || str_starts_with($statement, 'const ')) {
                    continue;
                }

                foreach (explode(',', $statement) as $import) {
                    $import = trim($import);
                    $aliasPosition = stripos($import, ' as ');

                    if ($aliasPosition !== false) {
                        $import = substr($import, 0, $aliasPosition);
                    }

                    $import = ltrim(trim($import), '\\');

                    if ($import !== '') {
                        $imports[] = $import;
                    }
                }

                continue;
            }
```

[`src/Architecture/SourceScanner.php` L93-L117](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/SourceScanner.php#L93-L117)

Объявления классов определяются по токенам `T_CLASS`, `T_INTERFACE`, `T_TRAIT`,
`T_ENUM`. Анонимный класс (`new class`) пропускается, потому что перед `T_CLASS`
стоит `T_NEW`. Абстрактность запоминается отдельным флагом: `T_ABSTRACT` идёт
перед `T_CLASS` и не всегда лежит в одном месте с именем.

```php
            if (!in_array($token[0], [T_CLASS, T_INTERFACE, T_TRAIT, T_ENUM], true)) {
                continue;
            }

            $previous = $this->previousMeaningful($tokens, $index);

            if ($token[0] === T_CLASS && in_array($previous, [T_NEW, T_DOUBLE_COLON], true)) {
                continue;
            }

            $name = $this->nextIdentifier($tokens, $index + 1);

            if ($name === null) {
                continue;
            }

            $kind = match ($token[0]) {
                T_INTERFACE => ClassKind::Interface,
                T_TRAIT => ClassKind::Trait,
                T_ENUM => ClassKind::Enum,
                default => ClassKind::ClassName,
            };
            $classes[] = new ClassInfo(
                name: ltrim($namespace . '\\' . $name, '\\'),
                kind: $kind,
                abstract: $kind === ClassKind::Interface || $abstractPending,
                line: (int) $token[2],
            );
            $abstractPending = false;
        }

        return new SourceFile(
            path: $path,
            namespace: $namespace,
            imports: array_values(array_unique($imports)),
            classes: $classes,
            lines: substr_count($source, "\n") + 1,
        );
    }
```

[`src/Architecture/SourceScanner.php` L119-L157](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/SourceScanner.php#L119-L157)

Сканер не различает `use Foo\Bar` и фактическое обращение к `Bar`. Импорт —
это намерение зависимости, а не доказательство вызова. Для архитектурного
правила это подходит: если класс импортирует чужой слой, он уже связан с ним
на уровне исходника, даже если вызов закомментирован.

### 2.2. Граф: только внутренние рёбра

`DependencyGraph::edges()` оставляет только импорты, начинающиеся с
`ArchitectureLab\`. Внешние пакеты и `PDO` в граф модулей не попадают; для них
есть отдельные правила baseline.

```php
    public function edges(array $files): array
    {
        $edges = [];

        foreach ($files as $file) {
            foreach ($file->imports as $import) {
                if (!str_starts_with($import, self::ROOT)) {
                    continue;
                }

                $edges[] = new DependencyEdge(
                    fromClass: $file->primaryClass(),
                    fromNamespace: $file->namespace,
                    toClass: $import,
                    toNamespace: $this->namespaceOfClass($import),
                    path: $file->path,
                );
            }
        }

        return $edges;
    }
```

[`src/Architecture/DependencyGraph.php` L17-L38](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/DependencyGraph.php#L17-L38)

Ребро направлено от класса-источника к импортированному классу. Из этих рёбер
строится два представления:

- граф классов — для отчёта и потенциального анализа;
- граф модулей — уникальные рёбра между пятью контекстами, по которым ищутся
  циклы и считаются метрики.

Модуль — это namespace с публичным API. `ModuleMap` перечисляет пять модулей и
для каждого хранит корень и `publicApiNamespace`. Всё, что не попало в модуль
(shared kernel, eventing, persistence, runtime), в графе модулей не участвует,
но его импорты всё равно проверяются правилами.

### 2.3. Правила: что запрещено

`RuleChecker::checkClean()` проходит по импортам каждого файла. Сначала
определяются модуль источника и модуль цели, затем проверяются направления.
Семь правил:

| Правило                          | Что запрещает                                        |
| -------------------------------- | ---------------------------------------------------- |
| `domain-purity`                  | `Domain` импортирует что-либо, кроме `Shared`        |
| `application-port-only`          | application импортирует `Persistence`                |
| `application-no-adapter`         | application импортирует собственный `Infrastructure` |
| `application-no-foreign-context` | application импортирует другой контекст напрямую     |
| `cross-context-public-api`       | импорт между контекстами идёт мимо `PublicApi`       |
| `delivery-public-api`            | `Integration`/`Cli` видит внутренности контекста     |
| `delivery-no-persistence`        | delivery импортирует `Persistence`                   |

Вот центральная часть проверки:

```php
    public function checkClean(array $files, ModuleMap $map): array
    {
        $violations = [];

        foreach ($files as $file) {
            foreach ($file->imports as $import) {
                if (!str_starts_with($import, 'ArchitectureLab\\')) {
                    continue;
                }

                $sourceModule = $map->moduleFor($file->primaryClass());
                $targetModule = $map->moduleFor($import);
                $sourceInDomain = str_contains($file->namespace, '\\Domain');
                $sourceInApplication = str_contains($file->namespace, '\\Application\\')
                    || str_ends_with($file->namespace, '\\Application');

                if ($sourceInDomain && !str_starts_with($import, 'ArchitectureLab\\Shared\\')) {
                    $violations[] = $this->violation(
                        'domain-purity',
                        $file,
                        $import,
                        'Domain code may depend only on the shared kernel',
                    );
                }

                if ($sourceInApplication) {
                    if (str_starts_with($import, 'ArchitectureLab\\Persistence\\')) {
                        $violations[] = $this->violation(
                            'application-port-only',
                            $file,
                            $import,
                            'Application code must use a port, not a persistence adapter',
                        );
                    }

                    if ($sourceModule !== null && str_starts_with($import, $sourceModule->namespace . '\\Infrastructure\\')) {
                        $violations[] = $this->violation(
                            'application-no-adapter',
                            $file,
                            $import,
                            'Application code must not import its own infrastructure adapter',
                        );
                    }

                    if ($sourceModule !== null && $targetModule !== null && $sourceModule->name !== $targetModule->name) {
                        $violations[] = $this->violation(
                            'application-no-foreign-context',
                            $file,
                            $import,
                            'Application code must reach another context through a port',
                        );
                    }
                }

                if ($sourceModule !== null && $targetModule !== null && $sourceModule->name !== $targetModule->name
                    && !str_starts_with($import, $targetModule->publicApiNamespace)) {
                    $violations[] = $this->violation(
                        'cross-context-public-api',
                        $file,
                        $import,
                        'Cross-context dependencies may use only the target PublicApi namespace',
                    );
                }

                if ($this->isDelivery($file->namespace) && $targetModule !== null
                    && !str_starts_with($import, $targetModule->publicApiNamespace)) {
                    $violations[] = $this->violation(
                        'delivery-public-api',
                        $file,
                        $import,
                        'Delivery code may see PublicApi DTOs, not a context internals',
                    );
                }

                if ($this->isDelivery($file->namespace) && str_starts_with($import, 'ArchitectureLab\\Persistence\\')) {
                    $violations[] = $this->violation(
                        'delivery-no-persistence',
                        $file,
                        $import,
                        'Delivery code must not know the persistence adapter',
                    );
                }
            }
        }

        return $violations;
    }
```

[`src/Architecture/RuleChecker.php` L21-L107](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/RuleChecker.php#L21-L107)

Правила проверяют импорты, а не размещение файлов. Класс может лежать в
`Domain`, но импортировать `Persistence` — это нарушение. И наоборот: файл может
лежать в `Infrastructure`, импортировать чужой `PublicApi` и быть совершенно
легальным антикоррупционным адаптером. Размещение — вопрос вкуса; направление
импорта — вопрос зависимости.

### 2.4. Циклы: Tarjan на графе модулей

Цикл находится алгоритмом Тарьяна. `CycleDetector::find()` возвращает
компоненты сильной связности, в которых больше одного узла. Одиночный узел с
ребром в себя не считается архитектурным циклом.

```php
    public function find(array $adjacency): array
    {
        $index = 0;
        $indices = [];
        $lowLinks = [];
        $stack = [];
        $onStack = [];
        $components = [];

        $strongConnect = function (string $node) use (
            &$strongConnect,
            &$index,
            &$indices,
            &$lowLinks,
            &$stack,
            &$onStack,
            &$components,
            $adjacency,
        ): void {
            $indices[$node] = $index;
            $lowLinks[$node] = $index;
            ++$index;
            $stack[] = $node;
            $onStack[$node] = true;

            foreach ($adjacency[$node] ?? [] as $target) {
                if (!isset($indices[$target])) {
                    $strongConnect($target);
                    $lowLinks[$node] = min($lowLinks[$node], $lowLinks[$target]);
                } elseif ($onStack[$target] ?? false) {
                    $lowLinks[$node] = min($lowLinks[$node], $indices[$target]);
                }
            }

            if ($lowLinks[$node] !== $indices[$node]) {
                return;
            }

            $component = [];

            do {
                $member = array_pop($stack);

                if ($member === null) {
                    break;
                }

                $onStack[$member] = false;
                $component[] = $member;
            } while ($member !== $node);

            if (count($component) > 1) {
                sort($component);
                $components[] = $component;
            }
        };

        foreach (array_keys($adjacency) as $node) {
            if (!isset($indices[$node])) {
                $strongConnect($node);
            }
        }

        return $components;
    }
```

[`src/Architecture/CycleDetector.php` L15-L79](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/CycleDetector.php#L15-L79)

Почему не на графе классов? Цикл между двумя классами внутри одного модуля —
это проблема связности, но не архитектурный цикл между контекстами. Граф
модулей отвечает на вопрос «могут ли два контекста зависеть друг от друга».
Классовые циклы остаются за пределами этой проверки; для них нужен отдельный
анализ, и лаборатория не делает вид, что уже его имеет.

### 2.5. Метрики: форма графа, а не оценка

Для каждого модуля считаются:

- `Ca` — afferent couplings, сколько модулей импортируют этот;
- `Ce` — efferent couplings, сколько модулей импортирует этот;
- `I = Ce / (Ca + Ce)` — instability (0 — стабильный, 1 — нестабильный);
- `A = interfaces / classes` — abstractness;
- `D = |A + I - 1|` — distance from the main sequence.

```php
        $efferent = array_map(static fn (array $targets): int => count($targets), $adjacency);
        $afferent = [];

        foreach ($adjacency as $targets) {
            foreach ($targets as $target) {
                $afferent[$target] = ($afferent[$target] ?? 0) + 1;
            }
        }

        $metrics = [];

        foreach (array_keys($adjacency) as $module) {
            $ce = $efferent[$module] ?? 0;
            $ca = $afferent[$module] ?? 0;
            $instability = $ca + $ce === 0 ? 0.0 : $ce / ($ca + $ce);
            $classCount = $classes[$module] ?? 0;
            $interfaceCount = $interfaces[$module] ?? 0;
            $abstractness = $classCount === 0 ? 0.0 : $interfaceCount / $classCount;
            $metrics[] = new ModuleMetrics(
                module: $module,
                classes: $classCount,
                interfaces: $interfaceCount,
                afferent: $ca,
                efferent: $ce,
                instability: $instability,
                abstractness: $abstractness,
                distance: abs($abstractness + $instability - 1.0),
            );
```

[`src/Architecture/ArchitectureAnalyzer.php` L135-L162](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/ArchitectureAnalyzer.php#L135-L162)

Метрики считаются по уникальным рёбрам между модулями, а не по числу импортов.
Если `Enrollment` импортирует три класса из `Catalog\PublicApi`, это одно ребро
`Enrollment -> Catalog`, а не три. Для формы графа этого достаточно; для силы
связи — нет, и это ограничение стоит помнить.

### 2.6. Сборка отчёта

`ArchitectureAnalyzer::analyzeClean()` сканирует `src`, исключает
`ArchitectureLab\Baseline\`, строит рёбра, проверяет правила, считает метрики и
циклы. Baseline анализируется отдельным методом `analyzeLayered()`.

```php
    public function analyzeClean(string $projectRoot): ArchitectureReport
    {
        $files = array_values(array_filter(
            $this->scanner->scan($projectRoot . '/src', $projectRoot),
            static fn (SourceFile $file): bool => !str_starts_with($file->namespace, 'ArchitectureLab\\Baseline\\'),
        ));
        $map = ModuleMap::architectureLab();
        $edges = $this->graph->edges($files);
        $adjacency = $this->moduleAdjacency($edges, $map);

        return new ArchitectureReport(
            files: $files,
            edges: $edges,
            violations: $this->rules->checkClean($files, $map),
            metrics: $this->metrics($files, $adjacency, $map),
            cycles: $this->cycles->find($adjacency),
        );
    }
```

[`src/Architecture/ArchitectureAnalyzer.php` L27-L44](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/ArchitectureAnalyzer.php#L27-L44)

`ArchitectureReport` — это факты, а не вердикт: файлы, рёбра, нарушения,
метрики, циклы. Вердикт выносит тест или CI, который требует `violations = []`
и `cycles = []`. Такое разделение позволяет использовать один анализатор и для
чистого дерева, и для baseline, и для экспериментов.

---

## 3. Эксперимент

Эксперимент сканирует чистое дерево и baseline, печатает метрики и нарушения.

```bash
make experiment ARGS="clean:dependency-rule"
```

Вывод одного прогона:

```text
Experiment: Clean Architecture dependency rule
Imports, violations, cycles and module metrics.

clean tree: files=127 classes=127 interfaces=20 lines=7030 edges=247 violations=0 cycles=0
layered baseline: files=7 classes=7 lines=316 violations=2

module         classes interfaces     Ca     Ce       I abstractness  distance
Billing             11          3      1      0   0.000        0.273     0.727
Catalog             10          2      1      0   0.000        0.200     0.800
Enrollment          20          4      0      2   1.000        0.200     0.200
Notifications        1          0      0      0   0.000        0.000     1.000
Reporting            9          0      0      0   0.000        0.000     1.000

Layered violations:
  framework-in-application     ArchitectureLab\Baseline\Layered\Application\LayeredEnrollmentService -> PDO
  layer-skip                   ArchitectureLab\Baseline\Layered\Presentation\LayeredEnrollmentController -> ArchitectureLab\Baseline\Layered\Infrastructure\LayeredEnrollmentRepository
```

Читать таблицу нужно осторожно. `Billing` и `Catalog` имеют `Ca = 1` и `Ce = 0`:
их импортирует `Enrollment` через публичные API, а сами они наружу не смотрят.
`Enrollment` имеет `Ce = 2`: два адаптера импортируют `CatalogApi` и
`BillingApi`. `Notifications` и `Reporting` имеют нулевые рёбра, потому что
runtime подписывает их слушателей, а не импортирует их классами. Граф импортов
не видит подписки на события — это важное ограничение, а не ошибка подсчёта.

`D = 1` у `Notifications` и `Reporting` не означает «плохой модуль». Это
означает «в графе импортов модуль конкретный и ни с кем не связан». Метрика
описывает форму, а не качество; в главе 4 это будет видно ещё яснее.

Бенчмарк измеряет стоимость проверки:

| Benchmark      | Итерации |  Медиана |       p95 |       max | ops/sec | PHP delta | peak delta |
| -------------- | -------: | -------: | --------: | --------: | ------: | --------: | ---------: |
| `clean.scan`   |        3 | 95.163ms | 117.870ms | 117.870ms |      32 |  +44 392B |   +42 384B |
| `layered.scan` |       20 | 39.401ms |  43.602ms |  43.602ms |     508 | +320 592B |         0B |

`clean.scan` выполняет три полных сканирования за повторение, поэтому медиана
95 мс — это примерно 31.7 мс на одно сканирование 127 файлов и 247 рёбер.
`layered.scan` выполняет двадцать сканирований семи файлов, поэтому 39.4 мс —
примерно 2 мс на сканирование. Стоимость растёт с числом файлов и объёмом
токенов, а не с числом модулей: это цена гейта, а не алгоритма Тарьяна.

---

## 4. Что ломается

**Сканер — не AST.** Он видит top-level `use`, объявления и глубину скобок, но
не понимает динамические имена классов, `class_exists($name)`, строки в
контейнере и полностью квалифицированные имена без `use`. Класс может обойти
правило, если загрузить зависимость через переменную. Это гейт, а не песочница.

**Групповые импорты.** `use Foo\{Bar, Baz}` читается как текст, а не как два
импорта. В этом проекте групповые импорты не используются, но новый код может
их добавить и получить ложное срабатывание или пропуск. Ограничение объявлено в
`docs/DECISIONS.md`; если группа понадобится, сканер нужно научить её
разворачивать.

**Импорт не равен вызову.** `use` может остаться после рефакторинга и создать
ребро, которого нет в рантайме. И наоборот, вызов по полному имени без `use` не
создаст ребра. Checker проверяет намерение, зафиксированное в исходнике, а не
фактический граф выполнения.

**Циклы только на уровне модулей.** Цикл между классами внутри одного модуля не
попадает в отчёт. Это сознательный выбор: вопрос главы — границы контекстов, а
не связность каждого класса. Для классовых циклов нужен отдельный инструмент.

**Метрики не являются оценкой.** `I`, `A`, `D` зависят от того, как модули
нарезаны, и от того, какие интерфейсы считать. `D = 1` у листа — нормальная
форма графа. Использовать `D` как KPI и требовать «приблизить к нулю» — значит
оптимизировать число, а не архитектуру.

**Baseline исключён по namespace.** `analyzeClean()` фильтрует всё, что
начинается с `ArchitectureLab\Baseline\`. Если добавить новый baseline-класс в
другое место, он попадёт в чистый отчёт. Исключение — договорённость о
расположении, а не свойство кода.

**Ноль нарушений не означает «хорошая архитектура».** Он означает, что
объявленные правила выполнены. Правила могут быть неполными; сама архитектура
может быть неудобной. Checker снижает риск регрессии, но не заменяет
проектирование.

---

## 5. Как это проверено

Архитектурные тесты делятся на две группы: проверка реального дерева и
проверка правил на синтетических файлах.

Реальное дерево:

- `testCleanTreeHasNoViolationsAndNoCycles` — ноль нарушений, ноль циклов,
  больше 100 файлов и классов, больше 15 интерфейсов;
- `testLayeredBaselineHasExactlyTheIntendedViolations` — ровно два нарушения
  baseline и конкретные правила;
- `testLayeredCheckerFindsTheLayerSkipAndPdoImport` — checker действительно
  видит оба нарушения.

Синтетические файлы:

- `testDomainPurityIsEnforced` — `Domain` не может импортировать чужой слой;
- `testDomainMayUseTheSharedKernel` — `Shared` разрешён;
- `testApplicationMustNotImportThePersistenceAdapter` — запрет на `Persistence`;
- `testApplicationMustNotImportItsOwnInfrastructure` — запрет на собственный
  адаптер;
- `testApplicationMustNotReachAnotherContextDirectly` — запрет на чужой контекст;
- `testCrossContextImportsMustUsePublicApi` — импорт только через `PublicApi`;
- `testInfrastructureMayUseAnotherContextPublicApi` — ACL-адаптер разрешён;
- `testDeliveryMustNotImportContextInternals` — delivery видит только API;
- `testCycleDetectorFindsAStronglyConnectedComponent` — Тарьян находит цикл;
- `testCycleDetectorIgnoresSelfLoopsAndAcyclicGraphs` — одиночные узлы и DAG не
  считаются циклом;
- `testReportSerializesItsFacts` — отчёт сериализуется без потери фактов.

Эти тесты важны потому, что checker — тоже код. Если он сломается и начнёт
возвращать пустой список, «ноль нарушений» станет ложью. Синтетические тесты
проверяют, что каждое правило умеет срабатывать, а не только молчать.

---

## 6. Что запомнить

- Clean Architecture — это правило направления зависимостей, а не набор папок.
- Проверять нужно импорты, а не размещение файлов.
- Сканер на токенах достаточно хорош для `use`-правил и честно ограничен.
- Цикл между модулями — архитектурное нарушение; цикл между классами — другая
  задача.
- `Ca`, `Ce`, `I`, `A`, `D` описывают форму графа, а не качество кода.
- Ноль нарушений — это результат проверки, а не сертификат архитектуры.
- Ограничения сканера должны быть записаны рядом с правилами, иначе их
  забудут.

---

## 7. Упражнения

1. Добавьте правило «`Shared` не импортирует контексты» и тест к нему.
2. Научите сканер разворачивать групповые импорты. Какие случаи нужно
   обработать, чтобы не сломать `use function`?
3. Постройте граф классов и найдите цикл внутри одного модуля. Почему
   `CycleDetector` его не показывает?
4. Добавьте в отчёт список самых импортируемых классов. Что он скажет о
   `Shared`?
5. Объясните, почему `Reporting` имеет `Ca = 0`, хотя runtime его использует.
   Как изменилась бы метрика, если бы runtime импортировал класс проекции?
6. Придумайте архитектурное правило, которое нельзя проверить по `use`-
   импортам. Обоснуйте, почему для него нужен другой инструмент.

---

## 8. Что дальше

Правило зависимостей работает, граф чист, циклов нет. Но пять контекстов всё
ещё живут в одном namespace-пространстве и одном процессе. В главе 4 они
становятся модулями с публичными API: `Catalog`, `Enrollment`, `Billing`,
`Reporting`, `Notifications`. Проверяется не только направление импортов, но и
то, что межконтекстные связи идут через `PublicApi`, а не через внутренности
соседа.
