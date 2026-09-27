-- 018 — жалобы на объявления и ответы на них.
--
-- До этого жалоба жила строкой в user_actions: человек нажимал «данные неверны»,
-- бот писал «разберу заново» — и всё. Узнать, помогло ли, было неоткуда. Чтобы
-- вернуться к человеку с итогом, нужно помнить, кто на что пожаловался, что
-- было в карточке в этот момент и на какое сообщение отвечать.
--
--   kind      price | rooms | mamad | address | removed | duplicate | agent |
--             notfit | other
--   detail    для notfit — какой критерий, для other — текст человека
--   before    JSON: значения спорных полей на момент жалобы
--   status    awaiting_text — ждём фразу после «Другое»;
--             waiting — ждём повторного разбора, потом ответим;
--             closed — ответ отправлен
--   reply     что ответили человеку

CREATE TABLE IF NOT EXISTS feedback (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(telegram_id) ON DELETE CASCADE,
    listing_id  TEXT REFERENCES listings(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    detail      TEXT,
    before      TEXT NOT NULL DEFAULT '{}',
    chat_id     INTEGER,
    card_msg_id INTEGER,
    status      TEXT NOT NULL DEFAULT 'waiting'
                 CHECK (status IN ('awaiting_text','waiting','closed')),
    reply       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback(status, created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback(user_id, status);
