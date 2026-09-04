-- 012 — счётчик обращений к модели и пометка «это не объявление».
--
-- Отметка llm_at (миграция 011) закрыла конкретный цикл, но она же и
-- единственное, что его сдерживает: сотрётся или не проставится — и всё
-- повторится. Счётчик попыток это второй, независимый предел: сколько бы
-- флагов ни сбилось, к одному объявлению модель не обратится больше трёх раз
-- за всю его жизнь.
--
-- Пометка junk_reason — для того, что объявлением не является. В базу попало
-- предложение работы в Цюрихе: слово «квартир» в тексте есть, поэтому фильтр
-- канала его пропустил. Разбор не нашёл ни цены, ни комнат, ни города —
-- значит одной попытки модели достаточно, чтобы закрыть вопрос навсегда.

ALTER TABLE listing_facts ADD COLUMN llm_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE listings     ADD COLUMN junk_reason  TEXT;

UPDATE listing_facts SET llm_attempts = COALESCE((
    SELECT COUNT(*) FROM llm_usage u WHERE u.listing_id = listing_facts.listing_id
), 0);

CREATE INDEX IF NOT EXISTS idx_listings_junk ON listings(junk_reason);
