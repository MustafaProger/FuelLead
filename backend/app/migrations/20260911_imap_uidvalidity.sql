-- UIDs identify messages only within one server-provided UIDVALIDITY namespace.
-- Keep existing cursors intact until INBOX reports its namespace at runtime.
ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS imap_uidvalidity BIGINT;

INSERT INTO schema_migrations(version) VALUES ('20260911_imap_uidvalidity') ON CONFLICT DO NOTHING;
