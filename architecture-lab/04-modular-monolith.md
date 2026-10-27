# Глава 4. Modular Monolith: модуль как граница, а не как сервис

> Проект: [`architecture-lab`](https://github.com/Researcher86/architecture-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/architecture-lab/releases/tag/v1.0.0).
> Предыдущая глава: Clean Architecture. Дальше: CQRS (глава 5).

Модульный монолит — это одна развёртываемая единица, внутри которой есть
границы. Соблазн маятника понятен: либо «монолит», где всё импортирует всё,
либо «микросервисы», где границы видны только в сети и в Kubernetes. Модульный
монолит занимает среднюю позицию: границы объявлены в коде как namespace и
публичные API, а развёртывание остаётся одним процессом.

В лаборатории пять контекстов — `Catalog`, `Enrollment`, `Billing`, `Reporting`
и `Notifications` — живут в одном PHP-процессе и одной SQLite-базе. Между ними
нет HTTP. Есть правило: чужой контекст можно импортировать только через его
`PublicApi`. Checker из главы 3 проверяет это правило, а эксперимент показывает,
что все четыре межконтекстных импорта идут через публичные API и ни один — через
внутренности.

Глава отвечает на вопрос: что именно делает модуль модулем, если у него нет
своего процесса, своей базы и своего деплоя.

---

## 1. Проблема

Большой монолит ломается не в один день. Сначала контроллер одного модуля
импортирует репозиторий другого, потому что «нужен всего один запрос». Потом
сервис биллинга начинает читать таблицу зачисления, потому что join дешевле
вызова. Потом отчёт зависит от внутреннего класса каталога. Через год нельзя
сказать, где кончается один контекст и начинается другой: границы существуют
только в головах.

Микросервисы решают часть этой проблемы ценой распределённых транзакций,
сетевых сбоев, версионирования API и операционной сложности. Для учебной задачи
это было бы нечестно: большая часть кода ушла бы в инфраструктуру, а не в
архитектуру.

Модульный монолит предлагает компромисс:

- **одна единица развёртывания** — один процесс, одна база, один `make up`;
- **явные границы** — namespace, `PublicApi`, anti-corruption адаптеры;
- **проверяемое правило** — checker запрещает импорт внутренностей соседа;
- **возможность извлечь модуль позже** — если границы настоящие, вынос в
  отдельный сервис становится инженерной задачей, а не переписыванием.

Цена тоже есть: границы не защищены сетью, общая база соблазняет join-ами,
а транзакция может незаметно пересечь несколько модулей. Глава показывает, где
эти риски остаются.

---

## 2. Устройство

### 2.1. Карта модулей

`ModuleMap::architectureLab()` — это явный список пяти модулей. У каждого есть
имя, корневой namespace, namespace публичного API и описание.

```php
    public static function architectureLab(): self
    {
        return new self([
            new Module(
                'Catalog',
                'ArchitectureLab\\Catalog',
                'ArchitectureLab\\Catalog\\PublicApi',
                'Course offerings, capacity and seat ownership',
            ),
            new Module(
                'Enrollment',
                'ArchitectureLab\\Enrollment',
                'ArchitectureLab\\Enrollment\\PublicApi',
                'Student requests and enrollment state transitions',
            ),
            new Module(
                'Billing',
                'ArchitectureLab\\Billing',
                'ArchitectureLab\\Billing\\PublicApi',
                'Invoices and payment decisions',
            ),
            new Module(
                'Reporting',
                'ArchitectureLab\\Reporting',
                'ArchitectureLab\\Reporting\\PublicApi',
                'Event-built read models',
            ),
            new Module(
                'Notifications',
                'ArchitectureLab\\Notifications',
                'ArchitectureLab\\Notifications',
                'The observable notification side effect',
            ),
        ]);
    }
```

[`src/Architecture/ModuleMap.php` L21-L55](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/ModuleMap.php#L21-L55)

`Module` — простой value object: имя, корень, публичный namespace, описание.
Карта не является контейнером и не создаёт объекты. Она нужна анализатору,
чтобы по имени класса ответить, к какому модулю он относится, и проверить, ведёт
ли импорт в публичную часть.

`Shared`, `Eventing`, `Persistence`, `Cqrs`, `Runtime`, `Integration` и
`Architecture` в карту не входят. Это не «шестой модуль»: общий kernel и
инфраструктура не имеют собственного бизнес-контекста. Правила для них всё равно
действуют, но метрики считаются только по пяти контекстам.

### 2.2. Публичный API

Публичный API — не «все публичные классы модуля». Это отдельный namespace,
который модуль обещает поддерживать. `CatalogApi` описывает пять операций:
опубликовать курс, зарезервировать место, освободить место, получить курс,
получить список.

```php
interface CatalogApi
{
    public function publish(OfferingId $id, string $title, int $priceCents, int $capacity, string $startsAt): void;

    public function reserveSeat(OfferingId $id): void;

    public function releaseSeat(OfferingId $id): void;

    public function offering(OfferingId $id): OfferingView;

    /**
     * @return list<OfferingView>
     */
    public function offerings(): array;
}
```

[`src/Catalog/PublicApi/CatalogApi.php` L15-L29](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Catalog/PublicApi/CatalogApi.php#L15-L29)

В сигнатурах нет `CourseOffering`, `CatalogService`, `PDO` или массива из базы.
Есть value objects из `Shared` и DTO `OfferingView`. Это важно: вызывающий
контекст не может случайно изменить агрегат или увидеть его внутреннее
состояние. Он получает снимок.

`CatalogService` реализует этот интерфейс и остаётся application-классом:

```php
final readonly class CatalogService implements CatalogApi
{
    public function __construct(
        private CourseOfferingRepository $offerings,
        private UnitOfWork $unitOfWork,
        private Outbox $outbox,
        private EventRecorder $recorder,
    ) {
    }

    public function publish(OfferingId $id, string $title, int $priceCents, int $capacity, string $startsAt): void
    {
        $offering = new CourseOffering(
            $id,
            $title,
            new Money($priceCents),
            new DateTimeImmutable($startsAt),
            $capacity,
        );

        $this->unitOfWork->transactional(function () use ($offering): void {
            $this->offerings->save($offering);
            $this->outbox->record($this->recorder->record(
                EventNames::COURSE_OFFERING_PUBLISHED,
                $offering->id()->value,
                [
                    'offering_id' => $offering->id()->value,
                    'title' => $offering->title(),
                    'price_cents' => $offering->price()->cents,
                    'currency' => $offering->price()->currency,
                    'capacity' => $offering->capacity(),
                    'starts_at' => $offering->startsAt()->format(DATE_ATOM),
                ],
            ));
        });
    }
```

[`src/Catalog/Application/CatalogService.php` L28-L63](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Catalog/Application/CatalogService.php#L28-L63)

Модуль владеет своим агрегатом, своим репозиторием и своей записью в outbox.
Публичный метод не возвращает агрегат и не раскрывает транзакцию. Событие
записывается в той же транзакции, что и документ курса; это пригодится в главе 6.

### 2.3. Правило межконтекстных импортов

Checker уже умеет проверять направление импортов. Для модульного монолита
добавляется правило: если источник и цель принадлежат разным модулям, импорт
должен начинаться с `publicApiNamespace` цели.

```php
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
```

[`src/Architecture/RuleChecker.php` L75-L102](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Architecture/RuleChecker.php#L75-L102)

Правило работает на уровне импортов, а не вызовов. Если `Enrollment` напишет
`use ArchitectureLab\Catalog\Domain\CourseOffering;`, checker увидит нарушение,
даже если объект нигде не используется. Если он импортирует
`ArchitectureLab\Catalog\PublicApi\CatalogApi`, всё в порядке. Именно поэтому
антикоррупционные адаптеры живут в `Infrastructure` вызывающего контекста: они
единственные, кому разрешено касаться чужого API, и они переводят его в
локальный порт.

### 2.4. Один runtime, пять модулей

Композиционный корень собирает все модули в одном процессе. Вот фрагмент, где
runtime подписывает слушателей Reporting и Notifications на события:

```php
        $bus = new InMemoryEventBus($counters);
        $offeringProjection = new OfferingCapacityProjection($store, $counters);
        $scheduleProjection = new StudentScheduleProjection($store, $counters);
        $offeringListener = new IdempotentListener('reporting.offering', $offeringProjection, $processed, $counters);
        $scheduleListener = new IdempotentListener('reporting.schedule', $scheduleProjection, $processed, $counters);
        $notificationListener = new IdempotentListener('notifications.enrollment', $this->notifications, $processed, $counters);

        foreach ([
            EventNames::COURSE_OFFERING_PUBLISHED,
            EventNames::SEAT_RESERVED,
            EventNames::SEAT_RELEASED,
            EventNames::ENROLLMENT_REQUESTED,
            EventNames::ENROLLMENT_CONFIRMED,
            EventNames::ENROLLMENT_CANCELLED,
        ] as $eventName) {
            $bus->subscribe($eventName, $offeringListener);
        }

        foreach ([
            EventNames::ENROLLMENT_REQUESTED,
            EventNames::ENROLLMENT_CONFIRMED,
            EventNames::ENROLLMENT_CANCELLED,
        ] as $eventName) {
            $bus->subscribe($eventName, $scheduleListener);
        }

        foreach ([EventNames::ENROLLMENT_CONFIRMED, EventNames::ENROLLMENT_CANCELLED] as $eventName) {
            $bus->subscribe($eventName, $notificationListener);
        }

        if ($mode === 'event-driven') {
            $billingListener = new IdempotentListener(
                'billing.enrollment_requested',
                new EnrollmentRequestedListener($billing),
                $processed,
                $counters,
            );
            $bus->subscribe(EventNames::ENROLLMENT_REQUESTED, $billingListener);
            $paymentResultListener = new IdempotentListener(
                'enrollment.payment_result',
                new PaymentResultListener($enrollmentRepository, $seats, $store, $outbox, $recorder, $clock),
                $processed,
                $counters,
            );
            $bus->subscribe(EventNames::PAYMENT_SUCCEEDED, $paymentResultListener);
            $bus->subscribe(EventNames::PAYMENT_FAILED, $paymentResultListener);
        }
```

[`src/Runtime/LabRuntime.php` L117-L163](https://github.com/Researcher86/architecture-lab/blob/v1.0.0/src/Runtime/LabRuntime.php#L117-L163)

`Reporting` не импортирует `Enrollment`, `Billing` или `Catalog`. Он подписан на
имена событий и читает payload. `Notifications` вообще не знает контекстов: это
`EventLog`, который реагирует на два имени. Связь есть, но она идёт через
published language, а не через классы.

Это важное отличие модульного монолита от «просто папок»: модули не вызывают
друг друга напрямую в обход API, а runtime — единственное место, где известно,
кто на что подписан.

### 2.5. Метрики модулей

Для каждого модуля анализатор считает `Ca`, `Ce`, `I`, `A`, `D`. В этой главе
важны первые два: они показывают, кто на кого смотрит.

```text
module         classes     Ca     Ce       I abstractness
Billing             11      1      0   0.000        0.273
Catalog             10      1      0   0.000        0.200
Enrollment          20      0      2   1.000        0.200
Notifications        1      0      0   0.000        0.000
Reporting            9      0      0   0.000        0.000
```

`Catalog` и `Billing` имеют `Ca = 1`: их импортирует `Enrollment`. `Enrollment`
имеет `Ce = 2`: он импортирует два чужих публичных API. `Reporting` и
`Notifications` имеют нули, потому что runtime подписывает их слушателей, а не
импортирует их классами. Метрика честно показывает граф импортов; граф
подписок она не видит, и это ограничение важно для интерпретации.

---

## 3. Эксперимент

Эксперимент сканирует дерево, считает межконтекстные рёбра и проверяет, сколько
из них идут через `PublicApi`.

```bash
make experiment ARGS="modules:coupling"
```

Вывод одного прогона:

```text
Experiment: Modular monolith
A module is a namespace with a public API, not a deployment unit.

modules: 5, cross-context imports: 4, through PublicApi: 4
violations: 0, cycles: 0

module         classes     Ca     Ce       I abstractness
Billing             11      1      0   0.000        0.273
Catalog             10      1      0   0.000        0.200
Enrollment          20      0      2   1.000        0.200
Notifications        1      0      0   0.000        0.000
Reporting            9      0      0   0.000        0.000
```

Четыре межконтекстных импорта — это два ACL-адаптера (`CatalogSeatAllocator`,
`BillingPaymentProcessor`) и два класса, которые они используют. Все четыре
идут через `PublicApi`. Нарушений нет, циклов нет. Это не значит, что модули
идеальны; это значит, что объявленное правило выполнено.

Теперь цена границы. Бенчмарк сравнивает чтение через `CatalogApi` с прямым
чтением документа из store:

| Benchmark              | Итерации | Медиана  | p95      | max      | ops/sec | PHP delta | peak delta |
| ---------------------- | -------: | -------: | -------: | -------: | ------: | --------: | ---------: |
| `modules.public_api`   |    1 000 | 1.632ms  | 1.911ms  | 1.911ms  | 612 745 |     +592B |         0B |
| `modules.store_read`   |    1 000 | 0.102ms  | 0.107ms  | 0.107ms  | 9 848 142 |     +592B |         0B |
| `modules.command_bus`  |      200 | 1.470ms  | 1.991ms  | 1.991ms  | 136 008 |   +2.56MB |   +2.63MB |

Прямое чтение примерно в 16 раз быстрее. Это не аргумент удалить границу.
Прямой путь знает имя коллекции и форму документа; именно эту связь правило
модуля и запрещает. Публичный API платит за гидратацию агрегата, проверку
инвариантов и преобразование в DTO. Вопрос не «что быстрее», а «что вы хотите
сохранить при следующем изменении».

`modules.command_bus` добавляет публикацию курса через `CommandBus`; его PHP
delta растёт, потому что каждый вызов оставляет документ курса и сообщение в
outbox. Это снова разные единицы: время, SQL, память — не один счёт.

---

## 4. Что ломается

**Namespace — не deployment.** Модуль можно вынести в отдельный процесс только
после того, как у него нет общих таблиц и синхронных вызовов в обход API. В этой
лаборатории все пять модулей делят один SQLite-файл. Это удобно для сценария, но
не является планом миграции на сервисы.

**Общая база.** Коллекции разделены по владельцам, но физически лежат в одной
таблице `documents`. Ничто не мешает запросу из `Reporting` прочитать коллекцию
`Enrollment`. Checker видит импорты, но не SQL: если код дотянется до store и
напишет имя чужой коллекции, правило не сработает. Владение коллекциями —
договорённость, а не ограничение базы.

**Транзакция через границы.** В синхронном режиме `RequestEnrollmentHandler`
вызывает Billing через порт после первой транзакции. Это два юнита работы, а не
один распределённый. Если процесс упадёт между ними, зачисление останется
`pending_payment`, а платёж может быть уже списан. В событийном режиме та же
проблема решается outbox и идемпотентностью, но не превращается в распределённую
транзакцию.

**Публичный API может стать god interface.** Если складывать в `CatalogApi` все
методы, которые понадобились соседям, модуль теряет границу. В лаборатории API
маленький; в реальном проекте его нужно версионировать и держать совместимым.
DTO — часть контракта, их изменение ломает вызывающих.

**Подписки не видны в графе.** `Reporting` и `Notifications` имеют `Ca = 0`,
хотя runtime их использует. Импортный граф не видит событий. Если модуль
общается только событиями, его связанность нужно измерять отдельно: по именам
событий, по payload и по подпискам, а не по `use`.

**Метрики не дают приоритета рефакторинга.** `D = 1` у листа — нормальная
форма, а не приказ «добавить интерфейсов». `I = 1` у `Enrollment` означает, что
он зависит от двух модулей и ни от кого не зависит; это ожидаемо для
оркестратора, а не «нестабильность», которую надо лечить.

**Один процесс скрывает сетевые режимы.** В модульном монолите вызов через
`PublicApi` — это вызов метода. Он не может timeout-нуть, вернуть 503 или
потерять сообщение. Все эти сценарии появятся только при выносе в сервис;
лаборатория их не моделирует и не обещает.

---

## 5. Как это проверено

- `testCrossContextImportsMustUsePublicApi` — импорт внутренностей соседа
  даёт нарушение `cross-context-public-api`;
- `testInfrastructureMayUseAnotherContextPublicApi` — ACL-адаптер в
  `Infrastructure` может импортировать чужой `PublicApi`;
- `testDeliveryMustNotImportContextInternals` — `Integration`/`Cli` не видят
  внутренности;
- `testApplicationMustNotReachAnotherContextDirectly` — application не вызывает
  чужой контекст напрямую;
- `testCleanTreeHasNoViolationsAndNoCycles` — на реальном дереве ноль нарушений
  и циклов;
- эксперимент `modules:coupling` печатает `4/4` межконтекстных импорта через
  `PublicApi`;
- сценарий из главы 7 проверяет, что модули действительно сходятся к одному
  состоянию: read model, write model и Catalog conservation.

---

## 6. Что запомнить

- Модуль — это namespace с публичным API и правилом импортов, а не отдельный
  деплой.
- `PublicApi` — обещание поддерживать контракт, а не все публичные классы.
- Межконтекстные связи идут через API; ACL-адаптер переводит чужой язык в
  локальный.
- Один runtime и одна база — сознательное упрощение, а не модель распределённой
  системы.
- Граф импортов не видит событий и SQL; ограничения надо называть.
- Цена границы видна в бенчмарке (16× на чтении), но это цена за сохранение
  контракта, а не «лишние миллисекунды».

---

## 7. Упражнения

1. Добавьте шестой модуль `Reviews` с собственным `PublicApi` и проверьте, что
   checker не пропускает импорт его внутренностей.
2. Попробуйте прочитать чужую коллекцию из `Reporting` напрямую. Какой тест
   это поймает, а какой — нет?
3. Сделайте `CatalogApi` слишком широким: добавьте метод, возвращающий
   `CourseOffering`. Какие правила и тесты упадут?
4. Объясните, почему `Notifications` имеет `Ca = 0`, хотя получает события.
   Предложите метрику для событийной связанности.
5. Сравните `modules.public_api` и `modules.store_read` на разном числе
   документов. Меняется ли отношение?
6. Нарисуйте, как вынесли бы `Billing` в отдельный сервис. Какие транзакции и
   контракты придётся изменить?

---

## 8. Что дальше

Модули научились не импортировать внутренности друг друга, но чтение всё ещё
идёт по write-модели: `forStudent()` читает все зачисления и фильтрует в PHP.
В главе 5 появляется CQRS: команды и запросы разделены, проекции строятся из
событий, а query handler читает один документ вместо сканирования коллекции.
Тот же сценарий покажет, что read model отстаёт от write model до доставки
событий — и это не баг, а свойство, которое нужно проверять.
