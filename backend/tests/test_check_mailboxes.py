import json
import queue
import socket
import ssl
from contextlib import nullcontext

import pytest

from app.commands import check_mailboxes
from app.config import Settings
from app.models import SenderAccount
from app.services.credentials import CredentialCipher, generate_encryption_key


class FakeSocket:
    def __init__(self, *, connect_error=None, tls_error=None, greeting=b"220 smtp.mail.ru ready\r\n"):
        self.connect_error = connect_error
        self.tls_error = tls_error
        self.greeting = greeting
        self.closed = False
        self.timeouts = []
        self.handshakes = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, address):
        if self.connect_error:
            raise self.connect_error

    def do_handshake(self):
        self.handshakes += 1
        if self.tls_error:
            raise self.tls_error

    def recv(self, size):
        if isinstance(self.greeting, Exception):
            raise self.greeting
        result, self.greeting = self.greeting[:size], self.greeting[size:]
        return result

    def close(self):
        self.closed = True


def mock_network(monkeypatch, connection, *, host="smtp.mail.ru", addresses=None):
    addresses = addresses or [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 465))]
    monkeypatch.setattr(check_mailboxes, "_resolve_addresses", lambda *_: addresses)
    monkeypatch.setattr(check_mailboxes.socket, "socket", lambda *_: connection)

    class FakeContext:
        def wrap_socket(self, connection, *, server_hostname, do_handshake_on_connect):
            assert server_hostname == host
            assert do_handshake_on_connect is False
            return connection

    monkeypatch.setattr(check_mailboxes.ssl, "create_default_context", FakeContext)


@pytest.mark.parametrize("protocol,host,port,greeting", [
    ("smtp", "smtp.mail.ru", 465, b"220 smtp.mail.ru ready\r\n"),
    ("smtp", "smtp.mail.ru", 465, b"220-smtp.mail.ru ready\r\n220 hello\r\n"),
    ("imap", "imap.mail.ru", 993, b"* OK IMAP ready\r\n"),
    ("imap", "imap.mail.ru", 993, b"* PREAUTH IMAP ready\r\n"),
])
def test_network_reads_greeting_with_verified_tls_and_no_commands(monkeypatch, protocol, host, port, greeting):
    connection = FakeSocket(greeting=greeting)
    mock_network(monkeypatch, connection, host=host)
    result = check_mailboxes.probe_mail_endpoint(host, port, protocol, timeout_seconds=2)

    assert result["ok"] is True
    assert result["auth_attempted"] is False
    assert result["stages"] == dict.fromkeys(("dns", "tcp", "tls", "greeting"), "ok")
    assert result["connected_address"] == "192.0.2.1"
    assert connection.handshakes == 1
    assert connection.closed is True
    assert all(0 < timeout <= 2 for timeout in connection.timeouts)


@pytest.mark.parametrize("stage,error", [
    ("dns", socket.gaierror("private password=secret")),
    ("tcp", ConnectionRefusedError("private password=secret")),
    ("tls", TimeoutError("private password=secret")),
    ("tls", ssl.SSLCertVerificationError("private password=secret")),
    ("greeting", TimeoutError("private password=secret")),
])
def test_network_failure_identifies_stage_without_exposing_exception_text(monkeypatch, stage, error):
    connection = FakeSocket(
        connect_error=error if stage == "tcp" else None,
        tls_error=error if stage == "tls" else None,
        greeting=error if stage == "greeting" else b"220 ready\r\n",
    )
    mock_network(monkeypatch, connection)
    if stage == "dns":
        def broken_dns(*_):
            raise error
        monkeypatch.setattr(check_mailboxes, "_resolve_addresses", broken_dns)
    result = check_mailboxes.probe_mail_endpoint("smtp.mail.ru", 465, "smtp")

    assert result["ok"] is False
    assert result["failed_stage"] == stage
    assert result["error_type"] == type(error).__name__
    assert result["stages"][stage] == "failed"
    assert "secret" not in json.dumps(result)
    assert result["auth_attempted"] is False
    if stage == "tls":
        assert result["stages"]["tcp"] == "ok"
        assert "VPN" in result["guidance"]
        assert "Пароль не использовался" in result["guidance"]
    if stage != "dns":
        assert connection.closed is True


@pytest.mark.parametrize("greeting", [b"421 Temporarily unavailable secret\r\n", b"* BYE secret\r\n", b"220 incomplete", b""])
def test_missing_or_refused_greeting_is_reported_without_raw_banner(monkeypatch, greeting):
    connection = FakeSocket(greeting=greeting)
    mock_network(monkeypatch, connection)
    result = check_mailboxes.probe_mail_endpoint("smtp.mail.ru", 465, "smtp")

    assert result["failed_stage"] == "greeting"
    assert result["stages"]["tls"] == "ok"
    assert "secret" not in json.dumps(result)
    assert connection.closed is True


def test_tcp_tries_other_resolved_address_when_first_route_is_unavailable(monkeypatch):
    first = FakeSocket(connect_error=OSError("no IPv6 route"))
    second = FakeSocket()
    mock_network(monkeypatch, second, addresses=[
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", 465, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 465)),
    ])
    sockets = iter((first, second))
    monkeypatch.setattr(check_mailboxes.socket, "socket", lambda *_: next(sockets))
    result = check_mailboxes.probe_mail_endpoint("smtp.mail.ru", 465, "smtp")

    assert result["ok"] is True
    assert result["connected_address"] == "192.0.2.1"
    assert first.closed is True
    assert second.closed is True


def test_one_total_deadline_covers_all_network_stages(monkeypatch):
    connection = FakeSocket()
    mock_network(monkeypatch, connection)
    clock = iter((0.0, 0.1, 0.2, 1.1, 1.2))
    monkeypatch.setattr(check_mailboxes.time, "monotonic", lambda: next(clock))
    result = check_mailboxes.probe_mail_endpoint("smtp.mail.ru", 465, "smtp", timeout_seconds=1)

    assert result["failed_stage"] == "tls"
    assert result["error_type"] == "TimeoutError"
    assert connection.handshakes == 0
    assert connection.closed is True


def test_stalled_dns_is_bounded_and_uses_daemon_thread(monkeypatch):
    calls = []

    class StalledThread:
        def __init__(self, *, target, daemon, name):
            calls.append((daemon, name))
        def start(self):
            pass

    class StalledQueue:
        def __init__(self, *, maxsize):
            assert maxsize == 1
        def get(self, *, timeout):
            calls.append(timeout)
            raise queue.Empty

    monkeypatch.setattr(check_mailboxes.threading, "Thread", StalledThread)
    monkeypatch.setattr(check_mailboxes.queue, "Queue", StalledQueue)
    with pytest.raises(TimeoutError):
        check_mailboxes._resolve_addresses("smtp.mail.ru", 465, 0.5)

    assert calls == [(True, "mail-diagnostic-dns"), 0.5]


@pytest.fixture()
def configured_command(monkeypatch, db):
    settings = Settings(_env_file=None, mail_credentials_encryption_key=generate_encryption_key())
    monkeypatch.setattr(check_mailboxes, "get_settings", lambda: settings)
    monkeypatch.setattr(check_mailboxes, "SessionLocal", lambda: nullcontext(db))

    def add_account(email, **values):
        account = SenderAccount(
            email=email,
            encrypted_password=CredentialCipher(settings.mail_credentials_encryption_key).encrypt("secret"),
            verification_status=values.pop("verification_status", "verified"),
            **values,
        )
        db.add(account)
        db.commit()
        return account
    return add_account


def test_network_mode_deduplicates_hosts_and_never_verifies_or_updates_accounts(monkeypatch, configured_command, capsys):
    first = configured_command("one@mail.ru")
    configured_command("two@mail.ru")
    checked_at = first.verification_checked_at
    probes = []
    def probe(host, port, protocol, *, timeout_seconds):
        probes.append((host, port, protocol, timeout_seconds))
        return {"type": "mail_network", "ok": True, "auth_attempted": False}
    monkeypatch.setattr(check_mailboxes, "probe_mail_endpoint", probe)
    monkeypatch.setattr(check_mailboxes, "verify_sender_account", lambda *_: pytest.fail("--network must not authenticate"))
    monkeypatch.setattr("sys.argv", ["check_mailboxes", "--network", "--network-timeout", "3"])

    with pytest.raises(SystemExit) as caught:
        check_mailboxes.main()

    assert caught.value.code == 0
    assert sorted(probes) == [("imap.mail.ru", 993, "imap", 3), ("smtp.mail.ru", 465, "smtp", 3)]
    assert first.verification_checked_at == checked_at
    output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(output) == 5
    assert output[0]["sends_email"] is False
    assert "secret" not in json.dumps(output)


def test_disabled_smtp_account_does_not_fail_diagnostics_or_trigger_probes(monkeypatch, configured_command, capsys):
    configured_command("disabled@mail.ru", smtp_enabled=False, verification_status="failed", imap_enabled=True)
    monkeypatch.setattr(check_mailboxes, "probe_mail_endpoint", lambda *_args, **_kwargs: pytest.fail("disabled account must not probe"))
    monkeypatch.setattr("sys.argv", ["check_mailboxes", "--network"])

    with pytest.raises(SystemExit) as caught:
        check_mailboxes.main()

    assert caught.value.code == 0


def test_failed_network_probe_sets_nonzero_exit_without_changing_saved_status(monkeypatch, configured_command, capsys):
    account = configured_command("one@mail.ru")
    monkeypatch.setattr(check_mailboxes, "probe_mail_endpoint", lambda *_args, **_kwargs: {"ok": False, "failed_stage": "tls"})
    monkeypatch.setattr("sys.argv", ["check_mailboxes", "--network"])

    with pytest.raises(SystemExit) as caught:
        check_mailboxes.main()

    assert caught.value.code == 1
    assert account.verification_status == "verified"


def test_default_remains_offline_and_verify_mode_uses_existing_verifier(monkeypatch, configured_command, capsys):
    account = configured_command("one@mail.ru")
    checks = []
    monkeypatch.setattr(check_mailboxes, "verify_sender_account", lambda _db, value, _settings: checks.append(value.id) or value)
    monkeypatch.setattr(check_mailboxes, "probe_mail_endpoint", lambda *_args, **_kwargs: pytest.fail("network mode was not selected"))
    for args in ([], ["--verify"]):
        monkeypatch.setattr("sys.argv", ["check_mailboxes", *args])
        with pytest.raises(SystemExit) as caught:
            check_mailboxes.main()
        assert caught.value.code == 0
        output = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert len(output) == 2
    assert checks == [account.id]


@pytest.mark.parametrize("args", [["--network", "--verify"], ["--network-timeout", "0"], ["--network-timeout", "61"], ["--network-timeout", "nan"]])
def test_invalid_modes_and_unbounded_timeouts_are_rejected_before_any_work(monkeypatch, args):
    monkeypatch.setattr("sys.argv", ["check_mailboxes", *args])
    monkeypatch.setattr(check_mailboxes, "get_settings", lambda: pytest.fail("arguments must fail before work"))
    with pytest.raises(SystemExit) as caught:
        check_mailboxes.main()
    assert caught.value.code == 2
