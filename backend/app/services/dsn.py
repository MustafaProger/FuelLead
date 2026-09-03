import re
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.message import Message

from app.services.provider import normalize_email


DELIVERY_ID_RE = re.compile(r"(?im)^X-FuelLead-Delivery-ID:\s*(\d+)\s*$")
ORIGINAL_MESSAGE_ID_RE = re.compile(r"(?im)^Original-Message-ID:\s*(<[^>]+>|\S+)\s*$")
STATUS_RE = re.compile(r"(?im)^Status:\s*([245]\.\d{1,3}\.\d{1,3})\s*$")
ACTION_RE = re.compile(r"(?im)^Action:\s*([^\s;]+)")
FINAL_RECIPIENT_RE = re.compile(r"(?im)^Final-Recipient:\s*[^;]*;\s*([^\s;]+)")
DIAGNOSTIC_RE = re.compile(r"(?im)^Diagnostic-Code:\s*([^\r\n]+)")


@dataclass(frozen=True, slots=True)
class DSNBounce:
    delivery_id: int | None
    original_message_id: str | None
    recipient: str | None
    status_code: str
    diagnostic: str | None


def _message_text(message: Message) -> str:
    chunks: list[str] = []
    for part in message.walk():
        content_type = part.get_content_type()
        if content_type not in ("message/delivery-status", "message/rfc822", "text/plain"):
            continue
        try:
            content = part.get_content()
        except Exception:
            content = None
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            chunks.extend(str(item) for item in content)
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            chunks.append(payload.decode("utf-8", errors="replace"))
    chunks.append(message.as_string(policy=policy.default))
    return "\n".join(chunks)


def parse_permanent_dsn(raw_message: bytes) -> DSNBounce | None:
    """Recognize standards-shaped permanent DSNs; never classify by one literal SMTP line."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except Exception:
        return None
    text = _message_text(message)
    status = STATUS_RE.search(text)
    action = ACTION_RE.search(text)
    if not status or not status.group(1).startswith("5."):
        return None
    if action and action.group(1).lower() not in ("failed", "failure"):
        return None
    delivery_match = DELIVERY_ID_RE.search(text)
    original_match = ORIGINAL_MESSAGE_ID_RE.search(text)
    recipient_match = FINAL_RECIPIENT_RE.search(text)
    diagnostic_match = DIAGNOSTIC_RE.search(text)
    recipient = normalize_email(recipient_match.group(1)) if recipient_match else None
    diagnostic = " ".join(diagnostic_match.group(1).split())[:500] if diagnostic_match else None
    return DSNBounce(
        delivery_id=int(delivery_match.group(1)) if delivery_match else None,
        original_message_id=original_match.group(1) if original_match else None,
        recipient=recipient,
        status_code=status.group(1),
        diagnostic=diagnostic,
    )
