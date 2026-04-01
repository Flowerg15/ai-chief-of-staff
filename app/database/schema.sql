-- ═══════════════════════════════════════════════════════════════════════════
-- AI Chief of Staff — Supabase Schema
-- Run this in your Supabase SQL editor.
-- ═══════════════════════════════════════════════════════════════════════════

-- Enable pgvector for semantic search
CREATE EXTENSION IF NOT EXISTS vector;

-- ─── contacts ────────────────────────────────────────────────────────────────
-- People Garret interacts with by email.
CREATE TABLE IF NOT EXISTS contacts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    email           TEXT NOT NULL UNIQUE,
    importance      INT DEFAULT 3 CHECK (importance BETWEEN 1 AND 5), -- 5 = most important
    company         TEXT,
    role            TEXT,
    deal_ids        UUID[] DEFAULT '{}',
    notes           TEXT,
    last_interaction TIMESTAMPTZ,
    embedding       vector(1536),    -- text-embedding-3-small
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS contacts_email_idx ON contacts (email);
CREATE INDEX IF NOT EXISTS contacts_embedding_idx ON contacts USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);

-- ─── deals ───────────────────────────────────────────────────────────────────
-- Active transactions, investments, or projects Garret is tracking.
CREATE TABLE IF NOT EXISTS deals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    stage           TEXT,           -- e.g. "LOI", "DD", "Closed", "Prospecting"
    key_parties     TEXT[],         -- Names / companies involved
    thread_ids      UUID[] DEFAULT '{}',
    key_dates       JSONB DEFAULT '{}', -- {"loi_signed": "2025-01-15", "close_target": "2025-03-01"}
    decision_log    JSONB DEFAULT '[]', -- [{date, decision, rationale}]
    notes           TEXT,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS deals_embedding_idx ON deals USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
CREATE INDEX IF NOT EXISTS deals_name_idx ON deals USING gin (to_tsvector('english', name));

-- ─── threads ─────────────────────────────────────────────────────────────────
-- Email conversation threads, summarised for context retrieval.
CREATE TABLE IF NOT EXISTS threads (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_thread_id TEXT NOT NULL UNIQUE,
    subject         TEXT,
    participants    TEXT[],         -- Email addresses
    summary         TEXT,           -- Claude-generated summary, updated over time
    deal_id         UUID REFERENCES deals(id),
    contact_ids     UUID[] DEFAULT '{}',
    last_updated    TIMESTAMPTZ DEFAULT NOW(),
    waiting_on_garret BOOLEAN DEFAULT FALSE,  -- True when someone awaits Garret's reply
    waiting_since   TIMESTAMPTZ,
    embedding       vector(1536),
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS threads_gmail_thread_id_idx ON threads (gmail_thread_id);
CREATE INDEX IF NOT EXISTS threads_embedding_idx ON threads USING ivfflat (embedding vector_cosine_ops) WITH (lists = 50);
CREATE INDEX IF NOT EXISTS threads_waiting_idx ON threads (waiting_on_garret) WHERE waiting_on_garret = TRUE;

-- ─── email_cache ─────────────────────────────────────────────────────────────
-- Raw email storage to avoid repeated Gmail API calls.
CREATE TABLE IF NOT EXISTS email_cache (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    gmail_message_id TEXT NOT NULL UNIQUE,
    gmail_thread_id TEXT NOT NULL,
    thread_id       UUID REFERENCES threads(id),
    sender          TEXT,
    recipient       TEXT[],
    subject         TEXT,
    body_text       TEXT,
    body_html       TEXT,
    attachments     JSONB DEFAULT '[]',  -- [{filename, mime_type, size, drive_id}]
    received_at     TIMESTAMPTZ,
    processed       BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS email_cache_gmail_message_id_idx ON email_cache (gmail_message_id);
CREATE INDEX IF NOT EXISTS email_cache_gmail_thread_id_idx ON email_cache (gmail_thread_id);
CREATE INDEX IF NOT EXISTS email_cache_received_at_idx ON email_cache (received_at DESC);
CREATE INDEX IF NOT EXISTS email_cache_processed_idx ON email_cache (processed) WHERE processed = FALSE;

-- ─── decisions ────────────────────────────────────────────────────────────────
-- Log of Garret's decisions made through the system.
CREATE TABLE IF NOT EXISTS decisions (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    date        TIMESTAMPTZ DEFAULT NOW(),
    context     TEXT,           -- What was the situation
    decision    TEXT NOT NULL,  -- What was decided
    rationale   TEXT,           -- Why
    deal_id     UUID REFERENCES deals(id),
    contact_ids UUID[] DEFAULT '{}',
    thread_id   UUID REFERENCES threads(id),
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── tone_samples ─────────────────────────────────────────────────────────────
-- Real sent emails used to calibrate Claude's drafting style.
CREATE TABLE IF NOT EXISTS tone_samples (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category    TEXT NOT NULL,  -- "formal_external" | "quick_internal" | "relationship"
    subject     TEXT,
    body        TEXT NOT NULL,
    to_name     TEXT,
    send_date   TIMESTAMPTZ,
    is_active   BOOLEAN DEFAULT TRUE,  -- False = excluded from prompts
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS tone_samples_category_idx ON tone_samples (category) WHERE is_active = TRUE;

-- ─── system_state ────────────────────────────────────────────────────────────
-- Key-value store for system state (last brief time, OAuth tokens, etc.)
CREATE TABLE IF NOT EXISTS system_state (
    key         TEXT PRIMARY KEY,
    value       JSONB NOT NULL,
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- ─── audit_log ────────────────────────────────────────────────────────────────
-- Every email sent through the system is logged here.
CREATE TABLE IF NOT EXISTS audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action          TEXT NOT NULL,    -- "email_sent" | "email_drafted" | "brief_generated"
    gmail_message_id TEXT,
    recipient       TEXT,
    subject         TEXT,
    confirmed_at    TIMESTAMPTZ,      -- When Garret pressed [Send]
    metadata        JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ─── Row Level Security ───────────────────────────────────────────────────────
-- Enable RLS on all tables (service key bypasses, anon key blocked)
ALTER TABLE contacts        ENABLE ROW LEVEL SECURITY;
ALTER TABLE deals           ENABLE ROW LEVEL SECURITY;
ALTER TABLE threads         ENABLE ROW LEVEL SECURITY;
ALTER TABLE email_cache     ENABLE ROW LEVEL SECURITY;
ALTER TABLE decisions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE tone_samples    ENABLE ROW LEVEL SECURITY;
ALTER TABLE system_state    ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log       ENABLE ROW LEVEL SECURITY;

-- Service role has full access (our app uses service key)
CREATE POLICY "service_full_access" ON contacts        FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON deals           FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON threads         FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON email_cache     FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON decisions       FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON tone_samples    FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON system_state    FOR ALL TO service_role USING (true);
CREATE POLICY "service_full_access" ON audit_log       FOR ALL TO service_role USING (true);

-- ─── Helper functions ────────────────────────────────────────────────────────

-- Auto-update updated_at timestamps
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER contacts_updated_at    BEFORE UPDATE ON contacts    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
CREATE TRIGGER deals_updated_at       BEFORE UPDATE ON deals       FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- Semantic search helper (returns top-k similar records)
CREATE OR REPLACE FUNCTION match_threads(
    query_embedding vector(1536),
    match_count     INT DEFAULT 5,
    match_threshold FLOAT DEFAULT 0.7
)
RETURNS TABLE(id UUID, subject TEXT, summary TEXT, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, subject, summary,
           1 - (embedding <=> query_embedding) AS similarity
    FROM threads
    WHERE embedding IS NOT NULL
      AND 1 - (embedding <=> query_embedding) > match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;

CREATE OR REPLACE FUNCTION match_deals(
    query_embedding vector(1536),
    match_count     INT DEFAULT 5,
    match_threshold FLOAT DEFAULT 0.7
)
RETURNS TABLE(id UUID, name TEXT, stage TEXT, similarity FLOAT)
LANGUAGE sql STABLE AS $$
    SELECT id, name, stage,
           1 - (embedding <=> query_embedding) AS similarity
    FROM deals
    WHERE embedding IS NOT NULL
      AND 1 - (embedding <=> query_embedding) > match_threshold
    ORDER BY embedding <=> query_embedding
    LIMIT match_count;
$$;
