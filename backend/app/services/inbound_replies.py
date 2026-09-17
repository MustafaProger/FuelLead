"""Client correspondence, with automatic replies excluded from status changes."""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ActivityHistory, Company, CompanyEmail, EmailSuppression, OutreachDelivery
from app.services.provider import normalize_email


REFUSAL_RE = re.compile(
    r"\b(?:не\s+(?:пишите|писать|присылайте|присылать|отправляйте|отправлять|беспокойте|звоните)"
    r"|не\s*интерес(?:но|ует|уют|ен|на|ны)\b|не\s*актуальн\w*"
    r"|не\s+нуж(?:но|ны|на|ен)\b|не\s+надо\b|не\s+требуется\b"
    r"|отпис(?:ать|аться|ывайте|ать\s+меня)|отпишите|отписка"
    r"|(?:удалите|исключите|уберите)\s+(?:меня|нас|мой|наш|адрес|почту|из)"
    r"|прекратите\s+(?:писать|рассылку|отправку)|отказываемся"
    r"|do\s+not\s+(?:email|contact|write)|stop\s+(?:emailing|sending)|unsubscribe|not\s+interested)\b",
    re.IGNORECASE,
)
QUOTE_LINE_RE = re.compile(
    r"^\s*(?:>+|[-_]{3,}|On\s+.+wrote:|.*(?:написал|написала|пишет)\s*:"
    r"|(?:From|От|От кого|Кому|To|Sent|Отправлено|Subject|Тема):"
    r"|Если предложение неактуально, ответьте)", re.IGNORECASE,
)


class _HTMLReply(HTMLParser):
    def __init__(self, *, hide_quotes=True):
        super().__init__(convert_charrefs=True)
        self.hide_quotes = hide_quotes
        self.chunks = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        marker = (attrs.get("class", "") + " " + attrs.get("id", "")).lower()
        quoted = tag in ("script", "style") or self.hide_quotes and (tag == "blockquote" or any(
            value in marker for value in ("gmail_quote", "yahoo_quoted", "moz-cite", "mail-quote", "mailru-quote", "divrplyfwdmsg")
        ))
        if tag not in ("br", "hr", "img", "meta", "link", "input", "wbr"):
            self.stack.append((tag, quoted or bool(self.stack and self.stack[-1][1])))
        if tag in ("br", "div", "p", "hr", "tr"):
            self.chunks.append("\n")

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in ("div", "p", "tr"):
            self.chunks.append("\n")

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.chunks.append(data)


@dataclass(frozen=True)
class ClientReply:
    sender: str
    message_id: str | None
    references: tuple[str, ...]
    text: str
    refusal: str | None
    key: str
    subject: str = ""
    body: str = ""
    sent_at: str | None = None
    reply_to: str | None = None
    attachments: tuple[str, ...] = ()
    is_automatic: bool = False


def parse_client_reply(raw: bytes) -> ClientReply | None:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if message.get_content_type() in ("multipart/report", "message/delivery-status"):
        return None
    auto_submitted = str(message.get("Auto-Submitted", "no")).split(";", 1)[0].strip().lower()
    if auto_submitted not in ("no", "auto-replied") or message.get("List-Id") or message.get("List-Unsubscribe"):
        return None
    is_automatic = auto_submitted == "auto-replied"
    addresses = getaddresses(message.get_all("From", []))
    if len(addresses) != 1 or not (sender := normalize_email(addresses[0][1])):
        return None
    if sender.split("@")[0] in ("mailer-daemon", "postmaster"):
        return None
    part = message.get_body(preferencelist=("plain", "html"))
    attachments = tuple(str(p.get_filename() or "Вложение")[:255] for p in message.iter_attachments())
    if part is None and not attachments:
        return None
    try:
        body = part.get_content() if part else ""
    except (LookupError, UnicodeError):
        body = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return None
    full_body = body
    if part and part.get_content_type() == "text/html":
        full_parser = _HTMLReply(hide_quotes=False)
        full_parser.feed(body)
        full_body = "".join(full_parser.chunks)
        parser = _HTMLReply()
        parser.feed(body)
        body = "".join(parser.chunks)
    lines = []
    for line in body.replace("\r", "").splitlines():
        if QUOTE_LINE_RE.match(line):
            break
        lines.append(line)
    text = "\n".join(lines).strip()[:12000]
    subject = re.sub(r"^(?:(?:re|fw|fwd|ответ)\s*:\s*)+", "", str(message.get("Subject", "")), flags=re.I).strip()
    # Clients sometimes put the entire request in the subject and only quote
    # our offer in the body (a real Mail.ru case). Require a short direct subject.
    if len(subject) <= 120 and REFUSAL_RE.match(subject):
        text = subject + ("\n" + text if text else "")
    detection_text = text
    if not text:
        text = "Вложения: " + ", ".join(attachments) if attachments else full_body.strip()[:12000]
        if not text:
            return None
    normalized = " ".join(detection_text.lower().replace("ё", "е").split())
    match = None if is_automatic else REFUSAL_RE.search(normalized)
    # A quoted opt-out instruction or negation is not the client's request.
    if match and re.search(r"(?:фраз\w*|слов\w*|ответьте|если|почему)\s.{0,45}$", normalized[:match.start()]):
        match = None
    if match and re.match(r"\s+(?:цен\w*|сумм\w*|номер\w*|реквизит\w*|на\s+английском)\b", normalized[match.end():]):
        match = None
    message_id = str(message.get("Message-ID", "")).strip()[:255] or None
    references = tuple(re.findall(r"<[^<>\s]+>", " ".join(
        str(message.get(name, "")) for name in ("In-Reply-To", "References")
    )))
    try:
        sent_at = parsedate_to_datetime(str(message.get("Date", "")))
        sent_at = (sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=timezone.utc)).isoformat()
    except (ValueError, TypeError, OverflowError):
        sent_at = None
    reply_addresses = getaddresses(message.get_all("Reply-To", []))
    reply_to = normalize_email(reply_addresses[0][1]) if len(reply_addresses) == 1 else None
    return ClientReply(sender, message_id, references, text, match.group() if match else None,
                       hashlib.sha256((sender + "\n" + message_id).encode() if message_id else raw).hexdigest(),
                       str(message.get("Subject", ""))[:998], full_body.strip()[:200000], sent_at,
                       reply_to, attachments, is_automatic)


def match_reply(db: Session, reply: ClientReply, account_id: int) -> tuple[list[Company], OutreachDelivery | None]:
    delivery = None
    company_ids = set()
    for reference in reversed(reply.references):
        delivery = db.scalar(select(OutreachDelivery).where(
            OutreachDelivery.sender_account_id == account_id, OutreachDelivery.message_id == reference,
        ).order_by(OutreachDelivery.id.desc()))
        if delivery and delivery.company_id:
            company_ids.add(delivery.company_id)
            break
        # Single-company sends are stored in activity_history, not deliveries.
        histories = db.scalars(select(ActivityHistory).where(ActivityHistory.event_type == "email_sent",
            ActivityHistory.event_data["message_id"].as_string() == reference))
        for history in histories:
            if (history.event_data or {}).get("sender_account_id") == account_id:
                company_ids.add(history.company_id)
        if company_ids:
            break
    if not company_ids and reply.is_automatic:
        # An automatic message without references must correspond to an actual
        # outgoing message on this mailbox, not merely an imported contact.
        deliveries = list(db.scalars(select(OutreachDelivery).where(
            OutreachDelivery.sender_account_id == account_id,
            func.lower(OutreachDelivery.recipient) == reply.sender,
            OutreachDelivery.status == "accepted",
        ).order_by(OutreachDelivery.id.desc())))
        company_ids.update(d.company_id for d in deliveries if d.company_id)
        delivery = deliveries[0] if deliveries else None
        company_ids.update(db.scalars(select(ActivityHistory.company_id).where(
            ActivityHistory.event_type == "email_sent",
            ActivityHistory.event_data["sender_account_id"].as_integer() == account_id,
            func.lower(ActivityHistory.event_data["recipient"].as_string()) == reply.sender,
        )))
    elif not company_ids:
        company_ids.update(db.scalars(select(CompanyEmail.company_id).where(
            func.lower(CompanyEmail.email) == reply.sender)))
        if company_ids:
            delivery = db.scalar(select(OutreachDelivery).where(
                OutreachDelivery.sender_account_id == account_id,
                OutreachDelivery.recipient == reply.sender,
            ).order_by(OutreachDelivery.id.desc()))
    companies = list(db.scalars(select(Company).where(Company.id.in_(company_ids)).order_by(Company.id)
                              .execution_options(populate_existing=True).with_for_update())) if company_ids else []
    return companies, delivery


def reject_company(db: Session, company: Company, *, reason: str, now: datetime,
                   extra_addresses=(), delivery: OutreachDelivery | None = None) -> None:
    if company.status != "rejected":
        company.status = "rejected"
        company.last_updated_at = now
    addresses = {normalize_email(e.email) for e in company.emails} | {normalize_email(e) for e in extra_addresses}
    for address in sorted(addresses - {None, ""}):
        suppression = db.scalar(select(EmailSuppression).where(EmailSuppression.email == address))
        if suppression and suppression.source == "client_refusal" and suppression.lifted_at is None:
            continue
        if suppression is None:
            suppression = EmailSuppression(email=address, reason=reason, source="client_refusal")
            db.add(suppression)
        suppression.reason = reason
        suppression.source = "client_refusal"
        suppression.lifted_at = None
        suppression.created_at = now
        if delivery:
            suppression.delivery_id = delivery.id
            suppression.campaign_id = delivery.campaign_id
        db.flush()


def apply_client_reply(db: Session, account_id: int, raw: bytes, *, now: datetime | None = None,
                       mailbox: str = "INBOX", uid: int | None = None) -> tuple[str, int | None]:
    """Apply in the caller's transaction; receipt and cursor must commit together."""
    reply = parse_client_reply(raw)
    if reply is None:
        return "unrecognized", None
    companies, delivery = match_reply(db, reply, account_id)
    if not companies:
        return "unmatched_reply", None
    timestamp = now or datetime.now(timezone.utc)
    for company in companies:
        existing = db.scalar(select(ActivityHistory).where(
            ActivityHistory.company_id == company.id,
            ActivityHistory.event_type == "email_reply",
            ActivityHistory.event_data["reply_key"].as_string() == reply.key,
        ))
        message_data = {"subject": reply.subject, "body": reply.body, "text": reply.text,
                        "references": list(reply.references), "sent_at": reply.sent_at,
                        "reply_to": reply.reply_to, "attachments": list(reply.attachments),
                        "sender_account_id": account_id, "content_version": 1,
                        "is_automatic": reply.is_automatic}
        if existing:
            # Historical rescans enrich the same event without changing status/read state.
            prior_data = existing.event_data or {}
            existing.event_data = {**prior_data, **message_data,
                                  "sender_account_id": prior_data.get("sender_account_id") or account_id}
            continue
        previous = company.status
        if reply.refusal:
            reject_company(db, company, reason="Отказ клиента: " + reply.refusal, now=timestamp,
                           extra_addresses=[reply.sender, *([delivery.recipient] if delivery else [])], delivery=delivery)
        elif not reply.is_automatic and company.status in ("new", "sent"):
            company.status = "answered"
            company.last_updated_at = timestamp
        db.add(ActivityHistory(company_id=company.id, event_type="email_reply",
            description=("Получен автоматический ответ" if reply.is_automatic else
                         "Получен отказ клиента" if reply.refusal else "Получен ответ клиента") + f" от {reply.sender}",
            from_status=previous, to_status=company.status, created_at=timestamp,
            event_data={"reply_key": reply.key, "message_id": reply.message_id,
                        "sender_account_id": account_id, "sender": reply.sender,
                        "mailbox": mailbox, "uid": uid, **message_data,
                        "refusal": reply.refusal, "delivery_id": delivery.id if delivery else None}))
        db.flush()
    return ("automatic_reply" if reply.is_automatic else "rejected" if reply.refusal else "answered"), delivery.id if delivery else None
