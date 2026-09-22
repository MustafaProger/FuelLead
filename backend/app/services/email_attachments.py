from dataclasses import dataclass
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import EmailAttachment

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_ATTACHMENTS = 5
FILE_TYPES = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".txt": "text/plain", ".csv": "text/csv",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".zip": "application/zip",
}


@dataclass(frozen=True)
class MailAttachment:
    filename: str
    content_type: str
    content: bytes


def attachment_metadata(attachment: EmailAttachment) -> dict:
    return {"id": attachment.id, "filename": attachment.filename,
            "content_type": attachment.content_type, "size": attachment.size}


def create_attachment(db: Session, filename: str, content: bytes) -> EmailAttachment:
    filename = filename.strip()
    if not filename or len(filename) > 180 or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in filename):
        raise ValueError("Недопустимое имя файла")
    content_type = FILE_TYPES.get(PurePosixPath(filename).suffix.lower())
    if not content_type:
        raise ValueError("Поддерживаются PDF, Word, Excel, PPTX, TXT, CSV, PNG, JPG и ZIP")
    if not content or len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Файл должен быть непустым и не больше 10 МБ")
    attachment = EmailAttachment(id=str(uuid4()), filename=filename,
                                 content_type=content_type, size=len(content), content=content)
    db.add(attachment)
    db.commit()
    return attachment


def get_attachments(db: Session, ids: list[str]) -> list[EmailAttachment]:
    if len(ids) > MAX_ATTACHMENTS or len(set(ids)) != len(ids):
        raise ValueError("Можно прикрепить до 5 разных файлов")
    files = [db.get(EmailAttachment, key) for key in ids]
    if any(file is None for file in files):
        raise ValueError("Вложение не найдено. Прикрепите файл заново")
    if sum(file.size for file in files) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Общий размер вложений не должен превышать 10 МБ")
    return files


def smtp_attachments(db: Session, ids: list[str]) -> list[MailAttachment]:
    return [MailAttachment(file.filename, file.content_type, file.content) for file in get_attachments(db, ids)]
