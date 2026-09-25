"""Read all retained inbound folders; default is a read-only reply report."""
import argparse
import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal, create_database
from app.models import ActivityHistory, Company, ImapProcessedMessage, SenderAccount
from app.services.credentials import CredentialCipher
from app.services.imap_bounces import MailruIMAPClient
from app.services.inbound_replies import apply_client_reply, match_reply, parse_client_reply, reject_company


def _emit(record):
    print(json.dumps(record, ensure_ascii=False), flush=True)


def _existing_company_ids(db, company_ids, reply_key):
    return set(db.scalars(select(ActivityHistory.company_id).where(
        ActivityHistory.company_id.in_(company_ids), ActivityHistory.event_type == 'email_reply',
        ActivityHistory.event_data['reply_key'].as_string() == reply_key)))


def reprocess_replies(db, settings, *, apply=False, replies_only=False, account_ids=None,
                      client_factory=MailruIMAPClient, emit=_emit):
    """Rescan without moving cursors. Missing/existing count unique company/reply pairs.

    A message copied between folders (or received by multiple accounts) can match
    the same stored reply. Count it once, matching apply_client_reply's deduplication.
    `created` counts new events only after their transaction has committed.
    """
    cipher = CredentialCipher(settings.mail_credentials_encryption_key)
    query = select(SenderAccount.id).where(
        SenderAccount.is_active.is_(True), SenderAccount.imap_enabled.is_(True),
        SenderAccount.encrypted_password.is_not(None), SenderAccount.imap_verification_status != 'failed')
    if account_ids is not None:
        query = query.where(SenderAccount.id.in_(account_ids))
    ids = list(db.scalars(query.order_by(SenderAccount.id)))
    summary = {'type': 'summary', 'account_ids': ids, 'scanned': 0, 'matched': 0,
               'missing': 0, 'existing': 0, 'duplicate_matches': 0, 'created': 0,
               'errors': 0, 'apply': apply, 'replies_only': replies_only}
    seen = set()
    for account_id in ids:
        account = db.get(SenderAccount, account_id)
        try:
            with client_factory(account, cipher.decrypt(account.encrypted_password),
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
                            summary['scanned'] += 1
                            cursor = uid
                            reply = parse_client_reply(raw)
                            if not reply:
                                continue
                            companies, _ = match_reply(db, reply, account_id)
                            company_ids = {company.id for company in companies}
                            existing = _existing_company_ids(db, company_ids, reply.key) if companies else set()
                            missing = company_ids - existing
                            for company_id in company_ids:
                                key = (company_id, reply.key)
                                if key in seen:
                                    summary['duplicate_matches'] += 1
                                else:
                                    seen.add(key)
                                    summary['matched'] += 1
                                    summary['existing' if company_id in existing else 'missing'] += 1
                            if companies:
                                emit({'account_id': account_id, 'mailbox': mailbox, 'uid': uid,
                                      'companies': [{'id': c.id, 'name': c.name} for c in companies],
                                      'sender': reply.sender, 'refusal': reply.refusal, 'apply': apply,
                                      'missing': len(missing), 'existing': len(existing)})
                            if apply:
                                outcome, delivery_id = apply_client_reply(db, account_id, raw, mailbox=mailbox, uid=uid)
                                if mailbox == 'INBOX' and account.imap_uidvalidity == validity:
                                    processed = db.scalar(select(ImapProcessedMessage).where(
                                        ImapProcessedMessage.sender_account_id == account_id, ImapProcessedMessage.uid == uid))
                                    if processed and processed.outcome in ('unrecognized', 'unmatched_reply'):
                                        processed.outcome = outcome
                                        processed.delivery_id = delivery_id
                                created = len(_existing_company_ids(db, missing, reply.key)) if missing else 0
                                db.commit()
                                summary['created'] += created
                            else:
                                db.rollback()
                        if len(messages) < 100:
                            break
                    emit({'account_id': account_id, 'mailbox': mailbox, 'scanned': count})
        except Exception as exc:
            db.rollback()
            summary['errors'] += 1
            emit({'account_id': account_id, 'error_type': type(exc).__name__})
    if apply and not replies_only:
        companies = list(db.scalars(select(Company).where(Company.status == 'rejected').with_for_update()))
        for company in companies:
            reject_company(db, company, reason='Зафиксированный отказ клиента', now=datetime.now(timezone.utc))
        db.commit()
        emit({'already_rejected_protected': len(companies)})
    emit(summary)
    return summary


def _positive_account_id(value):
    account_id = int(value)
    if account_id < 1:
        raise argparse.ArgumentTypeError('account ID must be positive')
    return account_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Save matched replies and protect already rejected companies')
    parser.add_argument('--replies-only', action='store_true',
                        help='Skip the final global protection of already rejected companies; incoming refusals still apply')
    parser.add_argument('--account-id', action='append', type=_positive_account_id,
                        help='Scan only this eligible mailbox ID; repeat to select several mailboxes')
    args = parser.parse_args(argv)
    settings = get_settings()
    if args.apply:
        create_database()
    with SessionLocal() as db:
        summary = reprocess_replies(db, settings, apply=args.apply, replies_only=args.replies_only,
                                    account_ids=args.account_id)
    if summary['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
