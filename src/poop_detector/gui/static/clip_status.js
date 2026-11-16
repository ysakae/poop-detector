"use strict";

// 録画・変換中の動画の状態を数秒ごとに確かめ、変わったら画面を更新する（Issue #187）。
// 変換中の動画は、一覧の行と選んだ動画の欄に data-pending-clip（ID）と data-clip-status（状態）を持つ。
// 再生中の動画や入力途中のフォームがあるときは読み直さず、「更新」を促す知らせだけを出す。
const INTERVAL = 5000;
let timer = null;
let checking = false;
let edited = false;
let announced = false;

function pendingClips() {
    // 一覧のクリックで選んだ動画の欄が差し替わるので、毎回 DOM から読み直す。
    const pending = new Map();
    for (const element of document.querySelectorAll("[data-pending-clip]")) {
        pending.set(element.dataset.pendingClip, element.dataset.clipStatus);
    }
    return pending;
}

function busy() {
    if (edited) return true;
    return [...document.querySelectorAll("video")].some((video) => !video.paused && !video.ended);
}

function announce() {
    announced = true;
    const message = document.createElement("p");
    message.className = "message message-info";
    message.setAttribute("role", "status");
    const link = document.createElement("a");
    link.href = location.href;
    link.textContent = "更新";
    message.append("録画・変換が終わった動画があります。", link, "すると表示が変わります。");
    const anchor = document.querySelector("#clip-pending") || document.querySelector(".workspace");
    anchor?.before(message);
}

function schedule() {
    if (timer !== null || checking || announced || document.hidden) return;
    timer = window.setTimeout(check, INTERVAL);
}

async function check() {
    timer = null;
    // タブが裏にあるときは止め、表に戻ったら visibilitychange で再開する。
    if (document.hidden) return;
    const pending = pendingClips();
    if (!pending.size) return;
    checking = true;
    try {
        const query = new URLSearchParams([...pending.keys()].map((id) => ["id", id]));
        const response = await fetch("/clips/status?" + query);
        if (!response.ok) throw new Error("HTTP " + response.status);
        const {clips} = await response.json();
        const changed = clips.some((clip) => pending.has(clip.id) && pending.get(clip.id) !== clip.status);
        if (changed) {
            if (busy()) announce();
            else location.reload();
            return;
        }
    } catch (error) {
        console.warn("動画の状態を確認できませんでした。次の回で試します。", error);
    } finally {
        checking = false;
    }
    schedule();
}

document.addEventListener("input", (event) => {
    if (event.target.closest("form")) edited = true;
});
document.addEventListener("visibilitychange", () => {
    if (!document.hidden && pendingClips().size) schedule();
});
if (pendingClips().size) schedule();
