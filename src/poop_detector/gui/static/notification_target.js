"use strict";

// 通知を開いたときの行き先へ移る。
// iOS のホーム画面のアプリは、通知を開いても最初の画面（start_url）を出したり、
// Service Worker からの画面の切り替えを受け付けなかったりする。
// Service Worker（sw.js）が行き先を預けておき、開いた画面のこのスクリプトが受け取って移る。

const STORE = "notification-target";
const KEY = "/notification-target";
// 通知を開いてから画面が出るまでの猶予。これより古い行き先は、開き直しただけとみなして捨てる。
const FRESH_MS = 60000;

/** 自分のサイトの中の画面を指すパスか（別のサイトへは移らない）。 */
const local = (url) => typeof url === "string" && url.startsWith("/") && !url.startsWith("//");

const go = (url) => {
    if (!local(url)) return;
    if (url !== location.pathname + location.search) location.assign(url);
};

const openTarget = async () => {
    if (!("caches" in window)) return;
    try {
        const store = await caches.open(STORE);
        const saved = await store.match(KEY);
        if (!saved) return;
        // 先に消し、戻る操作や開き直しで同じ行き先へ何度も移らないようにする。
        await store.delete(KEY);
        const target = await saved.json();
        if (Date.now() - Number(target.at) <= FRESH_MS) go(target.url);
    } catch (error) {
        // 預けた行き先を読めないときは、いまの画面のままにする（通知の画面は確認待ちからも開ける）。
        console.warn("通知の行き先を読めませんでした", error);
    }
};

openTarget();
// アプリを裏から戻したときは、画面を読み込み直さずに表に出る。
document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") openTarget();
});
window.addEventListener("pageshow", openTarget);
if ("serviceWorker" in navigator) {
    navigator.serviceWorker.addEventListener("message", (event) => {
        if (event.data && event.data.type === "notification-target") openTarget();
    });
}
