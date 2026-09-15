"""Provider coverage with isolated databases and fake transports only."""
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app import main
from app.config import Settings
from app.mail_providers import MAIL_PROVIDERS
from app.models import SenderAccount
from app.schemas import SenderAccountCreate, SenderAccountUpdate
from app.services.credentials import generate_encryption_key
from app.services.imap_bounces import MailruIMAPClient, process_imap_tick, process_reply_folders_tick
from app.services.outreach import _verified_senders
from app.services.sender_accounts import create_sender_account, recover_temporary_sender_accounts, verify_sender_account
from app.services.smtp import MailruSMTPClient


@pytest.mark.parametrize('provider', MAIL_PROVIDERS)
def test_create_verify_campaign_and_workers(db, monkeypatch, provider):
    preset = MAIL_PROVIDERS[provider]
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    calls = []
    class SMTP:
        def __init__(self, account, password, **kwargs):
            assert (account.smtp_host, account.smtp_port) == (preset.smtp_host, 465)
            assert password == 'fake-password'
        def verify(self): calls.append('smtp')
    class IMAP:
        uidvalidity = 1
        def __init__(self, account, password, **kwargs):
            assert (account.imap_host, account.imap_port) == (preset.imap_host, 993)
        def __enter__(self): calls.append('imap'); return self
        def __exit__(self, *args): pass
        def new_messages(self, *args): return iter([])
        def reply_mailboxes(self): calls.append('folders'); return []
    def verify(db, account, settings):
        return verify_sender_account(db, account, settings, smtp_client_factory=SMTP, imap_client_factory=IMAP)
    monkeypatch.setattr(main, 'verify_sender_account', verify)
    payload = main.add_sender_account(SenderAccountCreate(provider=provider, email=f'OWNER@{preset.domains[0]}', password='fake-password', imap_enabled=True), db, settings)
    assert payload['provider'] == provider
    assert payload['verification_status'] == payload['imap_verification_status'] == 'verified'
    assert payload['email'] == f'owner@{preset.domains[0]}'
    assert 'fake-password' not in str(payload)
    assert calls == ['smtp', 'imap']
    assert [a.id for a in _verified_senders(db)] == [payload['id']]
    account = db.get(SenderAccount, payload['id'])
    account.verification_status = 'temporary_error'
    account.verification_error_category = 'connection'
    account.verification_retry_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    account.sent_today = 12
    db.commit()
    calls.clear()
    factory = lambda: nullcontext(db)
    recover_temporary_sender_accounts(settings, session_factory=factory, smtp_client_factory=SMTP, imap_client_factory=IMAP)
    process_imap_tick(settings, session_factory=factory, client_factory=IMAP)
    process_reply_folders_tick(settings, session_factory=factory, client_factory=IMAP)
    assert calls == ['smtp', 'imap', 'imap', 'imap', 'folders']
    assert account.verification_status == 'verified'
    assert account.sent_today == 12


@pytest.mark.parametrize('provider', MAIL_PROVIDERS)
def test_transport_tls_endpoints_and_sent_policy(db, provider):
    preset = MAIL_PROVIDERS[provider]
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    account = create_sender_account(db, SenderAccountCreate(provider=provider, email=f'owner@{preset.domains[0]}', password='fake', imap_enabled=True), settings)
    calls = []
    class SMTP:
        def __init__(self, host, port, *, context, timeout):
            assert (host, port) == (preset.smtp_host, 465)
            assert context.check_hostname
        def ehlo(self): return 250, b'hello'
        def login(self, email, password): assert (email, password) == (account.email, 'fake')
        def mail(self, sender): return 250, b'ok'
        def rcpt(self, recipient): return 250, b'ok'
        def data(self, raw): calls.append('data'); return 250, b'2.0.0 accepted'
        def quit(self): pass
    class IMAP:
        def __init__(self, host, port, *, ssl_context, timeout):
            assert (host, port) == (preset.imap_host, 993)
            assert ssl_context.check_hostname
        def login(self, email, password): return 'OK', []
        def list(self): return 'OK', [b'(\\Sent) "/" "Sent"']
        def append(self, *args): calls.append('append'); return 'OK', []
        def logout(self): pass
    def imap_factory(account, password, **kwargs):
        return MailruIMAPClient(account, password, imap_factory=IMAP, **kwargs)
    result = MailruSMTPClient(account, 'fake', smtp_factory=SMTP, imap_client_factory=imap_factory).send('recipient@example.org', 'Subject', 'Body')
    assert result.sent_copy_saved
    assert calls == (['data'] if provider == 'gmail_smtp' else ['data', 'append'])


@pytest.mark.parametrize('provider,email', [('gmail_smtp', 'owner@mail.ru'), ('yandex_smtp', 'owner@gmail.com'), ('mailru_smtp', 'owner@yandex.ru'), ('gmail_api', 'owner@gmail.com'), ('unknown', 'owner@example.org')])
def test_wrong_provider_or_domain_rejected(provider, email):
    with pytest.raises(ValidationError):
        SenderAccountCreate(provider=provider, email=email, password='fake')


def test_grouped_google_password_for_create_and_replacement():
    data = SenderAccountCreate(provider='gmail_smtp', email='owner@gmail.com', password='abcd efgh ijkl mnop')
    assert data.password == 'abcdefghijklmnop'
    assert SenderAccountUpdate(password='abcd efgh ijkl mnop').password == data.password


def test_legacy_api_sender_is_not_selected(db):
    db.add(SenderAccount(provider='gmail_api', email='legacy@gmail.com', encrypted_password='legacy', verification_status='verified'))
    db.commit()
    assert _verified_senders(db) == []
