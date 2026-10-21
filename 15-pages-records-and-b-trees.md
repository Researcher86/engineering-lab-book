# Глава 15. Страницы, записи и B-деревья

> Проект:
> [`php-mini-database`](https://github.com/Researcher86/php-mini-database)
> (релиз `v1.1.0`, коммит `9adde5d`). Нужно знать: память процесса и устройство
> байтов (→ глава 1). Пригодятся дальше: SQL от текста к плану выполнения (→
> глава 16), транзакции и изоляция (→ глава 17), WAL, fsync и восстановление
> после сбоя (→ глава 18), инвертированный индекс (→ глава 20), `kill -9` как
> проверка (→ глава 26). Связанное уже пройденное: TCP-сервер с буферами (→
> глава 10) — но только по духу: страница не соединение, и ждать её не придётся.

Первая половина книги жила в сокетах: запрос приходит, обрабатывается, ответ
уходит. Часть IV начинается с того, что предметом становится **8192 байта на
диске**. У сокета всегда есть собеседник, у страницы — нет: она просто лежит в
файле и должна оставаться читаемой даже после того, как процесс посередине
записи убили.

База данных на самом деле отвечает всего на два вопроса:

1. **Дай мне строку по адресу.** «Верни ту строку, которая лежит вот здесь» — по
   адресу, который хранится в индексе, в WAL, в плане выполнения.
2. **Найди строки по значению.** «Верни все строки, у которых `id = 42`» — не
   отдав мне половину файла на проверку.

Оба вопроса решаются одним и тем же способом: файл нарезается на одинаковые
куски фиксированного размера, и весь дальнейший код работает уже с кусками, а не
с сырыми смещениями. Этот кусок — **страница**. В `php-mini-database` она
составляет 8192 байта, и через неё проходит всё: строки таблицы, узлы B-дерева,
метаданные индекса.

## 1. Проблема

### Файл — плоское пространство

Файл на диске — это последовательность байтов без какой-либо структуры.
Единственная операция, которую он понимает, — «прочитать N байтов с смещения M».
Всё остальное приходится строить самим, и каждая постройка платит за три
хрупкости сразу.

**Хрупкость первая: адрес = смещение, а смещения двигаются.** Пусть строка лежит
на байтах [4096, 4200). Любое изменение — обновление строки, удаление соседней
строки с компактификацией, вставка перед ней — сдвигает всё, что идёт после.
Адрес [4096, 4200) при этом перестаёт быть верным, а он хранится в индексах, в
журнале, в плане выполнения. Переписывать все хранители адреса при каждом сдвиге
— путь к рассинхрону; не хранить адреса вообще — путь к последовательному
обходу.

**Хрупкость вторая: обход стоит линейно.** «Найди строку, у которой значение
поля равно 42» без индекса означает прочитать файл целиком. Для тысячи строк это
незаметно, для миллиона — единственный операционный ответ: не делайте так.

**Хрупкость третья: запись не атомарна.** Диск не гарантирует, что запись 8192
байтов завершится целиком: процесс можно убить, буфер не дослать. Если структура
файла опирается на произвольные смещения, частично дописанная запись оставляет
файл, который невозможно отличить от исправного. Если же всё живёт по разметке
«кратно 8192», обрыв виден сразу: размер файла перестал делиться на размер
страницы.

### Ответ на все три — уровень косвенности

`php-mini-database` отвечает на все три хрупкости одним решением, применённым
трижды на разных уровнях:

- **Страница, а не смещение.** Файл нарезан на 8192-байтные страницы; страница N
  занимает байты [N × 8192, (N + 1) × 8192) — это арифметика, а не поиск.
  Страница сама несёт в заголовке свой номер и свой тип, поэтому файлу не нужна
  собственная карта содержимого.
- **Слот, а не смещение внутри страницы.** Запись адресуется парой «страница :
  слот» (`Storage\RecordId`), и слот — это номер в директории страницы, а не
  смещение байта. Байты записи могут двигаться внутри страницы сколько угодно;
  адрес остаётся прежним.
- **Ключ в B-дереве — не значение, а значение плюс адрес.** Чтобы два одинаковых
  значения не сливались в один байтовый ключ (об этом — ниже), к ключу
  пришивается тот самый `RecordId`. Уникальность ключа покупается тем же
  механизмом, что и стабильность адреса.

Итог: один объект — страница — решает и проблему адресации, и проблему поиска, и
проблему частичной записи. Дальше по главе мы увидим, как один и тот же класс
`Storage\Page` хранит строки таблицы **и** узлы B-дерева, и это не экономия, а
суть проекта.

## 2. Устройство

### Страничная арифметика

`Storage\PageManager` держит один файл и умеет две вещи: прочитать страницу N и
записать её обратно. Соответствие тривиально:

```text
heap.dat                                     events.idx
0        8192       16384      24576    0        8192       16384
+--------+----------+----------+---     +--------+-----------+------
| page 0 | page 1   | page 2   | ...   | page 0 | page 1    | ...
| HEAP   | HEAP     | HEAP     |       | BTREE_ | BTREE_    |
|        |          |          |       | META   | LEAF      |
+--------+----------+----------+---    +--------+-----------+------

страница N = байты [N × 8192, (N + 1) × 8192)
```

Ни индексной таблицы, ни каталога страниц нет: найти страницу — это умножить.
Заголовок страницы при этом несёт и номер, и тип, так что читатель, который по
ошибке оказался не на той странице, узнает об этом, а не распарсит чужие байты
как свои.

### Размеченная страница

Страница устроена классически — директория растёт от начала, данные от конца:

```text
0 +--------------------------------------+
  | pageId    uint32                     |
4 | type      uint16                     |
6 | slotCount uint16                     |
8 | freeEnd   uint16                     |  где начинаются данные
10 +--------------------------------------+
  | slot 0: offset uint16, length uint16 |  директория, растёт вниз
  | slot 1: ...                          |
  +--------------------------------------+
  | свободное место                      |
  +--------------------------------------+
  | данные записей, растут вверх от конца|
8192 +------------------------------------+
```

Как формулирует `docs/storage.md`, директория растёт вниз от заголовка, а данные
— вверх от конца страницы, и страница полна **ровно** тогда, когда они
встретились («a page is full exactly when the two meet»). Свободное место
считается один раз: 8192 минус заголовок, минус 4 байта на каждый когда-либо
выданный слот, минус длина каждой живой записи.

Удаление — это не уборка, а надпись. Слот превращается в **надгробие**
(tombstone): в его директорной записи стоят `offset = 0, length = 0`. Ноль —
корректный маркер именно потому, что данные растут от конца: смещение 0 лежит
внутри заголовка и никогда не может быть началом настоящей записи, поэтому
отдельный флаг не нужен. Сама директорная запись остаётся на месте — удалить её
значило бы перенумеровать все следующие слоты и сломать все адреса, которые на
них указывают. Зато `insert()` сначала ищет первое надгробие и переиспользует
его, поэтому цикл «удалить — вставить» не раздувает директорию.

### Адрес, который выживает

`Storage\RecordId` — это пара (номер страницы, номер слота). Это физический
адрес строки, и он отличается от того, что обычно называют первичным ключом:
строка сохраняет свой `RecordId`, когда меняет значение ключа, и получает новый,
когда вынуждена переехать на другую страницу.

Почему именно слот, а не смещение, `docs/DECISIONS.md` объясняет прямо:
директорную запись можно переписать, когда байты двигаются — при перекомпоновке
страницы или при удалении соседа, — и все остальные адреса остаются верными. И
далее: «A B-Tree leaf holds thousands of these addresses; an UPDATE that
shortened a row would otherwise have to find and rewrite every one of them».
Второй уровень косвенности окупается на каждом обновлении.

Из этого следует главное правило главы, которое повторяется трижды — в
`HeapFile::update()`, в `HeapFile::vacuum()` и в `HeapFile::rewrite()`: если
запись переехала, вернётся **новый** адрес, и тот, кто хранит старый, обязан
перестроиться. Индексы хранят адреса, поэтому именно они платят.

### Куча: куда девается место от удаления

`Storage\HeapFile` — это просто упорядоченная последовательность страниц с типом
`HEAP`, в которых лежат сериализованные строки одной таблицы. Он не знает ничего
о колонках: запись — это строка байтов.

Правило вставки занимает одну строку: пробовать **последнюю** страницу файла и
выделять новую, только если она полна. Свободное место, освобождённое удалением
в середине файла, никогда не ищется — в проекте нет карты свободного места.
`docs/DECISIONS.md` называет это прямо: «It never looks for the page in the
middle of the file that a DELETE left half empty».

Плата за это — `vacuum()`: переписывание файла целиком, с выбрасыванием
надгробий и уплотнением выживших записей. И здесь срабатывает то же правило
адресов: **каждый `RecordId` в файле меняется**, поэтому все индексы по таблице
после `vacuum()` подлежат пересборке. Обходного пути нет: пересылочный
(forwarding) словарь пришлось бы хранить в размере всей таблицы. Альтернатива —
карта свободного места — была отложена: это вторая on-disk структура, которую на
каждой фазе после этой пришлось бы держать согласованной с первой, включая
момент после сбоя.

### Запись строки: битмап NULL и порядок байтов

Строка таблицы укладывается в слот как `[null bitmap][кодированные значения]`:
бит на колонку, `ceil(колонок / 8)` байт, и NULL-колонка не оставляет в байтах
значений никакого следа. Формат намеренно совпадает с форматом результата по
сети, поэтому декодированную запись можно отдавать клиенту без перекодирования
значений.

Второе свойство формата важнее первого для этой главы: все многобайтовые числа
пишутся **big-endian со смещённым знаковым битом** — двоичное дополнение,
инвертированное XOR’ом на минимальное значение. Из этого следует монотонность,
которую `docs/DECISIONS.md` формулирует так: «compare two encoded values as raw
byte strings and the answer matches comparing the numbers». Без инверсии
знаковый бит поставил бы все отрицательные числа выше всех положительных, и −1
сортировался бы после 2147483647. Big-endian — по той же причине: старший байт
должен идти первым, иначе сравнение префикса не означает ничего.

Это свойство напрямую нужно B-дереву: узел сравнивает ключи `strcmp()` по сырым
байтам, не декодируя их, и диапазонный обход становится обходом байтовой
структуры. Один XOR на запись — дешёвая цена за это.

### B+дерево на той же самой странице

`Storage\BTreeIndex` — B+дерево по одному столбцу, и оно **не определяет
собственного формата страницы**. Узел — это ровно та же размеченная страница,
только с типом `BTREE_INTERNAL` или `BTREE_LEAF`, а вместо записи строки в слот
кладётся запись дерева:

```text
запись листа:        [0x01][ключ]              — 1 байт тега + ключ
запись внутреннего:  [0x01][ключ][4 байта]     — плюс номер дочерней
                                                     страницы
первый слот узла:    [0x00][специальное]        — у листа: номер
                          следующего листа; у внутреннего узла:
                          указатель «меньше всех ключей здесь»
```

Страница 0 файла `.idx` всегда `BTREE_META` и хранит номер текущего корня —
единственную часть дерева, чья идентичность меняется, когда дерево вырастает на
уровень. Листья связаны в цепочку слева направо, поэтому упорядоченный обход
идёт по цепочке, не спускаясь заново в дерево на каждом ключе.

```text
файл .idx
  0: BTREE_META:  root -> 3
                        3: BTREE_INTERNAL
        +----------------+----------------+----------------+
        | 0x00 -> 1      | sep K1 -> 5    | sep K2 -> 2    |
        +----------------+----------------+----------------+
             |                                   |
      1: BTREE_LEAF                       5: BTREE_LEAF
      ... -> ... ->                 ... -> ... ->
             \_________ цепочка листьев: 1 -> 5 -> 2 -> ... _________/
```

Спуск идёт по простому правилу: взять последний разделитель, который всё ещё не
больше ключа; если таких нет — самый левый указатель (тот самый `0x00`-слот).
Разделитель при сплите берётся из настоящих ключей листа — поэтому разделители и
ключи живут в одном и том же байтовом порядке.

Вместимость узла — прямое следствие макета. Запись листа — 1 байт тега плюс 4
байта значения плюс 8 байт `RecordId`, то есть 13 байт, плюс 4 байта директорной
записи: 17. После служебного первого слота свободно 8173 байта, то есть лист
держит **480** ключей — ровно столько, на каком вставке дерево из эксперимента
ниже и разделилось первый раз.

### Ключ, который не может повториться

Самое неочевидное решение главы: **хранящийся ключ — это не значение столбца**.
Лист хранит байты значения, за которыми следуют 8 байт `RecordId` строки. Два
значения, совпадающие в некуникативном индексе — ситуация обычная, — иначе были
бы буквально одним и тем же ключом дважды, а разделители B+дерева не умеют
различать равные ключи. История `docs/DECISIONS.md`:

> when a leaf full of duplicate-valued entries splits, the promoted separator
> equals the value itself — meaning descent for that value lands on whichever
> child the _most recent_ split routed it to, and the earlier leaves holding the
> same value, now on the other side of that separator, are never visited again,
> because `search()` only chains _forward_ through the leaf list from where it
> landed

То есть при сплите листа, полного дубликатов, разделителем становится само
значение; поиск по этому значению спускается в самый правый из «своих»
поддеревьев и никогда не доходит до левых листов с тем же значением — потому что
`search()` идёт по цепочке листьев только вперёд. Пришитый `RecordId` делает все
ключи попарно различными, сплиты — однозначными, а поиск по значению
превращается в поиск диапазона записей, у которых это значение является
префиксом. Всё остальное в классе продолжает сравнивать целые ключи `strcmp()`
как обычные уникальные строки.

Отсюда же — конструкция границ: минимально возможный суффикс (все нули) для
включающей границы, максимально возможный (все `\xff`) для исключающей. Один
набор правил, применённый один раз, обслуживает и `search()`, и `range()`, и
проверку уникальности.

### Что дерево и куча принципиально не делают

Три отказа, которые являются частью дизайна, а не его недостатками:

- **Нет буферного пула.** `PageManager::read()` каждый раз читает файл и
  декодирует новую страницу; `write()` кодирует и пишет заново. Пул — это, по
  формулировке проекта, «три новых способа быть неправым»: потерянная грязная
  страница, два вызывающих, мутацирующих одну закешированную `Page`, и кеш,
  разошедшийся с файлом после сбоя. Шов для него — один класс, и откладывается
  он до тех пор, пока его нечем измерить.
- **`BTreeIndex::delete()` не ребалансит.** Удаление вынимает запись из листа и
  переписывает одну страницу; недоуплотнённый узел не сливается с соседом,
  высота не уменьшается. Тот же торг, что и у кучи: «space comes back at a
  rebuild, not on every delete» — место возвращается пересборкой (`DROP INDEX` +
  `CREATE INDEX`), а не каждым удалением.
- **Нет страниц-переливов.** Запись крупнее `Page::MAX_RECORD_SIZE` (8178 байт)
  не разрезается на куски, а отклоняется — сразу и явно.

## 3. Код

Все выдержки ниже взяты из релиза `v1.1.0` проекта `php-mini-database` (коммит
`9adde5d`) через `git show`, поэтому номера строк в ссылках совпадают с текстом
выдержек.

### Размеры страницы — все решения, принятые в шести строках

```php
final class Page
{
    public const SIZE = 8192;

    private const HEADER_SIZE = 10;
    private const SLOT_SIZE = 4;

    /**
     * The largest record that can ever be stored: an otherwise empty page,
     * minus the header and the one directory entry the record itself needs.
     * Rows above this need overflow pages, which this database does not have
     * — it refuses them instead of silently truncating.
     */
    public const MAX_RECORD_SIZE = self::SIZE - self::HEADER_SIZE - self::SLOT_SIZE;
```

[`src/Storage/Page.php#L48-L61`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/Page.php#L48-L61)

**Перевод комментариев:**
- `MAX_RECORD_SIZE` (PHPDoc): самая большая запись, которую вообще можно
  сохранить: пустая страница минус заголовок и одна директорная запись, нужная
  самой записи. Строки больше этого требуют страниц-переливов, которых в этой
  базе нет, — она отказывает им вместо тихого усечения.

Всё, что в этой главе утверждалось про размеры, стоит здесь: 8192 — размер
страницы, 10 — заголовок, 4 — директорная запись, и 8178 — потолок записи.
Потолок выведен из арифметики, а не задан числом: пустая страница минус
заголовок минус тот единственный слот, который самой записи и нужен. Отсюда же
видно, почему «страница полна» — это точное состояние, а не приближение.

### Чтение страницы: проверка, а не доверие

```php
public static function fromBytes(int $expectedId, string $bytes): self
{
    if (strlen($bytes) !== self::SIZE) {
        throw new StorageException(sprintf('Page %d is %d bytes, expected %d.', $expectedId, strlen($bytes), self::SIZE));
    }

    /** @var array{id: int, type: int, slotCount: int, freeEnd: int} $header */
    $header = unpack('Nid/ntype/nslotCount/nfreeEnd', $bytes);

    if ($header['id'] !== $expectedId) {
        throw new StorageException(sprintf('Page at slot %d reports id %d.', $expectedId, $header['id']));
    }

    $type = PageType::tryFrom($header['type'])
        ?? throw new StorageException(sprintf('Page %d has unknown type %d.', $expectedId, $header['type']));

    $records = [];

    for ($slot = 0; $slot < $header['slotCount']; $slot++) {
        /** @var array{offset: int, length: int} $entry */
        $entry = unpack('noffset/nlength', substr($bytes, self::HEADER_SIZE + $slot * self::SLOT_SIZE, self::SLOT_SIZE));

        if ($entry['offset'] === 0) {
            $records[] = null;
            continue;
        }

        if ($entry['offset'] < $header['freeEnd'] || $entry['offset'] + $entry['length'] > self::SIZE) {
            throw new StorageException(sprintf('Page %d slot %d points outside the record area.', $expectedId, $slot));
        }

        $records[] = substr($bytes, $entry['offset'], $entry['length']);
    }

    return new self($expectedId, $type, $records);
}
```

[`src/Storage/Page.php#L89-L124`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/Page.php#L89-L124)

**Перевод комментариев:**
- `$header` (`@var`): разобранный заголовок страницы: `id`, `type`, `slotCount`,
  `freeEnd`.
- `$entry` (`@var`): элемент директории слота: `offset` и `length`.

Четыре проверки подряд, и каждая ловит свой отказ: длина файла кратна странице,
номер страницы в заголовке совпадает с ожидаемым (читатель не на той странице),
тип страницы известен перечислению, и ни один слот не указывает за пределы
области данных (`offset < freeEnd` означает, что запись залезла в директорию или
в заголовок). Обратите внимание на `offset === 0`: это и есть чтение надгробия —
записи нет, и слот остаётся занятым номером.

В памяти страница живёт **декодированной** — списком строк, где `null` —
надгробие, — а макет собирается только в `toBytes()`. Код ниже и есть весь ответ
на вопрос «почему не править байты на месте»: страница всё равно пишется
целиком, поэтому правка на месте покупала бы арифметику смещений, в которой
рождаются баги слот-страниц, и не покупала бы ничего.

### Запись страницы: макет собирается один раз

```php
public function toBytes(): string
{
    $directory = '';
    $data = '';
    $offset = self::SIZE;

    // Records are laid out back to front, so each one's offset is known
    // only after the ones already placed. The directory is built in slot
    // order alongside, which keeps slot numbers independent of where the
    // bytes ended up.
    foreach ($this->records as $record) {
        if ($record === null) {
            $directory .= pack('nn', 0, 0);
            continue;
        }

        $offset -= strlen($record);
        $data = $record . $data;
        $directory .= pack('nn', $offset, strlen($record));
    }

    $header = pack('Nnnn', $this->id, $this->type->value, count($this->records), $offset);
    $gap = self::SIZE - strlen($header) - strlen($directory) - strlen($data);

    return $header . $directory . str_repeat("\x00", $gap) . $data;
}
```

[`src/Storage/Page.php#L126-L151`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/Page.php#L126-L151)

**Перевод комментариев:**
- `// Records are laid out back to front, so each one's offset is known` —
  записи раскладываются от конца к началу, поэтому смещение каждой известно
  только после уже размещённых. Директория строится параллельно в порядке
  слотов, так что номера слотов не зависят от того, где оказались байты.

Записи кладутся от конца к началу, и директория строится параллельно с ними — в
порядке слотов. Именно поэтому номер слота не зависит от того, где в итоге
оказались байты: два прохода, два результата, никакой связи между порядком
слотов и порядком данных. Свободный зазор между директорией и данными
заполняется нулями: страница всегда ровно 8192 байта, что упрощает и чтение, и
саму запись на диск.

### Вставка: надбавка за слот и переиспользование надгробия

```php
public function insert(string $record): ?int
{
    self::assertStorable($record);

    $tombstone = $this->firstTombstone();
    $needed = strlen($record) + ($tombstone === null ? self::SLOT_SIZE : 0);

    if ($needed > $this->freeSpace()) {
        return null;
    }

    if ($tombstone !== null) {
        $this->records[$tombstone] = $record;

        return $tombstone;
    }

    $this->records[] = $record;

    return count($this->records) - 1;
}
```

[`src/Storage/Page.php#L158-L178`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/Page.php#L158-L178)

Две мелочи, на которых держится вся экономия удалений. Первая: новая запись
стоит на 4 байта дороже своей длины — директорная запись тоже размечается из
свободного места, поэтому «вместимость страницы» — это не 8182. Вторая: если
надгробие найдено, надбавка за слот не берётся, потому что слот уже существует.
И `null` здесь — не ошибка, а ответ: «на этой странице нет места, смотри
другую».

### Файл: номер страницы — это смещение

```php
public function read(int $pageId): Page
{
    if ($pageId < 0 || $pageId >= $this->pageCount) {
        throw new StorageException(sprintf('Page %d is outside "%s" (%d pages).', $pageId, $this->path, $this->pageCount));
    }

    $this->seek($pageId);

    $bytes = '';

    // A single fread() can return fewer bytes than asked for even on a
    // local file; loop until the page is whole rather than handing a
    // short buffer to Page::fromBytes() and calling the file corrupt.
    while (strlen($bytes) < Page::SIZE) {
        $chunk = fread($this->handle, Page::SIZE - strlen($bytes));

        if ($chunk === false || $chunk === '') {
            throw new StorageException(sprintf('Page %d of "%s" ends early.', $pageId, $this->path));
        }

        $bytes .= $chunk;
    }

    return Page::fromBytes($pageId, $bytes);
}

public function write(Page $page): void
{
    if ($page->id < 0 || $page->id > $this->pageCount) {
        throw new StorageException(sprintf('Cannot write page %d: "%s" has %d pages.', $page->id, $this->path, $this->pageCount));
    }

    $this->seek($page->id);

    if (fwrite($this->handle, $page->toBytes()) !== Page::SIZE) {
        throw new StorageException(sprintf('Short write of page %d to "%s".', $page->id, $this->path));
    }

    // Writing at pageCount extends the file by exactly one page, which
    // is how allocate() materialises the page it just handed out.
    $this->pageCount = max($this->pageCount, $page->id + 1);
}
```

[`src/Storage/PageManager.php#L63-L104`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/PageManager.php#L63-L104)

**Перевод комментариев:**
- `// A single fread() can return fewer bytes than asked for even on a` — один
  `fread()` может вернуть меньше запрошенного даже на локальном файле; чтение
  повторяется, пока страница не собрана целиком, вместо того чтобы отдать
  короткий буфер в `Page::fromBytes()` и объявить файл повреждённым.
- `// Writing at pageCount extends the file by exactly one page, which` — запись
  ровно по `pageCount` увеличивает файл ровно на одну страницу; так `allocate()`
  материализует только что выданную страницу.

`seek()` здесь — это `$pageId × 8192`, вся навигация по файлу. Три детали,
которые легко пропустить: чтение за пределами файла — ошибка, а не пустая
страница; `fread()` имеет право вернуть меньше запрошенного даже на локальном
файле, поэтому чтение циклится, пока страница не собрана целиком, и только потом
`fromBytes()` вправе называть файл повреждённым; и запись ровно на `pageCount` —
это способ вырастить файл на одну страницу, так что `allocate()` вообще ничего
не делает на диске.

### Куда идёт новая строка

```php
public function insert(string $record): RecordId
{
    $page = $this->pages->pageCount() === 0
        ? $this->pages->allocate(PageType::HEAP)
        : $this->pages->read($this->insertHint);

    $slot = $page->insert($record);

    if ($slot === null) {
        $page = $this->pages->allocate(PageType::HEAP);
        // A record too large for an empty page cannot be stored at all,
        // and insert() says so by throwing rather than returning null.
        $slot = (int) $page->insert($record);
    }

    $this->pages->write($page);
    $this->insertHint = $page->id;

    return new RecordId($page->id, $slot);
}
```

[`src/Storage/HeapFile.php#L55-L74`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/HeapFile.php#L55-L74)

**Перевод комментариев:**
- `// A record too large for an empty page cannot be stored at all,` — запись,
  которая не помещается даже на пустую страницу, невозможно сохранить вообще, и
  `insert()` сообщает об этом исключением, а не возвратом `null`.

`insertHint` — это вся «стратегия размещения»: номер последней страницы, которая
сработала. Новая страница выделяется только когда текущая полна, и именно
поэтому освободившееся в середине файла место не используется никогда. Второй
вызов `insert()` на свежей странице не может вернуть `null` — запись уже прошла
`assertStorable()`, и, если она не влезла бы, здесь поднят исключение.

### Обновление, которое меняет адрес

```php
public function update(RecordId $id, string $record): RecordId
{
    $page = $this->pages->read($id->pageId);

    if ($page->update($id->slot, $record)) {
        $this->pages->write($page);

        return $id;
    }

    // Refused before the old copy is deleted, not by the insert()
    // after it: a record no page can hold would otherwise fail with the
    // row already gone, before the caller logged anything to undo.
    Page::assertStorable($record);
    $page->delete($id->slot);
    $this->pages->write($page);

    return $this->insert($record);
}
```

[`src/Storage/HeapFile.php#L101-L119`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/HeapFile.php#L101-L119)

**Перевод комментариев:**
- `// Refused before the old copy is deleted, not by the insert()` — отказ
  происходит до удаления старой копии, а не в `insert()` после него: иначе
  запись, которую не вместит ни одна страница, падала бы, когда строка уже
  удалена и вызывающий ещё ничего не записал для отката.

Обновление на месте возвращается, если разница длин влезает в свободное место.
Если нет — строка удаляется и вставляется заново, и метод возвращает **новый**
идентификатор: старый слот мог переехать. Комментарий внутри объясняет порядок
проверок: `assertStorable()` вызывается до удаления, чтобы запись, которую не
вместит ни одна страница, не оставляла таблицу без строки, которую ещё никто не
успел записать в журнал (об этом — глава 18).

### VACUUM: единственное, что возвращает байты

```php
/**
 * Rewrite the file with the deleted records dropped and the survivors
 * packed tight, and return how many bytes that gave back.
 *
 * **Every RecordId in the file changes.** Nothing else here moves a
 * record without telling its caller, but vacuum moves all of them at
 * once, so anything that stores record ids — every index on the table —
 * has to be rebuilt afterwards. That is the price of not keeping a
 * forwarding map, which would have to be as large as the table.
 *
 * The rewrite goes to a temporary file that is renamed over the original
 * at the end, so a crash halfway through leaves the original intact and
 * loses only the temporary.
 */
public function vacuum(): int
{
    $path = $this->pages->path();
    $sizeBefore = $this->files->size($path);

    $this->replace('.vacuum', static fn (string $record): string => $record);

    return $sizeBefore - $this->files->size($path);
}
```

[`src/Storage/HeapFile.php#L178-L200`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/HeapFile.php#L178-L200)

**Перевод комментариев:**
- `vacuum()` (PHPDoc): переписывает файл, выбрасывая удалённые записи и плотно
  упаковывая выжившие, и возвращает число освобождённых байт. **Меняется каждый
  `RecordId` в файле.** Больше ничто здесь не двигает запись, не сообщая об этом
  вызывающему, а `vacuum` двигает все сразу, поэтому всё, что хранит id записей
  (каждый индекс таблицы), нужно пересобрать после. Это плата за отказ от карты
  перенаправления, которая была бы размером с таблицу. Перезапись идёт во
  временный файл, который в конце переименовывается поверх оригинала, так что
  сбой посередине оставляет оригинал целым и теряет только временный файл.

Полное переписывание под временным именем с переименованием поверх оригинала в
конце: сбой посередине оставляет оригинал на месте и теряет только временный
файл. `vacuum()` — это частный случай более общего `rewrite()`, который
прогоняет каждую запись через преобразование: именно так `ALTER TABLE`
перекодирует таблицу, в которой появилась или исчезла колонка (страница 17 главы
про транзакции покажет, почему строка не несёт собственной схемы).

### Строка: битмап NULL

```php
public function serialize(array $values, array $types): string
{
    $this->assertColumnCount($values, $types);

    $nullBitmap = $this->encodeNullBitmap($values);
    $body = '';

    foreach ($values as $i => $value) {
        if ($value === null) {
            continue;
        }

        $body .= $types[$i]->encode($types[$i]->cast($value));
    }

    return $nullBitmap . $body;
}
```

[`src/Storage/RecordSerializer.php#L31-L47`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/RecordSerializer.php#L31-L47)

Битмап идёт первым, значения — вторым, и NULL-значение просто не попадает в
тело: при разборе тип читает бит и либо возвращает `null`, либо декодирует
следующие байты своего типа. Плата за пропуск — полное доверие битмапе: если
запись короче битмапы, это ошибка формата, и она тоже проверяется.

### Узел дерева — это страница

```php
 * ### Where entries live
 *
 * Every node — internal or leaf — is one `Storage\Page`, exactly as a
 * `HeapFile` page holds table rows: this class supplies no page format of
 * its own, only a way to pack one entry into the bytes a page slot already
 * frames. A leaf entry is `[0x01][key bytes]` (the trailing 8 bytes of the
 * key *are* the `RecordId`, so nothing further needs to be stored
 * alongside it); an internal entry is `[0x01][key bytes][4-byte child page
 * id]`; and each node's first slot is reserved for a `0x00`-tagged one
 * holding whatever that node type needs instead of a key — the next
 * leaf's page id for a leaf, or the "less than every real key here" child
 * pointer for an internal node.
 *
 * Page 0 of the file is always a `BTREE_META` page holding the current
 * root's page id — the one piece of the tree whose identity changes as it
 * grows a level.
```

[`src/Storage/BTreeIndex.php#L36-L51`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L36-L51)

Это докблок к классу, а не отдельная реализация: у дерева нет собственной работы
с файлом, только способ упаковать запись в слот, который страница уже размечает.
`docs/DECISIONS.md` оценивает этот обмен: страница поставляла нужное —
переменной длины записи, переиспользование надгробий, запись целиком, — «at the
cost of the one-byte tag each record now needs to disambiguate its two possible
shapes». Восемь байт `RecordId` при этом живут внутри ключа, поэтому отдельный
указатель на строку в записи листа не нужен вообще.

### Ключ = значение + RecordId

```php
 * ### The key actually stored is not the indexed value
 *
 * A leaf's key is the indexed value's sortable bytes *followed by the
 * row's 8-byte `RecordId`*, never the value alone. Two rows with the same
 * indexed value — routine for a non-unique index — would otherwise be
 * *the same key twice*, and a B+Tree's separators cannot tell two equal
 * keys apart: an internal node splits by promoting one of them as a
 * separator, and a search descending past a separator equal to the key it
 * is looking for has no way to know that entries with that same value
 * still exist in the child on the *other* side of it. Appending the
 * `RecordId` makes every stored key unique even when the value repeats, so
 * separators are always unambiguous. A lookup for a value searches for the
 * range of stored keys carrying that value as their prefix — `search()`
 * and `range()` build the right prefix bounds for this once, and
 * everywhere else in this class just compares whole keys with `strcmp()`
 * as if they were ordinary, unique ones.
```

[`src/Storage/BTreeIndex.php#L19-L34`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L19-L34)

### Вставка: быстрый путь и разделение

```php
private function insertIntoNode(int $pageId, string $fullKey): ?array
{
    $page = $this->pages->read($pageId);

    if ($page->type === PageType::BTREE_LEAF) {
        if ($page->insert(self::TAG_ENTRY . $fullKey) !== null) {
            $this->pages->write($page);

            return null;
        }

        $entries = $this->readLeafEntries($page);
        $entries[] = $fullKey;
        usort($entries, strcmp(...));

        return $this->writeLeafWithSplit($pageId, $entries, $this->nextLeafPageId($page));
    }

    $entries = $this->readInternalEntries($page);
    $split = $this->insertIntoNode($this->childFor($entries, $fullKey), $fullKey);

    if ($split === null) {
        return null;
    }

    [$separator, $newChildId] = $split;
    $entries[] = ['key' => $separator, 'child' => $newChildId];
    usort($entries, $this->internalEntryComparator(...));

    return $this->writeInternalWithSplit($pageId, $entries);
}
```

[`src/Storage/BTreeIndex.php#L381-L411`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L381-L411)

Весь быстрый путь — это первые пять строк ветки листа: если `Page::insert()`
вернул номер слота, запись уже лежит в дереве, страница переписана, и сплит не
понадобился. Слоты при этом не обязаны храниться в отсортированном порядке:
`docs/DECISIONS.md` отмечает, что каждое чтение всё равно декодирует и сортирует
entries заново («every read (`readLeafEntries()`, `readInternalEntries()`)
decodes and re-sorts regardless of what order the slots happen to be in»).
Именно этот путь однажды исправил стресс-тест, который шёл 40 секунд: раньше
каждая вставка переписывала весь список записей страницы, и заполнение страницы
из пустой становилось операцией квадратичной по размеру страницы.

Рекурсия вниз идёт по одному ребру, а результат поднимается вверх как
`[разделитель, номер новой правой страницы]` — классический протокол сплита,
только «узел» здесь честная страница, а «указатель» — четыре байта в конце
записи.

### Разделение листа пополам

```php
private function writeLeafWithSplit(int $pageId, array $entries, ?int $nextLeafPageId): ?array
{
    $page = $this->buildLeaf($pageId, $entries, $nextLeafPageId);

    if ($page !== null) {
        $this->pages->write($page);

        return null;
    }

    $mid = intdiv(count($entries), 2);
    $left = array_slice($entries, 0, $mid);
    $right = array_slice($entries, $mid);

    $rightPage = $this->pages->allocate(PageType::BTREE_LEAF);
    $this->writeLeaf($rightPage->id, $right, $nextLeafPageId);
    $this->writeLeaf($pageId, $left, $rightPage->id);

    return [$right[0], $rightPage->id];
}
```

[`src/Storage/BTreeIndex.php#L507-L526`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L507-L526)

Сначала `buildLeaf()` пытается собрать страницу из всех записей — и, если это
удаётся, сплита нет и функция возвращает `null` (тот же метод отвечает и на
вопрос «влезет ли», и на «запиши», не копируя логику). Если не удалось:
середина, два куска, новая страница справа, старая переписывается слева, а
наверх возвращается **первый ключ правой половины** — тот самый разделитель,
нарисованный из настоящего ключа листа. Обратите внимание на связь: правый лист
наследует `nextLeafPageId`, а левый получает ссылку на правый, — цепочка листьев
сохраняется.

### Спуск: последний разделитель, не больший ключа

```php
private function childFor(array $entries, string $key): int
{
    $child = $entries[0]['child'];

    foreach ($entries as $entry) {
        if ($entry['key'] !== null && strcmp($entry['key'], $key) <= 0) {
            $child = $entry['child'];
        }
    }

    return $child;
}
```

[`src/Storage/BTreeIndex.php#L536-L547`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L536-L547)

Одна функция — весь спуск. Запись с `key === null` — это тот самый
`0x00`-слот-сентинел, «меньше всех ключей здесь»; сортировка
`readInternalEntries()` гарантирует, что он первый, поэтому начальное значение —
самый левый указатель. Условие `<= 0` (не строго меньше) важно: когда ключ равен
разделителю, спуск идёт вправо — и именно поэтому разделитель, равный искомому
значению без суффикса, в историческом баге ниже и уводил поиск мимо дубликатов.

### Префиксный поиск, который нельзя останавливать по отсутствию совпадения

```php
/** @return Generator<RecordId> */
private function scanPrefix(string $prefix): Generator
{
    $boundary = $this->boundary($prefix, true);
    $pageId = $this->leafPageIdFor($boundary);

    while ($pageId !== null) {
        $sawMatch = false;
        $entries = $this->readLeafEntries($this->pages->read($pageId));

        foreach ($entries as $entry) {
            if (substr($entry, 0, -self::RECORD_ID_LENGTH) === $prefix) {
                $sawMatch = true;
                yield $this->recordIdFromKey($entry);
            } elseif ($sawMatch) {
                return; // past the (contiguous) run of matching entries
            }
        }

        // The run can both start and end past a leaf boundary, so
        // neither edge may stop the walk early. It can *end* past one
        // because a value with several RecordIds spills onto the next
        // leaf. It can *start* past one because a split promotes the
        // right half's first whole key - value bytes plus RecordId -
        // as the separator, which sorts above the all-zero-suffix
        // boundary this scan descended by: the descent lands on the
        // left leaf and the run begins on the right one. Stop only
        // once an entry is genuinely past the run - with the entries
        // sorted, anything above $boundary that does not carry the
        // prefix is above every key that could.
        $last = $entries === [] ? null : $entries[array_key_last($entries)];

        if (!$sawMatch && $last !== null && strcmp($last, $boundary) > 0) {
            return;
        }

        $pageId = $this->nextLeafPageId($this->pages->read($pageId));
    }
}
```

[`src/Storage/BTreeIndex.php#L267-L305`](https://github.com/Researcher86/php-mini-database/blob/9adde5d113786ae8730a89721302270e1130c3e9/src/Storage/BTreeIndex.php#L267-L305)

**Перевод комментариев:**
- `scanPrefix()` (PHPDoc): возвращает генератор `RecordId`.
- `// past the (contiguous) run of matching entries` — за (непрерывной) серией
  совпадающих записей.
- `// The run can both start and end past a leaf boundary, so` — серия может и
  начинаться, и заканчиваться за границей листа, поэтому ни один край не должен
  останавливать обход досрочно. Заканчиваться за границей она может потому, что
  значение с несколькими `RecordId` перетекает на следующий лист. Начинаться за
  границей — потому что сплит продвигает первый целый ключ правой половины
  (байты значения плюс `RecordId`) в качестве разделителя, а тот сортируется
  выше границы «значение + все нули», по которой спустился этот скан: спуск
  попадает на левый лист, а серия начинается на правом. Останавливаться можно
  только когда запись действительно вышла за пределы серии: при отсортированных
  записях любая запись больше `$boundary`, не несущая префикс, больше любого
  ключа, который мог бы его нести.

Это самая внимательная функция класса, и она написана поверх настоящего бага.
`scanPrefix()` отвечает и за `search()`, и за проверку уникальности уникального
индекса. Раньше цикл шёл по цепочке листьев, пока лист ещё заканчивался
совпадением, и останавливался на первом листе без совпадений. Пропущенный
случай: серия **начинается** за границей листа. Сплит в качестве разделителя
продвигает первый **целый** ключ правой половины — значение вместе с 8-байтовым
`RecordId`, — а поиск спускается по границе «значение + все нули», которая
строго меньше. Спуск приземляется на левый лист, совпадений там нет, и поиск
заканчивался. Как формулирует `docs/DECISIONS.md`: «one key vanished per leaf
split, while the row itself sat untouched in the heap and answered `COUNT(*)`
normally», и далее: «That is a wrong answer, not a slow one». Ошибка читалась
как потеря данных приложения, а тест ловил её выборкой из шести ключей — и ни
один из шести не был разделителем.

## 4. Эксперимент

### Подготовка

```bash
cd php-mini-database
make build install
```

Три скрипта ниже кладутся в `/tmp` на хосте и копируются в контейнер, где проект
смонтирован в `/app`. Вывод — реальный, снятый при написании главы.

### 1. Анатомия страницы

Первый вопрос главы: что такое страница на самом деле. Скрипт строит её руками —
три вставки, одно удаление, ещё одна вставка, — а затем печатает и разобранное
состояние, и сырые байты заголовка с директорией:

```bash
docker compose cp /tmp/page-anatomy.php php:/tmp/page-anatomy.php
docker compose exec php php /tmp/page-anatomy.php
```

```php
<?php

declare(strict_types=1);

/**
 * One 8 KiB page, opened up.
 *
 * The page is the unit this database reads and writes: 10 bytes of header,
 * a slot directory that grows down, record bytes that grow up, free space
 * where the two meet. Everything the chapter claims about addresses is
 * visible here:
 *
 *   - a record is named by its slot number, never by its byte offset, so
 *     its bytes can move without invalidating anything that points at it;
 *   - a delete leaves a tombstone (offset 0, length 0) instead of removing
 *     the directory entry, because removing that entry would renumber every
 *     later slot on the page;
 *   - insert() reuses a tombstone before it grows the directory;
 *   - a record that cannot fit on an empty page is refused outright: this
 *     database has no overflow pages.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/page-anatomy.php php:/tmp/page-anatomy.php
 *   docker compose exec php php /tmp/page-anatomy.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\StorageException;
use PhpMiniDatabase\Storage\Page;
use PhpMiniDatabase\Storage\PageType;

/** What the page holds right now, slot by slot, and how much room is left. */
function show(Page $page, string $label): void
{
    printf(
        "%s — slotCount=%d recordCount=%d freeSpace=%d\n",
        $label,
        $page->slotCount(),
        $page->recordCount(),
        $page->freeSpace(),
    );

    for ($slot = 0; $slot < $page->slotCount(); $slot++) {
        $record = $page->read($slot);
        printf(
            "  slot %d: %s\n",
            $slot,
            $record === null ? 'tombstone' : sprintf('"%s" (%d bytes)', $record, strlen($record)),
        );
    }
}

/** The header and the slot directory, read straight out of the page bytes. */
function dump(Page $page): void
{
    $bytes = $page->toBytes();

    /** @var array{id: int, type: int, slotCount: int, freeEnd: int} $header */
    $header = unpack('Nid/ntype/nslotCount/nfreeEnd', substr($bytes, 0, 10));

    printf(
        "  header: pageId=%d type=%d slotCount=%d freeEnd=%d\n",
        $header['id'],
        $header['type'],
        $header['slotCount'],
        $header['freeEnd'],
    );
    printf("  first 10 bytes: %s\n", bin2hex(substr($bytes, 0, 10)));

    for ($slot = 0; $slot < $header['slotCount']; $slot++) {
        $entry = unpack('noffset/nlength', substr($bytes, 10 + $slot * 4, 4));
        printf("  slot %d: offset=%d length=%d\n", $slot, $entry['offset'], $entry['length']);
    }
}

$page = Page::create(7, PageType::HEAP);
show($page, 'a fresh page');

printf("alice  -> slot %d\n", (int) $page->insert('alice'));
printf("bob    -> slot %d\n", (int) $page->insert('bob'));
printf("carol  -> slot %d\n", (int) $page->insert('carol'));
show($page, 'after three inserts');

$page->delete(1);
show($page, 'after deleting slot 1 (bob)');

echo "the same page as bytes:\n";
dump($page);

printf("dave   -> slot %d (the tombstone, not a fourth directory entry)\n", (int) $page->insert('dave'));
show($page, 'after one more insert');
dump($page);

// An empty page, filled to the last byte: MAX_RECORD_SIZE is the header,
// one slot's directory entry, and nothing else.
$full = Page::create(8, PageType::HEAP);
$largest = str_repeat('x', Page::MAX_RECORD_SIZE);
printf(
    "\na %d-byte record on an empty page -> slot %d, freeSpace=%d\n",
    strlen($largest),
    (int) $full->insert($largest),
    $full->freeSpace(),
);

try {
    $full->insert($largest . 'y');
    echo "one byte more -> stored, which should be impossible\n";
} catch (StorageException $e) {
    printf("one byte more -> StorageException: %s\n", $e->getMessage());
}
```

**Перевод комментариев:**
- `page-anatomy.php` (PHPDoc): одна страница на 8 КиБ, разобранная по частям.
  Страница — единица чтения и записи этой базы: 10 байт заголовка, директория
  слотов, растущая вниз, байты записей, растущие вверх, и свободное место там,
  где они встречаются. Всё, что глава утверждает об адресах, видно здесь: запись
  называется номером слота, а не байтовым смещением, поэтому её байты могут
  переезжать, не ломая указатели; удаление оставляет надгробие (`offset 0`,
  `length 0`) вместо удаления директорной записи, иначе перенумеровались бы все
  последующие слоты; `insert()` переиспользует надгробие раньше, чем растит
  директорию; запись, не влезающая на пустую страницу, отвергается сразу —
  страниц-переливов здесь нет.
- `show()` (PHPDoc): что страница держит прямо сейчас, слот за слотом, и сколько
  места осталось.
- `dump()` (PHPDoc): заголовок и директория слотов, прочитанные прямо из байтов
  страницы.
- `$header` (`@var`): разобранный заголовок страницы: `id`, `type`, `slotCount`,
  `freeEnd`.
- `// An empty page, filled to the last byte: MAX_RECORD_SIZE is the header,` —
  пустая страница, заполненная до последнего байта: `MAX_RECORD_SIZE` — это
  заголовок, директорная запись одного слота и больше ничего.

```text
a fresh page — slotCount=0 recordCount=0 freeSpace=8182
alice  -> slot 0
bob    -> slot 1
carol  -> slot 2
after three inserts — slotCount=3 recordCount=3 freeSpace=8157
  slot 0: "alice" (5 bytes)
  slot 1: "bob" (3 bytes)
  slot 2: "carol" (5 bytes)
after deleting slot 1 (bob) — slotCount=3 recordCount=2 freeSpace=8160
  slot 0: "alice" (5 bytes)
  slot 1: tombstone
  slot 2: "carol" (5 bytes)
the same page as bytes:
  header: pageId=7 type=1 slotCount=3 freeEnd=8182
  first 10 bytes: 00000007000100031ff6
  slot 0: offset=8187 length=5
  slot 1: offset=0 length=0
  slot 2: offset=8182 length=5
dave   -> slot 1 (the tombstone, not a fourth directory entry)
after one more insert — slotCount=3 recordCount=3 freeSpace=8156
  slot 0: "alice" (5 bytes)
  slot 1: "dave" (4 bytes)
  slot 2: "carol" (5 bytes)
  header: pageId=7 type=1 slotCount=3 freeEnd=8178
  first 10 bytes: 00000007000100031ff2
  slot 0: offset=8187 length=5
  slot 1: offset=8183 length=4
  slot 2: offset=8178 length=5

a 8178-byte record on an empty page -> slot 0, freeSpace=0
one byte more -> StorageException: Record of 8179 bytes exceeds the 8178 byte maximum.
```

Здесь видно всё, о чём глава говорила в разделе «Устройство», в том же порядке.

**Директория растёт вниз, данные вверх.** Три записи `alice`, `bob`, `carol`
получают смещения 8187, 8184 и 8179... но не в том порядке, в каком вставлялись:
`toBytes()` кладёт их от конца к началу, и смещение каждой известно только после
предыдущих. Свободное место после трёх вставок — 8157: это 8192 − 10 (заголовок)
− 12 (три директорные записи по 4 байта) − 13 (`alice` + `bob` + `carol`).

**Удаление возвращает 3 байта, а не 7.** После удаления `bob` `freeSpace` вырос
с 8157 до 8160 — вернулись только байты самой записи. Четыре байта директорной
записи остались на месте, и `slotCount` по-прежнему равен 3: слот 1 — надгробие,
и его номер больше никому не достанется, пока страница не перепишется целиком.

**Ноль как маркер виден в байтах.** Заголовок `00000007000100031ff6` разбирается
как `pageId=7`, `type=1` (`HEAP`), `slotCount=3`, `freeEnd=8182` (0x1FF6). В
директории надгробие — ровно `0000 0000`, и `fromBytes()` читает его как `null`,
не сверяясь ни с каким флагом.

**Вставка после удаления вернулась в тот же слот.** `dave` занимает слот 1,
`slotCount` остаётся 3, а `freeSpace` падает до 8156 — на 4 байта записи и ни на
байт директории больше. Повторный цикл «удалить — вставить» будет держать
директорию ровно на трёх записях.

**Потолок — 8178 байт, и он арифметический.** Запись ровно в 8178 байт встаёт на
пустую страницу и оставляет `freeSpace=0`; 8179-я — это уже `StorageException`.
Никаких страниц-переливов: проект предпочитает отказ намеренно читаемому
формату, в котором обрезанная запись выглядел бы как корректная.

### 2. Куча, удаление и VACUUM

Второй вопрос: что происходит с местом, которое освобождает удаление. Скрипт
строит таблицу из 100 записей по 400 байт, удаляет первые 80 (это ровно первые
четыре страницы), вставляет ещё 40 и только потом вызывает `vacuum()`:

```bash
docker compose cp /tmp/heap-vacuum.php php:/tmp/heap-vacuum.php
docker compose exec php php /tmp/heap-vacuum.php
```

```php
<?php

declare(strict_types=1);

/**
 * What a heap file does with deleted space, and what VACUUM gives back.
 *
 * HeapFile::insert() tries the last page of the file and never looks for
 * room a DELETE freed in the middle — there is no free-space map. Under a
 * delete-heavy workload the file keeps growing while earlier pages sit
 * half empty, and only vacuum() gives the bytes back. Three consequences
 * are printed here:
 *
 *   - deleting does not shrink the file (a tombstone is four directory
 *     bytes that stay in place);
 *   - inserts after the deletes go past the emptied pages to the end of
 *     the file, so the file grows over space it already owns;
 *   - vacuum() reclaims whole pages, moves every record, and with them
 *     every RecordId — which is why every index on the table has to be
 *     rebuilt after it.
 *
 * It ends with the failure a page-addressed file is built to meet: a file
 * whose size is not a whole number of pages is refused on open, not
 * guessed at.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/heap-vacuum.php php:/tmp/heap-vacuum.php
 *   docker compose exec php php /tmp/heap-vacuum.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\StorageException;
use PhpMiniDatabase\Storage\HeapFile;
use PhpMiniDatabase\Storage\RecordId;

const RECORD_BYTES = 400;
const FIRST_BATCH = 100;
const SECOND_BATCH = 40;
const FIRST_PAGES = 4;

$path = sys_get_temp_dir() . '/ch15-heap.dat';

@unlink($path);
@unlink($path . '.vacuum');

$heap = HeapFile::open($path);
$record = static fn (int $i): string => sprintf('row%03d', $i) . str_repeat('x', RECORD_BYTES - 6);

for ($i = 0; $i < FIRST_BATCH; $i++) {
    $heap->insert($record($i));
}

printf(
    "%d records of %d bytes: %d pages, %d bytes on disk\n",
    FIRST_BATCH,
    RECORD_BYTES,
    $heap->pageCount(),
    filesize($path),
);

// Empty the first four pages completely.
$deleted = 0;

foreach ($heap->scan() as $id => $bytes) {
    if ($id->pageId < FIRST_PAGES) {
        $heap->delete($id);
        $deleted++;
    }
}

printf(
    "deleted %d of them: still %d pages, still %d bytes\n",
    $deleted,
    $heap->pageCount(),
    filesize($path),
);

$row090 = null;

foreach ($heap->scan() as $id => $bytes) {
    if ($bytes === $record(90)) {
        $row090 = $id;
    }
}

printf("row090 now lives at RecordId %s\n", $row090);

for ($i = FIRST_BATCH; $i < FIRST_BATCH + SECOND_BATCH; $i++) {
    $heap->insert($record($i));
}

printf(
    "%d more inserts: %d pages, %d bytes — the %d emptied pages were never consulted\n",
    SECOND_BATCH,
    $heap->pageCount(),
    filesize($path),
    FIRST_PAGES,
);

$reclaimed = $heap->vacuum();

printf(
    "vacuum: reclaimed %d bytes, %d pages, %d bytes on disk\n",
    $reclaimed,
    $heap->pageCount(),
    filesize($path),
);

$moved = null;

foreach ($heap->scan() as $id => $bytes) {
    if ($bytes === $record(90)) {
        $moved = $id;
    }
}

printf("row090 lives at %s now — every RecordId in the file moved\n", $moved);

try {
    $heap->read($row090);
    echo "the old address still reads, which would be data loss\n";
} catch (StorageException $e) {
    printf("reading the old address -> StorageException: %s\n", $e->getMessage());
}

$heap->close();

// One byte short of a whole page: a write that stopped halfway is not a
// page, and the file says so when it is next opened.
$kept = file_get_contents($path);
file_put_contents($path, substr((string) $kept, 0, -1));
$short = filesize($path);

try {
    HeapFile::open($path);
    echo "a torn file opened anyway, which should be impossible\n";
} catch (StorageException $e) {
    printf("\na %d-byte file -> StorageException: %s\n", $short, $e->getMessage());
}

@unlink($path);
```

**Перевод комментариев:**
- `heap-vacuum.php` (PHPDoc): что куча делает с удалённым местом и что
  возвращает `VACUUM`. `HeapFile::insert()` пробует последнюю страницу файла и
  никогда не ищет место, освобождённое `DELETE` в середине, — карты свободного
  места нет. При нагрузке с частыми удалениями файл растёт, пока ранние страницы
  стоят полупустыми, и только `vacuum()` возвращает байты. Здесь напечатаны три
  следствия: удаление не уменьшает файл (надгробие — это четыре директорных
  байта, которые остаются на месте); вставки после удалений идут мимо опустевших
  страниц в конец файла, так что файл растёт поверх уже принадлежащего ему
  пространства; `vacuum()` освобождает целые страницы, двигает каждую запись и
  вместе с ними каждый `RecordId` — поэтому каждый индекс таблицы приходится
  пересобирать после него. Заканчивается сбой, ради которого файл с адресацией
  по страницам и построен: файл, размер которого не кратен целому числу страниц,
  отвергается при открытии, а не угадывается.
- `// Empty the first four pages completely.` — полностью опустошить первые
  четыре страницы.
- `// One byte short of a whole page: a write that stopped halfway is not a` —
  на один байт меньше целой страницы: запись, остановившаяся на половине, — не
  страница, и файл говорит об этом при следующем открытии.

```text
100 records of 400 bytes: 5 pages, 40960 bytes on disk
deleted 80 of them: still 5 pages, still 40960 bytes
row090 now lives at RecordId 4:10
40 more inserts: 7 pages, 57344 bytes — the 4 emptied pages were never consulted
vacuum: reclaimed 32768 bytes, 3 pages, 24576 bytes on disk
row090 lives at 0:10 now — every RecordId in the file moved
reading the old address -> StorageException: Page 4 is outside "/tmp/ch15-heap.dat" (3 pages).

a 24575-byte file -> StorageException: File "/tmp/ch15-heap.dat" is 24575 bytes, not a whole number of 8192 byte pages.
```

Разберём арифметику — она здесь вся читается вслух. Запись 400 байт стоит 404:
длина плюс 4 байта директорной записи. Пустая страница держит 8182 − 404 × 20 =
102 байта, то есть ровно 20 записей, и 100 записей делятся на 5 страниц ровно.
`40960 = 5 × 8192` — файл после вставок.

**Удаление не трогает файл.** 80 удалений превратили первые четыре страницы в
надгробия, но `filesize()` не сдвинулся: вернулись бы лишь 4 байта на запись, а
упаковка — это перезапись, а не правка. Четыре страницы теперь свободны, и ни
одна об этом не знает.

**Вставки идут мимо.** Последняя страница заполнена (102 байта не вмещают 404),
поэтому новые записи выделяют страницы 5 и 6 — файл дорастает до
`57344 = 7 × 8192` байт, перерастая пространство, которым владеет сам. Ровно то,
что декларирует `HeapFile`: insert смотрит в конец файла, а карты свободного
места нет.

**VACUUM возвращает целые страницы.** `32768 = 4 × 8192` — ровно четыре
освобождённые страницы, потому что 60 выживших записей снова делятся на три
страницы по 20. Возвращаются не «байты записей», а страницы целиком: упаковка
переписывает файл с нуля.

**Адреса все сдвинулись.** `row090` жил на странице 4 в слоте 10 и после
`vacuum()` — на странице 0 в слоте 10. Слот мог сохраниться случайно (порядок
выживших не изменился), но страница — нет, и попытка прочитать старый адрес
честно падает: страницы 4 в файле из трёх страниц больше нет. Именно поэтому
документация требует пересобирать все индексы после `vacuum()` — в индексах
лежат эти адреса, и они отстали от данных.

**Оборванный файл узнаётся по размеру.** Один недописанный байт — и `24575` не
делится на 8192: `HeapFile::open()` отказывается открывать файл и называет
точную причину. Это и есть третья хрупкость из «Проблемы», превращённая в
свойство: частичная запись страницы не может быть перепутана с целой, потому что
весь формат кратен 8192.

### 3. Рост B-дерева

Третий вопрос: как выглядит дерево, когда оно растёт. Скрипт вставляет 2000
ключей в некуникативный индекс (значения −40…39, каждое ровно 25 раз) и печатает
строку при каждом структурном изменении, затем идёт по цепочке листьев, ищет все
80 значений и удаляет одно из них целиком:

```bash
docker compose cp /tmp/btree-growth.php php:/tmp/btree-growth.php
docker compose exec php php /tmp/btree-growth.php
```

```php
<?php

declare(strict_types=1);

/**
 * A B+Tree growing: what a "node" is, what a split costs, and what the
 * tree deliberately does not do.
 *
 * Every node is one 8 KiB page — the same slotted page a heap stores rows
 * in — and the only page that knows it belongs to a tree is page 0, the
 * meta page holding the current root. Insert 2000 keys and you can watch
 * the file grow pages, the root change identity when the tree gains a
 * level, and the leaves link into a chain that yields keys in order.
 *
 * Three properties the rest of the database leans on are printed at the
 * end: a repeated value never becomes two byte-identical keys (the stored
 * key carries the row's 8-byte RecordId), the value's bytes preserve
 * numeric order (so strcmp() on raw bytes IS the sort), and a delete never
 * rebalances anything.
 *
 * Run inside the container, where the project is mounted at /app:
 *
 *   docker compose cp /tmp/btree-growth.php php:/tmp/btree-growth.php
 *   docker compose exec php php /tmp/btree-growth.php
 */

require '/app/vendor/autoload.php';

use PhpMiniDatabase\Exception\ConstraintViolationException;
use PhpMiniDatabase\Schema\Type\IntType;
use PhpMiniDatabase\Storage\BTreeIndex;
use PhpMiniDatabase\Storage\PageManager;
use PhpMiniDatabase\Storage\PageType;
use PhpMiniDatabase\Storage\RecordId;

const ROWS = 2000;
const DISTINCT = 80;
const OFFSET = 40;

/** [root page id, internal pages, leaf pages, height] of the live tree. */
function shape(PageManager $pages): array
{
    $meta = $pages->read(0);
    $root = unpack('Nid', substr((string) $meta->read(0), 1))['id'];

    $internal = 0;
    $leaves = 0;
    $height = 0;
    $level = [$root];

    while ($level !== []) {
        $next = [];

        foreach ($level as $pageId) {
            $page = $pages->read($pageId);

            if ($page->type === PageType::BTREE_LEAF) {
                $leaves++;
                continue;
            }

            $internal++;

            foreach ($page->occupiedSlots() as $slot) {
                $next[] = unpack('Nchild', substr((string) $page->read($slot), -4))['child'];
            }
        }

        $height++;
        $level = $next;
    }

    return [$root, $internal, $leaves, $height];
}

/** Every leaf page id, in the order the chain links them. */
function leafChain(PageManager $pages): array
{
    $pageId = shape($pages)[0];

    while ($pages->read($pageId)->type === PageType::BTREE_INTERNAL) {
        $page = $pages->read($pageId);
        $pageId = unpack('Nchild', substr((string) $page->read($page->occupiedSlots()[0]), -4))['child'];
    }

    $chain = [];

    while ($pageId !== 0xFFFFFFFF) {
        $chain[] = $pageId;
        $page = $pages->read($pageId);
        $pageId = unpack('Nid', substr((string) $page->read(0), 1))['id'];
    }

    return $chain;
}

$type = new IntType();
$path = sys_get_temp_dir() . '/ch15-index.idx';

@unlink($path);

$pages = new PageManager($path);
$tree = new BTreeIndex($pages, $type);
$value = static fn (int $i): int => ($i % DISTINCT) - OFFSET; // -40 .. 39

echo "keys in order of insertion — a line per structural change:\n";

$before = null;

for ($i = 0; $i < ROWS; $i++) {
    $tree->insert($value($i), new RecordId(intdiv($i, 40), $i % 40));
    $now = shape($pages);

    if ($now !== $before) {
        [$root, $internal, $leaves, $height] = $now;
        printf(
            "after %4d keys: root=%d, %d internal, %2d leaves, height %d, %d pages\n",
            $i + 1,
            $root,
            $internal,
            $leaves,
            $height,
            $pages->pageCount(),
        );
        $before = $now;
    }
}

echo "\nfirst key of each leaf, following the chain:\n";

foreach (leafChain($pages) as $pageId) {
    $page = $pages->read($pageId);
    $first = null;

    foreach ($page->occupiedSlots() as $slot) {
        $record = (string) $page->read($slot);

        if ($record[0] === "\x01") {
            $first = $record;
            break;
        }
    }

    printf(
        "  leaf %d: key bytes %s -> value %d\n",
        $pageId,
        bin2hex(substr((string) $first, 1, 4)),
        $type->decode(substr((string) $first, 1, 4))[0],
    );
}

echo "\nthe same value bytes as plain numbers:\n";

foreach ([-1, 0, 1] as $n) {
    printf("  %d -> %s\n", $n, bin2hex($type->encode($n)));
}

$found = 0;
$wrong = 0;

for ($v = -OFFSET; $v < DISTINCT - OFFSET; $v++) {
    $matches = iterator_count($tree->search($v));
    $found += $matches;
    $wrong += $matches === ROWS / DISTINCT ? 0 : 1;
}

printf(
    "\nsearching all %d values: %d of %d rows found, %d value(s) wrong\n",
    DISTINCT,
    $found,
    ROWS,
    $wrong,
);

[$root, $internal, $leaves, $height] = shape($pages);
$pagesBefore = $pages->pageCount();
$deleted = 0;

for ($i = 0; $i < ROWS; $i++) {
    if ($value($i) === -OFFSET) {
        $tree->delete(-OFFSET, new RecordId(intdiv($i, 40), $i % 40));
        $deleted++;
    }
}

printf(
    "after %d deletes of one value: %d pages -> %d pages, height %d -> %d, %d row(s) left\n",
    $deleted,
    $pagesBefore,
    $pages->pageCount(),
    $height,
    shape($pages)[3],
    iterator_count($tree->search(-OFFSET)),
);

// A unique index is the other half of the story: the same value twice is a
// constraint violation, and a NULL is never in the index at all.
$uniquePath = sys_get_temp_dir() . '/ch15-unique.idx';

@unlink($uniquePath);

$unique = BTreeIndex::open($uniquePath, new IntType(), true);
$unique->insert(7, new RecordId(0, 0));

try {
    $unique->insert(7, new RecordId(0, 1));
    echo "a second 7 was accepted, which should be impossible\n";
} catch (ConstraintViolationException $e) {
    printf("\nsecond insert of 7 -> ConstraintViolationException: %s\n", $e->getMessage());
}

$unique->insert(null, new RecordId(0, 2));
printf("a NULL is never indexed: search(null) finds %d row(s)\n", iterator_count($unique->search(null)));

@unlink($path);
@unlink($uniquePath);
```

**Перевод комментариев:**
- `btree-growth.php` (PHPDoc): рост B+Tree: что такое «узел», сколько стоит
  сплит и чего дерево сознательно не делает. Каждый узел — страница на 8 КиБ, та
  же слотовая страница, в которой куча хранит строки; единственная страница,
  знающая о принадлежности дереву, — страница 0, мета-страница с текущим корнем.
  Вставьте 2000 ключей, и видно, как файл прирастает страницами, корень меняет
  идентичность, когда дерево получает уровень, а листья связываются в цепочку,
  отдающую ключи по порядку. В конце напечатаны три свойства, на которые
  опирается остальная база: повторяющееся значение никогда не превращается в два
  байтово одинаковых ключа (хранимый ключ несёт 8-байтовый `RecordId` строки),
  байты значения сохраняют числовой порядок (так что `strcmp()` по сырым байтам
  и есть сортировка), а удаление никогда ничего не ребалансирует.
- `shape()` (PHPDoc):
  `[id корневой страницы, внутренние страницы, листовые страницы, высота]`
  живого дерева.
- `leafChain()` (PHPDoc): id каждой листовой страницы в том порядке, в котором
  их связывает цепочка.
- `// A unique index is the other half of the story: the same value twice is a`
  — уникальный индекс — другая половина истории: то же значение дважды это
  нарушение ограничения, а `NULL` вообще никогда не попадает в индекс.

```text
keys in order of insertion — a line per structural change:
after    1 keys: root=1, 0 internal,  1 leaves, height 1, 2 pages
after  481 keys: root=3, 1 internal,  2 leaves, height 2, 4 pages
after  954 keys: root=3, 1 internal,  3 leaves, height 2, 5 pages
after  968 keys: root=3, 1 internal,  4 leaves, height 2, 6 pages
after 1879 keys: root=3, 1 internal,  5 leaves, height 2, 7 pages
after 1902 keys: root=3, 1 internal,  6 leaves, height 2, 8 pages
after 1960 keys: root=3, 1 internal,  7 leaves, height 2, 9 pages

first key of each leaf, following the chain:
  leaf 1: key bytes 7fffffd8 -> value -40
  leaf 5: key bytes 7fffffeb -> value -21
  leaf 6: key bytes 7ffffff5 -> value -11
  leaf 2: key bytes 7fffffff -> value -1
  leaf 8: key bytes 80000009 -> value 9
  leaf 4: key bytes 80000013 -> value 19
  leaf 7: key bytes 8000001d -> value 29

the same value bytes as plain numbers:
  -1 -> 7fffffff
  0 -> 80000000
  1 -> 80000001

searching all 80 values: 2000 of 2000 rows found, 0 value(s) wrong
after 25 deletes of one value: 9 pages -> 9 pages, height 2 -> 2, 0 row(s) left

second insert of 7 -> ConstraintViolationException: Duplicate key "7" in a unique index.
a NULL is never indexed: search(null) finds 0 row(s)
```

**Первый сплит — на 481-й вставке.** Ровно та цифра, что следует из макета:
запись листа 13 байт плюс 4 байта слота, после служебного первого слота свободно
8173 байта, то есть 480 записей (`9 + 480 × 17 = 8169 ≤ 8182`, а 481-я
потребовала бы 8186). На этой вставке страница 1 (корень-лист) разделилась,
корнем стал узел 3 — тот единственный момент, когда `BTREE_META` переписывается,
— и высота выросла с 1 до 2. Дальше листья делятся неравномерно (954, 968,
1879...), потому что граница рвёт последовательность ключей там, где середина
случайно выпала.

**Цепочка листьев идёт по ключам, а не по номерам страниц.** Листья в порядке
связей: 1 → 5 → 6 → 2 → 8 → 4 → 7. Номера не монотонны — страницы выделяются в
момент сплитов, а порядок задают ключи. Первый ключ каждого листа — 7fffffd8,
7fffffeb, ..., 7fffffff, 80000009: **возрастающая последовательность байтов,
переходящая через ноль**. Это и есть «порядок байтов = порядок значений»:
0x7fffffff — это −1, а 0x80000000 — уже 0. В обычном двоичном дополнении −1
записался бы как `ffffffff` и сортировался после всех; инверсия знакового бита
возвращает его на место. Спуск в дереве и сравнение в диапазонном обходе
работают на этих сырых байтах.

**Дубликаты полностью находимы.** Каждое из 80 значений встречается 25 раз и
разбросано по всей последовательности вставок, то есть после сплитов лежит на
разных листах. Поиск вернул 2000 записей из 2000. Это проверка того самого
исторического исправления: значения бы в дереве повторялись, и поиск после
сплита дошёл бы только до правой части; пришитый `RecordId` делает каждую из
2000 записей уникальным ключом, а поиск по значению — обходом диапазона.

**Удаление ничего не уплотняет.** 25 удалений одного значения вынули записи из
листов, но дерево осталось 9 страниц и высоты 2: ни одно ребалансирование не
произошло, и освободившиеся слоты просто стали надгробиями внутри листов. Поиск
вернул 0 — дерево осталось корректным, просто рыхлым, и восстановит плотность
только пересборкой.

**Уникальность и NULL — на том же механизме.** Вторая вставка того же значения в
уникальный индекс отклонена — с тем же `scanPrefix()` в роли проверки: если бы
поиск мог не найти первого значения, второе было бы принято. А `NULL` не
попадает в индекс вообще: трёхзначная логика уже говорит, что `x = NULL` никогда
не истинно, и находить по NULL нечего.

### 4. Индекс против последовательного обхода

Последний вопрос: сколько это стоит в работе SQL-слоя. Скрипт проекта строит
таблицу на 5000 строк и делает 200 точечных выборок дважды: по
проиндексированному `id` и по `id2` — столбцу с теми же значениями, но без
индекса, то есть через последовательный обход:

```bash
docker compose exec php php benchmarks/index_vs_seqscan.php
```

```text
Inserting 5000 rows...
Insert: 19.826s (252 rows/s)

200 point lookups, 5000 rows in the table:
  IndexScan (id):   0.1102s total, 0.5511ms/lookup
  SeqScan   (id2):  7.8752s total, 39.3758ms/lookup
  Speedup:          71.4x
```

**71.4× в пользу индекса** — и это консервативная оценка: обе величины растут
линейно с размером таблицы, поэтому разрыв при большем объёме будет шире.
Обратите внимание на вставку: 252 строки в секунду — это цена автокоммита, где
каждая строка это отдельная транзакция со своим `fsync()`; глава 18 покажет,
откуда берётся разница между этим числом и 392 строками/с в пакетной вставке.

Этот прогон — единственный из четырёх, который не воспроизводится побайтово: это
замер времени, и при повторном запуске на той же машине получилось 20.467 с
вставки и 68.0× ускорения. Три остальных эксперимента детерминированы: их вывод
— те же байты от прогона к прогону. Почему 0.55 мс на выборку здесь заметно
медленнее, чем точечный поиск в `SelectBench` из раздела «Цифры», разобрано там
же.

## 5. Отказы

| Что ломается                                         | Чем обнаруживается                                                                                 | Что происходит                                                                                                    |
| ---------------------------------------------------- | -------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Запись крупнее 8178 байт                             | `Page::assertStorable()` → `StorageException`                                                      | отказ вместо страниц-переливов и вместо тихой обрезки                                                             |
| Файл не кратен 8192                                  | конструктор `PageManager` → `StorageException` с точным размером                                   | оборванная запись не может быть принята за целую страницу                                                         |
| Читается страница за пределами файла                 | `PageManager::read()` → «Page N is outside» (видели в эксперименте 2)                              | обращение к устаревшему адресу падает, а не возвращает чужие данные                                               |
| `fread()` вернул меньше страницы                     | цикл до полного чтения; пустое чтение → «ends early»                                               | короткое чтение ≠ повреждение; повреждение — только когда файл кончился                                           |
| `fwrite()` написал не всё                            | «Short write of page N»                                                                            | запись считается несделанной                                                                                      |
| Страница с чужим номером или неизвестным типом       | `Page::fromBytes()` → `StorageException`                                                           | читатель узнаёт, что оказался не там, до разбора чужих байтов                                                     |
| Слот указывает в заголовок или в директорию          | `offset < freeEnd` → `StorageException`                                                            | сломанная директорная запись не молча читается как данные                                                         |
| Обновление, которое не влезает в страницу            | `Page::update()` → `false`                                                                         | строка удаляется и вставляется заново, возвращается **новый** `RecordId`; хранители старого обязаны перестроиться |
| `UPDATE` строки, которую не вместит ни одна страница | `assertStorable()` до удаления                                                                     | исключение, старая строка остаётся на месте                                                                       |
| `VACUUM` при живых индексах                          | все `RecordId` сдвинуты                                                                            | индексы обязаны быть пересобраны; чтение по старому адресу падает «outside»                                       |
| `BTreeIndex::delete()` недоуплотнённого листа        | нет исключений — нет и ребалансирования                                                            | дерево остаётся корректным, но рыхлым; место возвращает `DROP INDEX` + `CREATE INDEX`                             |
| Дубликат в уникальном индексе                        | `ConstraintViolationException`                                                                     | проверка идёт тем же `scanPrefix()`, что и поиск                                                                  |
| Сплит с дубликатами значения                         | исторический баг: `search()` возвращал часть строк                                                 | ключ = значение + `RecordId`; ловится `testNoKeyCanBeDuplicatedOnceTheLeavesHaveSplit`                            |
| Префиксная серия, начинающаяся за границей листа     | исторический баг: ключ «исчезал» после каждого сплита, а `INSERT` принимал повторный `PRIMARY KEY` | `scanPrefix()` останавливается по доказательству, а не по отсутствию совпадения                                   |

Последние две строки — не гипотетические, а пройденные проектом, и обе записаны
в `docs/DECISIONS.md`. Первая история: дерево сначала хранило значение без
суффикса, и «a stress test inserting many rows under one repeated key found it
broken: `search()` for that key returned only a fraction of the rows that
actually had it». Вторая породила худшее наблюдение главы:
`SELECT ... WHERE id = ?` по первичному ключу не находил строку, которая лежала
в таблице и отвечала на `COUNT(*)`, — «about 1% of rows at a thousand, one per
split, the same ids every run». Обе ошибки читались снаружи как потери данных
приложения, хотя на диске не пропало ни байта.

## 6. Как это проверено

`tests/Unit/Storage/` — 103 теста, и в них видна та же логика, что и в главе:
страница проверяется по байтам, а не только по поведению, потому что «a page
that round-trips but writes a different layout than documented would pass a
purely behavioural suite and break every reader written against the format»
(докблок `PageTest`).

`PageTest.php` — 18 тестов. Формат: `testAnEmptyPageIsExactlyOnePageLong`,
`testHeaderCarriesIdTypeAndSlotCount` (вручную разбирает заголовок и сверяет
`freeEnd` с `SIZE − 5`), `testRecordsRoundTripThroughBytes` (в том числе
бинарная запись с `\x00` и `\xff`). Слоты и надгробия:
`testSlotNumbersStartAtZeroAndCountUp`,
`testDeletingANeighbourLeavesOtherSlotsAddressable`,
`testDeletedSlotIsReusedBeforeTheDirectoryGrows`,
`testOccupiedSlotsSkipsTombstones`, `testDeletingTwiceIsAnError`. Потолки и
отказы: `testARecordLargerThanAPageIsRefused`,
`testARecordOfExactlyTheMaximumFits`,
`testInsertReturnsNullOnceTheRecordNoLongerFits`,
`testFreeSpaceAccountsForTheDirectoryEntry`, плюс три теста на повреждённые
байты: `testBytesOfTheWrongLengthAreRejected`,
`testAPageReadAtTheWrongNumberIsRejected`, `testAnUnknownPageTypeIsRejected`,
`testASlotPointingOutsideTheRecordAreaIsRejected`.

`PageManagerTest.php` — 8 тестов: `testPagesLandAtTheirOwnOffsetInTheFile`
(страницы лежат по N × 8192),
`testAllocatedPagesAreNumberedInOrderAndOnlyCountOnceWritten` (страница
существует только после записи), `testReadingBeyondTheEndIsRejected`,
`testWritingPastTheEndOfTheFileIsRejected`,
`testAFileThatIsNotAWholeNumberOfPagesIsRejected` — тот самый отказ, который
показал эксперимент 2.

`HeapFileTest.php` — 18 тестов. Форма файла в работе:
`testRecordsSpillOntoFurtherPages`, `testUpdateInPlaceKeepsTheRecordId`,
`testARecordThatOutgrowsItsPageMovesAndReportsItsNewId`,
`testAnUpdateTooLargeForAnyPageLeavesTheRecordUntouched`. VACUUM и перезапись:
`testVacuumDropsDeletedRecordsAndShrinksTheFile`,
`testVacuumLeavesTheFileWritable`, `testVacuumLeavesNoTemporaryFileBehind`,
`testRewritePutsEveryRecordThroughTheTransform`,
`testAFailedRewriteLeavesTheOriginalFileUntouched` — сбой преобразования
оставляет оригинал нетронутым, что и требует докблок `rewrite()`.

`BTreeIndexTest.php` — 20 тестов и 526 утверждений: вся работа поиска приходится
на них. Ключевые:

- `testManyInsertsForceSplitsAndEverythingStaysFindable` — 2000 вставок и
  проверка **каждого** ключа, не выборки; в комментарии теста прямо написано,
  почему: «one key per split used to disappear, and sampling walked straight
  past all of them»;
- `testADuplicateKeySpanningALeafSplitIsStillFullyFindableAndDeletable` — 1000
  строк с одним и тем же значением, после сплитов все 1000 находимы, удаление
  одного убирает ровно один;
- `testNoKeyCanBeDuplicatedOnceTheLeavesHaveSplit` — 600 ключей в уникальном
  индексе, и попытка вставить каждый из них повторно: ни одна не должна быть
  принята (та же проверка, что и поиск);
- `testDeletingAcrossManyEntriesLeavesTheRestFindable` — удаление через один без
  ребалансирования, оставшиеся находимы;
- `testRange*` — пять тестов на границы диапазона, включая
  `testUnboundedRangeVisitsEveryEntryInOrder` (упорядоченность цепочки листьев);
- `testStringKeysCompareLexicallyNotByLength` — VARCHAR-ключи: лексика, а не
  длина.

`RecordSerializerTest.php` (7 тестов) и `CatalogTest`, `TableSchemaCodecTest`,
`RecordIdTest` закрывают остальное хранилище.

Запускаем:

```text
$ docker compose exec php php vendor/bin/phpunit tests/Unit/Storage
OK (103 tests, 654 assertions)

$ make test
OK (1082 tests, 3338 assertions)
```

Отдельно по файлам: `PageTest` 18 тестов / 35 утверждений, `PageManagerTest` 8 /
15, `HeapFileTest` 18 / 30, `BTreeIndexTest` 20 / 526, `RecordSerializerTest` 7
/ 7.

Эксперименты выше — четыре живых прогона: анатомия страницы, куча с `vacuum()`,
рост дерева на 2000 ключей и бенчмарк «индекс против обхода».

## 7. Цифры

Размеры формата — из кода `Storage\Page` и `docs/storage.md`:

| Что                          | Значение                                                                          |
| ---------------------------- | --------------------------------------------------------------------------------- |
| Размер страницы              | 8192 байта                                                                        |
| Заголовок страницы           | 10 байт: `pageId` uint32, `type` uint16, `slotCount` uint16, `freeEnd` uint16     |
| Директорная запись           | 4 байта: `offset` uint16, `length` uint16                                         |
| Потолок записи               | 8178 байт (`8192 − 10 − 4`), страниц-переливов нет                                |
| Запись листа B-дерева        | 13 байт (тег + 4 байта `INT` + 8 байт `RecordId`) + 4 байта слота                 |
| Вместимость листа            | 480 ключей (`9 + 480 × 17 = 8169 ≤ 8182`) — подтверждено сплитом на 481-й вставке |
| Вместимость внутреннего узла | 390 детей (`9 + 389 × 21 = 8178 ≤ 8182`)                                          |
| Высота 2 при этих числах     | до ~187 000 ключей (480 × 389)                                                    |
| Адрес записи                 | `RecordId` = 8 байт в ключе индекса, строка «page:slot» в журнале                 |
| Кодировка `INT`              | 4 байта big-endian, знаковый бит инвертирован                                     |

Живые прогоны этой главы (Docker, PHP 8.5.11):

| Что                            | Значение                                                                                                                                      |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| Страница на 3 записях          | `freeSpace` 8182 → 8157; удаление вернуло 3 байта, не 7; повторная вставка — слот 1 без роста директории                                      |
| Потолок записи                 | 8178 байт — встаёт, 8179 — `StorageException`                                                                                                 |
| 100 записей по 400 байт        | 5 страниц, 40 960 байт                                                                                                                        |
| 80 удалений                    | файл не изменился: 5 страниц, 40 960 байт                                                                                                     |
| 40 вставок после удалений      | 7 страниц, 57 344 байта — четыре освобождённые страницы не использованы                                                                       |
| `vacuum()`                     | возвращено 32 768 байт (4 страницы), итог 3 страницы, 24 576 байт                                                                             |
| Адрес `row090`                 | `4:10` до `vacuum()`, `0:10` после; чтение старого адреса — отказ                                                                             |
| Оборванный файл                | 24 575 байт — отказ при открытии                                                                                                              |
| Дерево на 2000 ключах          | 1 мета-страница + 1 внутренний + 7 листьев = 9 страниц, высота 2                                                                              |
| Первый сплит                   | на 481-й вставке, ровно по расчёту вместимости листа                                                                                          |
| Поиск всех 80 значений         | 2000 из 2000 найдено                                                                                                                          |
| 25 удалений одного значения    | 9 страниц → 9 страниц, высота 2 → 2                                                                                                           |
| 5000 строк, точечные выборки   | `IndexScan` 0.5511 мс/выборка, `SeqScan` 39.3758 мс/выборка, ускорение 71.4× (повторный прогон: 0.5776 мс, 39.2818 мс, 68.0× — замер времени) |
| Тесты хранилища                | 103 теста, 654 утверждения                                                                                                                    |
| Весь набор `php-mini-database` | 1082 теста, 3338 утверждений                                                                                                                  |

Бенчмарки проекта (`make bench`, 11 прогонов, от `composer test` они отделены
намеренно):

| Что                                        | Значение                            |
| ------------------------------------------ | ----------------------------------- |
| `INSERT`, автокоммит (1 строка = 1 коммит) | 2000 строк за 8.432 с, 237 строк/с  |
| `INSERT`, одной транзакцией                | 2000 строк за 5.100 с, 392 строки/с |
| `INSERT` с вторичным индексом              | 2000 строк за 11.081 с, 180 строк/с |
| Полный обход (`COUNT` по 5000 строк)       | 0.040 с                             |
| Точечный поиск по `PRIMARY KEY`            | 7303 операции/с                     |
| Точечный поиск по вторичному индексу       | 7160 операций/с                     |

Числа согласуются между собой: автокоммитные 237 строк/с из `InsertBench` и 252
строки/с из `benchmarks/index_vs_seqscan.php` — одна и та же стоимость одной
транзакции на одной машине; разница между «392 строки/с одной транзакцией» и
«237 строк/с по одной» — это и есть цена `fsync()` на каждую строку, которую
глава 18 разберёт как WAL. Точечный поиск по первичному ключу в `SelectBench`
(7303 операции/с) быстрее, чем в отдельном бенчмарке (0.5511 мс ≈ 1815
выборок/с): второй гоняет полный `SELECT *` через планировщик с параметром и
замеряет 200 выборок на 5000 строк, то есть меряет и диспетчеризацию запроса, а
не только обход дерева.

Документированные характеристики проекта — из `README.md`: PHPStan level 8 без
ошибок, покрытие строк около 90%, 20 из 20 вех завершены, а сам проект определён
как «a completed educational database engine rather than an unfinished
prototype».

## 8. Упражнения

1. **Посчитайте.** Запись 400 байт стоит 404 байта с директорной записью.
   Сколько их влезает в страницу и почему не 20.25? Сколько страниц займет
   таблица на 1 000 000 таких записей после `vacuum()`? (Ответ: 20 на страницу;
   ~50 000 страниц, то есть ~390.6 MiB (409.6 МБ).)
2. **Посчитайте.** Лист держит 480 ключей, внутренний узел — 390 детей. При
   каком количестве строк высота 2 перестанет хватать? Сколько страниц и байт
   займёт такой индекс? (Ответ: больше ~187 000 строк; ~390 страниц, то есть
   ~3.1 МБ.)
3. **Объясните.** `Page::insert()` возвращает `null`, когда места нет, а
   `HeapFile::insert()` превращает это в новую страницу. Почему ошибка выражена
   возвратом, а не исключением, — и что пришлось бы сделать в `BTreeIndex`, если
   бы страница бросала исключение на полноте?
4. **Найдите ошибку.** Представьте, что `toBytes()` кладёт данные не от конца
   страницы, а от заголовка вверх, начиная с байта 10. Что сломается в этой
   схеме — и почему маркер «надгробие = offset 0» перестанет быть корректным?
   Как тогда помечать удалённый слот?
5. **Спроектируйте.** Карта свободного места: где её хранить, что обновлять при
   каждой вставке и каждом удалении и как согласовать её с страницами после
   сбоя. Почему `docs/DECISIONS.md` называет её «второй структурой, которую
   придётся держать согласованной в каждой фазе после этой» — и что придётся
   писать в WAL дополнительно к страницам?
6. **Разберите отказ.** Тест
   `testManyInsertsForceSplitsAndEverythingStaysFindable` проверяет каждый ключ,
   а не выборку, и в комментарии сказано, что выборка из шести ключей проходила
   мимо бага. Опишите конкретную последовательность (2000 вставок → сплит →
   поиск), при которой шесть случайных ключей не находят бы ни одного из
   «исчезнувших» — и почему исчезавшие ключи всегда одни и те же.
7. **Сравните.** `HeapFile::update()` возвращает новый `RecordId`, если строка
   переехала. Если бы адресом строки был смещение байта в файле, сколько
   хранителей этого адреса пришлось бы обновить при каждом переезде — и почему
   именно B-дерево делает такое сравнение невозможным по стоимости?

## Что дальше

- **Глава 16** — SQL от текста к плану выполнения: как `SELECT` превращается в
  вызовы `SeqScan` и `IndexScan`, и почему выбор между ними — вопрос о том, есть
  ли на таблице индекс.
- **Глава 17** — транзакции и изоляция: страница из этой главы становится
  объектом блокировки, а `RecordId` — тем адресом, который называет журнал.
- **Глава 18** — WAL, fsync и восстановление: почему запись страницы всегда
  целиком, что такое оборванная запись и как с ней жить.
- **Глава 20** — инвертированный индекс и BM25 в `php-mini-search`: тот же
  принцип «значение → адреса», но живущий в памяти и без страниц.
- **Глава 26** — как проверять тесты: почему зелёный `kill -9`-тест ещё не
  доказывает, что он способен упасть.
