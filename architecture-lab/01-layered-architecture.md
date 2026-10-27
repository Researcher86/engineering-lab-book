# Глава 1. Layered Architecture: слои на бумаге и зависимости в коде

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Это первая глава. Дальше: порты и адаптеры (глава 2).

Слоистая архитектура — это архитектура по умолчанию. Почти любой PHP-проект
начинается с папок `Controller`, `Service`, `Repository`, `Entity`, и почти любой
со временем обнаруживает, что папки есть, а направления зависимостей никто не
проверяет. Контроллер импортирует репозиторий, сервис знает `PDO`, сущность
знает массив из базы. Формально слои на месте; фактически границы исчезли.

В этой главе слоистая архитектура рассматривается не как образец, а как отправная
точка. В лаборатории есть работающий baseline `src/Baseline/Layered/`: он
проводит настоящее зачисление в SQLite, считает SQL-запросы и намеренно содержит
ровно две ошибки зависимостей. Дальше главы переписывают этот код, и baseline
остаётся точкой сравнения. Вопрос главы: что именно покупают слои, если их
понимать как правило зависимостей, а не как четыре папки.

---

## 1. Проблема

Идея слоистой архитектуры проста: код делится на горизонтальные слои, и
зависимости идут в одну сторону. Презентация знает приложение, приложение знает
домен, инфраструктура знает домен. Домен не знает ничего о базе, HTTP и
фреймворке. Тогда UI можно заменить, не переписывая правила, а базу — не
переписывая сценарии.

У этой идеи есть три типичные точки отказа.

**Первый отказ — фреймворк в приложении.** Сервис импортирует `PDO`, сам
открывает транзакцию и знает имена таблиц. Слой приложения становится
неотделим от драйвера базы. Юнит-тест такого сервиса требует SQLite, а замена
базы означает правку бизнес-логики.

**Второй отказ — layer skip.** Контроллеру понадобился отчёт, метода в сервисе
не было, и контроллер обратился прямо к репозиторию. Одна строка экономит
полдня; после неё UI знает слой данных, а правила отчёта живут в двух местах.

**Третий отказ — общая плоская модель.** Вместо агрегатов и границ появляется
одна таблица «на всё», а «домен» становится набором массивов, которые сервис
перекладывает из `SELECT` в `INSERT`. Слои остаются, но знание о схеме
просачивается наружу.

Baseline лаборатории воспроизводит все три. Это не карикатура на чужой код:
именно так выглядит код, который «уже работает» и который страшно трогать.
Задача главы — увидеть цену этого компромисса в числах и в зависимостях.

---

## 2. Устройство

### 2.1. Карта baseline

`src/Baseline/Layered/` — семь файлов и 316 строк:

```text
Presentation/LayeredEnrollmentController.php
Application/LayeredEnrollmentService.php
Domain/CourseFull.php
Domain/DuplicateEnrollment.php
Infrastructure/LayeredCourseRepository.php
Infrastructure/LayeredEnrollmentRepository.php
Infrastructure/Schema.php
```

Слои названы правильно. Проблема не в именах, а в том, что `Application`
импортирует `Infrastructure` и `PDO`, а `Presentation` импортирует
`Infrastructure` напрямую. Именно эти два импорта и находит checker.

### 2.2. Сервис, который владеет базой

Конструктор сервиса принимает `PDO` и два конкретных репозитория:

```php
final readonly class LayeredEnrollmentService
{
    public function __construct(
        private PDO $pdo,
        private LayeredCourseRepository $courses,
        private LayeredEnrollmentRepository $enrollments,
        private Counters $counters,
    ) {
    }
```

[`src/Baseline/Layered/Application/LayeredEnrollmentService.php` L24-L32](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Baseline/Layered/Application/LayeredEnrollmentService.php#L24-L32)

Здесь ещё нет SQL, но уже есть зависимость от драйвера. Сервис нельзя
инстанцировать без `PDO`; в тестах это означает настоящую SQLite-базу или
mock драйвера. Порты, через которые приложение могло бы говорить с хранилищем,
отсутствуют.

Метод `enroll()` идёт дальше: он проверяет дубликат, открывает транзакцию,
читает курс, проверяет вместимость, вставляет зачисление, увеличивает счётчик
мест и коммитит. Вся последовательность — в одном методе.

```php
    public function enroll(string $studentId, string $courseId): array
    {
        if ($this->enrollments->exists($studentId, $courseId)) {
            throw new DuplicateEnrollment(sprintf('%s already enrolled in %s', $studentId, $courseId));
        }

        $this->pdo->beginTransaction();
        ++$this->counters->sqlQueries;

        try {
            $course = $this->courses->find($courseId);

            if ($course === null) {
                throw new RuntimeException(sprintf('Unknown course: %s', $courseId));
            }

            if ($course['seats_taken'] >= $course['capacity']) {
                throw new CourseFull(sprintf('Course %s is full', $courseId));
            }

            $id = $this->enrollments->insert([
                'student_id' => $studentId,
                'course_id' => $courseId,
                'course_title' => $course['title'],
                'status' => 'confirmed',
                'paid' => 1,
                'amount_cents' => $course['price_cents'],
                'created_at' => new DateTimeImmutable()->format(DATE_ATOM),
            ]);
            $this->courses->incrementSeats($courseId);
            $this->pdo->commit();
            ++$this->counters->sqlQueries;

            return [
                'id' => $id,
                'student_id' => $studentId,
                'course_id' => $courseId,
                'status' => 'confirmed',
                'amount_cents' => $course['price_cents'],
            ];
        } catch (Throwable $exception) {
            $this->pdo->rollBack();
            ++$this->counters->sqlQueries;

            throw $exception;
        }
    }
```

[`src/Baseline/Layered/Application/LayeredEnrollmentService.php` L37-L83](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Baseline/Layered/Application/LayeredEnrollmentService.php#L37-L83)

Это классический transaction script. Он работает и даже транзакционен. Но
границы ответственности размыты: сервис знает и сценарий, и схему хранения, и
политику транзакции. `CourseFull` и `DuplicateEnrollment` — доменные исключения,
но домен не проверяет вместимость и уникальность: это делает сервис по данным,
которые достал репозиторий.

Отдельно заметим проверку дубликата до `beginTransaction()`. В одном процессе и
при одном соединении это безопасно, но в конкурентной среде между `SELECT` и
`INSERT` может вклиниться второй запрос. Уникальный индекс в схеме спасает от
дубликата, но не превращает проверку в атомарную бизнес-операцию. В главе 3 эта
ответственность переедет в агрегат и юнит работы.

### 2.3. Контроллер, который знает репозиторий

Контроллер получает сервис и репозиторий. Для команды `enroll()` он вызывает
сервис, а для отчёта — репозиторий напрямую:

```php
final readonly class LayeredEnrollmentController
{
    public function __construct(
        private LayeredEnrollmentService $service,
        private LayeredEnrollmentRepository $enrollments,
    ) {
    }

    /**
     * @return array{id: int, student_id: string, course_id: string, status: string, amount_cents: int}
     */
    public function enroll(string $studentId, string $courseId): array
    {
        return $this->service->enroll($studentId, $courseId);
    }

    /**
     * @return list<array<string, mixed>>
     */
    public function report(string $courseId): array
    {
        return $this->enrollments->forCourse($courseId);
    }
}
```

[`src/Baseline/Layered/Presentation/LayeredEnrollmentController.php` L17-L40](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Baseline/Layered/Presentation/LayeredEnrollmentController.php#L17-L40)

Строка `return $this->enrollments->forCourse($courseId);` — и есть layer skip.
Проблема не в том, что контроллер умеет читать список. Проблема в том, что он
теперь зависит от слоя данных: имя метода репозитория, форма массива и решение
«отчёт не проходит через приложение» стали частью презентации. Если завтра
отчёт должен скрывать отменённые зачисления или требовать права, это придётся
делать в контроллере или дублировать в сервисе.

### 2.4. Схема, которая стала моделью

Схема baseline плоская: курс и зачисление — две таблицы, а название курса
хранится в обеих.

```php
final class Schema
{
    public static function create(PDO $pdo): void
    {
        $pdo->exec(
            'CREATE TABLE IF NOT EXISTS layered_courses (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                seats_taken INTEGER NOT NULL DEFAULT 0,
                price_cents INTEGER NOT NULL
            )',
        );
        $pdo->exec(
            'CREATE TABLE IF NOT EXISTS layered_enrollments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                student_id TEXT NOT NULL,
                course_id TEXT NOT NULL,
                course_title TEXT NOT NULL,
                status TEXT NOT NULL,
                paid INTEGER NOT NULL,
                amount_cents INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (student_id, course_id)
            )',
        );
    }
```

[`src/Baseline/Layered/Infrastructure/Schema.php` L10-L36](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Baseline/Layered/Infrastructure/Schema.php#L10-L36)

`course_title` в `layered_enrollments` — это денормализация, которая когда-то
была осознанной. Но вместе с ней в зачисление попадает копия названия, которую
никто не обновляет при переименовании курса. `status` — строка, `paid` — целое
число, а `seats_taken` живёт отдельно от списка зачислений. Инвариант «сумма
подтверждённых мест не больше capacity» не принадлежит ни одному объекту: он
размазан между `SELECT`, `if` и `UPDATE`.

### 2.5. Как checker видит нарушение

Правила baseline описаны отдельно от правил чистого дерева. `checkLayered()`
смотрит на пары «слой источника → слой цели» и на специальные импорты:

```php
    public function checkLayered(array $files): array
    {
        $violations = [];

        foreach ($files as $file) {
            foreach ($file->imports as $import) {
                $sourceLayer = $this->layerOf($file->namespace);
                $targetLayer = $this->layerOf($import);

                if ($sourceLayer === 'Presentation' && $targetLayer === 'Infrastructure') {
                    $violations[] = $this->violation(
                        'layer-skip',
                        $file,
                        $import,
                        'Presentation reaches infrastructure directly, skipping the application layer',
                    );
                }

                if ($sourceLayer === 'Application' && ($import === 'PDO' || $import === 'PDOStatement')) {
                    $violations[] = $this->violation(
                        'framework-in-application',
                        $file,
                        $import,
                        'Application code imports the database driver and cannot be tested without it',
                    );
                }

                if ($sourceLayer === 'Domain'
                    && str_starts_with($import, 'ArchitectureLab\\')
                    && $targetLayer !== 'Domain'
                    && !str_starts_with($import, 'ArchitectureLab\\Shared\\')) {
                    $violations[] = $this->violation(
                        'domain-leak',
                        $file,
                        $import,
                        'The layered domain is not isolated from outer layers',
                    );
                }
            }
        }

        return $violations;
    }
```

[`src/Architecture/RuleChecker.php` L117-L159](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/RuleChecker.php#L117-L159)

Checker не исполняет код и не строит AST. Он читает `use`-импорты и сравнивает
пространства имён. Этого достаточно, чтобы поймать оба намеренных нарушения:
`LayeredEnrollmentService -> PDO` и `LayeredEnrollmentController ->
LayeredEnrollmentRepository`. Baseline исключён из чистого анализа и проверяется
отдельно; это важно, иначе его нарушения маскировали бы нарушения остального
дерева.

---

## 3. Эксперимент

Эксперимент создаёт SQLite в памяти, публикует один курс с capacity 200 и
проводит 200 зачислений через контроллер. Затем вызывает `report()` — тот самый
путь с layer skip — и отдельно анализирует baseline.

```bash
make experiment ARGS="layered:cost --students=200"
```

Вывод одного прогона:

```text
Experiment: Layered baseline
A working flow, measured as the "before" architecture.

students: 200, capacity: 200
SQL statements: 1201
elapsed: 4.071 ms
layer violations: 2
  framework-in-application     ArchitectureLab\Baseline\Layered\Application\LayeredEnrollmentService -> PDO
  layer-skip                   ArchitectureLab\Baseline\Layered\Presentation\LayeredEnrollmentController -> ArchitectureLab\Baseline\Layered\Infrastructure\LayeredEnrollmentRepository
```

Почему 1 201 запрос? Одно зачисление выполняет шесть операций, которые
учитывает счётчик: `exists()` (1 `SELECT`), `beginTransaction()` (1),
`find()` (1 `SELECT`), `insert()` (1 `INSERT`), `incrementSeats()` (1 `UPDATE`)
и `commit()` (1). 200 × 6 = 1 200, плюс один `SELECT` из `report()` — итого
1 201. Счётчик считает и управляющие операторы транзакции, поэтому «шесть
запросов на зачисление» — это шесть обращений к базе, а не шесть бизнес-шагов.

Время 4.071 мс — один прогон эксперимента, а не медиана. Бенчмарк измеряет ту же
операцию иначе: 200 итераций на повторение, пять повторений.

| Benchmark        | Итерации |   Медиана |       p95 |       max | ops/sec | PHP delta | peak delta |
| ---------------- | -------: | --------: | --------: | --------: | ------: | --------: | ---------: |
| `layered.enroll` |      200 |   3.337ms |   3.398ms |   3.398ms |  59 933 |     +592B |         0B |
| `layered.report` |      500 | 306.747ms | 315.663ms | 315.663ms |   1 630 |     +592B |      +648B |

`layered.enroll` вызывает `enroll()` 200 раз; `layered.report` вызывает
`report()` 500 раз и каждый раз возвращает все строки курса. Это не
репрезентативный размер страницы: 306 мс — цена повторяемого полного чтения, а
не свойство «слоистой архитектуры» как таковой. Именно поэтому в таблице есть
отдельная строка, а не одно общее число.

---

## 4. Что ломается

**Тестируемость.** Чтобы проверить `LayeredEnrollmentService`, нужен настоящий
`PDO`. В тестах baseline используется SQLite в памяти; это честный интеграционный
тест, но не юнит-тест бизнес-правила. Если завтра появится PostgreSQL, сервис
придётся менять или тащить в тест оба драйвера.

**Замена хранилища.** Репозитории возвращают массивы, а сервис знает их ключи:
`seats_taken`, `price_cents`, `title`. Смена схемы или переход на документное
хранилище задевает application layer. Порты и DTO из следующих глав появляются
именно для того, чтобы это знание осталось в инфраструктуре.

**Транзакция как ответственность сервиса.** `beginTransaction()` и `commit()`
находятся в приложении. Пока сценарий один, это терпимо. Когда появляется
второй агрегат и событие, сервис начинает комбинировать транзакции вручную, и
граница «что атомарно» определяется в каждом методе заново. В главе 6 эта
граница станет явной: запись агрегата и запись в outbox в одной транзакции.

**Layer skip.** `report()` не проходит через приложение. Сегодня это чтение
списка; завтра — отчёт с правами, фильтрацией и пагинацией. Контроллер уже
зависит от репозитория, поэтому изменения потянут его. Проверка зависимостей
ловит это как нарушение, но только если правило объявлено и запущено в CI.

**Плоская схема.** `course_title` хранится в зачислении, `status` — строка,
`paid` — число, `seats_taken` — отдельный счётчик. Нет одного места, где
инвариант «мест не больше capacity» проверяется для всех путей. Baseline
проверяет его в сервисе, но `incrementSeats()` можно вызвать и в обход.

**Сравнение нечестное по поведению.** Baseline не вызывает Billing: он сразу
ставит `status = confirmed` и `paid = 1`. Это упрощение, а не эквивалентная
реализация. Числа главы 1 описывают стоимость и форму кода, а не полный
сценарий. Полный сценарий появится в главе 7, и там сравнение будет по
инвариантам, а не по секундомеру.

**Слои без правила — это папки.** Наличие `Presentation`, `Application`,
`Domain` и `Infrastructure` ничего не гарантирует. Направление зависимостей
проверяет `RuleChecker`; без него layer skip останется незамеченным, пока кто-то
не прочитает `use`-блоки.

---

## 5. Как это проверено

Baseline покрыт пятью тестами:

- `testEnrollsAStudentAndUpdatesTheSeatCount` — успешное зачисление;
- `testDuplicateEnrollmentIsRefused` — повторное зачисление отклоняется;
- `testFullCourseIsRefusedAndTransactionRollsBack` — откат при заполненном
  курсе;
- `testUnknownCourseRollsBack` — откат при неизвестном курсе;
- `testReportUsesTheRepositoryDirectly` — отчёт действительно идёт мимо сервиса.

Архитектурные тесты проверяют не только baseline, но и то, что чистый код не
деградировал:

- `testLayeredBaselineHasExactlyTheIntendedViolations` требует ровно два
  нарушения и конкретные правила `framework-in-application` и `layer-skip`;
- `testLayeredCheckerFindsTheLayerSkipAndPdoImport` проверяет, что checker
  находит оба нарушения на реальном дереве;
- `testCleanTreeHasNoViolationsAndNoCycles` требует ноль нарушений и ноль
  циклов в остальном `src/`.

Полный прогон на релизе: 172 теста, 1 270 утверждений; PHPStan level 8 без
ошибок на 178 файлах; PHP-CS-Fixer не меняет ни один файл. Baseline участвует в
этом прогоне, но исключён из чистого архитектурного отчёта — это разные
контракты, и они проверяются отдельно.

---

## 6. Что запомнить

- Слоистая архитектура — это прежде всего направление зависимостей, а не набор
  папок.
- `PDO` в application layer делает сценарий неотделимым от драйвера базы.
- Layer skip начинается с одной удобной строки и заканчивается знанием UI о
  схеме данных.
- Плоская схема размазывает инвариант по нескольким запросам; агрегат собирает
  его в одном месте.
- Стоимость baseline измеряется счётчиками (`1 201` SQL на 200 зачислений) и
  временем, но эти числа описывают конкретный fixture, а не универсальный закон.
- Нарушение зависимостей должно проверяться автоматически, иначе оно остаётся
  договорённостью.

---

## 7. Упражнения

1. Добавьте в `LayeredEnrollmentService::enroll()` проверку, что студент не
   зачислен на курс, и объясните, почему `UNIQUE (student_id, course_id)` всё
   равно нужен.
2. Удалите `PDO` из конструктора сервиса, оставив только репозитории. Какие
   методы придётся добавить в репозитории, чтобы транзакция осталась в одном
   месте? Сравните с портом `UnitOfWork` из главы 2.
3. Напишите тест, который доказывает, что `report()` не проходит через
   application layer. Почему этот тест хрупкий?
4. Добавьте в `checkLayered()` правило «Domain не импортирует Infrastructure» и
   проверьте его на синтетическом файле.
5. Запустите `layered.report` с разным числом строк и объясните, почему время
   растёт линейно, а `ops/sec` падает.
6. Предложите, как разделить `LayeredEnrollmentService` на два класса без
   введения портов. Какие зависимости останутся у каждого?

---

## 8. Что дальше

Baseline показал цену прямой зависимости: приложение знает драйвер, презентация
знает репозиторий, а схема знает всё. В главе 2 появляется порт
`DocumentStore`: приложение формулирует, что ему нужно от хранилища, а
in-memory и SQLite-адаптеры реализуют это отдельно. Тот же сценарий зачисления
запускается на двух адаптерах, и решение сравнивается не по коду, а по
результату и числу SQL-запросов.
