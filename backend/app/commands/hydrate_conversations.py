"""Restore full content of known replies without altering company status or IMAP cursors."""
import argparse
import json
from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import ActivityHistory, SenderAccount
from app.services.credentials import CredentialCipher
from app.services.imap_bounces import MailruIMAPClient
from app.services.inbound_replies import parse_client_reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    settings = get_settings()
    errors = 0
    with SessionLocal() as db:
        accounts = list(db.scalars(select(SenderAccount).where(SenderAccount.is_active.is_(True), SenderAccount.imap_enabled.is_(True))))
        for account in accounts:
            scanned = updated = 0
            try:
                password = CredentialCipher(settings.mail_credentials_encryption_key).decrypt(account.encrypted_password)
                with MailruIMAPClient(account, password, timeout_seconds=settings.mail_imap_timeout_seconds) as client:
                    for mailbox in ['INBOX', *client.reply_mailboxes()]:
                        cursor, validity = 0, None
                        while True:
                            messages = client.messages_after(cursor, 100, mailbox=mailbox, expected_uidvalidity=validity)
                            if validity is not None and validity != client.uidvalidity:
                                raise RuntimeError('Mailbox changed; repeat hydration')
                            validity = client.uidvalidity
                            for uid, raw in messages:
                                scanned += 1; cursor = uid
                                reply = parse_client_reply(raw)
                                if not reply:
                                    continue
                                for event in db.scalars(select(ActivityHistory).where(ActivityHistory.event_type == 'email_reply',
                                        ActivityHistory.event_data['reply_key'].as_string() == reply.key).with_for_update()):
                                    data = event.event_data or {}
                                    if data.get('sender_account_id') != account.id or data.get('content_version') == 1:
                                        continue
                                    updated += 1
                                    if args.apply:
                                        event.event_data = {**data, 'body': reply.body, 'text': reply.text,
                                            'subject': reply.subject, 'sent_at': reply.sent_at, 'reply_to': reply.reply_to,
                                            'references': list(reply.references), 'attachments': list(reply.attachments), 'content_version': 1}
                                if args.apply: db.commit()
                                else: db.rollback()
                            if len(messages) < 100:
                                break
                print(json.dumps({'account_id': account.id, 'scanned': scanned, 'hydrated': updated, 'applied': args.apply}), flush=True)
            except Exception as exc:
                db.rollback(); errors += 1
                print(json.dumps({'account_id': account.id, 'error_type': type(exc).__name__}), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
