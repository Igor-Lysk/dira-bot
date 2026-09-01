-- 001_initial — схема v2.
--
-- Соглашение о трёх состояниях: признак есть / признака нет / нет данных.
-- В SQL это TEXT NULL с CHECK (value IN ('yes','no')). NULL означает «в тексте
-- про это не сказано» и никогда не сворачивается в 'no' — на этом различии
-- построен весь отбор (решение D3).

-- ── пользователи ────────────────────────────────────────────────────────────

CREATE TABLE users (
    telegram_id     INTEGER PRIMARY KEY,
    username        TEXT,
    first_name      TEXT,
    language        TEXT    NOT NULL DEFAULT 'ru',
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_admin        INTEGER NOT NULL DEFAULT 0,
    -- состояние визарда живёт в базе, а не в памяти процесса: рестарт бота
    -- не должен сбрасывать наполовину заполненный профиль
    onboarding_step TEXT,
    onboarding_data TEXT    NOT NULL DEFAULT '{}',   -- JSON
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen_at    TEXT
);

-- ── профили поиска ──────────────────────────────────────────────────────────

CREATE TABLE search_profiles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    name            TEXT    NOT NULL DEFAULT 'Основной',
    is_enabled      INTEGER NOT NULL DEFAULT 1,

    -- жёсткие рамки; NULL = ограничения нет
    cities          TEXT    NOT NULL DEFAULT '[]',   -- JSON-массив
    districts       TEXT    NOT NULL DEFAULT '[]',   -- JSON-массив, пусто = любой
    price_max       INTEGER,
    price_ideal     INTEGER,                          -- выше — не отсекаем, но понижаем ранг
    rooms_min       REAL,
    rooms_max       REAL,
    area_min        INTEGER,
    floor_min       INTEGER,
    floor_max       INTEGER,

    -- требования к признакам: три положения
    --   required      — только явное 'yes'
    --   allow_unknown — 'yes' либо нет данных (посмотрю сам, спрошу у хозяина)
    --   ignore        — не участвует в отборе
    req_mamad       TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_mamad IN ('required','allow_unknown','ignore')),
    req_elevator    TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_elevator IN ('required','allow_unknown','ignore')),
    req_parking     TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_parking IN ('required','allow_unknown','ignore')),
    req_balcony     TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_balcony IN ('required','allow_unknown','ignore')),
    req_pets        TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_pets IN ('required','allow_unknown','ignore')),
    req_furnished   TEXT NOT NULL DEFAULT 'ignore'
                     CHECK (req_furnished IN ('required','allow_unknown','ignore')),

    exclude_shared  INTEGER NOT NULL DEFAULT 1,       -- отсекать комнату с соседями
    exclude_sublet  INTEGER NOT NULL DEFAULT 1,
    stop_words      TEXT    NOT NULL DEFAULT '[]',    -- JSON-массив

    -- доставка
    delivery_mode   TEXT NOT NULL DEFAULT 'digest'
                     CHECK (delivery_mode IN ('realtime','digest')),
    digest_hour     INTEGER NOT NULL DEFAULT 9,
    quiet_from      INTEGER,                           -- час, локальное время
    quiet_to        INTEGER,
    max_per_day     INTEGER NOT NULL DEFAULT 20,
    is_paused       INTEGER NOT NULL DEFAULT 0,        -- пауза в базе, не в памяти

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_profiles_user ON search_profiles(user_id);

-- ── объявления ──────────────────────────────────────────────────────────────

CREATE TABLE listings (
    id              TEXT PRIMARY KEY,                 -- sha256 от url либо от текста
    source          TEXT NOT NULL,                    -- telegram | yad2 | facebook | homeless
    source_id       TEXT,
    channel         TEXT,                             -- канал/группа/город внутри источника
    url             TEXT,
    raw_text        TEXT NOT NULL,
    media           TEXT NOT NULL DEFAULT '[]',       -- JSON: file_id фотографий
    fingerprint     TEXT,                             -- по нормализованному тексту
    posted_at       TEXT,
    collected_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- 'pending' при сбое анализа: объявление вернётся в очередь, а не потеряется
    -- навсегда, как это было в v1 (F-11)
    status          TEXT NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new','pending','extracted','failed','duplicate')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE INDEX idx_listings_status      ON listings(status, collected_at);
CREATE INDEX idx_listings_fingerprint ON listings(fingerprint);
CREATE INDEX idx_listings_source      ON listings(source, collected_at);

-- ── факты ───────────────────────────────────────────────────────────────────

CREATE TABLE listing_facts (
    listing_id      TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,

    price           INTEGER,
    price_includes_bills TEXT CHECK (price_includes_bills IN ('yes','no')),
    commission      TEXT,                             -- 'none' | сумма или процент текстом
    rooms           REAL,
    area_sqm        INTEGER,
    floor           INTEGER,
    total_floors    INTEGER,
    city            TEXT,
    district        TEXT,
    street          TEXT,
    entry_date      TEXT,
    lease_months    INTEGER,
    deal_type       TEXT CHECK (deal_type IN ('rent','sale','sublet','shared')),
    furnished       TEXT CHECK (furnished IN ('full','partial','none')),

    mamad           TEXT CHECK (mamad IN ('yes','no')),
    -- найденная в тексте фраза, если защищённое помещение упомянуто неоднозначно
    -- («מרחב מוגן» без указания, в квартире оно или в подъезде). 18% корпуса —
    -- ни 'yes', ни «нет данных», и выбрасывать их нельзя
    mamad_evidence  TEXT,
    miklat          TEXT CHECK (miklat IN ('yes','no')),
    elevator        TEXT CHECK (elevator IN ('yes','no')),
    balcony         TEXT CHECK (balcony IN ('yes','no')),
    parking         TEXT CHECK (parking IN ('yes','no')),
    storage         TEXT CHECK (storage IN ('yes','no')),
    air_conditioning TEXT CHECK (air_conditioning IN ('yes','no')),
    pets_allowed    TEXT CHECK (pets_allowed IN ('yes','no')),
    garden          TEXT CHECK (garden IN ('yes','no')),
    renovated       TEXT CHECK (renovated IN ('yes','no')),
    immediate_entry TEXT CHECK (immediate_entry IN ('yes','no')),
    no_broker       TEXT CHECK (no_broker IN ('yes','no')),
    contact_type    TEXT CHECK (contact_type IN ('agent','private')),
    phones          TEXT NOT NULL DEFAULT '[]',       -- JSON

    -- какой слой что дал и по какой версии схемы: нужно, чтобы переизвлекать
    -- только устаревшее, а не весь корпус
    source_layer    TEXT NOT NULL DEFAULT 'rules'
                     CHECK (source_layer IN ('rules','llm','mixed')),
    schema_version  INTEGER NOT NULL DEFAULT 1,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_facts_price  ON listing_facts(price);
CREATE INDEX idx_facts_city   ON listing_facts(city, price);
CREATE INDEX idx_facts_rooms  ON listing_facts(rooms);
CREATE INDEX idx_facts_ver    ON listing_facts(schema_version);

-- ── история цены и повторные публикации ─────────────────────────────────────
-- Повтор не выбрасывается: запись обновляется, а прежняя цена ложится сюда.
-- Отсюда берётся «снова опубликовано, 8200 → 7800» (решение D9).

CREATE TABLE price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price       INTEGER NOT NULL,
    seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT
);

CREATE INDEX idx_price_history ON price_history(listing_id, seen_at);

-- ── совпадения ──────────────────────────────────────────────────────────────

CREATE TABLE matches (
    profile_id  INTEGER NOT NULL REFERENCES search_profiles(id) ON DELETE CASCADE,
    listing_id  TEXT    NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    rank        REAL    NOT NULL DEFAULT 0,
    reasons     TEXT    NOT NULL DEFAULT '[]',        -- JSON: из чего сложился ранг
    state       TEXT    NOT NULL DEFAULT 'new'
                 CHECK (state IN ('new','sent','hidden','saved','contacted',
                                  'waiting','visit','rejected')),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    sent_at     TEXT,
    state_at    TEXT,
    PRIMARY KEY (profile_id, listing_id)
);

CREATE INDEX idx_matches_feed  ON matches(profile_id, state, rank DESC);
CREATE INDEX idx_matches_queue ON matches(profile_id, state, created_at);

-- ── действия пользователя ───────────────────────────────────────────────────
-- Заменяют 👍/👎 из v1 (решение D4). Каждое действие полезно самому пользователю,
-- и оно же служит сигналом для подстройки весов ранга.

CREATE TABLE user_actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    listing_id  TEXT REFERENCES listings(id) ON DELETE CASCADE,
    action      TEXT NOT NULL
                 CHECK (action IN ('hide','state_change','wrong_data','open_link')),
    payload     TEXT NOT NULL DEFAULT '{}',           -- JSON: что именно изменилось
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_actions_user ON user_actions(user_id, created_at);

-- ── расход LLM ──────────────────────────────────────────────────────────────
-- В v1 расход нигде не считался, оценка «$1–2» была по памяти.

CREATE TABLE llm_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose         TEXT NOT NULL,                    -- extract | digest | ...
    model           TEXT NOT NULL,
    listing_id      TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_llm_usage ON llm_usage(created_at);
