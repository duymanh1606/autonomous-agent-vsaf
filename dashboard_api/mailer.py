"""Outgoing email for the dashboard's own accounts: signup verification codes
and temporary passwords. Nothing here is about customers or TikTok.

Configured from the environment (.env), read on every send so tests can patch
`os.environ`:

    SMTP_HOST       e.g. smtp.gmail.com - unset means "no mail server"
    SMTP_PORT       587 (STARTTLS) by default, 465 with SMTP_SECURITY=ssl
    SMTP_SECURITY   starttls (default) | ssl | none
    SMTP_USERNAME   login, usually the sending address
    SMTP_PASSWORD   for Gmail an App Password, never the account password
    SMTP_FROM       the From: address; defaults to SMTP_USERNAME

With no SMTP_HOST on a development machine, a message is appended to
`data/dashboard-outbox.log` (owner-only, 0600) instead, so signup and
password recovery can be exercised without a mail account. That file holds
codes and temporary passwords, which is why it is never the shared log and
why production refuses to fall back to it: there, a missing mail server is an
error the operator has to see (MailNotConfigured), not a silent file on disk.
"""

import logging
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from dashboard_api import security_config

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parents[1]
OUTBOX_PATH = BASE_DIR / "data" / "dashboard-outbox.log"
SEND_TIMEOUT_SECONDS = 15


class MailNotConfigured(RuntimeError):
    """Production has no SMTP_HOST, so nothing can be delivered."""


class MailDeliveryFailed(RuntimeError):
    """The mail server was reachable in principle but the send failed."""


def is_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST", "").strip())


def send(to: str, subject: str, body: str) -> None:
    if not is_configured():
        if security_config.is_production():
            raise MailNotConfigured("SMTP_HOST is not set")
        _write_outbox(to, subject, body)
        return

    host = os.environ["SMTP_HOST"].strip()
    security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
    port = int(os.environ.get("SMTP_PORT") or (465 if security == "ssl" else 587))
    username = os.environ.get("SMTP_USERNAME", "").strip()
    password = _password(host)
    sender = os.environ.get("SMTP_FROM", "").strip() or username

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    context = ssl.create_default_context()
    # Which step failed says more than the error: a connection closed while
    # "connect" is a network or provider block, while "login" is credentials.
    stage = "connect"
    try:
        if security == "ssl":
            client = smtplib.SMTP_SSL(host, port, timeout=SEND_TIMEOUT_SECONDS, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=SEND_TIMEOUT_SECONDS)
        with client:
            if security == "starttls":
                stage = "starttls"
                client.starttls(context=context)
            if username:
                stage = "login"
                client.login(username, password)
            stage = "send"
            client.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        # The address and the body stay out of the log: the body is a code or
        # a password, and the address is personal data. The server's own
        # reply does go in - "535 Username and Password not accepted" is what
        # tells an operator the App Password is wrong, and it holds neither.
        logger.warning(
            "[mail] send failed host=%s port=%s stage=%s error_type=%s detail=%s",
            host, port, stage, type(exc).__name__, _server_reply(exc),
        )
        raise MailDeliveryFailed(
            f"{type(exc).__name__} ở bước {stage}: {_server_reply(exc)}"
        ) from exc
    logger.info("[mail] sent subject=%r", subject)


def _password(host: str) -> str:
    password = os.environ.get("SMTP_PASSWORD", "")
    # Google shows an App Password as four groups of four letters and never
    # puts a space in one; pasted as shown, the spaces make Gmail refuse the
    # login. No other provider's password is touched.
    if host.casefold() == "smtp.gmail.com":
        password = password.replace(" ", "")
    return password


def _server_reply(exc: Exception) -> str:
    """What the mail server said (code and text), or the error itself."""
    code = getattr(exc, "smtp_code", None)
    raw = getattr(exc, "smtp_error", None)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = " ".join(str(raw if raw is not None else exc).split())
    return (f"{code} {text}" if code else text)[:200]


def _write_outbox(to: str, subject: str, body: str) -> None:
    OUTBOX_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTBOX_PATH, "a", encoding="utf-8") as handle:
        handle.write(
            f"--- {datetime.now(timezone.utc).isoformat()}\n"
            f"To: {to}\nSubject: {subject}\n\n{body}\n\n"
        )
    os.chmod(OUTBOX_PATH, 0o600)
    logger.info(
        "[mail] SMTP not configured; message written to %s (local, owner-only)",
        OUTBOX_PATH,
    )


def _probe(host: str, port: int, implicit_tls: bool) -> str:
    """Open a plain IPv4 connection and read the server's greeting.

    What a network block looks like differs from what a mail-server refusal
    looks like, and only the raw greeting separates them: "220 smtp.gmail.com
    ESMTP" means the path is open, a timeout or a closed connection means
    something between this host and the server stopped it.
    """
    import socket

    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
        address = infos[0][4]
        raw = socket.create_connection(address, timeout=8)
    except OSError as exc:
        return f"không kết nối được ({type(exc).__name__}: {exc})"
    try:
        sock = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            if implicit_tls else raw
        )
        sock.settimeout(8)
        greeting = sock.recv(256).decode("utf-8", errors="replace").strip()
        return f"mở - {greeting[:80]}" if greeting else "kết nối được nhưng bị đóng ngay, không có lời chào"
    except OSError as exc:
        return f"kết nối được nhưng lỗi khi chờ lời chào ({type(exc).__name__}: {exc})"
    finally:
        raw.close()


def _diagnose(to: str) -> int:
    """`python -m dashboard_api.mailer you@gmail.com`: send one test mail and
    say exactly what happened. Run it with the same environment the service
    has (see deploy/README.md), since the forgot-password page always answers
    "ok" and the reason a mail did not arrive is otherwise only in the log."""
    host = os.environ.get("SMTP_HOST", "").strip()
    print(f"SMTP_HOST     = {host or '(chưa đặt)'}")
    if host:
        security = os.environ.get("SMTP_SECURITY", "starttls").strip().lower()
        print(f"SMTP_SECURITY = {security}")
        print(f"SMTP_PORT     = {os.environ.get('SMTP_PORT') or (465 if security == 'ssl' else 587)}")
        print(f"SMTP_USERNAME = {os.environ.get('SMTP_USERNAME', '').strip() or '(chưa đặt)'}")
        print(f"SMTP_PASSWORD = {'đã đặt, ' + str(len(_password(host))) + ' ký tự' if _password(host) else '(chưa đặt)'}")
        print(f"SMTP_FROM     = {os.environ.get('SMTP_FROM', '').strip() or '(dùng SMTP_USERNAME)'}")
        print("Dò đường mạng (IPv4):")
        print(f"  {host}:587 (starttls) -> {_probe(host, 587, False)}")
        print(f"  {host}:465 (ssl)      -> {_probe(host, 465, True)}")
    try:
        send(to, "Thử gửi mail từ dashboard", "Mail thử - hệ thống gửi mail đang hoạt động.")
    except MailNotConfigured:
        print("KẾT QUẢ: chưa cấu hình SMTP_HOST - production không gửi được mail nào.")
        return 1
    except MailDeliveryFailed as exc:
        print(f"KẾT QUẢ: gửi thất bại - {exc}")
        return 1
    if not host:
        print(f"KẾT QUẢ: chưa có SMTP, mail được ghi vào {OUTBOX_PATH} (chỉ máy dev).")
        return 1
    print(f"KẾT QUẢ: đã gửi tới {to}. Không thấy thì xem cả thư mục Spam.")
    return 0


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Cách dùng: python -m dashboard_api.mailer <email-nhận-thử>")
        raise SystemExit(2)
    raise SystemExit(_diagnose(sys.argv[1]))
