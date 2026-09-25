from contextlib import nullcontext
from email.message import EmailMessage

import pytest
from sqlalchemy import select

from app.commands import reprocess_replies as command
from app.config import Settings
from app.models import ActivityHistory, Company, CompanyEmail, EmailSuppression, ImapProcessedMessage, ImapReplyFolder, SenderAccount
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.inbound_replies import apply_client_reply


def raw_reply(message_id, body='Пришлите условия', sender='client@example.ru'):
    message = EmailMessage()
    message['From'] = sender
    message['To'] = 'sender@mail.ru'
    message['Message-ID'] = message_id
    message['Subject'] = 'Ответ на предложение'
    message.set_content(body)
    return message.as_bytes()


@pytest.fixture()
def mailbox(db):
    key = generate_encryption_key()
    settings = Settings(_env_file=None, mail_credentials_encryption_key=key)
    account = SenderAccount(email='sender@mail.ru', encrypted_password=CredentialCipher(key).encrypt('password'),
                            imap_enabled=True, imap_verification_status='verified', imap_last_uid=900,
                            imap_uidvalidity=123)
    company = Company(name='Клиент', inn='7700000000', status='sent',
                      emails=[CompanyEmail(email='client@example.ru')])
    db.add_all([account, company])
    db.commit()
    folder = ImapReplyFolder(sender_account_id=account.id, mailbox='Archive', last_uid=700, uidvalidity=456)
    db.add(folder)
    db.commit()
    return settings, account, company, folder


def client_for(messages, *, validity=123, connected=None):
    class Client:
        def __init__(self, account, password, **kwargs):
            assert password == 'password'
            self.account_id = account.id
            self.uidvalidity = validity
            if connected is not None:
                connected.append(account.id)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def reply_mailboxes(self):
            return [name for name in messages.get(self.account_id, {}) if name != 'INBOX']

        def messages_after(self, cursor, limit, *, mailbox, expected_uidvalidity):
            assert expected_uidvalidity in (None, self.uidvalidity)
            return [(uid, raw) for uid, raw in messages.get(self.account_id, {}).get(mailbox, []) if uid > cursor][:limit]

    return Client


def test_dry_run_counts_unique_missing_and_existing_without_mutations(db, mailbox):
    settings, account, company, folder = mailbox
    old = raw_reply('<old@example.ru>')
    new = raw_reply('<new@example.ru>')
    apply_client_reply(db, account.id, old)
    event = db.scalar(select(ActivityHistory).where(ActivityHistory.event_type == 'email_reply'))
    event.event_data = {**event.event_data, 'read_at': '2026-09-24T12:00:00+00:00'}
    company.status = 'customer'
    db.commit()
    before = dict(event.event_data)
    records = []
    result = command.reprocess_replies(db, settings, emit=records.append,
        client_factory=client_for({account.id: {'INBOX': [(1, old), (2, new)], 'Archive': [(8, new)]}}))
    assert result == {'type': 'summary', 'account_ids': [account.id], 'scanned': 3, 'matched': 2,
                      'missing': 1, 'existing': 1, 'duplicate_matches': 1, 'created': 0,
                      'errors': 0, 'apply': False, 'replies_only': False}
    assert records[-1] == result
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert event.event_data == before and company.status == 'customer'
    assert (account.imap_last_uid, account.imap_uidvalidity) == (900, 123)
    assert (folder.last_uid, folder.uidvalidity) == (700, 456)
    assert db.query(EmailSuppression).count() == 0


@pytest.mark.parametrize('validity, expected_outcome', [(123, 'answered'), (999, 'unrecognized')])
def test_apply_and_second_rescan_prove_no_duplicates_and_preserve_cursors_read_state(db, mailbox, validity, expected_outcome):
    settings, account, company, folder = mailbox
    raw = raw_reply('<new@example.ru>')
    processed = ImapProcessedMessage(sender_account_id=account.id, uid=1, outcome='unrecognized')
    db.add(processed)
    db.commit()
    factory = client_for({account.id: {'INBOX': [(1, raw)], 'Archive': [(9, raw)]}}, validity=validity)
    result = command.reprocess_replies(db, settings, apply=True, replies_only=True, client_factory=factory, emit=lambda _: None)
    assert (result['missing'], result['existing'], result['created']) == (1, 0, 1)
    assert processed.outcome == expected_outcome
    event = db.scalar(select(ActivityHistory).where(ActivityHistory.event_type == 'email_reply'))
    event.event_data = {**event.event_data, 'read_at': '2026-09-24T12:00:00+00:00'}
    company.status = 'customer'
    db.commit()
    changed_at = company.last_updated_at
    result = command.reprocess_replies(db, settings, apply=True, replies_only=True, client_factory=factory, emit=lambda _: None)
    assert (result['missing'], result['existing'], result['created'], result['duplicate_matches']) == (0, 1, 0, 1)
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert event.event_data['read_at'] == '2026-09-24T12:00:00+00:00'
    assert company.status == 'customer' and company.last_updated_at == changed_at
    assert (account.imap_last_uid, account.imap_uidvalidity) == (900, 123)
    assert (folder.last_uid, folder.uidvalidity) == (700, 456)


def test_replies_only_skips_global_protection_but_still_applies_incoming_refusals(db, mailbox):
    settings, account, company, _ = mailbox
    unrelated = Company(name='Старый отказ', inn='7700000001', status='rejected',
                        emails=[CompanyEmail(email='unrelated@example.ru')])
    db.add(unrelated)
    db.commit()
    factory = client_for({account.id: {'INBOX': [(1, raw_reply('<refusal@example.ru>', 'Не пишите мне больше'))]}})
    command.reprocess_replies(db, settings, apply=True, replies_only=True, client_factory=factory, emit=lambda _: None)
    assert company.status == 'rejected'
    assert set(db.scalars(select(EmailSuppression.email))) == {'client@example.ru'}
    records = []
    command.reprocess_replies(db, settings, apply=True, client_factory=factory, emit=records.append)
    assert set(db.scalars(select(EmailSuppression.email))) == {'client@example.ru', 'unrelated@example.ru'}
    assert {'already_rejected_protected': 2} in records


def test_account_filter_scans_only_selected_eligible_mailboxes(db, mailbox):
    settings, account, _, _ = mailbox
    other = SenderAccount(email='other@mail.ru', encrypted_password=account.encrypted_password,
                          imap_enabled=True, imap_verification_status='verified')
    failed = SenderAccount(email='failed@mail.ru', encrypted_password=account.encrypted_password,
                           imap_enabled=True, imap_verification_status='failed')
    disabled = SenderAccount(email='disabled@mail.ru', encrypted_password=account.encrypted_password,
                             imap_enabled=False, imap_verification_status='verified')
    db.add_all([other, failed, disabled])
    db.commit()
    connected = []
    result = command.reprocess_replies(db, settings, account_ids=[other.id, failed.id, disabled.id],
                                      client_factory=client_for({}, connected=connected), emit=lambda _: None)
    assert connected == result['account_ids'] == [other.id]
    connected.clear()
    command.reprocess_replies(db, settings, client_factory=client_for({}, connected=connected), emit=lambda _: None)
    assert connected == [account.id, other.id]


def test_uidvalidity_change_aborts_before_applying_new_generation(db, mailbox):
    settings, account, _, folder = mailbox
    old = raw_reply('<old@example.ru>')
    new = raw_reply('<new-generation@example.ru>')

    class Changed(client_for({})):
        def messages_after(self, cursor, limit, *, mailbox, expected_uidvalidity):
            if cursor == 0:
                return [(uid, old) for uid in range(1, 101)]
            self.uidvalidity = 999
            return [(101, new)]

    result = command.reprocess_replies(db, settings, apply=True, replies_only=True,
                                      client_factory=Changed, emit=lambda _: None)
    assert result['errors'] == 1 and result['created'] == 1 and result['scanned'] == 100
    assert db.query(ActivityHistory).filter_by(event_type='email_reply').count() == 1
    assert (account.imap_last_uid, account.imap_uidvalidity, folder.last_uid) == (900, 123, 700)


def test_cli_passes_repeated_account_ids_and_reports_errors(db, mailbox, monkeypatch):
    settings, _, _, _ = mailbox
    calls = []
    monkeypatch.setattr(command, 'get_settings', lambda: settings)
    monkeypatch.setattr(command, 'SessionLocal', lambda: nullcontext(db))
    monkeypatch.setattr(command, 'create_database', lambda: calls.append('create_database'))

    def run(session, config, **kwargs):
        assert session is db and config is settings
        calls.append(kwargs)
        return {'errors': 1}

    monkeypatch.setattr(command, 'reprocess_replies', run)
    with pytest.raises(SystemExit) as exc:
        command.main(['--apply', '--replies-only', '--account-id', '2', '--account-id', '4'])
    assert exc.value.code == 1
    assert calls == ['create_database', {'apply': True, 'replies_only': True, 'account_ids': [2, 4]}]
    with pytest.raises(SystemExit) as exc:
        command.main(['--account-id', '0'])
    assert exc.value.code == 2
