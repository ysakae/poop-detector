"""テスト用のソフトウェアの認証器（パスキー）。

ES256 の鍵を持ち、ブラウザーの navigator.credentials.create・get が返すのと同じ形の
JSON を作る。サーバー側の検証（py_webauthn）は本物のまま通す。
"""

import base64
import hashlib
import json
import os

import cbor2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

# 認証器のデータのフラグ。UP（本人がいる）、UV（本人確認した）、AT（資格情報を含む）。
USER_PRESENT = 0x01
USER_VERIFIED = 0x04
ATTESTED = 0x40


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SoftAuthenticator:
    """1つのパスキーを持つ認証器。verify=False で、本人確認（UV）なしの応答を作る。"""

    def __init__(self, *, verify: bool = True) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = os.urandom(16)
        self.sign_count = 0
        self.user_handle: bytes | None = None
        self.flags = USER_PRESENT | (USER_VERIFIED if verify else 0)

    def _cose_key(self) -> bytes:
        numbers = self.key.public_key().public_numbers()
        return cbor2.dumps(
            {
                1: 2,  # 鍵の種類 EC2
                3: -7,  # アルゴリズム ES256
                -1: 1,  # 曲線 P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

    @staticmethod
    def _client_data(kind: str, options: dict, origin: str) -> bytes:
        payload = {"type": kind, "challenge": options["challenge"], "origin": origin}
        return json.dumps({**payload, "crossOrigin": False}).encode()

    def register(self, options: dict, origin: str) -> dict:
        """登録の options（options_to_json の形）に応え、RegistrationCredential の JSON を返す。"""
        self.user_handle = unb64(options["user"]["id"])
        rp_hash = hashlib.sha256(options["rp"]["id"].encode()).digest()
        attested = (
            bytes(16)
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + self._cose_key()
        )
        auth_data = (
            rp_hash + bytes([self.flags | ATTESTED]) + self.sign_count.to_bytes(4, "big") + attested
        )
        attestation = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth_data})
        client_data = self._client_data("webauthn.create", options, origin)
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "attestationObject": b64(attestation),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }

    def login(self, options: dict, origin: str, *, rp_id: str | None = None) -> dict:
        """ログインの options に応え、AuthenticationCredential の JSON を返す。"""
        self.sign_count += 1
        rp_hash = hashlib.sha256((rp_id or options["rpId"]).encode()).digest()
        auth_data = rp_hash + bytes([self.flags]) + self.sign_count.to_bytes(4, "big")
        client_data = self._client_data("webauthn.get", options, origin)
        signed = auth_data + hashlib.sha256(client_data).digest()
        signature = self.key.sign(signed, ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(self.credential_id),
            "rawId": b64(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(signature),
                "userHandle": b64(self.user_handle or b""),
            },
            "clientExtensionResults": {},
            "authenticatorAttachment": "platform",
        }
