"""SMTP is always fake; optionally exercise real PostgreSQL session locks."""
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.database import Base
from app.models import ActivityHistory, Company, CompanyEmail, EmailReplyAttempt, OutreachCampaign, OutreachDelivery, SenderAccount
from app.schemas import CompanyFilters, ConversationReplyRequest
from app.serializers import as_aware
from app.services.conversations import resolve_reply, send_reply
from app.services.credentials import CredentialCipher, generate_encryption_key
from app.services.mail_dispatch import mail_dispatch_slot
from app.services.outreach import (
    build_outreach_preflight, confirm_outreach_campaign, outreach_campaign_to_dict,
    pause_outreach_campaign, process_outreach_tick, stop_outreach_campaign,
)
from app.services.smtp import SMTPAccepted, SMTPDeliveryError


@pytest.fixture(params=["sqlite", "postgresql"])
def dispatch_db(request, tmp_path):
    if request.param == "postgresql":
        url = os.environ.get("FUELLEAD_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set FUELLEAD_TEST_POSTGRES_URL to an isolated test database")
        assert make_url(url).database.startswith("fuellead_test_"), "Refusing a non-test database"
        engine = create_engine(url)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'dispatch.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key(), outreach_automatic_send_enabled=False)
    with factory() as db:
        account = SenderAccount(email="sender@mail.ru", encrypted_password=CredentialCipher(settings.mail_credentials_encryption_key).encrypt("test"), verification_status="verified")
        client = Company(name="Client", inn="7700000000", status="answered")
        db.add_all([account, client]); db.commit()
        event = ActivityHistory(company_id=client.id, event_type="email_reply", description="Reply",
            event_data={"sender_account_id": account.id, "sender": "client@example.test", "subject": "Offer", "message_id": "<client@example.test>"})
        db.add(event)
        for index in range(3):
            db.add(Company(name=f"Lead {index}", inn=f"770000001{index}", emails=[CompanyEmail(email=f"lead{index}@example.test")]))
        db.commit()
        preflight = build_outreach_preflight(db, CompanyFilters(), settings)
        campaign = confirm_outreach_campaign(db, preflight["snapshot_id"], settings)
        ids = account.id, client.id, event.id, campaign.id
    try:
        yield factory, settings, ids
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def reply(factory, settings, ids, sender, request=None):
    with factory() as db:
        return send_reply(db, ids[1], request or ConversationReplyRequest(
            request_id=uuid4(), reply_id=ids[2], body="Client reply"), settings, sender_factory=sender)


def tick(factory, settings, sender, now=None):
    return process_outreach_tick(settings, session_factory=factory, sender_factory=sender, now=now, random_int=lambda low, high: low)


class Accepted:
    def __init__(self, *args, **kwargs): pass
    def send(self, *args, **kwargs):
        return SMTPAccepted(kwargs.get("message_id") or f"<{uuid4()}@example.test>", "250", "OK", sent_copy_saved=True)


class MustNotSend:
    def __init__(self, *args, **kwargs): pytest.fail("Unexpected SMTP operation")


@pytest.mark.parametrize("state", ["running", "cooldown", "paused", "stopped"])
def test_reply_preserves_campaign_state_and_spacing(dispatch_db, state):
    factory, settings, ids = dispatch_db
    rest_until = datetime.now(timezone.utc) + timedelta(hours=1)
    with factory() as db:
        campaign = db.get(OutreachCampaign, ids[3])
        campaign.status = state
        if state == "cooldown": campaign.next_send_at = campaign.round_rest_until = rest_until
        if state == "paused": campaign.pause_reason = "Приостановлено пользователем"
        db.commit()
    assert reply(factory, settings, ids, Accepted)["status"] == "accepted"
    with factory() as db:
        campaign, account = db.get(OutreachCampaign, ids[3]), db.get(SenderAccount, ids[0])
        assert campaign.status == state and account.sent_today == 1
        assert account.last_sent_at is not None
        if state == "paused": assert campaign.pause_reason == "Приостановлено пользователем"
        if state == "cooldown":
            assert as_aware(campaign.round_rest_until) == rest_until
            assert as_aware(campaign.next_send_at) == rest_until
        if state in ("running", "paused"):
            assert as_aware(campaign.next_send_at) >= as_aware(account.last_sent_at) + timedelta(seconds=60)
        assert outreach_campaign_to_dict(campaign)["reply_wait_reason"] is None
        scheduled = as_aware(campaign.next_send_at)
    tick(factory, settings, MustNotSend)
    if state == "running":
        tick(factory, settings, Accepted, scheduled)
        with factory() as db: assert db.get(SenderAccount, ids[0]).sent_today == 2


def test_reply_waits_for_inflight_campaign_then_worker_yields(dispatch_db):
    factory, settings, ids = dispatch_db
    campaign_started, finish_campaign, reply_started, finish_reply = (Event() for _ in range(4))
    class CampaignSMTP(Accepted):
        def send(self, *args, **kwargs):
            campaign_started.set()
            assert finish_campaign.wait(10)
            return super().send(*args, **kwargs)
    class ReplySMTP(Accepted):
        def send(self, *args, **kwargs):
            reply_started.set()
            assert finish_reply.wait(10)
            return super().send(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        campaign_future = pool.submit(tick, factory, settings, CampaignSMTP)
        try:
            assert campaign_started.wait(5)
            reply_future = pool.submit(reply, factory, settings, ids, ReplySMTP)
            assert not reply_started.wait(0.1)
            tick(factory, settings, MustNotSend)
            finish_campaign.set()
            campaign_future.result(timeout=5)
            assert reply_started.wait(5)
            tick(factory, settings, MustNotSend)
            with factory() as db:
                assert outreach_campaign_to_dict(db.get(OutreachCampaign, ids[3]))["reply_wait_reason"]
            finish_reply.set()
            assert reply_future.result(timeout=5)["status"] == "accepted"
        finally:
            finish_campaign.set(); finish_reply.set()
    with factory() as db:
        assert db.get(SenderAccount, ids[0]).sent_today == 2
        scheduled = as_aware(db.get(OutreachCampaign, ids[3]).next_send_at)
    tick(factory, settings, Accepted, scheduled)
    with factory() as db: assert db.get(SenderAccount, ids[0]).sent_today == 3


@pytest.mark.parametrize("action", [pause_outreach_campaign, stop_outreach_campaign])
def test_operator_action_during_reply_wins(dispatch_db, action):
    factory, settings, ids = dispatch_db
    class OperatorSMTP(Accepted):
        def send(self, *args, **kwargs):
            with factory() as db: action(db, db.get(OutreachCampaign, ids[3]))
            return super().send(*args, **kwargs)
    assert reply(factory, settings, ids, OperatorSMTP)["status"] == "accepted"
    tick(factory, settings, MustNotSend, datetime.now(timezone.utc) + timedelta(hours=2))
    with factory() as db: assert db.get(OutreachCampaign, ids[3]).status == ("paused" if action == pause_outreach_campaign else "stopped")


@pytest.mark.parametrize("uncertain", [False, True])
def test_failed_reply_releases_only_when_outcome_known(dispatch_db, uncertain):
    factory, settings, ids = dispatch_db
    class Broken(Accepted):
        def send(self, *args, **kwargs):
            raise SMTPDeliveryError("Test failure", category="connection", uncertain=uncertain)
    result = reply(factory, settings, ids, Broken)
    assert result["status"] == ("uncertain" if uncertain else "failed")
    if uncertain:
        tick(factory, settings, MustNotSend)
        with factory() as db:
            assert "неизвестен" in outreach_campaign_to_dict(db.get(OutreachCampaign, ids[3]))["reply_wait_reason"]
            resolve_reply(db, ids[1], result["request_id"], "accepted")
    tick(factory, settings, Accepted)
    with factory() as db: assert db.get(SenderAccount, ids[0]).sent_today == (2 if uncertain else 1)


def test_last_daily_slot_is_shared_and_duplicate_request_sends_once(dispatch_db):
    factory, settings, ids = dispatch_db
    with factory() as db:
        db.get(SenderAccount, ids[0]).daily_limit = 1
        db.commit()
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=ids[2], body="One reply")
    assert reply(factory, settings, ids, Accepted, request)["status"] == "accepted"
    assert reply(factory, settings, ids, MustNotSend, request)["status"] == "accepted"
    tick(factory, settings, MustNotSend, datetime.now(timezone.utc) + timedelta(minutes=5))
    with factory() as db:
        assert db.get(SenderAccount, ids[0]).sent_today == 1
        assert len(list(db.scalars(select(EmailReplyAttempt)))) == 1
    with pytest.raises(HTTPException) as error:
        reply(factory, settings, ids, MustNotSend)
    assert error.value.status_code == 429


def test_stale_smtp_receipt_blocks_new_sends(dispatch_db):
    factory, settings, ids = dispatch_db
    with factory() as db:
        delivery = db.scalar(select(OutreachDelivery).where(OutreachDelivery.campaign_id == ids[3]))
        delivery.status = "sending"
        db.commit()
    tick(factory, settings, MustNotSend)
    with pytest.raises(HTTPException): reply(factory, settings, ids, MustNotSend)


def test_dispatch_lock_survives_commit_and_releases_on_error(dispatch_db):
    factory, _, _ = dispatch_db
    with factory() as db, factory() as other:
        with pytest.raises(RuntimeError):
            with mail_dispatch_slot(db, reply=True) as acquired:
                assert acquired
                db.commit()
                with mail_dispatch_slot(other, reply=False) as second: assert not second
                raise RuntimeError("Test cleanup")
        with mail_dispatch_slot(other, reply=False) as acquired: assert acquired


def test_concurrent_duplicate_requests_share_one_receipt(dispatch_db):
    factory, settings, ids = dispatch_db
    started, finish = Event(), Event()
    request = ConversationReplyRequest(request_id=uuid4(), reply_id=ids[2], body="One reply")
    class Slow(Accepted):
        def send(self, *args, **kwargs):
            started.set()
            assert finish.wait(10)
            return super().send(*args, **kwargs)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(reply, factory, settings, ids, Slow, request)
        try:
            assert started.wait(5)
            second = pool.submit(reply, factory, settings, ids, MustNotSend, request)
            finish.set()
            assert first.result(timeout=5) == second.result(timeout=5)
        finally:
            finish.set()
    with factory() as db: assert db.get(SenderAccount, ids[0]).sent_today == 1
