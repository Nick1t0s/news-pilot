-- Миграция со старой схемы (news/posts/post_images/post_references/processing_log):
-- старые таблицы дропаются при первом старте, данные не переносятся (чистый старт).
DROP TABLE IF EXISTS processing_log;
DROP TABLE IF EXISTS post_references;
DROP TABLE IF EXISTS post_images;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'news') THEN
        DROP TABLE IF EXISTS posts CASCADE;
        DROP TABLE IF EXISTS news CASCADE;
    END IF;
END
$$;

-- Только опубликованные посты канала (+ эмбеддинги для дедупа и поиска продолжений).
CREATE TABLE IF NOT EXISTS posts (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    embedding vector(__DIM__),
    tg_message_id BIGINT,
    tg_url TEXT,
    published_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_posts_published_at ON posts (published_at);
CREATE INDEX IF NOT EXISTS ix_posts_embedding_hnsw ON posts USING hnsw (embedding vector_cosine_ops);

-- Накопительные счётчики (дедуп-скипы, лимит, ошибки, clear_run, ...).
CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value BIGINT NOT NULL DEFAULT 0
);
