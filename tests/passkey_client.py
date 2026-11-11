"""テスト用のブラウザー役。Host と Origin を決めて、パスキーの JSON の窓口とページを呼ぶ。"""

import http.client
import json

from support import exchange


class Client:
    """1つのブラウザー。Host と Origin を決めて、JSON の API とページを呼ぶ。"""

    def __init__(self, server, host=None, origin=None, *, https=False):
        self.server = server
        self.host = host or f"localhost:{server.server_port}"
        self.origin = origin or f"http://{self.host}"
        self.https = https
        self.cookie = None

    def headers(self, extra=None):
        headers = {"Host": self.host}
        if self.https:
            headers["X-Forwarded-Proto"] = "https"
        if self.cookie:
            headers["Cookie"] = self.cookie
        return {**headers, **(extra or {})}

    def get(self, path, extra=None):
        return exchange(self.server, "GET", path, headers=self.headers(extra), auth=False)

    def post(self, path, payload, *, origin=True):
        extra = {"Content-Type": "application/json"}
        if origin:
            extra["Origin"] = self.origin
        conn = http.client.HTTPConnection(*self.server.server_address)
        conn.request("POST", path, json.dumps(payload), self.headers(extra))
        response = conn.getresponse()
        body = response.read()
        conn.close()
        self.last_headers = response.headers
        if response.headers.get("Set-Cookie"):
            self.cookie = response.headers["Set-Cookie"].split(";", 1)[0]
        data = json.loads(body) if body.startswith(b"{") else None
        return response.status, data

    def register(self, authenticator, code, label="テスト端末"):
        status, begun = self.post("/webauthn/register/options", {"code": code})
        assert status == 200, begun
        credential = authenticator.register(begun["publicKey"], self.origin)
        return self.post(
            "/webauthn/register/verify",
            {"ceremony": begun["ceremony"], "credential": credential, "label": label},
        )

    def login(self, authenticator, *, next_path=None, rp_id=None):
        status, begun = self.post("/webauthn/login/options", {})
        assert status == 200, begun
        credential = authenticator.login(begun["publicKey"], self.origin, rp_id=rp_id)
        payload = {"ceremony": begun["ceremony"], "credential": credential}
        if next_path:
            payload["next"] = next_path
        return self.post("/webauthn/login/verify", payload)
