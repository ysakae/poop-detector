"""招待・メールアドレス確認・パスキー復旧のリンクを SMTP で送る。"""

import smtplib
import ssl
from email.message import EmailMessage

from ..config import load_secret_settings
from ..errors import InvalidInputError, UserError
from .accounts import ENROLLMENT_MINUTES, EnrollmentStore, User, normalize_email

SMTP_KEYS = (
    "POOP_SMTP_HOST",
    "POOP_SMTP_PORT",
    "POOP_SMTP_FROM",
    "POOP_SMTP_USERNAME",
    "POOP_SMTP_PASSWORD",
)


class MailError(UserError):
    """接続設定か配送に問題があり、メールを送れなかった。"""


def _send(recipient: str, subject: str, body: str) -> None:
    settings = load_secret_settings(SMTP_KEYS)
    host, port_value, sender, username, password = (settings[key] for key in SMTP_KEYS)
    host = host.strip()
    try:
        port = int(port_value.strip() or "587")
        sender = normalize_email(sender)
        if not host or not 1 <= port <= 65535 or bool(username) != bool(password):
            raise ValueError
    except ValueError:
        raise MailError("SMTP の接続先、送信元、認証設定を確認してください") from None

    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    context = ssl.create_default_context()
    client: smtplib.SMTP
    try:
        if port == 465:
            client = smtplib.SMTP_SSL(host, port, timeout=10, context=context)
        else:
            client = smtplib.SMTP(host, port, timeout=10)
        with client:
            if port != 465:
                client.starttls(context=context)
            if username:
                client.login(username, password)
            client.send_message(message)
    except (OSError, smtplib.SMTPException):
        raise MailError(
            "メールを送信できませんでした。SMTP の設定と接続を確認してください"
        ) from None


def _public_url(base: str | None) -> str:
    if not base or not base.startswith("https://"):
        raise InvalidInputError("メール送信には gui.public_url に公開 HTTPS URL を設定してください")
    return base.rstrip("/")


def _deliver(
    enrollments: EnrollmentStore,
    recipient: str,
    code: str,
    url: str,
    subject: str,
    instruction: str,
    minutes: float,
) -> None:
    body = (
        f"{instruction}\n\n{url}\n\nこのリンクは発行から{minutes:g}分間有効で、一度だけ使えます。\n"
    )
    try:
        _send(recipient, subject, body)
    except MailError:
        enrollments.revoke(code)
        raise


def invite_by_email(
    enrollments: EnrollmentStore,
    base: str | None,
    email: str,
    *,
    actor: str,
    name: str = "",
    role: str = "admin",
    minutes: float = ENROLLMENT_MINUTES,
    resend: bool = False,
) -> None:
    base = _public_url(base)
    email = normalize_email(email)
    code = enrollments.issue_email(
        email, name=name, role=role, created_by=actor, minutes=minutes, resend=resend
    )
    _deliver(
        enrollments,
        email,
        code,
        f"{base}/enroll?code={code}",
        "poop-detector への招待",
        "次のリンクを開き、利用する端末でパスキーを登録してください。",
        minutes,
    )


def verify_address(enrollments: EnrollmentStore, base: str | None, user: User, email: str) -> None:
    base = _public_url(base)
    email = normalize_email(email)
    code = enrollments.issue_email(email, created_by=user.id, user_id=user.id)
    _deliver(
        enrollments,
        email,
        code,
        f"{base}/account/email/confirm?code={code}",
        "poop-detector のメールアドレス確認",
        "ログイン済みのアカウントにメールアドレスをひも付けるには、次のリンクを開いてください。\n"
        f"別のドメインでログインしている場合は、アカウント画面に確認コードを入力してください。\n確認コード: {code}",
        ENROLLMENT_MINUTES,
    )


def recover_by_email(
    enrollments: EnrollmentStore, base: str | None, user: User, *, actor: str
) -> None:
    base = _public_url(base)
    if not user.email or not user.active:
        raise InvalidInputError("有効なユーザーの確認済みメールアドレスが必要です")
    code = enrollments.issue_recovery(user.id, created_by=actor)
    _deliver(
        enrollments,
        user.email,
        code,
        f"{base}/enroll?code={code}",
        "poop-detector のパスキー復旧",
        "次のリンクを開き、新しいパスキーを登録してください。",
        ENROLLMENT_MINUTES,
    )
