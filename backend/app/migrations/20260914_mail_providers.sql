ALTER TABLE sender_accounts DROP CONSTRAINT IF EXISTS ck_sender_accounts_provider;
ALTER TABLE sender_accounts ADD CONSTRAINT ck_sender_accounts_provider
    CHECK (provider IN ('gmail_api','mailru_smtp','gmail_smtp','yandex_smtp'));

INSERT INTO schema_migrations (version) VALUES ('20260914_mail_providers')
ON CONFLICT (version) DO NOTHING;
