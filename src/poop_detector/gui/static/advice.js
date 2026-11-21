"use strict";

// 回答待ちの相談の状態を数秒ごとに確かめ、届いたら画面を読み直す。
// 動かないときは、画面の「読み直す」のリンクで確かめられる。
const INTERVAL = 4000;
let timer = null;
const element = document.querySelector("[data-advice-status]");

function schedule() {
    if (timer === null) timer = window.setTimeout(check, INTERVAL);
}

async function check() {
    timer = null;
    if (document.hidden) return;
    try {
        const query = new URLSearchParams({thread: element.dataset.adviceStatus});
        const response = await fetch("/advice/status?" + query);
        // 会話が消えたなどで 404 のときは、続けても変わらないので止める。
        if (response.status === 404) return;
        if (!response.ok) throw new Error("HTTP " + response.status);
        const {state} = await response.json();
        if (state !== "queued" && state !== "running") {
            location.reload();
            return;
        }
    } catch (error) {
        console.warn("回答の状態を確認できませんでした。次の回で試します。", error);
    }
    schedule();
}

if (element) schedule();
document.addEventListener("visibilitychange", () => {
    if (element && !document.hidden) schedule();
});
