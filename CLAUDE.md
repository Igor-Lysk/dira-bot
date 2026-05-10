# Dira Bot — Project Archive

**Статус:** ✅ Завершён. Квартира найдена в мае 2026.
Бот проработал ~33 дня (7 апреля — 10 мая 2026). Сервер остановлен, БД сохранена локально в `data/dira.db`.

---

## Что это такое

Автономный бот для поиска квартиры в аренду в районе Тель-Авива.
Мониторит Telegram-каналы и Yad2, анализирует каждое объявление через Claude Haiku и отправляет алерты в Telegram с кнопками обратной связи 👍/👎.

---

## Итоговая статистика (из data/dira.db)

| Метрика | Значение |
|---------|----------|
| Период работы | 7 апр — 10 мая 2026 (33 дня) |
| Всего объявлений | 748 |
| Источники | Telegram: 682 / Yad2: 66 |
| Проанализировано | 748 |
| SEND (score ≥ 7) | 132 |
| MAYBE (score 5–6) | 88 |
| SKIP | 528 |
| Обратная связь | 7 (👍 3 / 👎 1 / 🗑 3) |
| Макс. найденный score | 9/10 |

БД содержит полный `raw_text` каждого объявления — можно делать ретроспективный анализ.

---

## Критерии поиска (финальные, зафиксированы в analyzer.py)

- **Города:** Тель-Авив, Рамат-Ган, Гиватаим, Бней-Брак (основные); Бат-Ям, Холон (−1 балл)
- **Комнаты:** ≥ 2.5 (израильский счёт — гостиная считается комнатой)
- **Цена:** до 8,000 ₪/мес (идеально до 7,500 — ещё −1 балл)
- **Мамад (ממ"ד):** ОБЯЗАТЕЛЕН — укреплённая комната внутри квартиры. Миклат/общее убежище — НЕ считается
- **Мебель, транспорт, дата въезда:** не учитываются

**Жёсткие SKIP (применяются до score, перевешивают всё):**
- Город не из списка → SKIP score 0
- Продажа (не аренда) → SKIP score 0
- Субаренда / саблет / комната с соседями → SKIP score 0
- Краткосрочная аренда < 6 мес → SKIP score 0
- Цена > 8,000 ₪ → SKIP score 0
- rooms < 2.5 → SKIP score 0
- Только миклат, нет мамада → SKIP score 0
- Мамад вообще не упомянут → score ≤ 3 → SKIP

**Пороги алертов:** SEND ≥ 7, MAYBE 5–6

---

## Архитектура

Всё работает в одном `asyncio` event loop. Никаких потоков, никаких subprocess.

```
Telegram-каналы (real-time)  ─┐
Yad2 через ScraperAPI (1h)   ─┤──► process_new_listing() ──► Claude ──► send_alert()
Apify Yad2/Facebook (2h/12h) ─┘         (main.py)           analyzer.py    bot.py
                                               │
                                               ▼
                                          SQLite DB
                                         (database.py)
                                               │
                                      APScheduler (scheduler.py)
                                       ├── daily digest 20:00
                                       └── learn preferences каждые 6h
```

### Пайплайн обработки (`process_new_listing` в main.py)

1. **Фильтр саблетов** — regex по "саблет/sublet/שותפ", с отменой при отрицании "не саблет"
2. **Фильтр комнат (pre-Claude)** — regex извлекает явное число комнат из текста; если < 2.5 — SKIP без вызова Claude вообще. Паттерны: иврит (`חדרים`), русский (`комнат`), английский (`bedrooms`)
3. **Cross-source dedup** — `db.find_similar(price±300, rooms, city)` — одна квартира на Yad2 + Telegram не даёт два алерта
4. **ID + fingerprint dedup** — SHA-256 от URL или текста
5. **Claude analysis** — score 0–10, SEND/MAYBE/SKIP, JSON
6. **Alert** — SEND или MAYBE → `send_alert()`

### Коллекторы

| Коллектор | Файл | Интервал | Примечание |
|-----------|------|----------|------------|
| `TelegramMonitor` | `telegram_monitor.py` | real-time | Telethon StringSession; `backfill(days=7)` при старте |
| `Yad2PageCollector` | `yad2_page.py` | каждый час | `__NEXT_DATA__` JSON; proxy chain при наличии ключей |
| `MadlanCollector` | `madlan.py` | каждые 2h | То же; **отключён** — антибот возвращал пустые данные |
| `ApifyYad2Collector` | `apify_yad2.py` | каждые 2h | Только при `APIFY_TOKEN` |
| `ApifyFacebookCollector` | `apify_facebook.py` | каждые 12h | Только при `APIFY_TOKEN`; платно за событие (~$3/1000) |

**Логика proxy для Yad2 (`collectors/_fetch.py`):**
ScraperAPI → Scrape.do → ScrapingBee → FlareSolverr (lazy lifecycle: стартует по требованию, останавливается сразу после)

---

## Telegram-каналы, которые мониторились

```python
TG_CHANNELS = [
    "Israel_arenda",            # Аренда в Израиле (рус) — самый активный
    "flamingorent",             # Аренда (бывает Хайфа, Claude фильтрует)
    "snyat_kvartiruy",          # Снять квартиру (рус)
    "aptfornew",                # Квартиры репатриантам
    "ambery_longrent_telaviv",  # Долгосрочная аренда ТА
    "jeremy_public",            # Agent Jeremy — Тель-Авив
    "jeremy_public_ramat_gan",  # Agent Jeremy — Рамат-Ган
    "isra_home_arenda",         # Аренда Israel
]
```

---

## Ключевые баги и фиксы (хронология)

### 1. Rootless Docker упал и не поднялся (17–22 апреля)
**Симптом:** Бот молчал 5 дней. `docker ps` недоступен.
**Причина:** boltdb timeout в rootless dockerd; при перезагрузке сервера автостарт не настроен.
**Фикс:** `systemctl --user enable docker` + пользовательский systemd unit для `dockerd`.
**Дополнительно:** post-receive hook в git bare repo использовал неправильный socket path (`/run/user/1003/docker.sock` → `/var/run/docker/run/docker.sock`).

### 2. APScheduler misfire — задачи не запускались при старте
**Симптом:** Yad2/Madlan не сканировались первый час после деплоя.
**Причина:** `next_run_time=datetime.now()` + `misfire_grace_time` по умолчанию (1h) — задача считалась просроченной.
**Фикс:** `misfire_grace_time=None` (никогда не считать просроченной).

### 3. FlareSolverr OOM — Chrome падал
**Симптом:** FlareSolverr стартовал но `fetch` не возвращал данные.
**Причина:** `mem_limit: 600m` в docker-compose — Chrome OOM-killился.
**Фикс:** `mem_limit: 900m`.

### 4. Telethon silent disconnect (27 апреля, ~36h молчания)
**Симптом:** Бот живой (healthcheck.io пингует), но алертов нет. Telegram-каналы не мониторятся.
**Причина:** `run_until_disconnected()` таск завершился без exception и без log.
**Фикс:** `is_healthy()` метод в `TelegramMonitor` — проверяет `client.is_connected()`, статус таска, и время последнего события (idle > 8h → нездоров). `scheduler.py` пропускает ping к healthchecks.io если монитор нездоров → через grace period приходит Telegram-алерт.

### 5. Madlan — антибот блокировал данные
**Симптом:** ScraperAPI/Scrape.do возвращали HTML без `__NEXT_DATA__` (CSR, данные грузятся XHR).
**Попытки:** GraphQL reverse engineering (`/api2`, `/api3`) — успешно нашли endpoint, но даже с правильным запросом данные пустые без сессионных кук.
**Решение:** Madlan job отключён, не тратим прокси-квоту на заблокированные запросы.

### 6. Объявление с 2 комнатами проходило как SEND (два инцидента)
**Листинг:** `https://t.me/jeremy_public/741` — "2 חדרים" + мамад, scored 7/10 SEND.
**Первый инцидент:** Prompt содержал "score ≤ 3 → SKIP" как мягкое правило. Claude его рационализировал при хороших других критериях.
**Первый фикс:** Реструктурировали prompt — явная секция "ЖЁСТКИЕ SKIP (применяются ДО score)", `rooms < 2.5 → SKIP score 0`.
**Второй инцидент:** Тот же листинг снова прошёл после фикса промпта. Claude всё равно rationalized.
**Финальный фикс:** Детерминированный regex в `process_new_listing()` — запускается ДО вызова `analyze_listing()`. Если находит явное число комнат < 2.5 → сразу SKIP, Claude не вызывается вообще. Паттерны: `חד(?:רים|ר\b)`, `комнат`, `bedrooms`.

### 7. Hebrew JSON gershayim ломал парсинг
**Причина:** `ממ"ד` содержит U+0022 (обычный double-quote), что ломает JSON внутри JSON-строки.
**Фикс:** `_sanitize_hebrew_json()` в `analyzer.py` заменяет ASCII `"` в известных ивритских аббревиатурах (ממ"ד, צה"ל, ת"א, ר"ג...) на U+05F4 (Hebrew punctuation gershayim) перед парсингом.

---

## Внешние сервисы и стоимость

| Сервис | Использование | Стоимость |
|--------|---------------|-----------|
| Anthropic API (Haiku) | ~748 анализов + reanalyze | ~$1–2 |
| Apify (Facebook scraper) | Backfill 100 posts × 15 групп — разовый | ~$4.75 |
| Apify (Yad2) | Ежедневно пока был в квоте | free tier |
| ScraperAPI | ~3,240 req/мес (Yad2 1h + Madlan) | free tier (5k/мес) |
| Scrape.do | Основной прокси для Yad2 | free tier |
| ScrapingBee | Fallback | free tier |
| FlareSolverr | Last-resort для Yad2, lazy lifecycle | self-hosted, бесплатно |
| Healthchecks.io | Dead-man switch, ping каждые 10 мин | free tier |
| VPS Berlin (REDACTED) | Shared с REDACTED | оплачивается отдельно |

---

## Переменные окружения (были в .env на сервере, в git не хранились)

```
TELEGRAM_BOT_TOKEN=        # aiogram бот
TELEGRAM_CHAT_ID=          # куда слать алерты
TELEGRAM_API_ID=           # Telethon user account
TELEGRAM_API_HASH=
TELEGRAM_SESSION_STRING=   # Telethon StringSession
ANTHROPIC_API_KEY=
CLAUDE_MODEL=claude-haiku-4-5-20251001
APIFY_TOKEN=
SCRAPERAPI_KEY=
SCRAPEDO_KEY=
SCRAPINGBEE_KEY=
HEALTHCHECK_URL=https://hc-ping.com/...
DB_PATH=/app/data/dira.db
```

---

## Инфраструктура сервера

- **VPS:** `REDACTED`, user `igor`, SSH key `~/.ssh/REDACTED`
- **Docker:** rootless, socket `/var/run/docker/run/docker.sock`
- **Деплой:** git bare repo `~/workspace/dira-bot.git` + post-receive hook → `docker compose up --build -d`
- **Автостарт:** `systemctl --user enable docker` + user linger (`loginctl enable-linger igor`)

**Статус после завершения:** всё остановлено и удалено. Остался `REDACTED` (другой проект).

---

## Структура проекта

```
dira-bot/
├── main.py              # Entry point, asyncio loop, process_new_listing pipeline
├── analyzer.py          # Claude Haiku integration, prompt templates
├── bot.py               # aiogram bot: alert formatting, feedback buttons, commands
├── config.py            # Env vars, thresholds, channel lists
├── database.py          # SQLite via aiosqlite: listings, analyses, feedback, prefs
├── scheduler.py         # APScheduler: Yad2 poll, digest, preferences, healthcheck
├── collectors/
│   ├── base.py                      # BaseCollector ABC
│   ├── telegram_monitor.py          # Telethon real-time + backfill
│   ├── yad2_page.py                 # Yad2 __NEXT_DATA__ scraper
│   ├── madlan.py                    # Madlan scraper (отключён)
│   ├── apify_yad2.py                # Apify Yad2 actor
│   ├── apify_facebook.py            # Apify Facebook groups actor
│   ├── _fetch.py                    # Proxy chain: ScraperAPI→Scrape.do→ScrapingBee→FlareSolverr
│   └── _flaresolverr_lifecycle.py   # Lazy start/stop FlareSolverr container
├── scripts/
│   └── reanalyze.py     # Ретроактивный перебор старых SKIPов с новыми критериями
├── data/
│   └── dira.db          # SQLite DB (скачана с сервера 10 мая 2026)
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## Советы по повторному использованию

Если понадобится адаптировать для другого города / другой страны:

1. **Критерии** — только в `ANALYZE_TEMPLATE` в `analyzer.py`. Менять можно без правки кода.
2. **Pre-filter комнат** — `_extract_rooms()` в `main.py`: добавь паттерны для нового языка если нужно.
3. **Telegram-каналы** — `TG_CHANNELS` в `config.py`. Просто список username без `@`.
4. **Yad2 города** — `YAD2_CITIES` в `config.py`: словарь `название → city_id`.
5. **Madlan** — потенциально работоспособен через сессионные куки браузера (не реализовано).
6. **Reanalyze** — `scripts/reanalyze.py` позволяет переиграть прошлые SKIP с новыми критериями без повторного сбора.
