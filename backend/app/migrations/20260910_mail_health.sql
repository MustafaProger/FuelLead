ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS verification_error_category VARCHAR(30);
ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS verification_retry_at TIMESTAMPTZ;
ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS imap_verification_status VARCHAR(30) NOT NULL DEFAULT 'unverified';
ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS imap_verification_error TEXT;
ALTER TABLE sender_accounts ADD COLUMN IF NOT EXISTS imap_verification_checked_at TIMESTAMPTZ;

-- Only proven legacy network errors are eligible for automatic connection checks.
-- Old 550 refusals require an explicit check; no delivery rows are retried.
UPDATE sender_accounts
SET verification_error_category = 'connection', verification_retry_at = now()
WHERE verification_status = 'temporary_error'
  AND verification_error_category IS NULL
  AND verification_error IN ('Не удалось подключиться к Mail.ru', 'Mail.ru не ответил вовремя. Повторите проверку позже');

INSERT INTO schema_migrations(version) VALUES ('20260910_mail_health') ON CONFLICT DO NOTHING;
