"""Read all retained inbound folders; default is a read-only refusal report."""
import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, create_database
from app.models import Company, ImapProcessedMessage, SenderAccount
from app.services.credentials import CredentialCipher
from app.services.imap_bounces import MailruIMAPClient
from app.services.inbound_replies import apply_client_reply, match_reply, parse_client_reply, reject_company


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Save matched replies and protect already rejected companies')
    args = parser.parse_args()
    settings = get_settings()
    if args.apply:
        create_database()
    cipher = CredentialCipher(settings.mail_credentials_encryption_key)
    errors = 0
    with SessionLocal() as db:
        ids = list(db.scalars(select(SenderAccount.id).where(
            SenderAccount.is_active.is_(True), SenderAccount.imap_enabled.is_(True),
            SenderAccount.encrypted_password.is_not(None), SenderAccount.imap_verification_status != 'failed')))
        for account_id in ids:
            account = db.get(SenderAccount, account_id)
            try:
                with MailruIMAPClient(account, cipher.decrypt(account.encrypted_password),
                                      timeout_seconds=settings.mail_imap_timeout_seconds) as client:
                    for mailbox in ['INBOX', *client.reply_mailboxes()]:
                        cursor = 0
                        validity = None
                        count = 0
                        while True:
                            messages = client.messages_after(cursor, 100, mailbox=mailbox, expected_uidvalidity=validity)
                            if validity is not None and validity != client.uidvalidity:
                                raise RuntimeError('Mailbox changed during rescan; retry the command')
                            validity = client.uidvalidity
                            if not messages:
                                break
                            for uid, raw in messages:
                                count += 1
                                cursor = uid
                                reply = parse_client_reply(raw)
                                if not reply:
                                    continue
                                companies, _ = match_reply(db, reply, account_id)
                                if companies:
                                    print(json.dumps({'account_id': account_id, 'mailbox': mailbox, 'uid': uid,
                                        'companies': [{'id': c.id, 'name': c.name} for c in companies],
                                        'sender': reply.sender, 'refusal': reply.refusal, 'apply': args.apply}, ensure_ascii=False), flush=True)
                                if args.apply:
                                    outcome, delivery_id = apply_client_reply(db, account_id, raw, mailbox=mailbox, uid=uid)
                                    if mailbox == 'INBOX' and account.imap_uidvalidity == validity:
                                        processed = db.scalar(select(ImapProcessedMessage).where(
                                            ImapProcessedMessage.sender_account_id == account_id, ImapProcessedMessage.uid == uid))
                                        if processed and processed.outcome in ('unrecognized', 'unmatched_reply'):
                                            processed.outcome = outcome
                                            processed.delivery_id = delivery_id
                                    db.commit()
                                else:
                                    db.rollback()
                            if len(messages) < 100:
                                break
                        print(json.dumps({'account_id': account_id, 'mailbox': mailbox, 'scanned': count}), flush=True)
            except Exception as exc:
                db.rollback()
                errors += 1
                print(json.dumps({'account_id': account_id, 'error_type': type(exc).__name__}), flush=True)
        if args.apply:
            companies = list(db.scalars(select(Company).where(Company.status == 'rejected').with_for_update()))
            for company in companies:
                reject_company(db, company, reason='Зафиксированный отказ клиента', now=datetime.now(timezone.utc))
            db.commit()
            print(json.dumps({'already_rejected_protected': len(companies)}), flush=True)
    if errors:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
