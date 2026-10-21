# Глава 21. Производные данные: версии, надгробия, переиндексация

> Проекты: [`php-mini-search`](https://github.com/Researcher86/php-mini-search)
> (релиз `v1.0.0`, коммит `7ecddc0`) и
> [`php-systems-platform`](https://github.com/Researcher86/php-systems-platform)
> (коммит `9a6a916`). Нужно знать: журнал как единственное надёжное состояние (→
> глава 18), анализ текста (→ глава 19), инвертированный индекс (→ глава 20).
> Пригодится дальше: границы компонентов (→ глава 22) и как распространяются
> отказы (→ глава 23).
>
> Это глава про самый неудобный вид данных: **производный**. Индекс
> `php-mini-search` — не источник истины, а копия строк из `php-mini-database`,
> которой владеет `php-systems-platform`. Копии приезжают асинхронно, могут
> обогнать друг друга, опоздать на минуту или не приехать вовсе.

Главы 18–20 построили поиск, который переживает сбой: журнал, восстановление и
индекс. Но всё это отвечает на вопрос «что делать, если процесс умер». Эта глава
— про вопрос «что делать, если сообщение не пришло»: он не про надёжность
железа, а про распределённое состояние, и решается не журналом, а правилами.

## 1. Проблема

Источник истины — база данных платформы. Индекс — производное: строку создали,
поменяли или удалили, а затем отправили сообщение `product.index`, прочитав
строку **в момент выполнения задания**. Между записью строки и отправкой задания
нет общей транзакции (в платформе нет транзакций вообще), и это честно
задокументированный предел: процесс, умерший между двумя действиями, оставляет
индекс на одно изменение позади до следующего изменения этого товара или до
полной переиндексации.

Раз индекс производный, у него есть свобода, которой нет у базы: его можно
построить заново. Именно поэтому повреждённый журнал здесь — не «отказаться
стартовать», а «потерянный кеш» (→ глава 18). Но у той же производности есть три
неприятных следствия, и они разные.

**Порядок.** Задания индексирования идут параллельно и приходят вразнобой.
Воркер, прочитавший строку до обновления, может доставить своё сообщение после
воркера, прочитавшего её после. Если такой PUT применить, индекс откатится к
устаревшему тексту и останется таким, пока кто-нибудь снова не тронет товар.
Первоначальный проект защищался от этого через `If-Match` со счётчиком версии,
которым владел **сервис**, — и это защищает от двух редакторов, но не от
опоздавшего задания: у опоздавшего `If-Match` тоже совпадёт с текущим состоянием
сервиса.

**Удаление.** Удалить документ и послать PUT — недостаточно: устаревший PUT (тот
самый, что опоздал) просто создаст документ заново. Нужна **памятка об
удалении** — надгробие с версией удаления, — и она должна существовать всегда, а
не «до следующей переиндексации». Соблазн очистить надгробия после полной
переиндексации понятен: источник только что переигран целиком. Но очередь может
доставить устаревшее задание уже после этого: воркер упал, задание ждало
истечения аренды, потом повторилось — и без надгробия удалённый товар
воскресает.

**Полнота.** Товар, удалённый, пока сервис лежал, или чьё задание ушло в очередь
мёртвых задач, не получит сообщения никогда. Переигрывание приходящих сообщений
такое не лечит: сообщения не существует. Нужна полная переиндексация: платформа
просит сервис открыть **поколение**, присылает все живые товары с меткой этого
поколения и завершает его. Всё, что поколение не тронуло, — удалено из базы, и
сервис удаляет это сам.

И тут же — ловушка. Обычные задания индексирования продолжают работать во время
переиндексации, иначе они копились бы в очереди до минуты. Но товар, созданный
**после** того, как переиндексация прошла его ключ, не получит метку поколения —
и `complete` его удалит. Правило, которое это закрывает: **пока поколение
открыто, каждая применённая запись принадлежит ему**. PUT с более новой версией
— свежая правда из источника, откуда бы он ни пришёл; удаление оставляет
надгробие, а `complete` надгробий не трогает никогда.

Отвергнутая альтернатива — теневой индекс с атомарной подменой, как алиасы в
Elasticsearch. Поиск никогда не видел бы полуготовой переиндексации, но два
индекса в памяти — это примерно 600 MB при бюджете 512 MB. Поэтому
переиндексация идёт **на месте**: во время неё поиск видит смесь старых и новых
документов — каждый документ по отдельности всегда согласован, но целого
«снимка» нет.

## 2. Устройство

### Два направления: правка и переиндексация

```text
php-systems-platform                         php-mini-search
────────────────────────                     ───────────────────────────
ProductRepository::save()
  INSERT/UPDATE products
    version = version + 1        ← одна версия на каждую правку
        │
        └─ product.index job  ──────→  PUT /documents/{sku}
           (несёт только sku,             source_version, title, body
            читает строку когда             │
            выполняется)                    ├─ version ≤ held? → applied: false
                                            ├─ version ≤ надгробие? → applied: false
                                            └─ иначе: журнал → fsync → индекс

ProductReindexJob::run()          →  POST /reindex
   for each allLive()             →  PUT … с generation
   completeReindex()              →  POST /reindex/{g}/complete
                                        └─ удалить всё живое без метки g
```

Всё, что решает, — это одно сравнение. `isStale()` проверяет версию записи
против **двух** вещей: текущего документа и надгробия. Запись применяется,
только если она строго новее обеих. Отсюда три поведения, которые надо
различать:

- новее — применяется, версия запоминается;
- равна или старее — `applied: false`. Это **не ошибка**: очередь повторов
  получает «уже применено» и прекращает попытки;
- равна или старее, но несёт номер **открытого** поколения и касается живого
  документа, помеченного другим поколением, — запись не применяется, но документ
  **помечается** подтверждённым этим поколением. Иначе `complete` удалит товар,
  который на самом деле жив и уже проиндексирован.

### Надгробие — это версия удаления

```text
PUT  p1 v2   → documents[p1] = [2, g]
DELETE p1 v3 → documents[p1] удалён, tombstones[p1] = 3
PUT  p1 v2   → 2 ≤ 3 → applied: false   ← воскрешения нет
PUT  p1 v3   → 3 ≤ 3 → applied: false   ← равно тоже не новее
PUT  p1 v4   → 4 > 3 → applied: true    ← воссоздание требует версии выше удаления
```

Версия строки растёт и через удаление: `ProductRepository::delete()` делает
`UPDATE … SET version = version + 1, deleted = TRUE`, а не удаляет строку.
Поэтому «воскресить» товар можно только записью с версией выше удаления — и
удаление не превращается в дыру, через которую проскакивает опоздавшее задание.

Надгробия не чистятся никогда. Одна запись `id → версия` на удалённый товар
стоит дёшево (при документе в 10 000 товаров — это тысячи пар строк), а попытка
их убрать уже один раз привела к воскрешению товара из очереди. **Компактизация
переносит надгробия целиком** — вместе с номерами поколений, которые тоже нельзя
потерять.

### Переиндексация — это тоже состояние

Поколение живёт в журнале, а не в памяти запроса: `reindex-begin` и
`reindex-complete` — это записи, а компактизация пишет запись `checkpoint` с
тремя счётчиками (`next_generation`, `open_generation`,
`last_completed_generation`). Поэтому переиндексация переживает перезапуск, а
незавершённая переиндексация остаётся незавершённой — что и нужно: `complete`
для неё ещё не пришёл, значит и «список того, что поколение тронуло», не полон.
Новая переиндексация отменяет открытую (`cancelled_generation` в ответе), и это
не сбой, а штатное поведение повторной попытки.

Когда сервис вообще готов искать — отдельный вопрос с двумя разными ответами на
один и тот же HTTP-код:

| Состояние                   | Причина                 | Как выходит из него               |
| --------------------------- | ----------------------- | --------------------------------- |
| журнала нет                 | `first_start`           | только завершённая переиндексация |
| журнал повреждён в середине | `log_corrupted`         | только завершённая переиндексация |
| поколение завершено         | `null` — поиск работает | —                                 |

Различие между `first_start` и `log_corrupted` — не косметика: «пусто, потому
что источник пуст» и «пусто, потому что никто не наполнял» — это разные
состояния, и оператор должен их видеть. Переиндексация нуля документов
легитимна: она завершает поколение и переводит сервис в готовность с пустым
корпусом. `/health/ready` при этом отвечает `503`, а не «no results», — потому
что пустая выдача выглядела бы как работающий сервис, который ничего не находит.

### Что стоит дорого

- **Поиск во время переиндексации** видит смесь. Документ всегда согласован
  внутри себя, но «снимка на момент» нет: частичная переиндексация — это не
  атомарная операция.
- **Надгробия растут** вместе с числом удалённых товаров и не чистятся.
- **Индекс отстаёт на одно изменение** при сбое между записью строки и
  публикацией задания — до следующего изменения товара или до переиндексации.
  Это цена отсутствия outbox, и она записана в `docs/failure-handling.md`.
- **Смена анализатора** (→ глава 19) меняет каждый терм, и здесь это означает
  переиндексацию всего корпуса — а не миграцию, потому что журнал хранит
  исходный текст, а не термы.

## 3. Код

### Контракт целиком — в докблоке `Corpus`

Здесь важно каждое слово: класс владеет журналом **и** индексом вместе, поэтому
он единственное место, где их порядок может разойтись. И он же единственное
место, где живут три правила из §2.

```php
/**
 * The searchable corpus: the one module that owns the log and the index
 * together, and so the only place their order can go wrong.
 *
 * Every write follows the same order: check the source version, analyse,
 * append to the log, fsync, apply to the index, return. A search can only
 * see what is already durable, and a crash between the append and the apply
 * loses nothing, because replay applies the record again.
 *
 * Replay and live writes go through the same apply() code, record by record,
 * so the state after a restart is the state before it by construction.
 *
 * The platform owns the data (docs/DECISIONS.md); what this class protects
 * is the order in which copies of it arrive:
 *
 *  - every write carries the row's source version, and one not newer than
 *    what is held — or than a tombstone — is stale and ignored;
 *  - a delete leaves a tombstone forever, so a late PUT cannot resurrect the
 *    document, even after a reindex;
 *  - while a reindex generation is open, every applied write is marked with
 *    it, and a stale PUT that names it marks the document it confirms;
 *    completing the generation removes every live document left unmarked.
 */
```

[`src/Corpus/Corpus.php#L13-L35`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L13-L35)

**Перевод комментариев:**
- `Corpus` (PHPDoc): искомый корпус — единственный модуль, который владеет
  журналом и индексом вместе, а потому единственное место, где их порядок может
  разойтись. Каждая запись идёт в одном порядке: проверить версию источника,
  разобрать текст, дописать журнал, `fsync`, применить к индексу, вернуть
  результат. Поиск видит только уже устойчивое, а сбой между дописыванием и
  применением ничего не теряет: replay применит запись снова. Replay и живые
  записи идут через один и тот же `apply()`, запись за записью, поэтому
  состояние после перезапуска совпадает с состоянием до него по построению.
  Данными владеет платформа (`docs/DECISIONS.md`); класс защищает порядок, в
  котором приходят их копии: каждая запись несёт версию строки источника, и
  запись не новее хранимой — или надгробия — устарела и игнорируется; удаление
  навсегда оставляет надгробие, поэтому опоздавший `PUT` не воскресит документ
  даже после переиндексации; пока поколение переиндексации открыто, каждая
  применённая запись помечается им, а устаревший `PUT`, называющий его, помечает
  подтверждаемый документ; завершение поколения удаляет все живые документы,
  оставшиеся без метки.

Состояние — это два отображения и три счётчика поколений. Отдельного поля
«надгробие + версия» нет: надгробие само по себе версия, и это делает проверку
старения одним выражением.

```php
/** @var array<string, array{int, ?int}> id → [source version, generation] of every live document */
private array $documents = [];

/** @var array<string, int> id → source version of the delete */
private array $tombstones = [];

/** @var array<string, int> id → bytes of the record that holds its current state, for the garbage ratio */
private array $liveBytes = [];

private int $nextGeneration = 1;

private ?int $openGeneration = null;

private ?int $lastCompletedGeneration = null;
```

[`src/Corpus/Corpus.php#L40-L53`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L40-L53)

**Перевод комментариев:**
- `$documents` (`@var`): id → [ версия источника, поколение] каждого живого
  документа.
- `$tombstones` (`@var`): id → версия источника при удалении.
- `$liveBytes` (`@var`): id → байты записи, хранящей текущее состояние, — для
  подсчёта доли мусора.

### PUT и DELETE: одно сравнение на два состояния

`put()` начинается с проверки старения, и только потом анализирует текст.
Обратите внимание на ветку внутри ветки: устаревшая запись может всё-таки
пометить документ — если она несёт номер открытого поколения и подтверждает
живой документ, помеченный другим поколением.

```php
public function put(string $id, int $sourceVersion, string $title, string $body, ?int $generation = null): WriteResult
{
    if ($this->isStale($id, $sourceVersion)) {
        $confirmsOpenGeneration = $generation !== null
            && $generation === $this->openGeneration
            && isset($this->documents[$id])
            && $this->documents[$id][1] !== $generation;

        if ($confirmsOpenGeneration) {
            $this->write(['op' => 'mark', 'id' => $id, 'generation' => $generation]);
        }

        return $this->staleResult($id);
    }

    $terms = ['title' => $this->analyzer->terms($title), 'body' => $this->analyzer->terms($body)];

    // An applied write during a reindex belongs to it, whoever sent it.
    $this->write([
        'op' => 'put',
        'id' => $id,
        'source_version' => $sourceVersion,
        'generation' => $this->openGeneration,
        'title' => $title,
        'body' => $body,
    ], $terms);

    return new WriteResult($id, true, $sourceVersion, $this->openGeneration);
}
```

[`src/Corpus/Corpus.php#L86-L114`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L86-L114)

**Перевод комментариев:**
- `// An applied write during a reindex belongs to it, whoever sent it.` —
  применённая запись во время переиндексации принадлежит ей, кто бы её ни
  прислал.

`delete()` короче и делает только одно: пишет запись об удалении. Но именно эта
запись создаёт надгробие, которое потом останавливает опоздавшие PUT — см.
`apply()` ниже.

```php
public function delete(string $id, int $sourceVersion): WriteResult
{
    if ($this->isStale($id, $sourceVersion)) {
        return $this->staleResult($id);
    }

    $this->write(['op' => 'delete', 'id' => $id, 'source_version' => $sourceVersion]);

    return new WriteResult($id, true, $sourceVersion, null);
}
```

[`src/Corpus/Corpus.php#L116-L125`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L116-L125)

### Поколение: открыть, завершить, отменить

Открытие поколения — это запись в журнал, поэтому оно переживает перезапуск.
Номер выдаётся монотонно: `nextGeneration` увеличивается в `apply()`, а не
здесь, потому что этот же код выполняется при восстановлении. Завершение же
удаляет всё живое, что поколение не пометило, но не трогает надгробия, и
возвращает число удалённых — по нему видно, была ли переиндексация
содержательной.

```php
/**
 * @return array{generation: int, cancelled_generation: ?int}
 */
public function beginReindex(): array
{
    $cancelled = $this->openGeneration;
    $generation = $this->nextGeneration;
    $this->write(['op' => 'reindex-begin', 'generation' => $generation]);

    return ['generation' => $generation, 'cancelled_generation' => $cancelled];
}

/**
 * @return int how many documents the generation did not mark, and were removed
 */
public function completeReindex(int $generation): int
{
    if ($generation !== $this->openGeneration) {
        throw new GenerationNotOpen("Generation {$generation} is not open.");
    }

    $removed = count(array_filter($this->documents, fn (array $document) => $document[1] !== $generation));
    $this->write(['op' => 'reindex-complete', 'generation' => $generation]);

    return $removed;
}
```

[`src/Corpus/Corpus.php#L127-L152`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L127-L152)

**Перевод комментариев:**
- `beginReindex()` (PHPDoc): возвращает номер поколения и отменённое поколение
  (`cancelled_generation`).
- `completeReindex()` (PHPDoc): сколько документов поколение не пометило и они
  были удалены.

### Готовность: два разных «нет»

Одно сравнение — и два разных ответа оператору.

```php
/**
 * Why search is refused, or null when it is not: the corpus has never
 * completed a reindex, or its log was lost.
 *
 * @return ReindexRequired::FIRST_START|ReindexRequired::LOG_CORRUPTED|null
 */
public function reindexRequired(): ?string
{
    if ($this->lastCompletedGeneration !== null) {
        return null;
    }

    return $this->logWasDamaged ? ReindexRequired::LOG_CORRUPTED : ReindexRequired::FIRST_START;
}
```

[`src/Corpus/Corpus.php#L178-L191`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L178-L191)

**Перевод комментариев:**
- `reindexRequired()` (PHPDoc): почему поиск запрещён, или `null`, когда он не
  запрещён: корпус ни разу не завершал переиндексацию или его журнал потерян.
  Возвращает `FIRST_START`, `LOG_CORRUPTED` или `null`.

### Порядок записи и применения

Старение — одна строка на два отображения. Именно из-за надгробия
`source_version <= ($this->tombstones[$id] ?? 0)` не даёт опоздавшему PUT
воскресить документ.

```php
private function isStale(string $id, int $sourceVersion): bool
{
    return $sourceVersion <= ($this->documents[$id][0] ?? 0)
        || $sourceVersion <= ($this->tombstones[$id] ?? 0);
}
```

[`src/Corpus/Corpus.php#L310-L314`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L310-L314)

Дальше — тот же порядок, что в главе 18: сначала журнал, потом состояние.
Записать в журнал и упасть до применения — безопасно, потому что replay применит
запись снова.

```php
/**
 * Append, sync, then apply. The order is the whole point of this class.
 *
 * @param array<string, mixed> $record
 * @param array<string, list<string>>|null $terms already analysed, for a put
 */
private function write(array $record, ?array $terms = null): void
{
    $this->log->append($record);
    $this->apply($record, $terms);
}
```

[`src/Corpus/Corpus.php#L325-L335`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L325-L335)

**Перевод комментариев:**
- `write()` (PHPDoc): дописать, синхронизировать, затем применить — в этом
  порядке весь смысл класса. `$record` — запись журнала, `$terms` — уже
  разобранные термы для `put`.

### `apply()`: одно место, где всё сходится

`apply()` вызывается и живой записью, и восстановлением, и это делает состояние
после перезапуска состоянием до него **по построению**. В первой половине —
`put` и `delete`: видно, как `delete` создаёт надгробие, а `put` его снимает.

```php
private function apply(array $record, ?array $terms = null): void
{
    $id = isset($record['id']) && is_scalar($record['id']) ? (string) $record['id'] : '';
    $version = isset($record['source_version']) && is_int($record['source_version']) ? $record['source_version'] : 0;
    $generation = isset($record['generation']) && is_int($record['generation']) ? $record['generation'] : null;
    $bytes = strlen(LogFormat::frame($record));

    switch ($record['op'] ?? null) {
        case 'put':
            $title = is_string($record['title'] ?? null) ? $record['title'] : '';
            $body = is_string($record['body'] ?? null) ? $record['body'] : '';
            $this->index->put($id, $terms ?? ['title' => $this->analyzer->terms($title), 'body' => $this->analyzer->terms($body)]);
            $this->documents[$id] = [$version, $generation];
            $this->liveBytes[$id] = $bytes;
            unset($this->tombstones[$id]);
            break;

        case 'delete':
            $this->index->delete($id);
            unset($this->documents[$id]);
            $this->tombstones[$id] = $version;
            $this->liveBytes[$id] = $bytes;
            break;
```

[`src/Corpus/Corpus.php#L344-L366`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L344-L366)

Вторая половина — всё, что связано с поколениями и компактизацией. Здесь же
лежит правило «пока поколение открыто, каждая применённая запись принадлежит
ему»: `openGeneration` подставляется в запись на уровне `put()`, а
`reindex-complete` удаляет только тех, чья метка не совпала, и превращает их в
надгробия — с их версиями.

```php
case 'mark':
    if (isset($this->documents[$id])) {
        $this->documents[$id][1] = $generation;
    }
    break;

case 'reindex-begin':
    $this->openGeneration = $generation;
    $this->nextGeneration = ($generation ?? 0) + 1;
    break;

case 'reindex-complete':
    foreach ($this->documents as $documentId => [$documentVersion, $documentGeneration]) {
        if ($documentGeneration !== $generation) {
            $documentId = (string) $documentId;
            $this->index->delete($documentId);
            unset($this->documents[$documentId]);
            $this->tombstones[$documentId] = $documentVersion;
            $this->liveBytes[$documentId] = strlen(LogFormat::frame(
                ['op' => 'delete', 'id' => $documentId, 'source_version' => $documentVersion],
            ));
        }
    }
    $this->openGeneration = null;
    $this->lastCompletedGeneration = $generation;
    break;

case 'checkpoint':
    $next = $record['next_generation'] ?? 1;
    $open = $record['open_generation'] ?? null;
    $last = $record['last_completed_generation'] ?? null;
    $this->nextGeneration = is_int($next) ? $next : 1;
    $this->openGeneration = is_int($open) ? $open : null;
    $this->lastCompletedGeneration = is_int($last) ? $last : null;
    break;
```

[`src/Corpus/Corpus.php#L368-L402`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L368-L402)

### Компактизация: настоящее плюс надгробия

Что именно попадает в компактный журнал — короткий ответ на вопрос «что должно
пережить перезапись»: один `checkpoint` со счётчиками поколений, по одной
актуальной записи на живой документ и **по одной на каждое надгробие**.

```php
/**
 * The records a compacted log consists of: one checkpoint with the state
 * that only history knows, then the current record of every live document
 * and every tombstone. $lastPuts are the put records of the live
 * documents, read back from the log, since the corpus keeps terms, not
 * text.
 *
 * @param array<string, array<string, mixed>> $lastPuts id → its last put record
 * @return iterable<array<string, mixed>>
 */
public function compactedRecords(array $lastPuts): iterable
{
    yield [
        'op' => 'checkpoint',
        'next_generation' => $this->nextGeneration,
        'open_generation' => $this->openGeneration,
        'last_completed_generation' => $this->lastCompletedGeneration,
    ];

    foreach ($this->documents as $id => [$version, $generation]) {
        $record = $lastPuts[$id];
        $record['source_version'] = $version;
        $record['generation'] = $generation;

        yield $record;
    }

    foreach ($this->tombstones as $id => $version) {
        yield ['op' => 'delete', 'id' => (string) $id, 'source_version' => $version];
    }
}
```

[`src/Corpus/Corpus.php#L267-L297`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Corpus.php#L267-L297)

**Перевод комментариев:**
- `compactedRecords()` (PHPDoc): записи, из которых состоит компактный журнал:
  один `checkpoint` с состоянием, которое знает только история, затем текущая
  запись каждого живого документа и каждого надгробия. `$lastPuts` — записи
  `put` живых документов, прочитанные обратно из журнала, поскольку корпус
  хранит термы, а не текст. Возвращает итерируемое записей.

Сама компактизация — это открыть корпус (то есть получить состояние, которое
надо сохранить), перечитать журнал за текстами живых документов, и перезаписать
файл целиком: временный файл, `fsync`, `rename`, синхронизация каталога (→ глава
18).

```php
/**
 * Offline compaction: `bin/mini-search compact`, with the service stopped.
 *
 * Every update appends a full copy of a document, so the log keeps the whole
 * history. Compaction keeps only the present:
 *
 *  1. open the corpus, which takes the lock, repairs a torn tail and
 *     replays everything — the state compaction must preserve;
 *  2. read the log once more for the text of each live document's last put,
 *     since the corpus keeps terms, not text;
 *  3. write a checkpoint plus one record per live document and per
 *     tombstone to a temporary file, sync it, rename it over the log, sync
 *     the directory (Log::rewrite()).
 *
 * Replaying the result gives the same corpus as replaying the original; that
 * property is the test.
 */
```

[`src/Corpus/Compaction.php#L10-L26`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Compaction.php#L10-L26)

**Перевод комментариев:**
- `Compaction` (PHPDoc): офлайн-компактизация, `bin/mini-search compact`, при
  остановленной службе. Каждое обновление дописывает полную копию документа,
  поэтому журнал хранит всю историю; компактизация оставляет только настоящее:
  открыть корпус — это берёт блокировку, чинит оборванный хвост и делает replay,
  — состояние, которое она обязана сохранить; ещё раз прочитать журнал за
  текстом последнего `put` каждого живого документа, так как корпус хранит
  термы, а не текст; записать `checkpoint` плюс по одной записи на живой
  документ и на каждое надгробие во временный файл, синхронизировать,
  переименовать поверх журнала, синхронизировать каталог (`Log::rewrite()`).
  Replay результата даёт тот же корпус, что и replay исходного; это свойство и
  есть тест.

```php
public static function run(string $path): array
{
    $corpus = Corpus::open($path);

    try {
        clearstatcache(true, $path);
        $before = (int) filesize($path);

        /** @var array<string, array<string, mixed>> $lastPuts */
        $lastPuts = [];
        LogScanner::scan($path, function (array $record) use (&$lastPuts): void {
            if (($record['op'] ?? null) === 'put' && is_string($record['id'] ?? null)) {
                $lastPuts[$record['id']] = $record;
            }
        });

        Log::rewrite($path, $corpus->compactedRecords($lastPuts));
    } finally {
        $corpus->close();
    }

    clearstatcache(true, $path);

    return ['bytes_before' => $before, 'bytes_after' => (int) filesize($path)];
}
```

[`src/Corpus/Compaction.php#L32-L56`](https://github.com/Researcher86/php-mini-search/blob/7ecddc0278acc5e4ac22098c7d9aa5ebebe86ef3/src/Corpus/Compaction.php#L32-L56)

### Сторона платформы: откуда берутся версии

Счётчик версий растёт в той же записи, что и само изменение, — иначе порядок
заданий нечем было бы проверять. Удаление тоже увеличивает версию: строка
остаётся, меняется только флаг `deleted`.

```php
use PhpSystemsPlatform\Domain\ProductRecord;
use PhpSystemsPlatform\Storage\Database;

/**
 * The catalog's write path, and the reads search needs. Every write bumps
 * the row's version; a delete only sets `deleted`, so the version keeps
 * growing across a delete and a re-create.
 *
 * Read-then-write without a transaction (the platform has none) is safe
 * here for the same reason it is for orders: serve handles one request at a
 * time, so two writes to one sku never interleave.
```

[`src/Storage/Repositories/ProductRepository.php#L7-L17`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Storage/Repositories/ProductRepository.php#L7-L17)

**Перевод комментариев:**
- `ProductRepository` (PHPDoc): путь записи каталога и чтения, нужные поиску.
  Каждая запись увеличивает версию строки; удаление только выставляет `deleted`,
  поэтому версия продолжает расти через удаление и воссоздание. Чтение-запись
  без транзакции (в платформе их нет) безопасно здесь по той же причине, что и
  для заказов: `serve` обслуживает один запрос за раз, поэтому две записи одного
  sku не чередуются.

```php
/**
 * Create the product, or replace it — a deleted one comes back. Returns
 * the row as written, at its new version.
 */
public function save(string $sku, string $title, string $description, string $price): ProductRecord
{
    $existing = $this->find($sku);

    if ($existing === null) {
        $this->database->write(
            'INSERT INTO products (' . self::COLUMNS . ') VALUES (?, ?, ?, ?, 1, FALSE)',
            [$sku, $title, $description, $price],
        );
    } else {
        $this->database->write(
            'UPDATE products SET title = ?, description = ?, price = ?, version = ?, deleted = FALSE WHERE sku = ?',
            [$title, $description, $price, $existing->version + 1, $sku],
        );
    }

    return $this->find($sku) ?? throw new \RuntimeException("Product {$sku} vanished after it was written.");
}
```

[`src/Storage/Repositories/ProductRepository.php#L36-L57`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Storage/Repositories/ProductRepository.php#L36-L57)

**Перевод комментариев:**
- `save()` (PHPDoc): создать товар или заменить его — удалённый возвращается.
  Возвращает строку в том виде, в каком она записана, с новой версией.

```php
/**
 * Soft-delete. Null when the sku was never written; an already deleted
 * product is returned as it is, without a new version.
 */
public function delete(string $sku): ?ProductRecord
{
    $existing = $this->find($sku);

    if ($existing === null || $existing->deleted) {
        return $existing;
    }

    $this->database->write('UPDATE products SET version = ?, deleted = TRUE WHERE sku = ?', [$existing->version + 1, $sku]);

    return $this->find($sku);
}
```

[`src/Storage/Repositories/ProductRepository.php#L59-L74`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Storage/Repositories/ProductRepository.php#L59-L74)

**Перевод комментариев:**
- `delete()` (PHPDoc): мягкое удаление. `null`, если sku никогда не записывался;
  уже удалённый товар возвращается как есть, без новой версии.

### Задание читает строку, а не значения

Это ключевое решение всей схемы: задание несёт **только sku**. Что бы ни
случилось с порядком заданий, выполнившееся последним отправляет текущую строку
— потому что читает её в момент выполнения. Версия строки — единственное, что
нужно, чтобы сервис это проверил.

```php
/**
 * "A product changed": tell php-mini-search what the product is now.
 *
 * The job carries only the sku and reads the row when it runs, never the
 * values at dispatch time. With jobs for one product in flight out of order,
 * whichever runs last still sends the current row, and the row's version lets
 * the search service ignore anything older than what it holds — including a
 * stale write that would otherwise bring a deleted product back.
 *
 * A soft-deleted row becomes a DELETE at its version. A search service that
 * is down fails the attempt (SearchUnavailable), and the queue retries it.
 * No idempotency guard is consulted: repeating the job is already harmless,
 * because the search service answers a repeated version with
 * `applied: false`.
 */
```

[`src/Queue/Jobs/ProductIndexJob.php#L12-L26`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/Jobs/ProductIndexJob.php#L12-L26)

**Перевод комментариев:**
- `ProductIndexJob` (PHPDoc): «товар изменился» — сообщить `php-mini-search`,
  каков товар теперь. Задание несёт только sku и читает строку при выполнении,
  никогда не значения на момент отправки. Когда задания одного товара идут
  вразнобой, последнее выполнившееся всё равно отправляет текущую строку, а
  версия строки позволяет службе поиска игнорировать всё старше хранимого —
  включая устаревшую запись, которая иначе вернула бы удалённый товар. Мягко
  удалённая строка становится `DELETE` со своей версией. Недоступная служба
  поиска проваливает попытку (`SearchUnavailable`), и очередь повторяет её.
  Проверка идемпотентности не нужна: повтор задания уже безвреден, потому что
  служба отвечает на повторную версию `applied: false`.

### Полная переиндексация

`product.reindex` — та самая операция, которой нельзя заменить инкрементальные
задания (и наоборот). Она открывает поколение, шлёт все живые строки и завершает
его.

```php
use PhpSystemsPlatform\Search\SearchClient;
use PhpSystemsPlatform\Storage\Repositories\ProductRepository;
use RuntimeException;

/**
 * A full reindex: open a generation, send every live product with it, then
 * complete it. php-mini-search then drops whatever the generation did not
 * touch — the products whose delete never reached it — and leaves
 * `reindex_required` if it was in it.
 *
```

[`src/Queue/Jobs/ProductReindexJob.php#L9-L18`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/Jobs/ProductReindexJob.php#L9-L18)

**Перевод комментариев:**
- `ProductReindexJob` (PHPDoc): полная переиндексация: открыть поколение,
  отправить каждый живой товар с ним, затем завершить его. После этого
  `php-mini-search` удаляет всё, чего поколение не коснулось, — товары, чьё
  удаление до него не дошло, — и оставляет `reindex_required`, если служба была
  в этом состоянии.

```php
/**
 * @return array{generation: int, sent: int, removed: int}
 */
public static function run(ProductRepository $products, SearchClient $search): array
{
    $generation = $search->beginReindex();
    $sent = 0;

    foreach ($products->allLive() as $product) {
        $search->put($product->sku, $product->version, $product->title, $product->description, $generation);
        $sent++;
    }

    return ['generation' => $generation, 'sent' => $sent, 'removed' => $search->completeReindex($generation)];
}
```

[`src/Queue/Jobs/ProductReindexJob.php#L37-L51`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Queue/Jobs/ProductReindexJob.php#L37-L51)

**Перевод комментариев:**
- `run()` (PHPDoc): возвращает номер поколения, число отправленных товаров и
  число удалённых документов.

### Стык по протоколу

`SearchClient` — единственное место, где платформа говорит с поиском, и говорит
по HTTP. Ни один класс `php-mini-search` не импортируется: сервис остаётся
отдельным процессом за протоколом. Здесь же видно, что версия и поколение едут в
теле запроса.

```php
/**
 * The platform's side of php-mini-search's HTTP contract (its docs/API.md),
 * and the only place the platform speaks to it. Plain curl and JSON: the
 * platform never imports a php-mini-search class, so the service stays a
 * separate process behind a protocol, like any other service would be.
 *
 * Timeouts are the contract's: 1 s to connect, 5 s for an answer. Nothing
 * is retried here — a write that fails throws SearchUnavailable, the job that
 * sent it fails its attempt, and the queue retries it. That is safe because
 * every write carries the row's source version: a duplicate answers
 * `applied: false` and changes nothing.
 */
```

[`src/Search/SearchClient.php#L10-L21`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Search/SearchClient.php#L10-L21)

**Перевод комментариев:**
- `SearchClient` (PHPDoc): сторона платформы в HTTP-контракте `php-mini-search`
  (его `docs/API.md`) и единственное место, где платформа с ним говорит. Обычные
  `curl` и JSON: платформа никогда не импортирует класс `php-mini-search`,
  поэтому служба остаётся отдельным процессом за протоколом, как и любая другая
  служба. Таймауты из контракта: 1 с на соединение, 5 с на ответ. Здесь ничего
  не повторяется: упавшая запись бросает `SearchUnavailable`, задание,
  отправившее её, проваливает попытку, и очередь повторяет его. Это безопасно,
  потому что каждая запись несёт версию источника строки: дубликат отвечает
  `applied: false` и ничего не меняет.

```php
/**
 * Index a product at its source version. False when the service already
 * holds that version or a newer one (or a newer delete).
 */
public function put(string $sku, int $version, string $title, string $body, ?int $generation = null): bool
{
    $document = ['source_version' => $version, 'title' => $title, 'body' => $body];
    if ($generation !== null) {
        $document['generation'] = $generation;
    }

    return $this->expectOk('PUT', '/documents/' . rawurlencode($sku), $document)['applied'] === true;
}

public function delete(string $sku, int $version): bool
{
    return $this->expectOk('DELETE', '/documents/' . rawurlencode($sku) . '?source_version=' . $version)['applied'] === true;
}
```

[`src/Search/SearchClient.php#L45-L62`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Search/SearchClient.php#L45-L62)

**Перевод комментариев:**
- `put()` (PHPDoc): проиндексировать товар с версией источника. `false`, если
  служба уже хранит эту версию или более новую (или более новое удаление).

```php
public function beginReindex(): int
{
    return (int) $this->expectOk('POST', '/reindex')['generation'];
}

/**
 * @return int how many documents the generation did not touch, and were removed
 */
public function completeReindex(int $generation): int
{
    return (int) $this->expectOk('POST', "/reindex/{$generation}/complete")['removed'];
}
```

[`src/Search/SearchClient.php#L121-L132`](https://github.com/Researcher86/php-systems-platform/blob/9a6a9161cd55ca41b1b27dcab6546e6dab268696/src/Search/SearchClient.php#L121-L132)

**Перевод комментариев:**
- `completeReindex()` (PHPDoc): сколько документов поколение не тронуло и они
  были удалены.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-search
make build install
```

Все четыре эксперимента выполняются в контейнере `php-mini-search`, проект
смонтирован в `/app`. Замеров времени нет, поэтому все выводы детерминированы:
каждый скрипт прогонялся дважды и совпал байт-в-байт. Платформа в экспериментах
не участвует: её роль — выставить версию и послать задание, а все решения
принимает корпус поиска. Код платформы из §3 показывает, откуда версия берётся;
здесь проверяется, что она делает.

### 1. Версии и надгробия

Восемь записей для одного товара. Смотрите не на «применилось», а на пары, в
которых версия повторяется: повтор — это не ошибка и не второе применение, это
`applied: false`, и именно оно позволяет очереди повторять задание сколько
угодно.

```bash
docker compose cp /tmp/ch21-versions.php php:/tmp/ch21-versions.php
docker compose exec php php /tmp/ch21-versions.php
```

```php
<?php

declare(strict_types=1);

/**
 * Source versions and tombstones: the two rules that make an out-of-order
 * delivery harmless.
 *
 * `PUT` and `DELETE` both carry the row's version, and the corpus applies a
 * write only if it is *newer* than what it already holds. The interesting
 * part is the equality: a redelivery of the same version is not an error and
 * not an update — it is `applied: false`, which is what lets the platform's
 * queue retry a job as many times as it likes.
 *
 * The last group is the tombstone. A delete leaves its version behind
 * forever, so the late `PUT` that would otherwise resurrect the product is
 * simply older than the delete, and is ignored. Re-creating the product then
 * needs a version *above* the delete, not above the old document.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch21-versions.php php:/tmp/ch21-versions.php
 *   docker compose exec php php /tmp/ch21-versions.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Corpus\Corpus;
use PhpMiniSearch\Corpus\WriteResult;

$directory = sys_get_temp_dir() . '/ch21-versions-' . getmypid() . '/';
mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    foreach (scandir($directory) ?: [] as $entry) {
        if ($entry !== '.' && $entry !== '..') {
            unlink($directory . $entry);
        }
    }

    rmdir($directory);
});

$corpus = Corpus::open($directory . 'corpus.log');

$show = static function (string $label, WriteResult $result): void {
    printf(
        "%-36s applied=%-3s version=%d generation=%s\n",
        $label,
        $result->applied ? 'yes' : 'no',
        $result->sourceVersion,
        $result->generation === null ? '-' : (string) $result->generation,
    );
};

printf("A product and its edits\n");
$show('PUT  p1 v1   (create)', $corpus->put('p1', 1, 'Кабель USB-C', 'медный'));
$show('PUT  p1 v1   (retried job)', $corpus->put('p1', 1, 'Кабель USB-C', 'медный'));
$show('PUT  p1 v2   (edit)', $corpus->put('p1', 2, 'Кабель USB-C', 'медный, 2 м'));
$show('PUT  p1 v1   (late job, older)', $corpus->put('p1', 1, 'старое', 'старое'));

printf("\nA delete, and a late PUT that would resurrect it\n");
$show('DELETE p1 v3', $corpus->delete('p1', 3));
$show('PUT  p1 v2   (older than delete)', $corpus->put('p1', 2, 'старое', 'старое'));
$show('PUT  p1 v3   (equal to delete)', $corpus->put('p1', 3, 'старое', 'старое'));
$show('PUT  p1 v4   (newer than delete)', $corpus->put('p1', 4, 'Кабель USB-C', 'медный, 2 м'));

printf("\nA second product, deleted and never re-created\n");
$show('PUT  p2 v1   (create)', $corpus->put('p2', 1, 'Розетка', 'белая'));
$show('DELETE p2 v2', $corpus->delete('p2', 2));

printf("\nState of the corpus\n");

foreach (['p1', 'p2'] as $id) {
    $described = $corpus->describe($id);
    $generation = $described['generation'] ?? null;

    printf(
        "describe(%s): state=%s source_version=%d generation=%s\n",
        $id,
        $described['state'] ?? 'absent',
        $described['source_version'] ?? 0,
        $generation === null ? '-' : (string) $generation,
    );
}

$stats = $corpus->stats();
printf("documents=%d tombstones=%d terms=%d\n", $stats['documents'], $stats['tombstones'], $stats['terms']);

$corpus->close();
```

**Перевод комментариев:**
- `Source versions and tombstones: the two rules that make an out-of-order`
  (PHPDoc): версии источника и надгробия — два правила, которые делают доставку
  не по порядку безвредной. `PUT` и `DELETE` несут версию строки, и корпус
  применяет запись, только если она **новее** хранимой. Интересна именно
  нестрогость: повторная доставка той же версии — не ошибка и не обновление, а
  `applied: false`, и это позволяет очереди платформы повторять задание сколько
  угодно раз. Последняя группа — надгробие. Удаление навсегда оставляет после
  себя версию, поэтому опоздавший `PUT`, который иначе воскресил бы товар,
  просто старше удаления и игнорируется. Воссоздание товара после этого требует
  версии **выше** удаления, а не выше старого документа.

```text
A product and its edits
PUT  p1 v1   (create)                applied=yes version=1 generation=-
PUT  p1 v1   (retried job)           applied=no  version=1 generation=-
PUT  p1 v2   (edit)                  applied=yes version=2 generation=-
PUT  p1 v1   (late job, older)       applied=no  version=2 generation=-

A delete, and a late PUT that would resurrect it
DELETE p1 v3                         applied=yes version=3 generation=-
PUT  p1 v2   (older than delete)     applied=no  version=3 generation=-
PUT  p1 v3   (equal to delete)       applied=no  version=3 generation=-
PUT  p1 v4   (newer than delete)     applied=yes version=4 generation=-

A second product, deleted and never re-created
PUT  p2 v1   (create)                applied=yes version=1 generation=-
DELETE p2 v2                         applied=yes version=2 generation=-

State of the corpus
describe(p1): state=live source_version=4 generation=-
describe(p2): state=deleted source_version=2 generation=-
documents=1 tombstones=1 terms=6
```

Три вещи в выводе заслуживают внимания.

Первая: повтор версии не ошибка. `PUT p1 v1` второй раз — `applied=no`, но не
`409` и не исключение. Сервис отвечает «эта запись не новее той, что у меня уже
есть», и задание прекращает попытки. Версия — контракт между источником и
копией, а не форма блокировки.

Вторая: `version=2` в первых строках — это версия, которую держит **сервис**, а
не отправитель. Отвечая на устаревшую запись, сервис сообщает свою версию, и
вызывающий может сверить её со своей строкой. Это делает `applied: false` не
просто «нет», а «нет, у меня версия такая-то».

Третья, и самая важная: воскрешения нет. Между `DELETE p1 v3` и `PUT p1 v2`
стоит одно выражение `isStale()`, где надгробие участвует наравне с документом.
Пустая ячейка `generation` — это `null`: у обычного задания своего поколения
нет. Наконец, `documents=1 tombstones=1` показывает цену: живой товар один, а
надгробие осталось и останется.

### 2. Поколения: переиндексация и две её ловушки

Переиндексация — единственный способ заметить то, что не пришло: товар `p5`
удалён из базы, но его задание не дошло, и он навсегда остался бы в индексе.
Полная переиндексация проходит по всем живым товарам и удаляет всё, чего не
тронула.

```bash
docker compose cp /tmp/ch21-reindex.php php:/tmp/ch21-reindex.php
docker compose exec php php /tmp/ch21-reindex.php
```

```php
<?php

declare(strict_types=1);

/**
 * A full reindex, generation by generation — and the trap it would have
 * without the "applied writes belong to the open generation" rule.
 *
 * First the reason reindexing exists at all: before a generation has ever
 * completed, the corpus refuses to search (`first_start`). Replaying the
 * platform's whole catalog is what clears that state.
 *
 * Then the two rules that keep the reindex honest while ordinary jobs keep
 * running:
 *
 *   - a product created after the reindex has already passed its key is
 *     still marked with the open generation, so `complete` does not remove
 *     it (p4 below);
 *   - a stale write that *names* the open generation marks the document it
 *     confirms, so a product that is already up to date is not removed
 *     either (the last group).
 *
 * And the rule that makes it useful: completing a generation removes every
 * live document the generation did not touch — the products whose delete
 * never reached the service (p5 below).
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch21-reindex.php php:/tmp/ch21-reindex.php
 *   docker compose exec php php /tmp/ch21-reindex.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Corpus\Corpus;
use PhpMiniSearch\Corpus\ReindexRequired;

$directory = sys_get_temp_dir() . '/ch21-reindex-' . getmypid() . '/';
mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    foreach (scandir($directory) ?: [] as $entry) {
        if ($entry !== '.' && $entry !== '..') {
            unlink($directory . $entry);
        }
    }

    rmdir($directory);
});

$corpus = Corpus::open($directory . 'corpus.log');

$search = static function (Corpus $corpus, string $query): string {
    try {
        return sprintf('%d hit(s)', $corpus->search($query, 10)->total);
    } catch (ReindexRequired $required) {
        return 'refused: ' . $required->reason;
    }
};

$live = static function (Corpus $corpus): string {
    $ids = [];

    foreach (['p1', 'p2', 'p3', 'p4', 'p5', 'p6'] as $id) {
        $described = $corpus->describe($id);

        if (($described['state'] ?? null) === 'live') {
            $ids[] = sprintf('%s(v%d/g%s)', $id, $described['source_version'], $described['generation'] ?? '-');
        }
    }

    return $ids === [] ? '(none)' : implode(' ', $ids);
};

printf("Before any reindex has completed\n");
printf("  search: %s\n", $search($corpus, 'кабель'));

// An old document whose delete never reached the service: only the reindex
// can notice that it is gone from the source.
$corpus->put('p5', 1, 'Устаревший товар', 'которого уже нет в базе');
printf("  an ordinary job indexed p5: live=%s\n", $live($corpus));

$opened = $corpus->beginReindex();
printf("  beginReindex: generation=%d cancelled=%s\n", $opened['generation'], $opened['cancelled_generation'] === null ? '-' : (string) $opened['cancelled_generation']);

$generation = $opened['generation'];
$corpus->put('p1', 2, 'Кабель USB-C', 'медный', $generation);
$corpus->put('p2', 1, 'Розетка', 'белая', $generation);
$corpus->put('p3', 1, 'Переходник', 'USB-C на USB-A', $generation);
$corpus->put('p4', 1, 'Хаб', 'на четыре порта', $generation);

printf("\nDuring generation %d: four products sent, p5 left out on purpose\n", $generation);
printf("  live: %s\n", $live($corpus));

$removed = $corpus->completeReindex($generation);
printf("\ncomplete(generation %d): removed %d\n", $generation, $removed);
printf("  live: %s\n", $live($corpus));
printf("  search: %s\n", $search($corpus, 'кабель'));

printf("\nThe trap: a product created while a reindex is open\n");
$second = $corpus->beginReindex();
printf("  beginReindex: generation=%d cancelled=%s\n", $second['generation'], $second['cancelled_generation'] === null ? '-' : (string) $second['cancelled_generation']);

// An ordinary indexing job, with no generation of its own, for a product the
// reindex has already passed. It is still marked with the open generation.
$corpus->put('p6', 1, 'Новый товар', 'создан во время переиндексации');
printf("  live before complete: %s\n", $live($corpus));
printf("  complete(generation %d): removed %d\n", $second['generation'], $corpus->completeReindex($second['generation']));
printf("  live after complete: %s\n", $live($corpus));

printf("\nThe other trap: a stale write that names the open generation\n");
$third = $corpus->beginReindex();
$generation = $third['generation'];

// p6 is up to date already, so this PUT is stale — but it names the open
// generation, and that is what marks p6 as confirmed by it.
$stale = $corpus->put('p6', 1, 'Новый товар', 'создан во время переиндексации', generation: $generation);
printf("  PUT p6 v1 with generation %d: applied=%s\n", $generation, $stale->applied ? 'yes' : 'no');
printf("  live before complete: %s\n", $live($corpus));
printf("  complete(generation %d): removed %d\n", $generation, $corpus->completeReindex($generation));
printf("  live after complete: %s\n", $live($corpus));

$stats = $corpus->stats();
printf(
    "\nstats: documents=%d tombstones=%d last_completed_generation=%s\n",
    $stats['documents'],
    $stats['tombstones'],
    $stats['last_completed_generation'] === null ? '-' : (string) $stats['last_completed_generation'],
);

$corpus->close();
```

**Перевод комментариев:**
- `A full reindex, generation by generation — and the trap it would have`
  (PHPDoc): полная переиндексация, поколение за поколением, и ловушка, которая
  возникла бы без правила «применённые записи принадлежат открытому поколению».
  Сначала — зачем переиндексация вообще нужна: пока ни одно поколение не
  завершено, корпус отказывает в поиске (`first_start`). Именно переигрывание
  всего каталога платформы и снимает это состояние. Затем два правила, которые
  держат переиндексацию честной, пока обычные задания продолжают работать:
  товар, созданный после того, как переиндексация уже прошла его ключ, всё равно
  помечается открытым поколением, поэтому `complete` его не удаляет (`p4` ниже);
  устаревшая запись, которая **называет** открытое поколение, помечает
  подтверждаемый документ, поэтому уже актуальный товар тоже не удаляется
  (последняя группа). И правило, которое делает всё полезным: завершение
  поколения удаляет каждый живой документ, которого поколение не коснулось, —
  товары, чьё удаление так и не дошло до службы (`p5` ниже).
- `// An old document whose delete never reached the service: only the reindex`
  — старый документ, чьё удаление так и не дошло до службы: заметить, что его
  больше нет в источнике, может только переиндексация.
- `// An ordinary indexing job, with no generation of its own, for a product the`
  — обычное задание индексирования без собственного поколения для товара,
  который переиндексация уже прошла. Оно всё равно помечается открытым
  поколением.
- `// p6 is up to date already, so this PUT is stale — but it names the open` —
  `p6` уже актуален, поэтому этот `PUT` устарел, но он называет открытое
  поколение, и именно это помечает `p6` как подтверждённый им.

```text
Before any reindex has completed
  search: refused: first_start
  an ordinary job indexed p5: live=p5(v1/g-)
  beginReindex: generation=1 cancelled=-

During generation 1: four products sent, p5 left out on purpose
  live: p1(v2/g1) p2(v1/g1) p3(v1/g1) p4(v1/g1) p5(v1/g-)

complete(generation 1): removed 1
  live: p1(v2/g1) p2(v1/g1) p3(v1/g1) p4(v1/g1)
  search: 1 hit(s)

The trap: a product created while a reindex is open
  beginReindex: generation=2 cancelled=-
  live before complete: p1(v2/g1) p2(v1/g1) p3(v1/g1) p4(v1/g1) p6(v1/g2)
  complete(generation 2): removed 4
  live after complete: p6(v1/g2)

The other trap: a stale write that names the open generation
  PUT p6 v1 with generation 3: applied=no
  live before complete: p6(v1/g3)
  complete(generation 3): removed 0
  live after complete: p6(v1/g3)

stats: documents=1 tombstones=5 last_completed_generation=3
```

Первое, что видно: до первой завершённой переиндексации поиск **отказывает** —
`refused: first_start`, а не пустой список. Корпус не притворяется работающим;
`search()` бросает `ReindexRequired`, а HTTP-слой превращает это в `503`.

Именно поэтому `p5` вообще существует как проблема: обычное задание
проиндексировало его, и он живой (`live=p5(v1/g-)`, поколения нет). После
`beginReindex: generation=1` четыре товара уходят с меткой `g1`, а `p5` остаётся
без неё. `complete(generation 1): removed 1` — товар `p5` удалён, и вместе с
документом появилось надгробие.

Дальше — две ловушки, обе про то, что переиндексация идёт **на месте**, пока
обычные задания продолжают работать.

Ловушка первая: товар, созданный во время переиндексации. `p6` создан уже после
того, как переиндексация прошла его ключ, и у него нет метки `g2` — но он всё
равно выжил (`live after complete: p6(v1/g2)`) и получил метку `g2`. Правило:
пока поколение открыто, каждая **применённая** запись помечается этим
поколением, даже если у задания своего номера нет. Без него `complete` удалил бы
только что созданный товар.

Ловушка вторая, тоньше: устаревшая запись, **называющая** открытое поколение.
`PUT p6 v1 with generation 3` не применился (`applied=no` — `v1` не новее `v1`),
но `p6` получил метку `g3`, и `complete(generation 3): removed 0` его не тронул.
Логика такая: переиндексация дошла до этого товара и подтвердила его — сама
запись является подтверждением, даже когда содержимое менять не нужно. Иначе
полная переиндексация удаляла бы каждый товар, который уже актуален.

Финальная строка `tombstones=5` — это не утечка, а замысел: четыре товара,
снятые завершением поколения 2, остаются надгробиями, чтобы опоздавшие PUT не
воскресили их после переиндексации.

### 3. Перезапуск, повреждённый журнал и пустой журнал

Здесь проверяются два утверждения о состоянии. Перезапуск здорового журнала
обязан вернуть **тот же** корпус: replay и живые записи идут одним `apply()` (→
глава 18), поэтому документы, надгробия и счётчики поколений совпадают по
построению. А журнал, повреждённый в середине, обязан не «обрезаться до чего-то
похожего на рабочий индекс», а отправить файл в архив и честно просить
переиндексацию.

```bash
docker compose cp /tmp/ch21-restart.php php:/tmp/ch21-restart.php
docker compose exec php php /tmp/ch21-restart.php
```

```php
<?php

declare(strict_types=1);

/**
 * What a restart preserves, and what a lost log means.
 *
 * Two claims are checked. A restart of a healthy log must give back the same
 * corpus: same documents, same tombstones, same generations — because replay
 * and live writes run the same `apply()` code, record by record. And a log
 * damaged in the middle must not be quietly truncated into something that
 * looks like a working index: the file is moved aside and the corpus reports
 * `log_corrupted`, which is a 503 until a reindex completes.
 *
 * The last part shows the third state: a corpus with no log at all, which is
 * `first_start` — a different reason for the same 503, because "empty because
 * the source is empty" and "empty because nobody filled it" are not the same
 * thing.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch21-restart.php php:/tmp/ch21-restart.php
 *   docker compose exec php php /tmp/ch21-restart.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Corpus\Corpus;
use PhpMiniSearch\Corpus\ReindexRequired;

$directory = sys_get_temp_dir() . '/ch21-restart-' . getmypid() . '/';
mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    $remove = static function (string $path) use (&$remove): void {
        if (!file_exists($path)) {
            return;
        }

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

$path = $directory . 'corpus.log';
$corpus = Corpus::open($path);

$generation = $corpus->beginReindex()['generation'];
$corpus->put('p1', 1, 'Кабель USB-C', 'медный', $generation);
$corpus->put('p2', 1, 'Розетка', 'белая', $generation);
$corpus->completeReindex($generation);
$corpus->delete('p2', 2);

$before = $corpus->state();
$stats = $corpus->stats();

printf(
    "before restart: documents=%d tombstones=%d last_completed_generation=%s log=%d bytes\n",
    $stats['documents'],
    $stats['tombstones'],
    $stats['last_completed_generation'] === null ? '-' : (string) $stats['last_completed_generation'],
    $stats['log']['bytes'],
);

$corpus->close();

$reopened = Corpus::open($path);
$after = $reopened->state();

printf("after restart:  recovery=%s\n", $reopened->recovery()->status);
printf("state identical to the state before the restart: %s\n", $after == $before ? 'yes' : 'no');
printf("reindex required: %s\n", $reopened->reindexRequired() ?? 'no');
printf("search after restart: %d hit(s)\n", $reopened->search('кабель', 10)->total);
printf("p2 is still %s\n", $reopened->describe('p2')['state'] ?? 'absent');

$reopened->close();

// Damage a record that is not the last one: a byte flipped inside the first
// record's body.
$bytes = (string) file_get_contents($path);
$position = strpos($bytes, '"p1"');
$bytes[$position] = 'X';
file_put_contents($path, $bytes);

$damaged = Corpus::open($path);

printf("\nafter a damaged log:\n");
printf("  recovery: %s\n", $damaged->recovery()->status);
printf("  reindex required: %s\n", $damaged->reindexRequired() ?? 'no');
printf("  documents=%d tombstones=%d\n", $damaged->stats()['documents'], $damaged->stats()['tombstones']);
printf("  damaged copy kept aside: %s\n", glob($path . '.damaged-*') === [] ? 'no' : 'yes');

try {
    $damaged->search('кабель', 10);
    printf("  search: %d hit(s)\n", 0);
} catch (ReindexRequired $required) {
    printf("  search: refused (%s)\n", $required->reason);
}

$damaged->close();

$fresh = Corpus::open($directory . 'never-used.log');
printf("\na log that does not exist yet:\n");
printf("  recovery: %s\n", $fresh->recovery()->status);
printf("  reindex required: %s\n", $fresh->reindexRequired() ?? 'no');

try {
    $fresh->search('кабель', 10);
} catch (ReindexRequired $required) {
    printf("  search: refused (%s)\n", $required->reason);
}

$fresh->close();
```

**Перевод комментариев:**
- `What a restart preserves, and what a lost log means` (PHPDoc): что сохраняет
  перезапуск и что значит потерянный журнал. Проверяются два утверждения.
  Перезапуск здорового журнала обязан вернуть тот же корпус: те же документы, те
  же надгробия, те же поколения — потому что replay и живые записи выполняют
  один и тот же код `apply()`, запись за записью. А журнал, повреждённый в
  середине, не должен тихо обрезаться до чего-то похожего на рабочий индекс:
  файл откладывается в сторону, а корпус сообщает `log_corrupted`, то есть
  `503`, пока не завершится переиндексация. Последняя часть показывает третье
  состояние: корпус вообще без журнала — это `first_start`, другая причина того
  же `503`, потому что «пусто, потому что источник пуст» и «пусто, потому что
  никто не наполнял» — не одно и то же.
- `// Damage a record that is not the last one: a byte flipped inside the first`
  — повредить запись, которая не последняя: перевёрнут байт внутри тела первой
  записи.

```text
before restart: documents=1 tombstones=1 last_completed_generation=1 log=378 bytes
after restart:  recovery=clean
state identical to the state before the restart: yes
reindex required: no
search after restart: 1 hit(s)
p2 is still deleted

after a damaged log:
  recovery: damaged
  reindex required: log_corrupted
  documents=0 tombstones=0
  damaged copy kept aside: yes
  search: refused (log_corrupted)

a log that does not exist yet:
  recovery: missing
  reindex required: first_start
  search: refused (first_start)
```

Первая половина: `recovery=clean`, `state identical … yes`,
`reindex required: no`, поиск сразу даёт тот же ответ, а `p2` остаётся
`deleted`. Всё это — про то, что надгробие и счётчик
`last_completed_generation=1` легли в журнал и вернулись из него.

Вторая половина — порча в **середине**: байт внутри первой записи (`"p1"`)
изменён, последняя запись цела. `recovery: damaged`, а не `torn`: обрыв хвоста —
это нормальный конец (его срезают), а порча середины — потеря. Поэтому
документов после открытия `0`, надгробий `0`, файл отложен в сторону
(`damaged copy kept aside: yes`), а поиск отвечает `refused (log_corrupted)`.
Заметьте: это не «вернуть то, что успело прочитаться». Неполный индекс хуже
отсутствующего, потому что выглядит рабочим.

Третья часть: журнала нет вовсе — `recovery: missing`, `refused (first_start)`.
Тот же HTTP-код `503`, но причина другая, и оператор должен видеть разницу между
«источник пуст» и «никто не наполнял».

### 4. Компактизация: надгробие переживает перезапись

Пять версий одного товара — это пять полных копий в журнале. Логично выбросить
историю. Логично также «выбросить надгробия: товара же нет». Оба желания здесь и
проверяются: компактизация оставляет только настоящее, но настоящее включает
надгробия и счётчики поколений.

```bash
docker compose cp /tmp/ch21-compaction.php php:/tmp/ch21-compaction.php
docker compose exec php php /tmp/ch21-compaction.php
```

```php
<?php

declare(strict_types=1);

/**
 * Compaction: keep the present, and keep the tombstones with it.
 *
 * Every update appends a full copy of the document, so the log is a history,
 * not a state. Compaction rewrites it as a checkpoint plus one record per
 * live document and per tombstone. The property that matters is not the
 * smaller file: it is that replaying the result gives the same corpus —
 * documents, tombstones, generations — or a deleted product would come back
 * from the dead on the next restart.
 *
 * The last line feeds the compacted log a stale PUT for a deleted product.
 * The tombstone survived the rewrite, so the write is still refused.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/ch21-compaction.php php:/tmp/ch21-compaction.php
 *   docker compose exec php php /tmp/ch21-compaction.php
 */

require '/app/vendor/autoload.php';

use PhpMiniSearch\Corpus\Compaction;
use PhpMiniSearch\Corpus\Corpus;

$directory = sys_get_temp_dir() . '/ch21-compaction-' . getmypid() . '/';
mkdir($directory, 0o775, true);

register_shutdown_function(static function () use ($directory): void {
    foreach (scandir($directory) ?: [] as $entry) {
        if ($entry !== '.' && $entry !== '..') {
            unlink($directory . $entry);
        }
    }

    rmdir($directory);
});

$path = $directory . 'corpus.log';
$corpus = Corpus::open($path);

// Five versions of one product: five full copies of it in the log.
for ($version = 1; $version <= 5; $version++) {
    $corpus->put('p1', $version, 'Кабель USB-C', "медный, версия {$version}");
}

$corpus->put('p2', 1, 'Розетка', 'белая');
$corpus->delete('p2', 2);
$corpus->put('p3', 1, 'Переходник', 'USB-C');

$generation = $corpus->beginReindex()['generation'];
$corpus->put('p1', 6, 'Кабель USB-C', 'медный, версия 6', $generation);
$corpus->put('p3', 2, 'Переходник', 'USB-C на USB-A', $generation);
$corpus->completeReindex($generation);

$before = $corpus->state();
$stats = $corpus->stats();

printf(
    "before compaction: log=%d bytes documents=%d tombstones=%d garbage=%.4f\n",
    $stats['log']['bytes'],
    $stats['documents'],
    $stats['tombstones'],
    $stats['log']['garbage_ratio'],
);

$corpus->close();

$sizes = Compaction::run($path);
printf("compaction wrote %d bytes over %d\n", $sizes['bytes_after'], $sizes['bytes_before']);

$reopened = Corpus::open($path);
$after = $reopened->state();
$stats = $reopened->stats();

printf(
    "after compaction:  log=%d bytes documents=%d tombstones=%d garbage=%.4f\n",
    $stats['log']['bytes'],
    $stats['documents'],
    $stats['tombstones'],
    $stats['log']['garbage_ratio'],
);
printf("state identical across the rewrite: %s\n", $after == $before ? 'yes' : 'no');

$late = $reopened->put('p2', 1, 'старое', 'старое');
printf("a late PUT for the deleted product: applied=%s\n", $late->applied ? 'yes' : 'no');

$reopened->close();
```

**Перевод комментариев:**
- `Compaction: keep the present, and keep the tombstones with it` (PHPDoc):
  компактизация: сохранить настоящее и вместе с ним надгробия. Каждое обновление
  дописывает полную копию документа, поэтому журнал — это история, а не
  состояние. Компактизация перезаписывает его как `checkpoint` плюс по одной
  записи на живой документ и на каждое надгробие. Важно не то, что файл стал
  меньше, а то, что replay результата даёт тот же корпус — документы, надгробия,
  поколения, — иначе удалённый товар воскрес бы при следующем перезапуске.
  Последняя строка подаёт компактному журналу устаревший `PUT` для удалённого
  товара. Надгробие пережило перезапись, поэтому запись по-прежнему отвергается.
- `// Five versions of one product: five full copies of it in the log.` — пять
  версий одного товара: пять полных его копий в журнале.

```text
before compaction: log=1301 bytes documents=2 tombstones=1 garbage=0.7594
compaction wrote 413 bytes over 1301
after compaction:  log=413 bytes documents=2 tombstones=1 garbage=0.2421
state identical across the rewrite: yes
a late PUT for the deleted product: applied=no
```

`log=1301 bytes → 413`: пять копий товара схлопнулись в одну. Но важна не
экономия, а строка `state identical across the rewrite: yes` — состояние до и
после перезаписи совпало целиком, включая `documents=2` и `tombstones=1`.

И последняя строка — та, ради которой надгробия оставлены:
`a late PUT for the deleted product: applied=no`. Тот самый PUT, который
воскресил бы товар, если бы компактизация почистила надгробия, отвергнут и после
перезаписи журнала.

Обратите внимание на `garbage=0.7594 → 0.2421`, но не как на «мусор почти
убрали». Мусор (доля байт, недостижимых из текущего состояния) никогда не станет
нулём: `checkpoint`, надгробия и служебные поля сами занимают место. Смысл числа
— в порядке величины, а не в идеальном нуле.

## 5. Отказы

| Что ломается                                    | Чем обнаруживается                                    | Что происходит                                                                                         |
| ----------------------------------------------- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| Задание приходит не в том порядке               | Версия записи старее хранимой                         | `applied: false`; сервис отвечает своей версией; задание не повторяется                                |
| Очередь доставила задание дважды                | Та же версия второй раз                               | `applied: false` — повтор безвреден, отдельного ключа идемпотентности не нужно                         |
| Опоздавший PUT после удаления                   | Надгробие с версией удаления                          | `applied: false`; товар не воскресает                                                                  |
| Удаление без надгробия                          | Опоздавший PUT с версией выше документа               | документ появляется заново; поэтому надгробия не чистятся                                              |
| Удаление пришло раньше создания                 | `delete` со версией выше будущего `put`               | надгробие старше — `put` с меньшей версией отвергается (`testADeleteThatArrivesBeforeItsPutStillWins`) |
| Задание потерялось (DLQ, сбой)                  | Сверки нет — источник не сравнивается с индексом      | товар остаётся в индексе навсегда; лечится только полной переиндексацией                               |
| Сбой между записью строки и публикацией задания | — (двух разных хранилищ)                              | индекс отстаёт на одно изменение; задокументированный предел без outbox                                |
| Переиндексация прошла ключ, товар создан        | У созданного товара нет метки поколения               | запись применяется и **помечается** открытым поколением; `complete` его не тронет                      |
| Устаревшая запись во время переиндексации       | Запись не применяется, но называет открытое поколение | документ подтверждается поколением — иначе `complete` удалит актуальный товар                          |
| Вторая переиндексация поверх открытой           | `beginReindex()` вернул `cancelled_generation`        | открытая отменяется; её `complete` вернёт `GenerationNotOpen`                                          |
| Перезапуск во время переиндексации              | Поколение восстановлено из журнала                    | переиндексация остаётся **незавершённой**; `complete` для неё ещё не пришёл                            |
| Журнал повреждён в середине                     | CRC записи не сходится, запись не последняя           | файл отложен в сторону, `reindex_required` = `log_corrupted`, `503`                                    |
| Журнала нет                                     | Файл отсутствует                                      | `reindex_required` = `first_start`, `503`                                                              |
| Пустая переиндексация                           | Ноль живых товаров в источнике                        | `complete` завершает поколение; сервис становится готовым с пустым корпусом                            |
| Смена анализатора                               | Меняется каждый терм                                  | полная переиндексация из журнала (там исходный текст, а не термы); миграции нет                        |
| Два процесса на одном журнале                   | Блокировка файла                                      | второй `open()` не получает блокировку (`testTheLogCanOnlyBeOpenedByOneProcessAtATime`)                |

## 6. Как это проверено

Правила главы проверяются двумя наборами тестов — по одному на каждую сторону
протокола.

`tests/Corpus/CorpusTest.php` — 19 методов, 288 строк, и каждый метод назван
утверждением главы:

- `testWritesAreSearchableAndCarryTheirSourceVersion`;
- `testAnOlderOrEqualVersionIsStaleAndChangesNothing` — и «старее», и «равно»;
- `testALateUpdateCannotResurrectADeletedDocument` — эксперимент 1;
- `testADeleteThatArrivesBeforeItsPutStillWins`;
- `testANewerPutRecreatesADeletedDocument` — воссоздание требует версии выше
  удаления;
- `testCompletingAReindexRemovesWhatItDidNotTouch` — эксперимент 2;
- `testADocumentCreatedDuringAReindexSurvivesItsCompletion` — ловушка первая;
- `testAStalePutFromTheReindexConfirmsTheDocumentAcrossARestart` — ловушка
  вторая, проверяется ещё и через перезапуск;
- `testANewReindexCancelsTheOpenOne`;
- `testAReindexInProgressSurvivesARestart`;
- `testAFirstStartRequiresAReindexBeforeSearching`;
- `testAReindexOfNothingMakesAnEmptyCorpusReady`;
- `testADamagedLogIsKeptAsideAndTheCorpusAsksForAReindex` — эксперимент 3;
- `testEveryRuleHoldsAfterARestart`;
- `testAWriteTheLogRejectedIsNeverSearchable`;
- `testTheLogCanOnlyBeOpenedByOneProcessAtATime`.

`tests/Corpus/CompactionTest.php` — два метода, и оба про свойство, а не про
размер файла: `testACompactedLogReplaysToTheSameCorpus` (эксперимент 4) и
`testCompactingTwiceChangesNothing` (идемпотентность).

Прогон: `docker compose exec php php vendor/bin/phpunit tests/Corpus` — **26
тестов, 57 утверждений, OK**. Полный набор `php-mini-search` — 180 тестов.

Сторона платформы: `tests/Integration/ProductSearchIntegrationTest.php`
проверяет сквозной путь — `PUT`/`DELETE` товара, задание, ответ поиска. Три
теста подтверждают, что `testSearchShowsTheCatalogsRowNotTheIndexs` (поиск
показывает строку каталога, а не индекс-снимок) и что повторный прогон
переиндексации идемпотентен (`testRunIsIdempotentAfterATruncatedRun`). Прогон:
`docker compose exec php php vendor/bin/phpunit tests/Integration/ProductSearchIntegrationTest.php`
— **3 теста, 23 утверждения, OK**.

## 7. Цифры

| Файл                                                                  | Строк | Что делает                               |
| --------------------------------------------------------------------- | ----- | ---------------------------------------- |
| `php-mini-search/src/Corpus/Corpus.php`                               | 405   | версии, надгробия, поколения, `apply()`  |
| `php-mini-search/src/Corpus/Compaction.php`                           | 57    | компактизация: настоящее + надгробия     |
| `php-mini-search/src/Corpus/WriteResult.php`                          | 21    | `applied`, `sourceVersion`, `generation` |
| `php-mini-search/src/Corpus/ReindexRequired.php`                      | 28    | две причины одного `503`                 |
| `php-systems-platform/src/Storage/Repositories/ProductRepository.php` | 111   | версия в той же записи, что и правка     |
| `php-systems-platform/src/Queue/Jobs/ProductIndexJob.php`             | 59    | задание читает строку, а не значения     |
| `php-systems-platform/src/Queue/Jobs/ProductReindexJob.php`           | 52    | открыть, послать, завершить              |
| `php-systems-platform/src/Search/SearchClient.php`                    | 202   | единственный стык по HTTP                |

| Свойство                                 | Значение                                                  |
| ---------------------------------------- | --------------------------------------------------------- |
| Цена надгробия                           | одна пара `id → версия` на удалённый товар                |
| Отвергнутая альтернатива: теневой индекс | ~600 MB против бюджета 512 MB                             |
| Порог «старое»                           | версия строго **больше** хранимой, иначе `applied: false` |
| Причина отказа до первой переиндексации  | `first_start`                                             |
| Причина отказа при потерянном журнале    | `log_corrupted`                                           |

Живые прогоны (все четыре детерминированы, два прогона каждого совпали
байт-в-байт):

| Что               | Значение                                                                                                          |
| ----------------- | ----------------------------------------------------------------------------------------------------------------- |
| `ch21-versions`   | 22 строки; 11 записей, 6 из них `applied=no`; `documents=1 tombstones=1`                                          |
| `ch21-reindex`    | 24 строки; `removed 1` при первой переиндексации, `removed 4` при второй, `removed 0` при третьей; `tombstones=5` |
| `ch21-restart`    | 18 строк; `state identical … yes`; повреждённый журнал → `log_corrupted`, копия отложена                          |
| `ch21-compaction` | 5 строк; 1301 → 413 байт; `state identical … yes`; опоздавший PUT отвергнут                                       |

## 8. Упражнения

1. **Проследите.** Товар создан (`v1`), изменён (`v2`), удалён (`v3`),
   опоздавшее задание прислало `PUT v2`, затем пришло `PUT v4`. Выпишите для
   каждого шага `applied` и версию, которую вернул сервис. Почему `PUT v3`
   (равный удалению) тоже отвергается?
2. **Докажите гипотезу.** Инженер предлагает: «надгробия можно чистить после
   завершённой переиндексации — источник же только что переигран целиком».
   Придумайте последовательность событий (включая аренду очереди и упавший
   воркер), при которой товар воскреснет, и покажите, на каком шаге стирается
   информация, нужная для отказа.
3. **Найдите правило.** Во время открытого поколения приходит `PUT` с версией,
   **меньшей** хранимой, но с номером этого поколения. Почему документ всё равно
   помечается, хотя содержимое не меняется? Что произошло бы, если бы эта запись
   просто отвечала `applied: false` и забывалась?
4. **Сравните.** Теневой индекс с атомарной подменой против переиндексации на
   месте (→ глава 20). Какую гарантию даёт первый и какую цену платит каждый? На
   каком размере корпуса выбор меняется?
5. **Спроектируйте тест.** Как поймать «товар, созданный, пока переиндексация
   шла», не воспроизводя гонку вручную и не полагаясь на время? Опишите, какие
   швы для этого нужны в `SearchClient`.
6. **Разберитесь в цене.** Надгробия живут вечно. Оцените их место на диске при
   1 млн товаров и 10 % удалений — и предложите способ всё-таки их чистить, не
   открывая дыру для воскрешения.

## Что дальше

Индекс `php-mini-search` умеет пережить отказы (главы 18–20) и не разъехаться с
источником (эта глава). Но пока платформа, база и поиск живут в одном процессе,
а стык между ними — вызов метода, «согласие» между ними выглядит надёжнее, чем
есть. Глава 22 раздвигает границы: что даёт компонент, который не импортирует
классы соседа, и что ломается, когда граница проходит по сети. Глава 23 — про
то, как отказ одного компонента распространяется на остальные и как его
удержать.
