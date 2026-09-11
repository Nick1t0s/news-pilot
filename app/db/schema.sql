CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS news (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    text TEXT NOT NULL,
    url TEXT NOT NULL,
    full_text_fetched BOOLEAN NOT NULL DEFAULT FALSE,
    rss_image_urls JSONB,
    published_at TIMESTAMPTZ,
    embedding vector(__DIM__),
    status TEXT NOT NULL DEFAULT 'pending',
    duplicate_of_id BIGINT REFERENCES news(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_news_source_external_id UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS ix_news_status ON news (status);
CREATE INDEX IF NOT EXISTS ix_news_created_at ON news (created_at);
CREATE INDEX IF NOT EXISTS ix_news_embedding_hnsw ON news USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS posts (
    id BIGSERIAL PRIMARY KEY,
    news_id BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    embedding vector(__DIM__),
    tg_message_id BIGINT,
    tg_url TEXT,
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_posts_news_id ON posts (news_id);
CREATE INDEX IF NOT EXISTS ix_posts_status ON posts (status);
CREATE INDEX IF NOT EXISTS ix_posts_created_at ON posts (created_at);
CREATE INDEX IF NOT EXISTS ix_posts_embedding_hnsw ON posts USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS post_images (
    id SERIAL PRIMARY KEY,
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    local_path TEXT,
    position INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_post_images_post_id ON post_images (post_id);

CREATE TABLE IF NOT EXISTS post_references (
    id SERIAL PRIMARY KEY,
    post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    referenced_post_id BIGINT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    CONSTRAINT uq_post_references_pair UNIQUE (post_id, referenced_post_id)
);

CREATE INDEX IF NOT EXISTS ix_post_references_post_id ON post_references (post_id);

CREATE TABLE IF NOT EXISTS processing_log (
    id SERIAL PRIMARY KEY,
    news_id BIGINT NOT NULL REFERENCES news(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_processing_log_news_id ON processing_log (news_id);
CREATE INDEX IF NOT EXISTS ix_processing_log_news_stage ON processing_log (news_id, stage);
