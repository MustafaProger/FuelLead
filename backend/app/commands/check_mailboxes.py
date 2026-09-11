"""Safe mailbox diagnostics. Never sends mail, prints secrets or requeues deliveries."""
import argparse
import json

from sqlalchemy import select

from app.config import get_settings
from app.database import SessionLocal
from app.models import SenderAccount
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.sender_accounts import sender_account_to_dict, verify_sender_account


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Check SMTP/IMAP login and save connection status; no email is sent")
    args = parser.parse_args()
    settings = get_settings()
    print(json.dumps({"automatic_send_enabled": settings.outreach_automatic_send_enabled, "sends_email": False}))
    failed = False
    with SessionLocal() as db:
        for account in db.scalars(select(SenderAccount).where(SenderAccount.provider == "mailru_smtp").order_by(SenderAccount.id)).all():
            credentials_ok = True
            try:
                CredentialCipher(settings.mail_credentials_encryption_key).decrypt(account.encrypted_password)
            except CredentialEncryptionError:
                credentials_ok = False
            if args.verify and account.is_active and account.smtp_enabled:
                account = verify_sender_account(db, account, settings)
            data = sender_account_to_dict(account)
            data["credentials_decrypt_ok"] = credentials_ok
            print(json.dumps(data, ensure_ascii=False), flush=True)
            failed |= account.is_active and (not credentials_ok or account.verification_status != "verified" or (account.imap_enabled and account.imap_verification_status != "verified"))
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
