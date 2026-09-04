from datetime import datetime, timedelta, timezone

import pytest

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.models import ActivityHistory, Company, CompanyEmail, EmailSuppression, OutreachCampaign, SenderAccount
from app.schemas import CompanyFilters
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.outreach import (
    OutreachPolicyError,
    build_outreach_preflight,
    confirm_outreach_campaign,
    pause_outreach_campaign,
    process_outreach_tick,
    recover_interrupted_outreach,
    resume_outreach_campaign,
    stop_outreach_campaign,
)
from app.services.smtp import SMTPAccepted, SMTPDeliveryError


def settings_with_key(**overrides) -> Settings:
    values = {
        "mail_credentials_encryption_key": generate_encryption_key(),
        "outreach_automatic_send_enabled": False,
        "outreach_campaign_size": 500,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def add_sender(db, settings: Settings, email: str, *, successes: int = 0, daily_limit: int = 50) -> SenderAccount:
    account = SenderAccount(
        email=email,
        display_name=email.split("@", 1)[0],
        encrypted_password=CredentialCipher(settings.mail_credentials_encryption_key).encrypt("app-password"),
        verification_status="verified",
        successful_full_batches=successes,
        current_batch_size=min(12, 5 + successes // 2),
        daily_limit=daily_limit,
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def add_company(db, index: int, *, status: str = "new", email: str | None = None, active: bool = True) -> Company:
    company = Company(name=f"Компания {index}", inn=f"77{index:010d}", status=status, is_active=active)
    if email is not None:
        company.emails.append(CompanyEmail(email=email))
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def confirmed_campaign(db, settings: Settings) -> OutreachCampaign:
    preflight = build_outreach_preflight(db, CompanyFilters(), settings)
    assert preflight["snapshot_id"]
    return confirm_outreach_campaign(db, preflight["snapshot_id"], settings)


class RecordingSMTP:
    sent: list[tuple[str, str]] = []

    def __init__(self, account, password, **_):
        self.account = account
        assert password == "app-password"

    def send(self, recipient, *_args, **_kwargs):
        self.__class__.sent.append((self.account.email, recipient))
        return SMTPAccepted(f"<{len(self.sent)}@mail.ru>", "2.0.0", "OK")


def tick_at_schedule(db, settings, factory, now, *, random_value=60):
    campaign = db.query(OutreachCampaign).filter(OutreachCampaign.status.in_(("running", "cooldown"))).first()
    at = campaign.next_send_at if campaign and campaign.next_send_at else now
    if at and at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    process_outreach_tick(settings, sender_factory=factory, session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False), now=at, random_int=lambda low, high: max(low, min(high, random_value)))
    db.expire_all()
    return at


def test_snapshot_filters_suppressions_and_expires_in_ten_minutes(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="first@example.ru")
    add_company(db, 2, email="blocked@example.ru")
    add_company(db, 3, status="sent", email="old@example.ru")
    db.add(EmailSuppression(email="blocked@example.ru", reason="Отказ", source="manual"))
    db.commit()

    preflight = build_outreach_preflight(db, CompanyFilters(), settings)
    campaign = db.get(OutreachCampaign, preflight["snapshot_id"])

    assert preflight["selected_count"] == 1
    assert preflight["skipped"]["suppressed"] == 1
    assert campaign.status == "draft"
    assert campaign.sender_account_ids == [1]
    assert campaign.recipients_snapshot == [{"company_id": 1, "recipient": "first@example.ru"}]
    assert 599 <= (campaign.snapshot_expires_at - campaign.created_at).total_seconds() <= 601


def test_resend_enabled_event_allows_a_historical_recipient_again(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    company = add_company(db, 1, email="again@example.ru")
    sent_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.add(ActivityHistory(
        company=company,
        event_type="email_sent",
        description="SMTP-сервер принял письмо",
        from_status="new",
        to_status="sent",
        event_data={"recipient": "again@example.ru"},
        created_at=sent_at,
    ))
    db.add(ActivityHistory(
        company=company,
        event_type="email_resend_enabled",
        description="Разрешена повторная рассылка",
        from_status="sent",
        to_status="new",
        event_data={"recipients": ["again@example.ru"]},
        created_at=sent_at + timedelta(seconds=1),
    ))
    db.commit()

    preflight = build_outreach_preflight(db, CompanyFilters(), settings)

    assert preflight["selected_count"] == 1
    assert preflight["skipped"]["already_contacted"] == 0


def test_expired_snapshot_cannot_be_confirmed(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="first@example.ru")
    preflight = build_outreach_preflight(db, CompanyFilters(), settings)
    campaign = db.get(OutreachCampaign, preflight["snapshot_id"])
    campaign.snapshot_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    with pytest.raises(OutreachPolicyError, match="устарел"):
        confirm_outreach_campaign(db, campaign.id, settings)
    assert campaign.status == "stopped"


def test_confirmed_snapshot_keeps_interval_and_sender_limit(db):
    snapshot_settings = settings_with_key(
        outreach_message_interval_min_seconds=60,
        outreach_message_interval_max_seconds=60,
    )
    first = add_sender(db, snapshot_settings, "one@mail.ru", daily_limit=1)
    add_sender(db, snapshot_settings, "two@mail.ru", daily_limit=50)
    for index in range(1, 4):
        add_company(db, index, email=f"snapshot{index}@example.ru")
    campaign = confirmed_campaign(db, snapshot_settings)
    first.daily_limit = 50
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()

    changed_settings = settings_with_key(
        mail_credentials_encryption_key=snapshot_settings.mail_credentials_encryption_key,
        outreach_message_interval_min_seconds=85,
        outreach_message_interval_max_seconds=85,
    )
    tick_at_schedule(db, changed_settings, RecordingSMTP, campaign.next_send_at, random_value=85)
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    assert current.current_interval_seconds is None  # snapshotted daily limit ended this partial batch
    assert current.sender_position == 1
    tick_at_schedule(db, changed_settings, RecordingSMTP, campaign.next_send_at, random_value=85)
    current = db.get(OutreachCampaign, campaign.id)
    assert current.current_interval_seconds == 60
    assert [sender for sender, _ in RecordingSMTP.sent[-2:]] == ["one@mail.ru", "two@mail.ru"]


def test_three_senders_rotate_by_five_then_persist_round_rest(db):
    RecordingSMTP.sent = []
    settings = settings_with_key()
    for email in ("one@mail.ru", "two@mail.ru", "three@mail.ru"):
        add_sender(db, settings, email)
    for index in range(1, 17):
        add_company(db, index, email=f"lead{index}@example.ru")
    campaign = confirmed_campaign(db, settings)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()

    for _ in range(15):
        tick_at_schedule(db, settings, RecordingSMTP, now)
    assert [sender for sender, _ in RecordingSMTP.sent] == ["one@mail.ru"] * 5 + ["two@mail.ru"] * 5 + ["three@mail.ru"] * 5

    tick_at_schedule(db, settings, RecordingSMTP, now, random_value=77)
    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "cooldown"
    assert current.round_rest_until == current.next_send_at
    assert 77 * 60 <= (current.next_send_at.replace(tzinfo=timezone.utc) - now).total_seconds()
    tick_at_schedule(db, settings, RecordingSMTP, now, random_value=77)
    current = db.get(OutreachCampaign, campaign.id)
    assert current.current_round == 2
    assert RecordingSMTP.sent[-1][0] == "one@mail.ru"


def test_batch_growth_only_after_two_full_successful_batches_and_survives_campaigns(db):
    RecordingSMTP.sent = []
    settings = settings_with_key()
    account = add_sender(db, settings, "warm@mail.ru", successes=1)
    for index in range(1, 6):
        add_company(db, index, email=f"first{index}@example.ru")
    campaign = confirmed_campaign(db, settings)
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()
    for _ in range(5):
        tick_at_schedule(db, settings, RecordingSMTP, campaign.next_send_at)
    db.expire_all()
    assert db.get(SenderAccount, account.id).successful_full_batches == 2
    assert db.get(SenderAccount, account.id).current_batch_size == 6


def test_partial_batch_does_not_grow(db):
    RecordingSMTP.sent = []
    settings = settings_with_key()
    account = add_sender(db, settings, "partial@mail.ru", successes=1)
    for index in range(1, 4):
        add_company(db, index, email=f"partial{index}@example.ru")
    campaign = confirmed_campaign(db, settings)
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()
    for _ in range(3):
        tick_at_schedule(db, settings, RecordingSMTP, campaign.next_send_at)
    db.expire_all()
    assert db.get(SenderAccount, account.id).successful_full_batches == 1
    assert db.get(SenderAccount, account.id).current_batch_size == 5


def test_random_interval_is_persisted_and_pause_resume_preserves_it(db):
    RecordingSMTP.sent = []
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="one@example.ru")
    add_company(db, 2, email="two@example.ru")
    campaign = confirmed_campaign(db, settings)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()
    process_outreach_tick(settings, sender_factory=RecordingSMTP, session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False), now=now, random_int=lambda *_: 73)
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    saved_next = current.next_send_at
    assert current.current_interval_seconds == 73
    assert current.deliveries[0].interval_seconds == 73
    pause_outreach_campaign(db, current)
    resume_outreach_campaign(db, current)
    assert current.next_send_at == saved_next


def test_stop_is_irreversible_and_cancels_queued(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="one@example.ru")
    add_company(db, 2, email="two@example.ru")
    campaign = confirmed_campaign(db, settings)
    stop_outreach_campaign(db, campaign)
    assert campaign.status == "stopped"
    assert {item.status for item in campaign.deliveries} == {"cancelled"}


def test_temporary_sender_error_stops_batch_blocks_three_rounds_and_next_sender_continues(db):
    settings = settings_with_key()
    first = add_sender(db, settings, "one@mail.ru")
    add_sender(db, settings, "two@mail.ru")
    add_company(db, 1, email="one@example.ru")
    add_company(db, 2, email="two@example.ru")
    campaign = confirmed_campaign(db, settings)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()

    class FirstFails(RecordingSMTP):
        def send(self, recipient, *_args, **_kwargs):
            if self.account.email == "one@mail.ru":
                raise SMTPDeliveryError("Временное ограничение Mail.ru", category="temporary", smtp_code="4.7.0")
            return super().send(recipient, *_args, **_kwargs)

    tick_at_schedule(db, settings, FirstFails, now)
    db.expire_all()
    assert db.get(SenderAccount, first.id).blocked_until_round == 4
    tick_at_schedule(db, settings, FirstFails, now)
    assert RecordingSMTP.sent[-1][0] == "two@mail.ru"


def test_permanent_recipient_bounce_is_suppressed_and_campaign_continues(db):
    RecordingSMTP.sent = []
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_sender(db, settings, "two@mail.ru")
    add_company(db, 1, email="bad@example.ru")
    add_company(db, 2, email="good@example.ru")
    campaign = confirmed_campaign(db, settings)
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()

    class BounceFirst(RecordingSMTP):
        def send(self, recipient, *_args, **_kwargs):
            if recipient == "bad@example.ru":
                raise SMTPDeliveryError(
                    "Адрес окончательно отклонён",
                    category="recipient",
                    smtp_code="5.2.1",
                    permanent_recipient_failure=True,
                )
            return super().send(recipient, *_args, **_kwargs)

    tick_at_schedule(db, settings, BounceFirst, campaign.next_send_at)
    tick_at_schedule(db, settings, BounceFirst, campaign.next_send_at)
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    assert current.deliveries[0].status == "bounced"
    assert db.query(EmailSuppression).filter_by(email="bad@example.ru").one().smtp_code == "5.2.1"
    assert RecordingSMTP.sent[-1] == ("two@mail.ru", "good@example.ru")


def test_uncertain_interrupts_without_retry_and_restart_marks_sending_uncertain(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="one@example.ru")
    campaign = confirmed_campaign(db, settings)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    campaign.next_send_at = now
    db.commit()

    class UncertainSMTP(RecordingSMTP):
        def send(self, *_args, **_kwargs):
            raise SMTPDeliveryError("Результат неизвестен", category="uncertain", uncertain=True)

    tick_at_schedule(db, settings, UncertainSMTP, now)
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "interrupted"
    assert current.deliveries[0].status == "uncertain"

    current.deliveries[0].status = "sending"
    current.status = "running"
    db.commit()
    recover_interrupted_outreach(db)
    assert current.status == "interrupted"
    assert current.deliveries[0].status == "uncertain"


def test_pre_send_change_is_suppressed_without_transport(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    company = add_company(db, 1, email="one@example.ru")
    campaign = confirmed_campaign(db, settings)
    company.status = "rejected"
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()

    class MustNotConstruct:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("SMTP must not be called")

    tick_at_schedule(db, settings, MustNotConstruct, campaign.next_send_at)
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "completed"
    assert current.deliveries[0].status == "suppressed"


def test_campaign_claim_prevents_second_worker_from_claiming_another_delivery(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="one@example.ru")
    add_company(db, 2, email="two@example.ru")
    campaign = confirmed_campaign(db, settings)
    campaign.worker_claim_token = "worker-one"
    db.commit()

    class MustNotConstruct:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("second worker must not claim")

    process_outreach_tick(settings, sender_factory=MustNotConstruct, session_factory=sessionmaker(bind=db.get_bind(), expire_on_commit=False), now=datetime.now(timezone.utc))
    assert all(item.status == "queued" for item in campaign.deliveries)


def test_stop_wins_if_attempt_finishes_uncertain(db):
    settings = settings_with_key()
    add_sender(db, settings, "one@mail.ru")
    add_company(db, 1, email="one@example.ru")
    add_company(db, 2, email="two@example.ru")
    campaign = confirmed_campaign(db, settings)
    campaign.next_send_at = datetime(2026, 9, 3, 9, 0, tzinfo=timezone.utc)
    db.commit()
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    class StopThenDisconnect(RecordingSMTP):
        def send(self, *_args, **_kwargs):
            with factory() as other_db:
                stop_outreach_campaign(other_db, other_db.get(OutreachCampaign, campaign.id))
            raise SMTPDeliveryError("Результат неизвестен", category="uncertain", uncertain=True)

    process_outreach_tick(
        settings,
        sender_factory=StopThenDisconnect,
        session_factory=factory,
        now=campaign.next_send_at,
    )
    db.expire_all()
    current = db.get(OutreachCampaign, campaign.id)
    assert current.status == "stopped"
    assert [item.status for item in current.deliveries] == ["uncertain", "cancelled"]
