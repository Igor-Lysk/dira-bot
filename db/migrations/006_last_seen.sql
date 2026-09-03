-- 006 — присутствие объявления в выдаче доски.
--
-- Доска снимает объявление, когда квартиру сдали, а дата публикации остаётся
-- прежней: в базе есть живые объявления Homeless от июля 2025 года. Значит для
-- досок признак свежести — не дата, а присутствие в последнем скане.

ALTER TABLE listings ADD COLUMN last_seen_at TEXT;
ALTER TABLE listings ADD COLUMN missed_scans INTEGER NOT NULL DEFAULT 0;

UPDATE listings SET last_seen_at = collected_at WHERE last_seen_at IS NULL;
