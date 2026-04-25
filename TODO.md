# TODO

## Bugs

- [x] **Разобраться, почему объявления из Telegram не приходят в реальном времени**
  **Найдено (21.04 утро):** `NewMessage(chats=[raw_ids])` не совпадал с channel events —
  Telegram каналы используют marked peer ID (`-1001xxxxxxxxx`), а не raw `entity.id`.
  Код пробовал добавить оба, но `except Exception: pass` молча глотал ошибки.
  **Правка 1:** `add_event_handler(entities)` (передаём объекты) +
  `asyncio.create_task(client.run_until_disconnected())`.
  **После 7ч наблюдения:** всё ещё 0 событий (`grep 'TG event' → 0`). Бэкфил собрал
  204 поста, handler молчал.
  **Реальная причина (21.04 вечер):** Telethon шлёт real-time `NewMessage` только для
  диалогов, где user участвует. `iter_messages` работает на public каналах без подписки,
  а live-апдейты — нет.
  **Правка 2:** в `start()` перед регистрацией handler'а вызываем `JoinChannelRequest`
  для каждого entity (идемпотентно).
  **Статус:** задеплоено 21.04 вечером. Подтверждение — строки `DEBUG TG event:` в логах.

- [x] **Разобраться почему с 22 апреля не было ни одного уведомления**
  **Причина:** rootless docker упал 17.04 (containerd/boltdb timeout), 22.04 VPS перезагрузили — docker не поднялся сам, т.к. автостарт был только через `.profile` при SSH-логине.
  **Починка (23.04):** создан user-systemd unit `~/.config/systemd/user/docker.service`, enabled + started. Linger=yes → поднимается на boot VPS.

- [x] **Отправлять сообщение в Telegram-чат, когда бот перестаёт работать** (health-check / dead-man switch)
  **Решение:** healthchecks.io — бот пингует каждые 10 мин, при тишине > 30 мин → один алерт от `@healthchecks_io_bot` в Telegram (личка). URL в `.env` как `HEALTHCHECK_URL`. Задеплоено 23.04.

- [ ] **Madlan: прокси проходят, но `__NEXT_DATA__` отсутствует**
  Уточнено 25.04: Scrape.do и ScrapingBee возвращают 90-145KB реального HTML (anti-bot пройден), но Madlan мигрировал на client-side rendering — нужный нам JSON в этой пачке HTML не приезжает. Возможные решения: FlareSolverr с длинным `BROWSER_TIMEOUT` чтобы дождаться XHR-запросов; либо реверс-инжиниринг внутреннего API Madlan; либо отключить Madlan совсем.

## С 1 мая (Apify сбрасывает лимит)

- [ ] Убедиться что Apify Facebook и Yad2 коллекторы снова работают с новыми настройками
  (5 постов × 7 групп × каждые 12ч ≈ $0.63/мес)

## Улучшения

- [ ] Добавить команду `/search <текст>` — поиск по уже собранным объявлениям в БД
- [ ] Уведомление если бот молчит >24ч (возможно всё заблокировано или упало)
- [ ] Madlan: добавить Бней-Брак в список городов (есть в `YAD2_CITIES`, но не в `madlan.py`)

## На будущее

- [ ] Добавить импорт в БД из личной Excel-таблицы с данными из Facebook-скраппера
