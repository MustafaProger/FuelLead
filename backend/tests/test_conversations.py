from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.config import Settings
from app.models import ActivityHistory, Company, CompanyEmail, EmailReplyAttempt, EmailSuppression, SenderAccount
from app.schemas import ConversationReplyRequest
from app.services.conversations import conversation_detail, conversation_list, mark_read, send_reply
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.inbound_replies import apply_client_reply, parse_client_reply
from app.services.smtp import SMTPAccepted, SMTPDeliveryError


def setup_thread(db, text="Пришлите условия"):
    key = generate_encryption_key()
    settings = Settings(_env_file=None, mail_credentials_encryption_key=key)
    account = SenderAccount(email="sender@mail.ru", encrypted_password=CredentialCipher(key).encrypt("password"), verification_status="verified")
    company = Company(name="Транспорт", inn="7700000000", status="sent")
    company.emails = [CompanyEmail(email="client@example.ru")]
    db.add_all([account, company]); db.commit()
    raw = EmailMessage()
    raw["From"] = "client@example.ru"; raw["To"] = account.email
    raw["Message-ID"] = "<client@example.ru>"; raw["Subject"] = "Условия поставки"
    raw["Date"] = "Tue, 15 Sep 2026 10:00:00 +0300"
    raw.set_content(text)
    apply_client_reply(db, account.id, raw.as_bytes()); db.commit()
    event = db.scalar(select(ActivityHistory).where(ActivityHistory.event_type == "email_reply"))
    return settings, account, company, event, raw


class Sender:
    calls = []
    def __init__(self, account, password, **kwargs): self.account = account
    def send(self, recipient, subject, body, **kwargs):
        self.calls.append((self.account.id, recipient, subject, body, kwargs))
        return SMTPAccepted(kwargs["message_id"], "250", "OK", sent_copy_saved=True)


def test_full_body_headers_and_rescan_preserve_read_and_status(db):
    _, account, company, event, raw = setup_thread(db, "Ответ " * 1000 + "\n> Старое предложение")
    result = conversation_detail(db, company.id)
    assert len(result["messages"][0]["body"]) > 2000
    assert result["messages"][0]["subject"] == "Условия поставки"
    assert result["messages"][0]["created_at"].startswith("2026-09-15T10:00")
    assert result["messages"][0]["is_automatic"] is False
    assert conversation_list(db)["unread_count"] == 1
    mark_read(db, company.id, event.id)
    company.status = "customer"; db.commit()
    apply_client_reply(db, account.id, raw.as_bytes()); db.commit()
    assert conversation_list(db)["unread_count"] == 0 and company.status == "customer"
    assert len(conversation_detail(db, company.id)["messages"]) == 1
    assert conversation_list(db, unread=True)["items"] == []


def test_automatic_reply_is_visible_and_labelled_in_conversation(db):
    _, account, company, _, raw = setup_thread(db)
    company.status = 'sent'
    db.add(ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', event_data={
        'message_id': '<original@mail.ru>', 'sender_account_id': account.id, 'recipient': 'client@example.ru'}))
    raw.replace_header('Message-ID', '<vacation@example.ru>')
    raw.replace_header('Subject', 'Автоматический ответ: Условия поставки')
    raw['Auto-Submitted'] = 'auto-replied'
    raw['In-Reply-To'] = '<original@mail.ru>'
    raw.set_content('С 14 по 21 сентября я в отпуске. Не пишите на этот адрес в период отпуска.')
    db.commit()
    apply_client_reply(db, account.id, raw.as_bytes()); db.commit()
    listing = conversation_list(db)
    assert listing['items'][0]['is_automatic'] is True
    assert listing['unread_count'] == 2 and company.status == 'sent'
    detail = conversation_detail(db, company.id)
    automatic = next(item for item in detail['messages'] if item.get('is_automatic'))
    assert automatic['direction'] == 'incoming' and automatic['status'] == 'received'
    assert automatic['body'].startswith('С 14 по 21 сентября я в отпуске.')
    assert automatic['unread'] is True
    assert all(item['is_automatic'] is False for item in detail['messages'] if item['direction'] == 'outgoing')
    mark_read(db, company.id, int(automatic['id']))
    assert conversation_list(db)['unread_count'] == 0
    assert db.query(EmailSuppression).count() == 0


@pytest.mark.parametrize('reply_delay', [-3, 0, 3])
def test_reply_follows_referenced_outgoing_despite_clock_skew(db, reply_delay):
    _, account, company, reply, _ = setup_thread(db)
    accepted_at = datetime(2026, 9, 17, 1, 2, 33, tzinfo=timezone.utc)
    reply_date = (accepted_at + timedelta(seconds=reply_delay)).astimezone(timezone(timedelta(hours=3))).isoformat()
    reply.event_data = {**reply.event_data, 'sent_at': reply_date, 'references': ['<offer@mail.ru>']}
    original = ActivityHistory(company_id=company.id, event_type='email_sent', description='sent', created_at=accepted_at,
        event_data={'message_id': '<offer@mail.ru>', 'sender_account_id': account.id, 'recipient': 'client@example.ru'})
    earlier = ActivityHistory(company_id=company.id, event_type='email_sent', description='earlier',
        created_at=accepted_at - timedelta(minutes=1), event_data={'message_id': '<earlier@mail.ru>'})
    later = ActivityHistory(company_id=company.id, event_type='email_sent', description='later',
        created_at=accepted_at + timedelta(minutes=1), event_data={'message_id': '<later@mail.ru>'})
    db.add_all([later, original, earlier]); db.commit()
    messages = conversation_detail(db, company.id)['messages']
    assert [item['id'] for item in messages] == [str(earlier.id), str(original.id), str(reply.id), str(later.id)]
    assert messages[2]['created_at'] == reply_date


def test_read_snapshot_does_not_swallow_a_new_reply(db):
    _, account, company, old, raw = setup_thread(db)
    raw.replace_header("Message-ID", "<new@example.ru>")
    apply_client_reply(db, account.id, raw.as_bytes()); db.commit()
    mark_read(db, company.id, old.id)
    assert conversation_list(db)["unread_count"] == 1
    assert conversation_list(db, search="client@")["total"] == 1
    assert conversation_list(db, search="unrelated")["total"] == 0


def test_reply_uses_same_mailbox_headers_literal_body_and_idempotency(db):
    settings, account, company, event, _ = setup_thread(db)
    Sender.calls = []
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=event.id, body="Здравствуйте! {{это не шаблон}}")
    first = send_reply(db, company.id, request, settings, sender_factory=Sender)
    assert send_reply(db, company.id, request, settings, sender_factory=Sender) == first
    assert first["status"] == "accepted" and len(Sender.calls) == 1
    source, target, subject, body, headers = Sender.calls[0]
    assert source == account.id and target == "client@example.ru"
    assert subject == "Re: Условия поставки" and body == request.body
    assert headers["in_reply_to"] == "<client@example.ru>"
    assert headers["references"] == ("<client@example.ru>",)
    assert account.sent_today == 1 and company.status == "answered"
    messages = conversation_detail(db, company.id)["messages"]
    assert len(messages) == 2
    assert any(m["body"] == request.body and m["direction"] == "outgoing" for m in messages)


@pytest.mark.parametrize("blocked", ["rejected", "suppressed", "disabled", "unverified", "quota"])
def test_reply_cannot_bypass_mailbox_and_refusal_rules(db, blocked):
    settings, account, company, event, _ = setup_thread(db)
    if blocked == "rejected": company.status = "rejected"
    elif blocked == "suppressed": db.add(EmailSuppression(email="client@example.ru", reason="Отказ", source="manual"))
    elif blocked == "disabled": account.is_active = False
    elif blocked == "unverified": account.verification_status = "failed"
    else: account.sent_today_date = datetime.now(settings.timezone).date(); account.sent_today = account.daily_limit
    db.commit()
    with pytest.raises(HTTPException):
        send_reply(db, company.id, ConversationReplyRequest(request_id=uuid4(), reply_id=event.id, body="Ответ"), settings, sender_factory=Sender)
    assert db.query(EmailReplyAttempt).count() == 0


@pytest.mark.parametrize("uncertain", [True, False])
def test_network_result_preserves_receipt_and_never_retries_same_request(db, uncertain):
    settings, account, company, event, _ = setup_thread(db)
    class Broken(Sender):
        calls = 0
        def send(self, *args, **kwargs):
            Broken.calls += 1
            raise SMTPDeliveryError("Ошибка сети", category="connection", uncertain=uncertain)
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=event.id, body="Ответ")
    result = send_reply(db, company.id, request, settings, sender_factory=Broken)
    assert result["status"] == ("uncertain" if uncertain else "failed")
    assert send_reply(db, company.id, request, settings, sender_factory=Broken) == result
    assert Broken.calls == 1 and account.sent_today == int(uncertain)
    assert not db.scalar(select(ActivityHistory.id).where(ActivityHistory.event_type == "email_sent"))
    assert any(m["status"] == result["status"] for m in conversation_detail(db, company.id)["messages"])
    if uncertain:
        with pytest.raises(HTTPException):
            send_reply(db, company.id, request.model_copy(update={"request_id": uuid4()}), settings, sender_factory=Broken)


def test_cross_company_reference_and_reused_request_payload_rejected(db):
    settings, _, company, event, _ = setup_thread(db)
    other = Company(name="Другой", inn="7700000001"); db.add(other); db.commit()
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=event.id, body="Ответ")
    with pytest.raises(HTTPException): send_reply(db, other.id, request, settings, sender_factory=Sender)
    send_reply(db, company.id, request, settings, sender_factory=Sender)
    with pytest.raises(HTTPException): send_reply(db, company.id, request.model_copy(update={"body": "Другое"}), settings, sender_factory=Sender)


def test_attachments_and_script_content(db):
    _, account, company, _, raw = setup_thread(db)
    raw.replace_header("Message-ID", "<html@example.ru>")
    raw.set_content('<p>Спасибо</p><script>alert(1)</script><blockquote>Не писать</blockquote>', subtype="html")
    raw.add_attachment(b"content", maintype="application", subtype="pdf", filename="Условия.pdf")
    parsed = parse_client_reply(raw.as_bytes())
    assert "alert(1)" not in parsed.body and not parsed.refusal
    assert "Не писать" in parsed.body and "Не писать" not in parsed.text
    apply_client_reply(db, account.id, raw.as_bytes()); db.commit()
    assert any(m["attachments"] == ["Условия.pdf"] for m in conversation_detail(db, company.id)["messages"])
    raw = EmailMessage(); raw["From"] = "client@example.ru"
    raw.add_attachment(b"pdf", maintype="application", subtype="pdf", filename="Файл.pdf")
    assert parse_client_reply(raw.as_bytes()).attachments == ("Файл.pdf",)


def test_quote_only_does_not_turn_quoted_opt_out_into_refusal():
    raw = EmailMessage(); raw["From"] = "client@example.ru"; raw.set_content("> Не писать")
    assert parse_client_reply(raw.as_bytes()).refusal is None


def test_blank_reply_rejected():
    with pytest.raises(ValidationError): ConversationReplyRequest(request_id=uuid4(), reply_id=1, body=" \n ")


def test_uncertain_result_can_be_resolved_without_resending(db):
    from app.services.conversations import resolve_reply
    settings, _, company, event, _ = setup_thread(db)
    class Broken(Sender):
        def send(self, *args, **kwargs): raise RuntimeError("unknown")
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=event.id, body="Ответ")
    assert send_reply(db, company.id, request, settings, sender_factory=Broken)["status"] == "uncertain"
    assert resolve_reply(db, company.id, str(request.request_id), "accepted")["status"] == "accepted"
    assert send_reply(db, company.id, request, settings, sender_factory=Broken)["status"] == "accepted"
    assert len(conversation_detail(db, company.id)["messages"]) == 2


def test_api_authentication_and_conversation_routes(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app import main
    from app.database import get_db
    from app.auth import AUTH_COOKIE_NAME, create_session_token
    _, _, company, event, _ = setup_thread(db)
    settings = Settings(_env_file=None, fuellead_auth_email="operator@example.ru", fuellead_auth_password="test",
                        fuellead_auth_session_secret="isolated-test-secret")
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    main.app.dependency_overrides[get_db] = lambda: db
    try:
        client = TestClient(main.app)
        assert client.get("/api/conversations").status_code == 401
        assert client.post(f"/api/companies/{company.id}/conversation/reply", json={}).status_code == 401
        client.cookies.set(AUTH_COOKIE_NAME, create_session_token(settings.fuellead_auth_email, settings))
        assert client.get("/api/conversations").json()["total"] == 1
        assert client.get(f"/api/companies/{company.id}/conversation").json()["latest_reply_id"] == event.id
        assert client.post(f"/api/companies/{company.id}/conversation/read", json={"through_id": event.id}).status_code == 200
        assert client.get("/api/conversations?unread=true").json()["items"] == []
    finally:
        main.app.dependency_overrides.clear()
