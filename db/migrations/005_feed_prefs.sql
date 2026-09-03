-- 005 — лента запоминает, как её смотрят.
--
-- Сортировка и фильтр выбираются кнопками под лентой, но при следующем /feed
-- всё сбрасывалось на «по релевантности». Человек, который смотрит по цене,
-- смотрит по цене каждый раз.

ALTER TABLE search_profiles ADD COLUMN feed_order TEXT NOT NULL DEFAULT 'rank';
ALTER TABLE search_profiles ADD COLUMN feed_filter TEXT NOT NULL DEFAULT 'all';
