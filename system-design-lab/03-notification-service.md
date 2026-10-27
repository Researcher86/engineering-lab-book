# Глава 3. Notification Service: одно событие — не одно сообщение

> Проект:
> [`system-design-lab`](https://github.com/Researcher86/system-design-lab), тег
> [`v1.0.0`](https://github.com/Researcher86/system-design-lab/releases/tag/v1.0.0).
> Предыдущая глава: URL shortener (глава 2). Следующая глава: job queue (глава
> 4).

Создание короткой ссылки — это событие. Пользователь ждёт уведомления, но
«уведомление» не является одной сущностью. У него есть канал: email, push, SMS.
У канала есть разрешение пользователя. У события и канала есть ключ
дедупликации. У пользователя есть тихие часы и режим дайджеста. Одно событие
превращается в несколько независимых обещаний доставки, и каждое из них может
быть выполнено, отложено или подавлено.

В этой главе мы разберём планировщик уведомлений. Он не отправляет письма и не
ходит в сеть: он возвращает план и счётчики. Это сознательное разделение.
Планирование отвечает на вопрос «что должно произойти»; очередь из главы 4 — на
вопрос «что делать, если это не произошло с первого раза».

---

## 1. Проблема

Представьте событие «ссылка создана». Пользователь включил email и push. Если
сервис считает событие единицей работы, он отправит одно уведомление, хотя
обещаний два. Если он считает единицей канал, он получит два независимых
сообщения, два ключа дедупликации и две записи в очереди. Разница видна не
сразу, но именно она определяет, что произойдёт при повторной доставке события:
подавится ли дубликат в обоих каналах или только в одном.

Дальше включаются ограничения пользователя. Ночью не нужно будить push-ом, но
email можно отложить до утра. Если пользователь выбрал дайджест, десять событий
за день должны стать одним письмом, а не десятью. Срочное уведомление должно
обойти тихие часы и дайджест, но не дедупликацию: срочность не означает «можно
отправить дважды».

Наконец, у планировщика есть цена. Каждое событие проходит по списку каналов,
делает проверку предпочтений, пытается занять ключ дедупликации и, возможно,
кладёт элемент в буфер дайджеста. Это O(channels) на событие. При трёх каналах и
миллионе событий — три миллиона намерений. Их нельзя спрятать за одним словом
«отправили уведомление».

---

## 2. Устройство

### 2.1. Канал — единица обещания

`Channel` — маленький enum, но он задаёт единицу fan-out. У каждого канала своё
предпочтение и свой ключ дедупликации.

```php
/**
 * A delivery channel is a separate message with its own preference and dedup
 * key. Fan-out is measured in channel intents, not in events.
 */
enum Channel: string
{
    case Email = 'email';
    case Push = 'push';
    case Sms = 'sms';

    /**
     * @return list<Channel>
     */
    public static function first(int $count): array
    {
        if ($count < 0 || $count > count(self::cases())) {
            throw new InvalidArgumentException(sprintf(
                'Channel count must be between 0 and %d, got %d',
                count(self::cases()),
                $count,
            ));
        }

        return array_slice(self::cases(), 0, $count);
    }
}
```

[`src/Notification/Channel.php` L9-L34](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/Channel.php#L9-L34)

`UserPreferences` хранит список каналов, а не битмаску. Список можно проверить
на дубликаты при создании и напечатать в отчёте. Битмаска была бы компактнее, но
менее очевидной.

```php
/**
 * One user's delivery choices.
 *
 * Channels are a list, not a bitmask, so fan-out has to iterate them and a
 * preference change is visible in the result. Digest mode batches non-urgent
 * messages; quiet hours defer them. An urgent notification bypasses both.
 */
final readonly class UserPreferences
{
    /**
     * @param list<Channel> $channels
     */
    public function __construct(
        public array $channels,
        public bool $digest = false,
        public ?QuietHours $quietHours = null,
    ) {
        $seen = [];

        foreach ($channels as $channel) {
            if (isset($seen[$channel->value])) {
                throw new InvalidArgumentException(sprintf('Duplicate channel in preferences: %s', $channel->value));
            }

            $seen[$channel->value] = true;
        }
    }

    public static function all(bool $digest = false, ?QuietHours $quietHours = null): self
    {
        return new self(Channel::cases(), $digest, $quietHours);
    }

    public static function none(): self
    {
        return new self([]);
    }

    public function allows(Channel $channel): bool
    {
        return in_array($channel, $this->channels, true);
    }
}
```

[`src/Notification/UserPreferences.php` L9-L51](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/UserPreferences.php#L9-L51)

### 2.2. Порядок решений

Сервис проходит по каналам в фиксированном порядке: предпочтение → дедупликация
→ тихие часы → дайджест → немедленная доставка. Этот порядок — часть контракта.
Дедупликация стоит до тихих часов, поэтому отложенное сообщение всё равно
занимает ключ и не будет отправлено дважды после окна. Срочность проверяется
только на шагах тихих часов и дайджеста.

```php
    public function notify(Notification $notification): NotificationResult
    {
        $this->counters->add('events');
        $preferences = $this->preferences->get($notification->tenantId, $notification->userId);

        if ($preferences->channels === []) {
            $this->counters->add('suppressedByPreferences');

            return new NotificationResult([], 0, 0, 1, 0);
        }

        $deliveries = [];
        $digested = 0;
        $suppressedDuplicates = 0;
        $channelIntents = 0;

        foreach ($preferences->channels as $channel) {
            ++$channelIntents;
            $this->counters->add('channelIntents');

            $key = implode("\0", [
                $notification->tenantId,
                $notification->userId,
                $channel->value,
                $notification->eventKey,
            ]);

            if (!$this->dedup->claim($key, $notification->createdAtMs)) {
                ++$suppressedDuplicates;
                $this->counters->add('suppressedDuplicates');

                continue;
            }

            if (!$notification->urgent && $preferences->quietHours?->contains($notification->createdAtMs) === true) {
                $deliveries[] = new Delivery(
                    tenantId: $notification->tenantId,
                    userId: $notification->userId,
                    channel: $channel,
                    eventKey: $notification->eventKey,
                    scheduledAtMs: $preferences->quietHours->nextAllowedMs($notification->createdAtMs),
                    status: DeliveryStatus::Deferred,
                );
                $this->counters->add('planned');
                $this->counters->add('deferred');

                continue;
            }

            if (!$notification->urgent && $preferences->digest) {
                $this->digest->add($notification, $channel);
                ++$digested;
                $this->counters->add('digested');

                continue;
            }
```

[`src/Notification/NotificationService.php` L33-L88](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/NotificationService.php#L33-L88)

Ключ дедупликации включает tenant, пользователя, канал и `eventKey`. Если убрать
канал, повторная отправка в email подавит push. Если убрать tenant, два
арендатора с одинаковым `eventKey` начнут мешать друг другу. Это не детали
реализации, а границы области действия.

### 2.3. TTL и повторная попытка

Dedup store хранит время истечения ключа. Первое обращение занимает ключ;
повторное внутри TTL возвращает `false`; после истечения ключ занимается снова.
TTL не продлевается повторной попыткой — иначе бесконечный retry мог бы держать
ключ вечно.

```php
    public function claim(string $key, int $nowMs): bool
    {
        if ($nowMs < 0) {
            throw new InvalidArgumentException('Timestamp cannot be negative');
        }

        $this->counters->add('claims');

        if ($this->ttlMs === 0) {
            $this->counters->add('accepted');

            return true;
        }

        $expiresAt = $this->expiresAt[$key] ?? null;

        if ($expiresAt !== null && $expiresAt > $nowMs) {
            $this->counters->add('suppressed');

            return false;
        }

        $this->expiresAt[$key] = $nowMs + $this->ttlMs;
        $this->counters->add('accepted');

        if ($this->cleanupInterval > 0 && $this->counters->get('claims') % $this->cleanupInterval === 0) {
            $this->purge($nowMs);
        }

        return true;
    }
```

[`src/Notification/InMemoryDedupStore.php` L42-L72](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/InMemoryDedupStore.php#L42-L72)

TTL, равный нулю, отключает дедупликацию, а не включает «вечную» память. Это
явное состояние полезно в тестах и в экспериментах: можно сравнить поток с
подавлением и без него, не меняя класс.

### 2.4. Тихие часы

Окно задаётся в UTC и может пересекать полночь. `contains()` проверяет час;
`nextAllowedMs()` переносит отложенное сообщение на первое разрешённое
мгновение. Равные начало и конец отключают окно, а не означают «тихо весь день».

```php
/**
 * A daily quiet window in UTC, inclusive at the start and exclusive at the end.
 *
 * The window may wrap midnight. Equal start and end disables quiet hours
 * instead of meaning "quiet all day"; that makes the disabled state explicit
 * and avoids an accidental 24-hour suppression. A deferred notification is
 * moved to the next end boundary, which is the first allowed instant.
 */
final readonly class QuietHours
{
    public const HOUR_MS = 3_600_000;
    public const DAY_MS = 86_400_000;

    public function __construct(
        public int $startHour,
        public int $endHour,
    ) {
        if ($this->startHour < 0 || $this->startHour > 23) {
            throw new InvalidArgumentException('Quiet-hours start must be an hour between 0 and 23');
        }

        if ($this->endHour < 0 || $this->endHour > 23) {
            throw new InvalidArgumentException('Quiet-hours end must be an hour between 0 and 23');
        }
    }

    public function enabled(): bool
    {
        return $this->startHour !== $this->endHour;
    }

    public function contains(int $nowMs): bool
    {
        $this->assertTimestamp($nowMs);

        if (!$this->enabled()) {
            return false;
        }

        $hour = intdiv($nowMs % self::DAY_MS, self::HOUR_MS);

        if ($this->startHour < $this->endHour) {
            return $hour >= $this->startHour && $hour < $this->endHour;
        }

        return $hour >= $this->startHour || $hour < $this->endHour;
    }

    public function nextAllowedMs(int $nowMs): int
    {
        $this->assertTimestamp($nowMs);

        if (!$this->contains($nowMs)) {
            return $nowMs;
        }

        $dayStart = intdiv($nowMs, self::DAY_MS) * self::DAY_MS;
        $candidate = $dayStart + $this->endHour * self::HOUR_MS;

        while ($candidate <= $nowMs) {
            $candidate += self::DAY_MS;
        }

        return $candidate;
    }
```

[`src/Notification/QuietHours.php` L9-L73](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/QuietHours.php#L9-L73)

`while ($candidate <= $nowMs)` нужен для окна, которое уже началось вчера и
заканчивается сегодня: конец может быть раньше текущего момента, и тогда
следующее разрешённое время — завтра.

### 2.5. Дайджест

Digest buffer группирует события по tenant, пользователю, каналу и дню. Пакет
становится доступным, когда часы перешли в следующий день. До этого события
лежат в памяти.

```php
    public function add(Notification $notification, Channel $channel): void
    {
        $day = intdiv($notification->createdAtMs, $this->dayMs);
        $key = $notification->tenantId . "\0" . $notification->userId . "\0" . $channel->value . "\0" . $day;

        if (!isset($this->buckets[$key])) {
            $this->buckets[$key] = [
                'tenantId' => $notification->tenantId,
                'userId' => $notification->userId,
                'channel' => $channel,
                'day' => $day,
                'items' => [],
            ];
        }

        $this->buckets[$key]['items'][] = $notification;
        $this->counters->add('buffered');
    }
```

[`src/Notification/DigestBuffer.php` L44-L61](https://github.com/Researcher86/system-design-lab/blob/v1.0.0/src/Notification/DigestBuffer.php#L44-L61)

`due()` отделяет пакеты прошлых дней от текущего. Если вызвать `flushAll()`,
уйдут и текущие — это путь завершения симуляции, а не штатное расписание.

---

## 3. Эксперимент

### 3.1. Fan-out по каналам

`notify:fanout` держит 10 000 событий и добавляет каналы по одному.

```bash
make experiment ARGS="notify:fanout"
```

| каналов | событий | намерений | доставок | намерений на событие |
| ------: | ------: | --------: | -------: | -------------------: |
|       1 |  10 000 |    10 000 |   10 000 |                  1.0 |
|       2 |  10 000 |    20 000 |   20 000 |                  2.0 |
|       3 |  10 000 |    30 000 |   30 000 |                  3.0 |

Число событий постоянно, число сообщений — нет. Каждый канал добавляет свою
проверку предпочтения, свой ключ дедупликации и свою доставку. Дайджест или
тихие часы могут схлопнуть это позже, но намерение уже создано и посчитано.

### 3.2. Дедупликация и TTL

`notify:dedup` отправляет 10 000 уникальных событий, затем 5 000 дубликатов
внутри TTL и ещё 5 000 после истечения. Каналов два, TTL — 60 000 мс.

```bash
make experiment ARGS="notify:dedup"
```

| фаза                 | попыток | намерений | доставлено | подавлено |
| -------------------- | ------: | --------: | ---------: | --------: |
| уникальные события   |  10 000 |    20 000 |     20 000 |         0 |
| дубликаты внутри TTL |   5 000 |    10 000 |          0 |    10 000 |
| дубликаты после TTL  |   5 000 |    10 000 |     10 000 |         0 |

Внутри TTL подавлены все 10 000 намерений; после истечения те же ключи снова
приняты, и доставка повторилась. Это ожидаемое поведение TTL, а не ошибка.
Дедупликация гарантирует «не чаще одного раза за окно», а не «ровно один раз
навсегда».

Store удерживает один ключ на пару «событие + канал», то есть 20 000 ключей, а
не 20 000 попыток. Размер карты зависит от числа ключей, а не от числа retry.

### 3.3. Дайджест

`notify:digest` прогоняет 10 000 событий по 1 000 пользователям и двум каналам
сначала немедленно, затем в дневном дайджесте.

```bash
make experiment ARGS="notify:digest"
```

| режим      | событий | намерений | сообщений | элементов в сообщении |
| ---------- | ------: | --------: | --------: | --------------------: |
| немедленно |  10 000 |    20 000 |    20 000 |                   1.0 |
| дайджест   |  10 000 |    20 000 |     2 000 |                  10.0 |

Дайджест заменил 20 000 сообщений на 2 000: по одному сообщению на пользователя,
канал и день. Сэкономлено 18 000 сообщений. Цена — 20 000 событий, удержанных в
памяти до конца дня, и задержка доставки. Это обмен latency на количество
сообщений, а не бесплатная оптимизация.

### 3.4. Тихие часы

`notify:quiet-hours` распределяет 240 событий по 24 часам. Окно — 22:00–08:00
UTC, один канал.

```bash
make experiment ARGS="notify:quiet-hours"
```

| метрика               | значение |
| --------------------- | -------: |
| немедленно            |      140 |
| отложено              |      100 |
| средняя задержка      |    5.4 ч |
| максимальная задержка |     10 ч |

Сто событий не потерялись: они получили `scheduledAtMs` на конец окна. Средняя
задержка 5.4 часа, максимум 10 часов — это события, пришедшие сразу после 22:00.
Окно исключает конечный час: 08:00 уже разрешено.

---

## 4. Бенчмарк

Финальный прогон `v1.0.0`:

```bash
make benchmark ARGS="notification --repetitions=3"
```

| операция              | итерации | медиана, мс |    ops/sec |
| --------------------- | -------: | ----------: | ---------: |
| preference lookup     |  100 000 |       8.056 | 12 413 236 |
| dedup new key         |  100 000 |      21.024 |  4 756 365 |
| dedup duplicate       |  100 000 |      14.966 |  6 681 794 |
| quiet hours contains  |  100 000 |       9.973 | 10 026 780 |
| digest add            |   20 000 |       4.511 |  4 433 443 |
| notify one channel    |   20 000 |      26.275 |    761 181 |
| notify three channels |   20 000 |      52.733 |    379 268 |
| notify duplicate      |  100 000 |     130.728 |    764 948 |

Планирование одного канала стоит около 26 мс на 20 000 событий, трёх — 52.7 мс.
Это чуть меньше, чем строгое утроение, потому что часть работы (получение
предпочтений) делается один раз на событие.

Повторная отправка (`notify duplicate`) стоит 130.7 мс на 100 000 — дороже, чем
первичная проверка, потому что каждое событие проходит по каналам, строит ключ и
делает claim, даже если результат — отказ. Дедупликация не бесплатна; она
экономит доставку, а не работу планировщика.

---

## 5. Что ломается

**Один ключ на все каналы.** Если ключ не включает канал, повторная отправка в
email подавит push. Если не включает tenant, арендаторы начнут влиять друг на
друга. Если не включает `eventKey`, разные события с одинаковым текстом
схлопнутся.

**TTL как «навсегда».** Дедупликация с TTL не даёт exactly-once. После истечения
окна то же событие снова доставится. Для борьбы с этим нужен более долгий срок
хранения или idempotency key на стороне получателя, но и у неё есть свой TTL и
своя цена.

**Тихие часы и часовой пояс.** Окно считается в UTC. Пользователь живёт в другом
поясе, а DST может сдвинуть границу. Лаборатория не моделирует ни переходы, ни
локальные зоны; в production это отдельный слой.

**Равные границы окна.** `startHour === endHour` означает «выключено», а не
«тихо 24 часа». Это защита от случайной блокировки всех уведомлений, но её надо
знать.

**Срочность.** Urgent обходит тихие часы и дайджест, но не дедупликацию. Если
сделать urgent исключением и из дедупликации, повторный retry сможет отправить
срочное сообщение дважды.

**Дайджест — это память.** Все события лежат в буфере до конца дня. При миллионе
событий это не «просто счётчик». Нужны границы, вытеснение и отдельное
хранилище.

**Fan-out.** Три канала — это три обещания, три ключа и три записи в очередь.
При десяти каналах и миллионе событий планировщик создаст десять миллионов
намерений. Это надо считать до запуска, а не после.

**Планирование ≠ доставка.** Сервис не спит, не retry-ит и не отправляет. Ошибка
транспорта становится работой очереди из следующей главы. Книга не приписывает
этому классу гарантий доставки.

---

## 6. Как это проверено

- `tests/Notification/NotificationServiceTest.php` покрывает предпочтения,
  дубликаты, TTL, тихие часы, дайджест, срочность и порядок решений.
- `tests/Notification` проверяет границы окна, переход через полночь, отключение
  окна, очистку dedup store и группировку дайджеста.
- `notify:fanout` измеряет намерения и доставки при одном, двух и трёх каналах.
- `notify:dedup` отдельно считает доставки и подавления внутри TTL и после него.
- `notify:digest` сравнивает число сообщений, а не число событий.
- `notify:quiet-hours` показывает, что отложенное сообщение не потеряно и что
  задержка распределена по часам.

К моменту завершения главы набор содержал 176 тестов; финальный набор проекта —
396 тестов и 19 064 assertions. `make check` включает PHPUnit, PHPStan level 8 и
PHP-CS-Fixer.

---

## 7. Что запомнить

1. Событие — не сообщение. Единица планирования — канал, а не событие.
2. Ключ дедупликации должен включать tenant, пользователя, канал и событие.
3. TTL — это окно, а не вечная гарантия. После него дубликат снова доставится.
4. Тихие часы откладывают, а не удаляют. `nextAllowedMs()` — часть контракта.
5. Дайджест меняет latency на количество сообщений и удерживает события в
   памяти.
6. Срочность обходит тихие часы и дайджест, но не дедупликацию.
7. Fan-out стоит O(channels) на событие и умножает очередь, подписи и retry.

---

## 8. Упражнения

1. Добавьте четвёртый канал. Как изменятся `notify:fanout` и стоимость
   `notify three channels`?
2. Запустите `notify:dedup` с `--ttl-ms=0`. Что произойдёт с дубликатами и
   почему?
3. Поставьте окно 08:00–22:00 вместо 22:00–08:00. Совпадут ли суммы deferred и
   immediate? А распределение задержек?
4. Сделайте ключ без канала и повторите тест. Какой сценарий сломается первым?
5. Предложите хранение дайджеста, которое не держит все события в памяти.
6. Решите, должен ли urgent обходить дедупликацию. Обоснуйте ответ через
   guarantees, а не через удобство.

---

Дальше — Job Queue. План уведомлений и вебхуков должен пережить сбой транспорта;
следующая глава показывает, как lease, retry и dead letter превращают план в
работу.
