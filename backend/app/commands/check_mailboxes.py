"""Safe mailbox diagnostics. Never sends mail, prints secrets or requeues deliveries."""
import argparse
import json
import queue
import socket
import ssl
import threading
import time

from sqlalchemy import select

from app.mail_providers import MAIL_PROVIDERS, SMTP_SENDER_PROVIDERS
from app.config import get_settings
from app.database import SessionLocal
from app.models import SenderAccount
from app.services.credentials import CredentialCipher, CredentialEncryptionError
from app.services.sender_accounts import sender_account_to_dict, verify_sender_account


class InvalidMailGreeting(RuntimeError):
    pass


def _resolve_addresses(host: str, port: int, timeout: float) -> list:
    # getaddrinfo has no timeout argument. A daemon keeps even a stalled
    # system resolver from holding this diagnostic process open indefinitely.
    result = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put((socket.getaddrinfo(host, port, type=socket.SOCK_STREAM), None))
        except Exception as exc:
            result.put((None, exc))

    threading.Thread(target=resolve, daemon=True, name="mail-diagnostic-dns").start()
    try:
        addresses, error = result.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError("DNS lookup deadline exceeded") from exc
    if error is not None:
        raise error
    if not addresses:
        raise socket.gaierror("DNS returned no addresses")
    return addresses


def probe_mail_endpoint(host: str, port: int, protocol: str, *, timeout_seconds: float = 10.0) -> dict:
    """Read a verified-TLS server greeting, with no AUTH or client commands."""
    deadline = time.monotonic() + timeout_seconds
    result = {
        "type": "mail_network",
        "protocol": protocol,
        "host": host,
        "port": port,
        "auth_attempted": False,
        "ok": False,
        "stages": dict.fromkeys(("dns", "tcp", "tls", "greeting"), "not_checked"),
    }
    stage = "dns"
    connection = None

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("Endpoint diagnostic deadline exceeded")
        return value

    try:
        addresses = _resolve_addresses(host, port, remaining())
        result["stages"][stage] = "ok"
        result["resolved_addresses"] = list(dict.fromkeys(address[4][0] for address in addresses))
        stage = "tcp"
        connect_error = None
        for index, (family, socktype, proto, _, address) in enumerate(addresses):
            try:
                connection = socket.socket(family, socktype, proto)
                # Leave time for fallback when an IPv6/IPv4 route is absent.
                connection.settimeout(remaining() / (len(addresses) - index))
                connection.connect(address)
                result["connected_address"] = address[0]
                break
            except OSError as exc:
                connect_error = exc
                if connection is not None:
                    connection.close()
                    connection = None
        if connection is None:
            raise connect_error or ConnectionError("No connected address")
        result["stages"][stage] = "ok"
        stage = "tls"
        context = ssl.create_default_context()
        connection = context.wrap_socket(connection, server_hostname=host, do_handshake_on_connect=False)
        connection.settimeout(remaining())
        connection.do_handshake()
        result["stages"][stage] = "ok"
        stage = "greeting"
        greeting = b""
        while b"\n" not in greeting and len(greeting) < 4096:
            connection.settimeout(remaining())
            chunk = connection.recv(min(1024, 4096 - len(greeting)))
            if not chunk:
                raise ConnectionError("Server closed before greeting")
            greeting += chunk
        first_line = greeting.split(b"\n", 1)[0].rstrip(b"\r")
        if protocol == "smtp":
            valid = first_line.startswith((b"220 ", b"220-"))
        else:
            valid = first_line.upper().startswith((b"* OK ", b"* PREAUTH "))
        if not valid or b"\n" not in greeting:
            raise InvalidMailGreeting("Unexpected mail server greeting")
        result["stages"][stage] = "ok"
        result["ok"] = True
        result["guidance"] = "DNS, TCP, TLS и приветствие сервера доступны. Пароль и авторизация не проверялись."
    except Exception as exc:
        result["stages"][stage] = "failed"
        result["failed_stage"] = stage
        # Provider banners and exception strings may contain arbitrary data.
        result["error_type"] = type(exc).__name__
        result["guidance"] = {
            "dns": "Не удалось разрешить имя почтового сервера. Проверьте DNS и доступность сети. Пароль не использовался.",
            "tcp": "Не удалось установить TCP-соединение. Проверьте доступность порта, ограничения сети и маршрут VPN. Пароль не использовался.",
            "tls": "TCP-соединение установлено, но TLS не завершён. Проверьте сертификат, сетевую фильтрацию и маршрут VPN к почтовому серверу. Пароль не использовался; его замена не устранит эту ошибку.",
            "greeting": "TLS установлен, но сервер не прислал ожидаемое приветствие. Проверьте доступность сервиса и ограничения соединений. Пароль не использовался.",
        }[stage]
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
    result["elapsed_seconds"] = round(max(0, time.monotonic() - deadline + timeout_seconds), 3)
    return result


def _network_timeout(value: str) -> float:
    timeout = float(value)
    if not 0 < timeout <= 60:
        raise argparse.ArgumentTypeError("network timeout must be greater than 0 and at most 60 seconds")
    return timeout


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--verify", action="store_true", help="Check SMTP/IMAP login and save connection status; no email is sent")
    mode.add_argument("--network", action="store_true", help="Read-only DNS/TCP/TLS/greeting checks; no AUTH, emails or network configuration changes")
    parser.add_argument("--network-timeout", type=_network_timeout, default=10.0, help="Overall deadline in seconds per network endpoint (default: 10, maximum: 60)")
    args = parser.parse_args()
    settings = get_settings()
    print(json.dumps({"automatic_send_enabled": settings.outreach_automatic_send_enabled, "sends_email": False}))
    failed = False
    endpoints = set()
    with SessionLocal() as db:
        for account in db.scalars(select(SenderAccount).where(SenderAccount.provider.in_(SMTP_SENDER_PROVIDERS)).order_by(SenderAccount.id)).all():
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
            active_smtp = account.is_active and account.smtp_enabled
            failed |= active_smtp and (not credentials_ok or account.verification_status != "verified" or (account.imap_enabled and account.imap_verification_status != "verified"))
            if args.network and active_smtp:
                endpoints.add((account.smtp_host or MAIL_PROVIDERS[account.provider].smtp_host, account.smtp_port or 465, "smtp"))
                endpoints.add((account.imap_host or MAIL_PROVIDERS[account.provider].imap_host, account.imap_port or 993, "imap"))
    for host, port, protocol in sorted(endpoints):
        result = probe_mail_endpoint(host, port, protocol, timeout_seconds=args.network_timeout)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        failed |= not result["ok"]
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
