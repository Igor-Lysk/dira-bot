-- 008 — комиссия маклера как критерий поиска.
--
-- Комиссия это месячная аренда сверху, то есть реальные деньги, и для многих
-- она отсекает вариант жёстче, чем лишние 200 ₪ в месяц. Три положения, как у
-- остальных признаков: только без комиссии / можно без данных / неважно.

ALTER TABLE search_profiles ADD COLUMN req_no_commission TEXT NOT NULL DEFAULT 'ignore'
    CHECK (req_no_commission IN ('required','allow_unknown','ignore'));
