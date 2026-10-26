# Глава 7. External sorting: память ограничивает chunk, а не вход

> Проект: [`algorithms-lab`](https://github.com/Researcher86/algorithms-lab),
> тег
> [`v1.0.0`](https://github.com/Researcher86/algorithms-lab/releases/tag/v1.0.0).
> Предыдущая глава: [top-K и потоки](06-top-k-and-stream-processing.md). Это
> последняя глава книги; дальше — интеграционный сценарий.

Внешняя сортировка нужна, когда файл больше памяти. Идея проста: прочитать
столько записей, сколько помещается, отсортировать их, записать отсортированный
прогон на диск и затем слить прогоны. Сложность начинается в деталях: сколько
данных читать за раз, сколько прогонов открывать, сколько проходов слияния
получится и как сохранить устойчивость при равных ключах.

Главный тезис: память ограничивает **chunk**, а не размер входа. Но chunk — это
не «столько байт PHP-памяти». Массив строк, буферы и заголовки занимают больше,
чем сумма длин записей. В этой главе 128 MiB чанка измеряются как 291.4 MiB PHP
peak, а 500 MiB вход сортируется под лимитом 128M с chunk 32 MiB и fan-in 32.

---

## 1. Проблема

`file()` + `usort()` требует `O(N)` памяти: весь файл оказывается в массиве
строк. Если файл 10 GiB, а `memory_limit` 512M, это не сработает, даже если
сортировка `O(N log N)`.

External merge sort разбивает работу на три фазы:

1. **chunk**: читаем часть входа, пока не достигнем бюджета, сортируем и пишем
   на диск как run;
2. **merge**: сливаем несколько run в один, пока не останется один;
3. **output**: последний merge пишет результат.

Число проходов зависит от числа начальных run `R` и fan-in `F`: `ceil(log_F R)`
(с финальным merge как отдельным проходом в этой реализации). Чем больше chunk,
тем меньше run и проходов, но тем больше живая память. Чем больше fan-in, тем
меньше проходов, но тем больше открытых файлов и голов run одновременно.

Устойчивость (stability) — отдельное требование. Если ключи равны, порядок
записей должен сохраниться. PHP `usort` стабилен внутри chunk; при слиянии нужен
tie-break по номеру run, иначе равные записи из разных run могут переставиться.

---

## 2. Устройство

### 2.1. ChunkReader: бюджет по байтам

`ChunkReader` принимает iterable строк и группирует их в chunk по
приблизительному размеру: длина записи плюс один байт на newline. Если
добавление следующей записи превысит лимит, текущий chunk выдаётся, а новая
запись начинает следующий. Одна запись больше лимита всё равно попадёт в chunk —
иначе её некуда деть.

```php
    public function chunks(iterable $records): Generator
    {
        $chunk = [];
        $bytes = 0;

        foreach ($records as $record) {
            $size = strlen($record) + 1;

            if ($chunk !== [] && $bytes + $size > $this->chunkBytes) {
                yield $chunk;
                $chunk = [];
                $bytes = 0;
            }

            $chunk[] = $record;
            $bytes += $size;
        }

        if ($chunk !== []) {
            yield $chunk;
        }
    }
```

[`src/ExternalSort/ChunkReader.php` L32-L53](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/ExternalSort/ChunkReader.php#L32-L53)

Этот класс — граница памяти. Он не знает, сколько всего записей; он держит
только текущий chunk. Именно поэтому вход может быть больше памяти.

### 2.2. Sort: chunk → spill → merge

`ExternalSorter::sort()` читает chunk, сортирует его `usort`, пишет run во
временный файл и накапливает пути. Когда run больше `fanIn`, сортировщик сливает
их группами по `fanIn` в новые временные run, удаляет старые и повторяет.
Финальный merge пишет в выходной файл. Счётчик `passes` увеличивается на каждом
групповом merge и на финальном; поэтому даже один run проходит через один
финальный merge.

```php
    public function sort(iterable $records, string $outputPath): SortStats
    {
        $this->order->reset();
        $this->temporaryFiles = [];
        $runs = [];
        $recordCount = 0;
        $bytesRead = 0;
        $bytesWritten = 0;
        $chunks = 0;

        try {
            $reader = new ChunkReader($this->chunkBytes);

            foreach ($reader->chunks($records) as $chunk) {
                ++$chunks;
                $recordCount += count($chunk);

                foreach ($chunk as $record) {
                    $bytesRead += strlen($record) + 1;
                }

                usort($chunk, $this->compare);
                $runs[] = $this->writeRun($chunk, $bytesWritten);
            }

            if ($runs === []) {
                $this->writeEmpty($outputPath);

                return $this->stats($recordCount, 0, 0, $bytesRead, $bytesWritten);
            }

            $passes = 0;
            $fanIn = $this->fanIn;

            if ($fanIn < 2) {
                throw new LogicException('Fan-in must be at least 2');
            }

            while (count($runs) > $fanIn) {
                $next = [];

                foreach (array_chunk($runs, $fanIn) as $group) {
                    $next[] = $this->mergeToTemporary($group, $bytesRead, $bytesWritten);
                }

                foreach ($runs as $run) {
                    if (is_file($run)) {
                        @unlink($run);
                    }

                    $this->forget($run);
                }

                $runs = $next;
                ++$passes;
            }

            $this->merge($runs, $outputPath, $bytesRead, $bytesWritten);
            ++$passes;

            return $this->stats($recordCount, $chunks, $passes, $bytesRead, $bytesWritten);
        } finally {
            $this->cleanup();
        }
    }
```

[`src/ExternalSort/ExternalSorter.php` L87-L151](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/ExternalSort/ExternalSorter.php#L87-L151)

`SortStats` фиксирует число записей, начальных run, проходов, прочитанных и
записанных байт, сравнений, chunk и fan-in. Байты логические: это длины записей,
а не блоки файловой системы. `bytesRead` включает вход и все прочитанные run;
`bytesWritten` — все spill и output.

### 2.3. Merge: heap и stability

Слияние открывает каждый run через `LineReader::lines()`, читает первую запись,
кладёт её в `BinaryHeap` и затем извлекает минимум. После извлечения читается
следующая запись из того же run и снова кладётся в heap. Когда run
заканчивается, он удаляется из активных генераторов.

```php
        foreach ($runs as $runIndex => $run) {
            $generator = LineReader::lines($run);
            $generator->rewind();

            if (!$generator->valid()) {
                continue;
            }

            $value = (string) $generator->current();
            $bytesRead += strlen($value) + 1;
            $generators[$runIndex] = $generator;
            $heap->insert(new MergeEntry($value, $runIndex));
        }

        $handle = fopen($outputPath, 'wb');

        if ($handle === false) {
            throw new RuntimeException(sprintf('Cannot create output file: %s', $outputPath));
        }

        $buffer = '';

        try {
            while (!$heap->isEmpty()) {
                $entry = $heap->extract();

                if (!$entry instanceof MergeEntry) {
                    throw new LogicException('The merge heap returned a non-MergeEntry value');
                }

                $line = $entry->value . "\n";
                $buffer .= $line;
                $bytesWritten += strlen($line);

                if (strlen($buffer) >= 1_048_576) {
                    $this->writeAll($handle, $buffer);
                    $buffer = '';
                }

                $generator = $generators[$entry->run] ?? null;

                if ($generator === null) {
                    throw new LogicException('The merge entry points at a closed run');
                }

                $generator->next();

                if ($generator->valid()) {
                    $value = (string) $generator->current();
                    $bytesRead += strlen($value) + 1;
                    $heap->insert(new MergeEntry($value, $entry->run));
                } else {
                    unset($generators[$entry->run]);
                }
            }
```

[`src/ExternalSort/ExternalSorter.php` L219-L273](https://github.com/Researcher86/algorithms-lab/blob/v1.0.0/src/ExternalSort/ExternalSorter.php#L219-L273)

Компаратор сначала сравнивает записи пользовательской функцией, затем — номер
run. Это tie-break для равных ключей: запись из более раннего run выходит
раньше. При multi-pass merge номера run относятся к текущему проходу, но группы
сохраняют порядок, поэтому устойчивость композируется. Отдельный sequence number
в запись не добавляется.

`writeRun()` и `merge()` буферизуют вывод до 1 MiB перед `fwrite`, чтобы не
делать системный вызов на каждую строку. Это не меняет логический I/O, но влияет
на время.

---

## 3. Эксперимент

Входные записи — 76 байт: 10-значный ключ, пробел, 64-байтовый payload и
newline. 100 MiB — это 1 379 705 записей; 500 MiB — 6 898 526. Генерация
датасета исключена из времени; файлы кешируются в `var/datasets/`.

```bash
make experiment ARGS="sort:memory-limit --seed=book"
```

100 MiB, fan-in 8:

|   chunk | runs | passes | elapsed | logical I/O |  MiB/s |  PHP peak |
| ------: | ---: | -----: | ------: | ----------: | -----: | --------: |
|   8 MiB |   13 |      2 | 5.389 s |   600.0 MiB | 111.33 |  29.6 MiB |
|  32 MiB |    4 |      1 | 3.954 s |   400.0 MiB | 101.15 | 118.3 MiB |
| 128 MiB |    1 |      1 | 3.190 s |   400.0 MiB | 125.38 | 291.4 MiB |

8 MiB требует два прохода и переписывает данные лишний раз: 600 MiB логического
I/O против 400 MiB. 128 MiB быстрее, но PHP peak 291.4 MiB, а не 128 MiB. Под
лимитом 128M такой chunk не помещается. 32 MiB — компромисс: 118.3 MiB peak и
один проход.

Fan-in, 100 MiB, 8 MiB chunk, 13 начальных run:

| fan-in | runs | passes | open files | elapsed | logical I/O |  MiB/s | PHP peak |
| -----: | ---: | -----: | ---------: | ------: | ----------: | -----: | -------: |
|      2 |   13 |      4 |          2 | 6.370 s | 1 000.0 MiB | 156.98 | 29.6 MiB |
|      8 |   13 |      2 |          8 | 5.320 s |   600.0 MiB | 112.79 | 29.6 MiB |
|     32 |   13 |      1 |         13 | 4.985 s |   400.0 MiB |  80.23 | 29.6 MiB |
|    128 |   13 |      1 |         13 | 4.647 s |   400.0 MiB |  86.07 | 29.6 MiB |

`MiB/s` для fan-in 2 выше, потому что в числителе больше проходов; по wall time
он хуже. Fan-in 32 и 128 не уменьшают число проходов дальше, потому что run
всего 13. Открывать больше файлов, чем есть run, бессмысленно.

Шаблоны входа, 10 000 записей, 16 KiB chunk, fan-in 4:

| pattern   | records | runs | passes | external | in-memory | checksum | PHP peak |
| --------- | ------: | ---: | -----: | -------: | --------: | -------- | -------: |
| random    |  10 000 |   47 |      3 |  0.091 s |   0.012 s | ok       |  0.8 MiB |
| sorted    |  10 000 |   47 |      3 |  0.079 s |   0.009 s | ok       |  0.8 MiB |
| reversed  |  10 000 |   47 |      3 |  0.080 s |   0.013 s | ok       |  0.8 MiB |
| duplicate |  10 000 |   47 |      3 |  0.096 s |   0.011 s | ok       |  0.8 MiB |

Сортировщик не ищет pre-sorted fast path: все четыре паттерна идут через
chunk-sort-spill-merge. `duplicate` — тест устойчивости: много равных ключей, и
checksum совпадает со стабильным in-memory `usort`. На маленьком входе in-memory
сортировка быстрее; это ожидаемо и не является аргументом против external sort.

I/O и 128M:

|   input | limit | runs | passes |  elapsed |        read |     written | logical I/O | MiB/s |  PHP peak |
| ------: | ----: | ---: | -----: | -------: | ----------: | ----------: | ----------: | ----: | --------: |
| 100 MiB |  512M |   13 |      1 |  4.659 s |   200.0 MiB |   200.0 MiB |   400.0 MiB | 85.86 |  29.6 MiB |
| 500 MiB |  512M |   16 |      1 | 27.201 s | 1 000.0 MiB | 1 000.0 MiB | 2 000.0 MiB | 73.53 | 118.4 MiB |
| 500 MiB |  128M |   16 |      1 | 26.843 s | 1 000.0 MiB | 1 000.0 MiB | 2 000.0 MiB | 74.51 | 118.4 MiB |

500 MiB под 128M помещается, но 118.4 MiB peak оставляет около 10 MiB запаса.
Это не «сортировка в 128 MiB»; это сортировка с chunk 32 MiB и буферами, которая
уложилась в лимит. Page cache может обслужить часть чтений из памяти; пиковое
значение — PHP allocator, не RSS.

Время, 8 MiB вход, один sort на итерацию:

| benchmark              |     median |        p95 |
| ---------------------- | ---------: | ---------: |
| external-sort-chunk-1m | 435.965 ms | 531.748 ms |
| external-sort-chunk-4m | 221.379 ms | 222.934 ms |
| external-sort-fanin-2  | 421.688 ms | 432.094 ms |
| external-sort-fanin-16 | 336.893 ms | 339.214 ms |

4 MiB chunk быстрее на этом входе, потому что 8 MiB данных превращаются в два
run и один merge. Это не общий закон: на 500 MiB картина другая.

---

## 4. Что ломается

**Chunk — не память.** 128 MiB записей дали 291.4 MiB PHP peak. Нельзя выбирать
chunk по `memory_limit` без запаса на массив строк, буферы и головы run. 500 MiB
/ 128M с chunk 32 MiB оставляет всего около 10 MiB headroom.

**Лишний проход дорог.** 8 MiB chunk на 100 MiB даёт 13 run и два прохода;
logical I/O 600 MiB вместо 400 MiB, время 5.389 s вместо 3.954 s. Проходы надо
считать до запуска, а не после.

**Fan-in больше run не помогает.** Fan-in 32 и 128 на 13 run дают одинаковые 400
MiB и один проход. Открытые файлы — ограниченный ресурс; увеличивать fan-in без
необходимости вредно.

**Pre-sorted вход не ускоряется.** Нет replacement selection и fast path:
sorted/reversed/duplicate идут тем же путём. Это честно измеренный предел
реализации.

**Логический I/O не физический.** Байты считаются по записям, а не по блокам
файловой системы. Page cache, bind mount и Docker storage могут изменить
картину. Нельзя выдавать `MiB/s` за пропускную способность диска.

**Маленький вход быстрее в памяти.** 10 000 записей external sort 0.091 s против
0.012 s in-memory. External sort нужен для больших входов, а не для красоты.

---

## 5. Как это проверено

Тесты проверяют пустой вход, одну запись, записи больше chunk, несколько
проходов, fan-in 2 и стабильность на дубликатах. Для каждого полного выхода
считается checksum и сравнивается с стабильным in-memory `usort` на том же
наборе. `SortStats` проверяет число записей, run, проходов и логических байт.

`tests/ExternalSort/BoundedMemoryTest.php` генерирует 8 MiB вход, запускает
child с `-d memory_limit=8M`, chunk 512 KiB и fan-in 4: external sort
завершается, а naive `file()` + `usort()` падает с allowed-memory error.
Child-процесс нужен потому, что fatal allocator error нельзя надёжно поймать в
PHPUnit. `make check` запускает тесты, PHPStan и форматтер.

---

## 6. Что запомнить

- Внешняя сортировка ограничивает память chunk, а не вход.
- Число проходов зависит от `R` и fan-in; финальный merge считается проходом.
- Chunk в байтах — не PHP peak: 128 MiB записей дали 291.4 MiB.
- Fan-in уменьшает проходы только до числа run; дальше он добавляет файлы.
- Устойчивость обеспечивается стабильным `usort` и tie-break по run.
- Логический I/O — не физический; page cache и файловая система меняют картину.
- Pre-sorted вход не получает бесплатного ускорения в этой реализации.

---

## 7. Упражнения

1. Реализуйте replacement selection: используйте кучу размера chunk, чтобы
   pre-sorted вход давал более длинные run. Насколько уменьшится число проходов
   на sorted/reversed?
2. Подберите chunk и fan-in для 10 GiB входа под `memory_limit=256M`. Обоснуйте
   запас на PHP-буферы, а не только на байты записей.
3. Добавьте сжатие run. Как изменится logical I/O и время CPU? Где будет узкое
   место?
4. Измерьте физический I/O через `/proc` или `strace` и сравните с `SortStats`.
   Почему числа не совпадают?
5. Проверьте устойчивость на 1 000 000 записей с 100 уникальными ключами. Какой
   tie-break нужен при multi-pass merge?

---

## 8. Что дальше: интеграционный сценарий

Семь глав собираются в один сценарий `make scenario`: поток access-log → Bloom
для первого появления URL → точный top-K → внешняя сортировка отчёта. На 100 000
запросов и 20 000 возможных URL получилось 10 442 различных URL; Bloom вставил
10 440 кандидатов и дал 2 ложноположительных срабатывания (0.019% при цели 1% и
оценке 0.032%, потому что фильтр был рассчитан на 20 000 URL). Отчёт на 10 442
записи прошёл 10 начальных run и 2 прохода слияния при chunk 32 KiB и fan-in 8;
файл занял 316 255 байт. Ingest занял 0.246 s при 1.79 MiB peak, сортировка
отчёта — 0.088 s при 0.45 MiB.

Это не бенчмарк и не медиана: один детерминированный прогон, который показывает,
как структуры соединяются. Полные числа и окружение — в `docs/BENCHMARKS.md`
проекта; код закреплён тегом `v1.0.0`, на который ссылаются все главы книги.
