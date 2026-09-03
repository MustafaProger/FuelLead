CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(100) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sender_accounts (
    id SERIAL PRIMARY KEY,
    provider VARCHAR(30) NOT NULL DEFAULT 'mailru_smtp',
    email VARCHAR(320) NOT NULL UNIQUE,
    display_name VARCHAR(200) NOT NULL DEFAULT '',
    encrypted_password TEXT,
    smtp_host VARCHAR(255) NOT NULL DEFAULT 'smtp.mail.ru',
    smtp_port INTEGER NOT NULL DEFAULT 465,
    imap_host VARCHAR(255) NOT NULL DEFAULT 'imap.mail.ru',
    imap_port INTEGER NOT NULL DEFAULT 993,
    smtp_enabled BOOLEAN NOT NULL DEFAULT true,
    imap_enabled BOOLEAN NOT NULL DEFAULT false,
    is_active BOOLEAN NOT NULL DEFAULT true,
    verification_status VARCHAR(30) NOT NULL DEFAULT 'unverified',
    verification_error TEXT,
    verification_checked_at TIMESTAMPTZ,
    daily_limit INTEGER NOT NULL DEFAULT 50,
    sent_today INTEGER NOT NULL DEFAULT 0,
    sent_today_date DATE,
    successful_full_batches INTEGER NOT NULL DEFAULT 0,
    current_batch_size INTEGER NOT NULL DEFAULT 5,
    blocked_until_round INTEGER,
    block_reason TEXT,
    last_sent_at TIMESTAMPTZ,
    imap_last_uid BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_sender_accounts_provider CHECK (provider IN ('gmail_api','mailru_smtp')),
    CONSTRAINT ck_sender_accounts_verification_status CHECK (
        verification_status IN ('unverified','verified','failed','blocked','temporary_error')
    ),
    CONSTRAINT ck_sender_accounts_daily_limit CHECK (daily_limit > 0)
);

ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS accepted_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS bounced_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS uncertain_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS suppressed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS subject_snapshot TEXT;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS body_snapshot TEXT;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS recipients_snapshot JSON NOT NULL DEFAULT '[]';
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS sender_account_ids JSON NOT NULL DEFAULT '[]';
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS scheduler_settings JSON NOT NULL DEFAULT '{}';
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS snapshot_expires_at TIMESTAMPTZ;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS confirmed_at TIMESTAMPTZ;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS current_round INTEGER NOT NULL DEFAULT 1;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS sender_position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS batch_position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS current_batch_target INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS current_batch_sender_id INTEGER REFERENCES sender_accounts(id) ON DELETE SET NULL;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS current_interval_seconds INTEGER;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS round_rest_until TIMESTAMPTZ;
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS worker_claim_token VARCHAR(64);
ALTER TABLE outreach_campaigns ADD COLUMN IF NOT EXISTS worker_claimed_at TIMESTAMPTZ;
ALTER TABLE outreach_campaigns ALTER COLUMN started_at DROP NOT NULL;

-- Drop the legacy check before translating legacy values. Otherwise PostgreSQL
-- rejects the UPDATE even though the replacement constraint follows below.
ALTER TABLE outreach_campaigns DROP CONSTRAINT IF EXISTS ck_outreach_campaigns_status;
UPDATE outreach_campaigns SET accepted_count = sent_count WHERE accepted_count = 0 AND sent_count > 0;
UPDATE outreach_campaigns SET status = 'stopped' WHERE status = 'cancelled';
UPDATE outreach_campaigns
SET status = 'interrupted',
    pause_reason = COALESCE(pause_reason, 'Старая Gmail-кампания остановлена миграцией; автоматический повтор запрещён'),
    next_send_at = NULL
WHERE status IN ('running','paused') AND sender_account_ids::text IN ('[]','null');

ALTER TABLE outreach_campaigns ADD CONSTRAINT ck_outreach_campaigns_status CHECK (
    status IN ('draft','running','paused','cooldown','interrupted','completed','stopped')
);

ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS sender_account_id INTEGER REFERENCES sender_accounts(id) ON DELETE SET NULL;
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS smtp_code VARCHAR(40);
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS smtp_response VARCHAR(500);
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS interval_seconds INTEGER;
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS claim_token VARCHAR(64);
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;
ALTER TABLE outreach_deliveries ADD COLUMN IF NOT EXISTS accepted_at TIMESTAMPTZ;

ALTER TABLE outreach_deliveries DROP CONSTRAINT IF EXISTS ck_outreach_deliveries_status;
UPDATE outreach_deliveries SET status = 'accepted', accepted_at = COALESCE(accepted_at, sent_at) WHERE status = 'sent';
ALTER TABLE outreach_deliveries ADD CONSTRAINT ck_outreach_deliveries_status CHECK (
    status IN ('queued','sending','accepted','failed','bounced','uncertain','suppressed','cancelled')
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_outreach_deliveries_claim_token
    ON outreach_deliveries (claim_token) WHERE claim_token IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_outreach_deliveries_sender_account_id
    ON outreach_deliveries (sender_account_id);
CREATE INDEX IF NOT EXISTS ix_outreach_deliveries_accepted_at
    ON outreach_deliveries (accepted_at);

CREATE TABLE IF NOT EXISTS email_suppressions (
    id SERIAL PRIMARY KEY,
    email VARCHAR(320) NOT NULL UNIQUE,
    reason VARCHAR(500) NOT NULL,
    source VARCHAR(50) NOT NULL,
    campaign_id INTEGER REFERENCES outreach_campaigns(id) ON DELETE SET NULL,
    delivery_id INTEGER REFERENCES outreach_deliveries(id) ON DELETE SET NULL,
    smtp_code VARCHAR(40),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lifted_at TIMESTAMPTZ,
    comment TEXT
);

CREATE TABLE IF NOT EXISTS imap_processed_messages (
    id SERIAL PRIMARY KEY,
    sender_account_id INTEGER NOT NULL REFERENCES sender_accounts(id) ON DELETE CASCADE,
    uid BIGINT NOT NULL,
    message_id VARCHAR(255),
    outcome VARCHAR(30) NOT NULL,
    delivery_id INTEGER REFERENCES outreach_deliveries(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_imap_account_uid UNIQUE (sender_account_id, uid)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_one_active_outreach_campaign
    ON outreach_campaigns ((1))
    WHERE status IN ('running','paused','cooldown','interrupted');

INSERT INTO schema_migrations (version)
VALUES ('20260903_mailru_scheduler')
ON CONFLICT (version) DO NOTHING;
