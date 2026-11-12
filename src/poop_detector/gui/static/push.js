// 自分の設定の「通知（Web Push）」。この端末のブラウザーで購読し、購読をサーバーに登録する。
(() => {
  const box = document.getElementById("web-push");
  if (!box) return;
  const status = document.getElementById("push-status");
  const enable = document.getElementById("push-enable");
  const disable = document.getElementById("push-disable");
  const csrf = box.dataset.csrf;

  /** @param {string} value base64url @returns {Uint8Array} */
  const decode = value => {
    const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
    return Uint8Array.from(atob(padded), char => char.charCodeAt(0));
  };

  /** @param {string} path @param {Record<string, string>} fields */
  async function send(path, fields) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ ...fields, csrf }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || "登録できませんでした");
  }

  function show(subscribed, message) {
    enable.hidden = subscribed;
    disable.hidden = !subscribed;
    status.textContent = message;
  }

  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    show(false, "このブラウザーでは使えません。iPhone・iPad は、Safari の共有メニューから「ホーム画面に追加」して開いてください。");
    enable.hidden = true;
    return;
  }

  let registration = null;
  (async () => {
    try {
      registration = await navigator.serviceWorker.register("/sw.js");
      const subscription = await registration.pushManager.getSubscription();
      show(Boolean(subscription), subscription ? "この端末で通知を受け取ります。" : "この端末ではまだ受け取りません。");
    } catch {
      show(false, "通知の準備ができませんでした。HTTPS の URL で開いているか確認してください。");
    }
  })();

  enable.addEventListener("click", async () => {
    try {
      if ((await Notification.requestPermission()) !== "granted") {
        show(false, "通知が許可されていません。ブラウザーの設定で、このサイトの通知を許可してください。");
        return;
      }
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: decode(box.dataset.publicKey),
      });
      const { endpoint, keys } = subscription.toJSON();
      await send("/account/push/subscribe", { endpoint, p256dh: keys.p256dh, auth: keys.auth });
      show(true, "この端末で通知を受け取ります。");
    } catch (error) {
      show(false, error instanceof Error ? error.message : "登録できませんでした");
    }
  });

  disable.addEventListener("click", async () => {
    try {
      const subscription = await registration.pushManager.getSubscription();
      if (subscription) {
        await send("/account/push/unsubscribe", { endpoint: subscription.endpoint });
        await subscription.unsubscribe();
      }
      show(false, "この端末では受け取りません。");
    } catch (error) {
      show(true, error instanceof Error ? error.message : "解除できませんでした");
    }
  });
})();
