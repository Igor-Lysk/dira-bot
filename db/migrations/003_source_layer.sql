-- 003 — у фактов появляется третий источник: сама доска объявлений.
--
-- Yad2 и Homeless отдают цену, комнаты, этаж и улицу структурно. Это не
-- извлечение, а данные из источника, и переспрашивать их у модели незачем:
-- каждое такое объявление экономит вызов. SQLite не умеет менять CHECK, поэтому
-- таблица пересоздаётся.

ALTER TABLE listing_facts RENAME TO listing_facts_old;

CREATE TABLE listing_facts (
    listing_id      TEXT PRIMARY KEY REFERENCES listings(id) ON DELETE CASCADE,
    price           INTEGER,
    price_includes_bills TEXT CHECK (price_includes_bills IN ('yes','no')),
    commission      TEXT,
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
    phones          TEXT NOT NULL DEFAULT '[]',
    source_layer    TEXT NOT NULL DEFAULT 'rules'
                     CHECK (source_layer IN ('rules','llm','mixed','source')),
    schema_version  INTEGER NOT NULL DEFAULT 1,
    extracted_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO listing_facts SELECT * FROM listing_facts_old;
DROP TABLE listing_facts_old;

CREATE INDEX idx_facts_price ON listing_facts(price);
CREATE INDEX idx_facts_city  ON listing_facts(city, price);
CREATE INDEX idx_facts_rooms ON listing_facts(rooms);
CREATE INDEX idx_facts_ver   ON listing_facts(schema_version);
