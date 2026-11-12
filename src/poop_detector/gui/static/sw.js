// Service Worker。Web Push の通知を出し、開くとその画面へ移る。オフラインのときは短い案内だけを出す。
// 通知の本文は監視が暗号化して送ったもので、映像や URL の秘密は含まない。
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));

self.addEventListener("push", event => {
  let message = { title: "トイレの通知", body: "", url: "/" };
  try {
    message = { ...message, ...event.data.json() };
  } catch {
    // 読めない本文でも、通知が届いたことは知らせる。
  }
  // 自分のサイトの中の画面だけを開く（本文の url は相対パス）。
  const url = new URL(message.url, self.location.origin);
  const target = url.origin === self.location.origin ? url.pathname + url.search : "/";
  event.waitUntil(self.registration.showNotification(message.title, {
    body: message.body,
    icon: "/assets/icon-192.png",
    badge: "/assets/icon-192.png",
    data: { url: target },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = event.notification.data?.url ?? "/";
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const client of windows) {
      if (new URL(client.url).origin === self.location.origin && "focus" in client) {
        await client.navigate(target);
        return client.focus();
      }
    }
    return self.clients.openWindow(target);
  })());
});

self.addEventListener("fetch", event => {
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => new Response(
    '<!doctype html><html lang="ja"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>接続できません</title><p style="font-family:sans-serif;padding:24px">監視するマシンに接続できません。ネットワークを確認してから開き直してください。</p></html>',
    { headers: { "Content-Type": "text/html; charset=utf-8" } },
  )));
});
