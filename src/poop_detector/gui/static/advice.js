"use strict";

// 回答待ちの相談の状態を数秒ごとに確かめ、届いたら画面を読み直す。
// 動かないときは、画面の「読み直す」のリンクで確かめられる。
const INTERVAL = 4000;
const element = document.querySelector("[data-advice-status]");

async function check() {
    if (document.hidden) return;
    try {
        const query = new URLSearchParams({thread: element.dataset.adviceStatus});
        const response = await fetch("/advice/status?" + query);
        if (!response.ok) throw new Error("HTTP " + response.status);
        const {state} = await response.json();
        if (state !== "queued" && state !== "running") {
            location.reload();
            return;
        }
    } catch (error) {
        console.warn("回答の状態を確認できませんでした。次の回で試します。", error);
    }
    window.setTimeout(check, INTERVAL);
}

if (element) window.setTimeout(check, INTERVAL);
document.addEventListener("visibilitychange", () => {
    if (element && !document.hidden) window.setTimeout(check, INTERVAL);
});
