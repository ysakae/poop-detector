// パスキーの登録とログイン。サーバーの /webauthn/... と、ブラウザーの WebAuthn をつなぐ。
// 値の受け渡しは base64url の JSON で行う（py_webauthn の options_to_json と同じ形）。
"use strict";

(() => {
  const message = document.getElementById("passkey-message");

  function show(text) {
    if (message) message.textContent = text;
  }

  function decode(text) {
    const base64 = text.replace(/-/g, "+").replace(/_/g, "/");
    const padded = base64 + "=".repeat((4 - (base64.length % 4)) % 4);
    return Uint8Array.from(atob(padded), (char) => char.charCodeAt(0)).buffer;
  }

  function encode(buffer) {
    const bytes = new Uint8Array(buffer);
    let binary = "";
    bytes.forEach((byte) => {
      binary += String.fromCharCode(byte);
    });
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function creationOptions(options) {
    return {
      ...options,
      challenge: decode(options.challenge),
      user: { ...options.user, id: decode(options.user.id) },
      excludeCredentials: (options.excludeCredentials || []).map((item) => ({
        ...item,
        id: decode(item.id),
      })),
    };
  }

  function requestOptions(options) {
    return {
      ...options,
      challenge: decode(options.challenge),
      allowCredentials: (options.allowCredentials || []).map((item) => ({
        ...item,
        id: decode(item.id),
      })),
    };
  }

  function credentialJSON(credential) {
    const response = credential.response;
    const result = {
      id: credential.id,
      rawId: encode(credential.rawId),
      type: credential.type,
      authenticatorAttachment: credential.authenticatorAttachment || undefined,
      clientExtensionResults: credential.getClientExtensionResults(),
      response: { clientDataJSON: encode(response.clientDataJSON) },
    };
    if (response.attestationObject) {
      result.response.attestationObject = encode(response.attestationObject);
      result.response.transports = response.getTransports ? response.getTransports() : [];
    } else {
      result.response.authenticatorData = encode(response.authenticatorData);
      result.response.signature = encode(response.signature);
      if (response.userHandle) result.response.userHandle = encode(response.userHandle);
    }
    return result;
  }

  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      credentials: "same-origin",
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "うまくいきませんでした。もう一度お試しください");
    return data;
  }

  async function register(button) {
    const label = document.getElementById("passkey-label");
    const begun = await post("/webauthn/register/options", { code: button.dataset.code });
    show("端末の指示に従って、パスキーを作ってください");
    const credential = await navigator.credentials.create({
      publicKey: creationOptions(begun.publicKey),
    });
    const result = await post("/webauthn/register/verify", {
      ceremony: begun.ceremony,
      credential: credentialJSON(credential),
      label: label ? label.value : "",
    });
    show("登録しました");
    window.location.assign(result.redirect);
  }

  async function login(button) {
    const begun = await post("/webauthn/login/options", {});
    show("端末の指示に従って、パスキーを選んでください");
    const credential = await navigator.credentials.get({
      publicKey: requestOptions(begun.publicKey),
    });
    const result = await post("/webauthn/login/verify", {
      ceremony: begun.ceremony,
      credential: credentialJSON(credential),
      next: button.dataset.next,
    });
    window.location.assign(result.redirect);
  }

  document.querySelectorAll("[data-passkey]").forEach((button) => {
    if (!window.PublicKeyCredential) {
      button.disabled = true;
      show("このブラウザーはパスキーに対応していません");
      return;
    }
    button.addEventListener("click", async () => {
      button.disabled = true;
      show("");
      try {
        await (button.dataset.passkey === "register" ? register(button) : login(button));
      } catch (error) {
        // 利用者が取り消したときは NotAllowedError になる。
        show(
          error.name === "NotAllowedError"
            ? "取り消されたか、時間切れになりました。もう一度お試しください"
            : error.message,
        );
        button.disabled = false;
      }
    });
  });
})();
