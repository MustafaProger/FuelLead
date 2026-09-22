from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from app import database, main
from app.auth import AUTH_COOKIE_NAME, create_session_token
from app.config import Settings, get_settings
from app.database import get_db
from app.schemas import EmailPreviewRequest, EmailSendRequest, EmailTemplateUpdate, CompanyFilters
from app.services.email_attachments import create_attachment, get_attachments, MailAttachment, MAX_ATTACHMENT_BYTES
from app.services.email_templates import artel_offer_preset, render_email_content
from app.services.outreach import build_outreach_preflight, confirm_outreach_campaign
from app.services.smtp import MailruSMTPClient, SMTPAccepted
from test_email_templates import make_company, mail_settings_and_account
from test_outreach import add_company, add_sender, settings_with_key, tick_at_schedule
from test_smtp import FakeSMTP, FakeSentIMAP, account


def test_html_escapes_company_and_strips_active_content_but_preserves_email_layout():
    plain, html = render_email_content("html", "", '''<script>alert(1)</script><style>bad</style>
        <p style="color:#123456;position:fixed" onclick="evil()">{{company_name}}</p>
        <a href="javascript:alert(1)">Позвонить</a><iframe src="https://example.org"></iframe>
        <table width="100%"><tr><td style="padding:16px">ДТ — 1%</td></tr></table>''',
        {"company_name": '<img src=x onerror="evil()"> & ООО'}, Settings(_env_file=None))
    assert "<script" not in html and "<iframe" not in html and "javascript:" not in html
    assert "onclick=" not in html and "position:" not in html
    assert "&lt;img" in html and "&amp; ООО" in html
    assert "color:#123456" in html and 'width="100%"' in html and "padding:16px" in html
    assert 'ДТ — 1%' in plain and "ответьте «Не писать»" in plain
    assert "Не писать" in html


def test_html_without_visible_content_is_rejected():
    with pytest.raises(ValueError, match="видимый текст"):
        render_email_content("html", "", '<script>alert(1)</script><img src="https://example.org/x">', {}, Settings(_env_file=None))


def test_preset_contains_authorized_terms_and_personalization(db):
    preset = artel_offer_preset()
    company = make_company(db)
    result = main.preview_email_template(EmailPreviewRequest(company_id=company.id, **preset), db, Settings(_env_file=None))
    for text in ('ЛУКОЙЛ', 'Teboil', '1%', '2%', '8 964 517-78-08', '@mustafa_proger', company.name):
        assert text in result["body"]
    assert "СТК" not in result["body"] and "490" not in result["body"]
    assert "{{" not in result["html_body"]
    assert "tel:+79645177808" in result["html_body"]
    assert result["html_body"].lstrip().startswith("<div")


def test_save_preview_and_plain_compatibility(db):
    settings = Settings(_env_file=None)
    file = create_attachment(db, "Условия.pdf", b"%PDF-1.4 example")
    draft = EmailTemplateUpdate(**artel_offer_preset(), attachment_ids=[file.id])
    saved = main.update_email_template(draft, db, settings)
    assert saved["body_format"] == "html" and saved["attachments"][0]["filename"] == "Условия.pdf"
    preview = main.preview_email_template(EmailPreviewRequest(), db, settings)
    assert preview["company_name"] == 'ООО «Пример»' and preview["html_body"]
    assert preview["attachments"][0]["id"] == file.id
    # An explicit empty editor must not silently preview a saved HTML letter.
    with pytest.raises(HTTPException) as exc:
        main.preview_email_template(EmailPreviewRequest(body_format="html", html_template=""), db, settings)
    assert exc.value.status_code == 422
    main.update_email_template(EmailTemplateUpdate(subject_template="Тема", body_template="Текст {{date}}"), db, settings)
    preview = main.preview_email_template(EmailPreviewRequest(), db, settings)
    assert preview["html_body"] is None and preview["body"].startswith("Текст ")
    assert preview["attachments"] == []


@pytest.mark.parametrize("filename,content", [("../file.pdf", b"x"), ("a\r\nb.pdf", b"x"), ("a.exe", b"x"), ("a.pdf", b"")])
def test_reject_invalid_attachments(db, filename, content):
    with pytest.raises(ValueError):
        create_attachment(db, filename, content)


def test_attachment_missing_duplicate_and_total_size_checks(db):
    file = create_attachment(db, "first.pdf", b"x" * (MAX_ATTACHMENT_BYTES // 2 + 1))
    second = create_attachment(db, "second.pdf", b"y" * (MAX_ATTACHMENT_BYTES // 2))
    for ids in (["missing"], [file.id, file.id], [file.id, second.id], [str(i) for i in range(6)]):
        with pytest.raises(ValueError):
            get_attachments(db, ids)


@pytest.mark.parametrize("html", [None, "<p>Здравствуйте, ООО «Тест»</p>"])
def test_smtp_mime_and_sent_copy_preserve_html_and_cyrillic_filename(html):
    FakeSMTP.instances = []
    FakeSentIMAP.messages = []
    sender = account()
    sender.imap_enabled = True
    content = b"%PDF-1.4\x00\xffexample"
    result = MailruSMTPClient(sender, "secret", smtp_factory=FakeSMTP, imap_client_factory=FakeSentIMAP, imap_timeout_seconds=17).send(
        "client@example.ru", "Предложение", "Здравствуйте, ООО «Тест»", html_body=html,
        attachments=[MailAttachment("Предложение АРТЭЛЬ.pdf", "application/pdf", content)])
    raw = next(command[1] for command in FakeSMTP.instances[0].commands if isinstance(command, tuple) and command[0] == "DATA")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message.get_content_type() == "multipart/mixed"
    assert "Здравствуйте" in message.get_body(preferencelist=("plain",)).get_content()
    if html:
        assert message.get_body(preferencelist=("html",)).get_content().strip() == html
    else:
        assert message.get_body(preferencelist=("html",)) is None
    attached = list(message.iter_attachments())
    assert attached[0].get_filename() == "Предложение АРТЭЛЬ.pdf"
    assert attached[0].get_payload(decode=True) == content
    assert result.sent_copy_saved and FakeSentIMAP.messages[0][0] == raw
    assert all(byte < 128 for byte in raw)


def test_single_company_uses_saved_html_and_attachments(db, monkeypatch):
    company = make_company(db)
    settings, _ = mail_settings_and_account(db)
    file = create_attachment(db, "Предложение.pdf", b"%PDF-1.4 example")
    main.update_email_template(EmailTemplateUpdate(**artel_offer_preset(), attachment_ids=[file.id]), db, settings)
    class Sender:
        def __init__(self, *args, **kwargs): pass
        def send(self, recipient, subject, body, **kwargs):
            assert company.name in body and "ЛУКОЙЛ" in body
            assert "{{" not in kwargs["html_body"]
            assert kwargs["attachments"][0].content == file.content
            return SMTPAccepted("<test@example.ru>", "250", "OK")
    monkeypatch.setattr(main, "MailruSMTPClient", Sender)
    main.send_company_email(company.id, EmailSendRequest(), db, settings)
    assert company.history[0].event_data["attachments"][0]["id"] == file.id


def test_campaign_freezes_html_and_files_when_template_changes(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="lead@example.ru")
    file = create_attachment(db, "Предложение.pdf", b"%PDF original")
    main.update_email_template(EmailTemplateUpdate(**artel_offer_preset(), attachment_ids=[file.id]), db, settings)
    preflight = build_outreach_preflight(db, CompanyFilters(), settings)
    assert preflight["sample"]["html_body"] and preflight["sample"]["attachments"][0]["id"] == file.id
    campaign = confirm_outreach_campaign(db, preflight["snapshot_id"], settings)
    main.update_email_template(EmailTemplateUpdate(subject_template="Новый текст", body_template="Другое предложение"), db, settings)
    class Sender:
        def __init__(self, *args, **kwargs): pass
        def send(self, recipient, subject, body, **kwargs):
            assert "ЛУКОЙЛ" in body and "Другое предложение" not in body
            assert "1%" in kwargs["html_body"]
            assert kwargs["attachments"][0].content == b"%PDF original"
            return SMTPAccepted("<snapshot@example.ru>", "250", "OK")
    tick_at_schedule(db, settings, Sender, datetime.now(timezone.utc))
    assert campaign.deliveries[0].status == "accepted"


def test_content_schema_migration_preserves_legacy_data_and_is_idempotent(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE email_templates (id INTEGER PRIMARY KEY, body_template TEXT NOT NULL)")
        conn.exec_driver_sql("INSERT INTO email_templates VALUES (1, 'Старый текст')")
        conn.exec_driver_sql("CREATE TABLE outreach_campaigns (id INTEGER PRIMARY KEY, status TEXT)")
        conn.exec_driver_sql("INSERT INTO outreach_campaigns VALUES (1, 'paused')")
        conn.exec_driver_sql("CREATE TABLE outreach_deliveries (id INTEGER PRIMARY KEY, body TEXT, status TEXT)")
        conn.exec_driver_sql("INSERT INTO outreach_deliveries VALUES (1, 'Готовое письмо', 'queued')")
    monkeypatch.setattr(database, "engine", engine)
    database._upgrade_email_content_schema()
    database._upgrade_email_content_schema()
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT body_template,body_format,html_template,attachment_ids FROM email_templates").one() == ('Старый текст', 'text', '', '[]')
        assert conn.exec_driver_sql("SELECT status,attachment_ids FROM outreach_campaigns").one() == ('paused', '[]')
        assert conn.exec_driver_sql("SELECT body,status,html_body FROM outreach_deliveries").one() == ('Готовое письмо', 'queued', None)


def test_authenticated_attachment_upload_download_and_limits(db, monkeypatch):
    settings = Settings(_env_file=None, fuellead_auth_email="test@example.ru", fuellead_auth_password="test-only", fuellead_auth_session_secret="test-only-session-secret")
    main.app.dependency_overrides[get_db] = lambda: db
    main.app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    try:
        client = TestClient(main.app)
        assert client.post("/api/email-template/attachments?filename=a.pdf", content=b"a").status_code == 401
        client.cookies.set(AUTH_COOKIE_NAME, create_session_token(settings.fuellead_auth_email, settings))
        response = client.post("/api/email-template/attachments", params={"filename": "Условия.pdf"}, content=b"%PDF example", headers={"Content-Type": "application/octet-stream"})
        assert response.status_code == 201
        attachment_id = response.json()["id"]
        result = client.get(f"/api/email-template/attachments/{attachment_id}")
        assert result.content == b"%PDF example" and "attachment;" in result.headers["content-disposition"]
        assert client.post("/api/email-template/attachments?filename=big.pdf", content=b"x" * (MAX_ATTACHMENT_BYTES + 1)).status_code == 413
        assert client.put("/api/email-template", json={**artel_offer_preset(), "attachment_ids": [attachment_id]}).status_code == 200
        assert client.post("/api/email-template/preview", json={}).json()["attachments"][0]["id"] == attachment_id
        client.cookies.clear()
        assert client.get(f"/api/email-template/attachments/{attachment_id}").status_code == 401
    finally:
        main.app.dependency_overrides.clear()


def test_letterhead_survives_sanitizing_but_other_data_urls_do_not():
    from app.services.email_branding import artel_letterhead
    preset = artel_offer_preset()
    _, html = render_email_content("html", "", preset["html_template"], {
        "company_name": "ООО Пример", "date": "21 сентября 2026 г."
    }, Settings(_env_file=None))
    assert artel_letterhead().data_url in html
    assert "АРТЭЛЬ — реализация нефтепродуктов" in html
    assert "__ARTEL_LETTERHEAD" not in html
    _, malicious = render_email_content("html", "", '<p>Текст</p><img src="data:image/svg+xml;base64,PHN2Zz4=">'
        '<a href="data:text/html;base64,PHNjcmlwdD4=">Ссылка</a>', {}, Settings(_env_file=None))
    assert "data:" not in malicious


@pytest.mark.parametrize("with_attachment", [False, True])
def test_smtp_embeds_original_letterhead_as_related_image(with_attachment):
    from app.services.email_branding import artel_letterhead
    FakeSMTP.instances = []
    FakeSentIMAP.messages = []
    sender = account()
    sender.imap_enabled = True
    preset = artel_offer_preset()
    plain, html = render_email_content("html", "", preset["html_template"], {
        "company_name": "ООО Пример", "date": "21 сентября 2026 г."
    }, Settings(_env_file=None))
    files = [MailAttachment("Условия.pdf", "application/pdf", b"%PDF extra")] if with_attachment else []
    MailruSMTPClient(sender, "secret", smtp_factory=FakeSMTP, imap_client_factory=FakeSentIMAP, imap_timeout_seconds=17).send(
        "client@example.ru", "Предложение", plain, html_body=html, attachments=files)
    raw = next(c[1] for c in FakeSMTP.instances[0].commands if isinstance(c, tuple) and c[0] == "DATA")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message.get_content_type() == ("multipart/mixed" if with_attachment else "multipart/alternative")
    image = artel_letterhead()
    sent_html = message.get_body(preferencelist=("html",)).get_content()
    assert f'cid:{image.content_id}' in sent_html and "data:image" not in sent_html
    assert "ООО Пример" in message.get_body(preferencelist=("plain",)).get_content()
    images = [part for part in message.walk() if part.get_content_type() == "image/jpeg"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == f"<{image.content_id}>"
    assert images[0].get_content_disposition() == "inline"
    assert images[0].get_payload(decode=True) == image.content
    assert any(p.get_content_type() == "multipart/related" for p in message.walk())
    assert any(p.get_filename() == "Условия.pdf" for p in message.walk()) == with_attachment
    assert FakeSentIMAP.messages[0][0] == raw
