from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import ActivityHistory, Company, CompanyEmail, OutreachCampaign, OutreachDelivery
from app.schemas import CompanyFilters
from app.services.gmail import GmailOAuthError
from app.services.outreach import (
    build_outreach_preflight,
    create_outreach_campaign,
    process_outreach_tick,
    recover_interrupted_outreach,
)


def add_company(
    db,
    name: str,
    inn: str,
    *,
    status: str = "new",
    emails: tuple[str, ...] = (),
    is_active: bool = True,
) -> Company:
    company = Company(
        name=name,
        inn=inn,
        status=status,
        is_active=is_active,
        primary_okved_code="49.41",
        primary_okved_name="Грузовые перевозки",
    )
    company.emails.extend(CompanyEmail(email=email) for email in emails)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def outreach_settings(**overrides) -> Settings:
    values = {
        "outreach_sender_email": "sender@example.ru",
        "gmail_client_id": "client-id",
        "gmail_client_secret": "client-secret",
        "gmail_refresh_token": "refresh-token",
        "outreach_campaign_size": 20,
        "outreach_daily_limit": 20,
        "outreach_hourly_limit": 5,
        "outreach_min_interval_seconds": 300,
        "outreach_max_per_domain_per_day": 2,
        "outreach_automatic_send_enabled": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_preflight_uses_only_new_active_unique_uncontacted_primary_addresses(db):
    add_company(db, "Новая", "7700000001", emails=("first@example.ru", "second@example.ru"))
    add_company(db, "Отправленная", "7700000002", status="sent", emails=("sent-status@example.ru",))
    add_company(db, "Дубль", "7700000003", emails=("first@example.ru",))
    contacted = add_company(db, "Контакт был", "7700000004", emails=("sent@example.ru",))
    add_company(db, "Без почты", "7700000005")
    add_company(db, "Неактивная", "7700000006", emails=("inactive@example.ru",), is_active=False)
    db.add(
        ActivityHistory(
            company=contacted,
            event_type="email_sent",
            description="Письмо отправлено",
            event_data={"recipient": "sent@example.ru"},
        )
    )
    db.commit()

    result = build_outreach_preflight(db, CompanyFilters(), outreach_settings())

    assert result["matched_count"] == 6
    assert result["eligible_count"] == 1
    assert result["selected_count"] == 1
    assert result["sample"]["recipient"] == "first@example.ru"
    assert "ответьте «Не писать»" in result["sample"]["body"]
    assert result["skipped"] == {
        "not_new": 1,
        "inactive": 1,
        "without_email": 1,
        "already_contacted": 1,
        "duplicate_address": 1,
    }


def test_campaign_snapshots_policy_and_limits_batch_size(db):
    for index in range(3):
        add_company(
            db,
            f"Компания {index}",
            f"770000001{index}",
            emails=(f"lead{index}@example{index}.ru",),
        )
    settings = outreach_settings(outreach_campaign_size=2)

    campaign = create_outreach_campaign(db, CompanyFilters(), settings)

    assert campaign.status == "running"
    assert campaign.recipient_count == 2
    assert campaign.matched_count == 3
    assert len(campaign.deliveries) == 2
    assert all(delivery.status == "queued" for delivery in campaign.deliveries)
    assert all("ответьте «Не писать»" in delivery.body for delivery in campaign.deliveries)


def test_preflight_reports_overlapping_skip_reasons(db):
    add_company(db, "Отправленная без почты", "7700000019", status="sent")

    result = build_outreach_preflight(db, CompanyFilters(), outreach_settings())

    assert result["skipped"]["not_new"] == 1
    assert result["skipped"]["without_email"] == 1


def test_worker_automatically_creates_campaign_for_new_company(db):
    company = add_company(db, "Автоматическая", "7700000018", emails=("auto@carrier.ru",))
    settings = outreach_settings(outreach_automatic_send_enabled=True)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)
    sent: list[str] = []

    class FakeSender:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, recipient: str, *_):
            sent.append(recipient)
            return "automatic-message"

    process_outreach_tick(
        settings,
        sender_factory=FakeSender,
        session_factory=factory,
        now=datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc),
    )
    db.expire_all()

    campaign = db.query(OutreachCampaign).one()
    assert sent == ["auto@carrier.ru"]
    assert campaign.status == "completed"
    assert campaign.sent_count == 1
    assert db.get(Company, company.id).status == "sent"


def test_worker_sends_one_delivery_updates_company_and_completes(db):
    company = add_company(db, "Перевозчик", "7700000020", emails=("lead@carrier.ru",))
    settings = outreach_settings()
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)
    sent: list[tuple[str, str, str]] = []

    class FakeSender:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, recipient: str, subject: str, body: str) -> str:
            sent.append((recipient, subject, body))
            return "gmail-message-1"

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()
    process_outreach_tick(
        settings,
        sender_factory=FakeSender,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    current_campaign = db.get(OutreachCampaign, campaign.id)
    delivery = db.get(OutreachDelivery, campaign.deliveries[0].id)
    current_company = db.get(Company, company.id)
    assert [item[0] for item in sent] == ["lead@carrier.ru"]
    assert current_campaign.status == "completed"
    assert current_campaign.sent_count == 1
    assert delivery.status == "sent"
    assert delivery.message_id == "gmail-message-1"
    assert current_company.status == "sent"
    assert current_company.history[0].event_data["campaign_id"] == campaign.id


def test_worker_pauses_campaign_after_gmail_error(db):
    company = add_company(db, "Перевозчик", "7700000030", emails=("lead@carrier.ru",))
    settings = outreach_settings()
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)

    class FailingSender:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, *_):
            raise GmailOAuthError("Gmail временно ограничил отправку", status_code=429)

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()
    process_outreach_tick(
        settings,
        sender_factory=FailingSender,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "paused"
    assert current.failed_count == 1
    assert "ограничил" in current.pause_reason
    assert current.deliveries[0].status == "failed"
    failed_company = db.get(Company, company.id)
    assert failed_company.status == "error"
    assert failed_company.history[0].event_type == "email_failed"
    assert failed_company.history[0].to_status == "error"
    assert "Gmail временно ограничил отправку" in failed_company.history[0].description


def test_worker_waits_when_recent_manual_message_used_interval(db):
    company = add_company(db, "Перевозчик", "7700000040", emails=("lead@carrier.ru",))
    settings = outreach_settings(outreach_min_interval_seconds=600)
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.add(
        ActivityHistory(
            company=company,
            event_type="email_sent",
            description="Ручная отправка",
            event_data={"recipient": "other@another.ru"},
            created_at=now - timedelta(seconds=60),
        )
    )
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)

    class MustNotSend:
        def __init__(self, *_):
            raise AssertionError("Gmail sender should not be created while interval is active")

    delay = process_outreach_tick(
        settings,
        sender_factory=MustNotSend,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    current = db.get(OutreachCampaign, campaign.id)
    assert delay == 540
    assert current.status == "running"
    assert current.next_send_at.replace(tzinfo=timezone.utc) == now + timedelta(seconds=540)
    assert current.deliveries[0].status == "queued"


def test_worker_skips_rolling_hour_check_when_hourly_limit_is_disabled(db):
    company = add_company(db, "Перевозчик", "7700000042", emails=("lead@carrier.ru",))
    settings = outreach_settings(
        outreach_daily_limit=500,
        outreach_hourly_limit=0,
        outreach_min_interval_seconds=10,
    )
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    for index in range(5):
        db.add(
            ActivityHistory(
                company=company,
                event_type="email_sent",
                description="Предыдущая отправка",
                event_data={"recipient": f"other{index}@another{index}.ru"},
                created_at=now - timedelta(minutes=index + 1),
            )
        )
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)
    sent: list[str] = []

    class FakeSender:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, recipient: str, *_):
            sent.append(recipient)
            return "message-without-hourly-limit"

    process_outreach_tick(
        settings,
        sender_factory=FakeSender,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    assert sent == ["lead@carrier.ru"]
    assert db.get(OutreachCampaign, campaign.id).status == "completed"


def test_worker_rechecks_company_before_sending_queued_delivery(db):
    company = add_company(db, "Отказавшаяся", "7700000041", emails=("stop@carrier.ru",))
    settings = outreach_settings()
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    company.status = "rejected"
    campaign.next_send_at = now
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)

    class MustNotSend:
        def __init__(self, *_):
            raise AssertionError("A rejected company must be skipped before Gmail is called")

    process_outreach_tick(
        settings,
        sender_factory=MustNotSend,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "completed"
    assert current.sent_count == 0
    assert current.cancelled_count == 1
    assert current.deliveries[0].status == "cancelled"
    assert "больше не соответствует" in current.deliveries[0].error_message


def test_recovery_pauses_ambiguous_inflight_delivery_without_retry(db):
    company = add_company(db, "Перевозчик", "7700000050", emails=("lead@carrier.ru",))
    campaign = create_outreach_campaign(db, CompanyFilters(), outreach_settings())
    campaign.deliveries[0].status = "sending"
    db.commit()

    recover_interrupted_outreach(db)

    assert campaign.status == "paused"
    assert campaign.failed_count == 1
    assert campaign.deliveries[0].status == "failed"
    assert "автоматический повтор отключён" in campaign.deliveries[0].error_message
    assert db.get(Company, company.id).status == "error"


def test_cancellation_during_inflight_send_is_not_overwritten_by_completion(db):
    add_company(db, "Перевозчик", "7700000060", emails=("lead@carrier.ru",))
    settings = outreach_settings()
    campaign = create_outreach_campaign(db, CompanyFilters(), settings)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False, autoflush=False)

    class CancelDuringSend:
        def __init__(self, config):
            self.config = config

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def send(self, *_):
            with factory() as other_db:
                current = other_db.get(OutreachCampaign, campaign.id)
                current.status = "cancelled"
                current.pause_reason = "Отменено пользователем"
                current.completed_at = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
                other_db.commit()
            return "accepted-before-cancel"

    now = datetime(2026, 9, 1, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()
    process_outreach_tick(
        settings,
        sender_factory=CancelDuringSend,
        session_factory=factory,
        now=now,
    )
    db.expire_all()

    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "cancelled"
    assert current.sent_count == 1
    assert current.deliveries[0].status == "sent"
