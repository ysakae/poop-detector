"""パスキー（WebAuthn）による登録とログイン。HTTP には依存しない。

署名と証明の検証は py_webauthn に任せる。ここでは次を受け持つ。
- 開いたドメインから、RP ID と origin を決める（外部の URL か localhost。IP アドレスでは使えない）
- 登録とログインのやり取り（チャレンジ）を、使い捨てで短い期限付きでメモリーに持つ
- 登録用 URL を使ったことにし、ユーザーとパスキーを台帳に記録する
本人確認（UV）を必須にし、パスキーは端末に保存されるもの（discoverable）だけを受け付ける。
"""

import json
import logging
import secrets
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import uuid4

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from ..errors import InvalidInputError, UserError
from ..web_address import Site
from .accounts import Accounts, Enrollment

logger = logging.getLogger(__name__)
RP_NAME = "poop-detector"
# 認証器の操作を待つ時間（ミリ秒）と、やり取りを覚えておく時間（秒）。
TIMEOUT_MS = 120_000
CEREMONY_SECONDS = 300
MAX_CEREMONIES = 1000
LABEL_MAX_LENGTH = 40
LOGIN_FAILED = "パスキーでログインできませんでした。登録したパスキーを選んでください"
ENROLLMENT_UNUSABLE = (
    "この登録用 URL は使えません。期限が切れたか、すでに使われています。"
    "招待した管理者にメールの再送を依頼するか、監視するマシンで users recover を実行してください"
)


class PasskeyError(UserError, ValueError):
    """パスキーの登録やログインに失敗した。文言はそのまま画面に出してよい。"""


@dataclass(frozen=True)
class RelyingParty:
    """パスキーを結び付けるドメイン（RP ID）と、ブラウザーから見たオリジン。"""

    id: str
    origin: str


def relying_party(site: Site, host: str | None) -> RelyingParty | None:
    """要求の Host から RP を決める。IP アドレス（127.0.0.1）のときは None。"""
    host = (host or "").lower()
    if host == f"localhost:{site.port}":
        return RelyingParty("localhost", f"http://localhost:{site.port}")
    public = site.public_base
    if public is not None and host in site.hosts and not host.startswith("127.0.0.1"):
        origin = public.rstrip("/")
        hostname = origin.split("://", 1)[1].split(":", 1)[0]
        return RelyingParty(hostname, origin)
    return None


@dataclass(frozen=True)
class Ceremony:
    """登録かログインの1回のやり取り。"""

    kind: Literal["register", "login"]
    challenge: bytes
    rp: RelyingParty
    expires: float
    user_handle: str | None = None
    code: str | None = None


class CeremonyStore:
    """やり取りを、使い捨てで期限付きにメモリーに持つ。スレッドから呼ぶので排他を取る。"""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.clock = clock
        self._items: OrderedDict[str, Ceremony] = OrderedDict()
        self._lock = threading.Lock()

    def begin(self, ceremony: Ceremony) -> str:
        ceremony_id = secrets.token_urlsafe(24)
        with self._lock:
            now = self.clock()
            for key in [key for key, item in self._items.items() if item.expires <= now]:
                del self._items[key]
            self._items[ceremony_id] = ceremony
            while len(self._items) > MAX_CEREMONIES:
                self._items.popitem(last=False)
        return ceremony_id

    def take(self, ceremony_id: str, kind: str) -> Ceremony | None:
        """やり取りを取り出す（2回目は None）。種類が違うか期限切れなら None。"""
        with self._lock:
            ceremony = self._items.pop(ceremony_id, None)
        if ceremony is None or ceremony.kind != kind or ceremony.expires <= self.clock():
            return None
        return ceremony

    def deadline(self) -> float:
        return self.clock() + CEREMONY_SECONDS


def _options(options: Any) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(options_to_json(options))
    return result


def _transports(credential: dict[str, Any]) -> list[str]:
    response = credential.get("response")
    values = response.get("transports") if isinstance(response, dict) else None
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, str)][:8]


def _label(value: Any) -> str:
    label = value.strip()[:LABEL_MAX_LENGTH] if isinstance(value, str) else ""
    return label or "パスキー"


class PasskeyAuth:
    """登録用 URL からの登録と、パスキーでのログイン。"""

    def __init__(self, accounts: Accounts, ceremonies: CeremonyStore | None = None) -> None:
        self.accounts = accounts
        self.ceremonies = ceremonies or CeremonyStore()

    def enrollment(self, code: str) -> Enrollment:
        enrollment = self.accounts.enrollments.peek(code)
        if enrollment is None:
            raise PasskeyError(ENROLLMENT_UNUSABLE)
        return enrollment

    def begin_registration(self, code: str, rp: RelyingParty) -> tuple[str, dict[str, Any]]:
        """登録用 URL の値から、ブラウザーに渡す登録の options を作る。"""
        enrollment = self.enrollment(code)
        name = enrollment.name or "ユーザー"
        excluded: list[PublicKeyCredentialDescriptor] = []
        if enrollment.user_id is None:
            if self.accounts.users.by_name(name) is not None:
                raise PasskeyError(
                    f"「{name}」という名前はすでに使われています。別の名前で招待してください"
                )
            handle = uuid4().hex
        else:
            handle = enrollment.user_id
            excluded = [
                PublicKeyCredentialDescriptor(id=base64url_to_bytes(key.id))
                for key in self.accounts.passkeys.for_user(handle)
                if key.rp_id == rp.id
            ]
        options = generate_registration_options(
            rp_id=rp.id,
            rp_name=RP_NAME,
            user_name=name,
            user_id=handle.encode(),
            user_display_name=name,
            timeout=TIMEOUT_MS,
            authenticator_selection=AuthenticatorSelectionCriteria(
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=excluded,
        )
        ceremony = Ceremony(
            "register", options.challenge, rp, self.ceremonies.deadline(), handle, code
        )
        return self.ceremonies.begin(ceremony), _options(options)

    def finish_registration(self, ceremony_id: str, credential: dict[str, Any], label: Any) -> str:
        """ブラウザーが返した資格情報を確かめて記録し、ログインしたユーザーの ID を返す。"""
        ceremony = self.ceremonies.take(ceremony_id, "register")
        if ceremony is None or ceremony.code is None or ceremony.user_handle is None:
            raise PasskeyError("登録の手続きの期限が切れました。もう一度お試しください")
        try:
            verified = verify_registration_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=ceremony.rp.id,
                expected_origin=ceremony.rp.origin,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError):
            logger.warning("パスキーの登録の応答を確かめられませんでした")
            raise PasskeyError(
                "パスキーを確かめられませんでした。画面ロックなどの本人確認を有効にした端末で、"
                "もう一度お試しください"
            ) from None
        try:
            with self.accounts.enrollments.registration(ceremony.code) as (db, enrollment):
                if enrollment is None:
                    raise PasskeyError(ENROLLMENT_UNUSABLE)
                user_id = enrollment.user_id
                if user_id is None:
                    user_id = self.accounts.users.create(
                        enrollment.name or "ユーザー",
                        enrollment.role,
                        user_id=ceremony.user_handle,
                        email=enrollment.email,
                        connection=db,
                    )
                elif not db.execute(
                    "SELECT 1 FROM users WHERE id = ? AND disabled_at IS NULL", (user_id,)
                ).fetchone():
                    raise PasskeyError(ENROLLMENT_UNUSABLE)
                self.accounts.passkeys.add(
                    user_id,
                    ceremony.rp.id,
                    bytes_to_base64url(verified.credential_id),
                    verified.credential_public_key,
                    verified.sign_count,
                    _transports(credential),
                    _label(label),
                    connection=db,
                )
        except InvalidInputError as error:
            raise PasskeyError(str(error)) from None
        except sqlite3.IntegrityError:
            raise PasskeyError("このパスキーはすでに登録されています") from None
        self.accounts.users.logged_in(user_id)
        return user_id

    def begin_login(self, rp: RelyingParty) -> tuple[str, dict[str, Any]]:
        """ログインの options を作る。どのパスキーかは端末に選んでもらう（discoverable）。"""
        options = generate_authentication_options(
            rp_id=rp.id,
            timeout=TIMEOUT_MS,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        ceremony = Ceremony("login", options.challenge, rp, self.ceremonies.deadline())
        return self.ceremonies.begin(ceremony), _options(options)

    def finish_login(self, ceremony_id: str, credential: dict[str, Any]) -> str:
        """ブラウザーが返した署名を確かめ、ログインしたユーザーの ID を返す。

        失敗の理由（知らないパスキー、無効なユーザーなど）は画面で区別しない。
        """
        ceremony = self.ceremonies.take(ceremony_id, "login")
        if ceremony is None:
            raise PasskeyError("ログインの手続きの期限が切れました。もう一度お試しください")
        credential_id = credential.get("id")
        key = self.accounts.passkeys.get(credential_id) if isinstance(credential_id, str) else None
        user = self.accounts.users.get(key.user_id) if key else None
        if key is None or user is None or not user.active or key.rp_id != ceremony.rp.id:
            logger.warning("登録されていないか使えないパスキーでのログインを断りました")
            raise PasskeyError(LOGIN_FAILED)
        try:
            verified = verify_authentication_response(
                credential=credential,
                expected_challenge=ceremony.challenge,
                expected_rp_id=ceremony.rp.id,
                expected_origin=ceremony.rp.origin,
                credential_public_key=key.public_key,
                credential_current_sign_count=key.sign_count,
                require_user_verification=True,
            )
        except (WebAuthnException, ValueError, TypeError, KeyError):
            logger.warning("パスキーの署名を確かめられないログインを断りました")
            raise PasskeyError(LOGIN_FAILED) from None
        self.accounts.passkeys.used(key.id, verified.new_sign_count)
        self.accounts.users.logged_in(user.id)
        return user.id
