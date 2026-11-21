"use strict";

const image = document.querySelector("#live-frame");
const frameStatus = document.querySelector("#live-frame-status");
const judgment = document.querySelector("#live-judgment");
const reason = document.querySelector("#live-reason");
const judgmentTime = document.querySelector("#live-judgment-time");
const clipLink = document.querySelector("#live-clip-link");
const view = document.querySelector("#live-view");
const controls = document.querySelector("#live-controls");
const maximizeButton = document.querySelector("#live-maximize");
const jst = new Intl.DateTimeFormat("ja-JP", {
    dateStyle: "short",
    timeStyle: "medium",
    timeZone: "Asia/Tokyo",
});
const waiting = "監視からの映像を待っています。";
let retryAt = 0;
let startedAt = 0;

// 通信を抑える工夫。外の回線で見るときは、映像を1秒に SAVER_FPS コマまでにする。
// 端末が回線の種類やデータセーバーを教えてくれるとき（Android の Chrome など）は自動で抑え、
// 「通信を節約」のボタンで選び直せる（選んだ状態は端末ごとに覚える）。
const SAVER_FPS = 2;
const SAVER_KEY = "live-saver";
const STALE_SECONDS = 5; // これより古い映像は「何秒前の映像」と示す
const ageBadge = document.querySelector("#live-age");
const saverButton = document.querySelector("#live-saver");

function storedSaver() {
    try {
        const value = localStorage.getItem(SAVER_KEY);
        return value === null ? null : value === "1";
    } catch {
        return null; // 保存を使えない環境（プライベートブラウズなど）では、自動の判断に任せる
    }
}

function automaticSaver() {
    const connection = navigator.connection;
    if (!connection) return false;
    return Boolean(connection.saveData || connection.type === "cellular"
        || ["slow-2g", "2g", "3g"].includes(connection.effectiveType));
}

let saving = storedSaver() ?? automaticSaver();
// 映像が画面から外れている間（下へスクロールしたとき）は、受け取りを止める。
let offscreen = false;

function streamUrl() {
    return saving ? "/live/stream?fps=" + SAVER_FPS : "/live/stream";
}

function showSaver() {
    saverButton.setAttribute("aria-pressed", String(saving));
}

saverButton.addEventListener("click", () => {
    saving = !saving;
    try {
        localStorage.setItem(SAVER_KEY, saving ? "1" : "0");
    } catch {
        // 覚えられなくても、この画面では選んだとおりにする。
    }
    showSaver();
    if (image.hasAttribute("src")) image.src = streamUrl();
});
showSaver();

// 映像の最大化。全画面の API があればそれを使い、ない端末（iPhone の Safari）では
// 画面いっぱいに固定して広げる。
function isMaximized() {
    return view.hasAttribute("data-maximized");
}

// 最大化の間は画面を消さない（Screen Wake Lock。iPhone・iPad は iOS 16.4 以降の Safari、
// ホーム画面に追加した場合は iOS 18.4 以降）。HTTPS か localhost でだけ使え、
// 使えないブラウザーでは何もしない。画面が隠れるとブラウザーが自動で手放す。
let wakeLock = null;

async function keepAwake(awake) {
    if (!("wakeLock" in navigator)) return;
    if (!awake) {
        const held = wakeLock;
        wakeLock = null;
        if (held) {
            await held.release().catch((error) => console.warn("画面の点灯を解除できません", error));
        }
        return;
    }
    if (document.hidden || (wakeLock && !wakeLock.released)) return;
    try {
        wakeLock = await navigator.wakeLock.request("screen");
    } catch (error) {
        // 省電力モードなどで断られても、映像の表示は続ける。
        console.warn("画面を点けたままにできません", error);
    }
}

function showMaximized(maximized) {
    keepAwake(maximized);
    view.toggleAttribute("data-maximized", maximized);
    maximizeButton.setAttribute("aria-pressed", String(maximized));
    // 動画の操作バーと同じく、アイコンだけのボタンの名前で操作を伝える。
    const label = maximized ? "全画面を終了" : "全画面表示";
    maximizeButton.setAttribute("aria-label", label);
    maximizeButton.title = label;
    if (!maximized && image.hidden) controls.hidden = true;
}

function setFallback(maximized) {
    view.classList.toggle("live-maximized", maximized);
    document.body.classList.toggle("live-locked", maximized);
    showMaximized(maximized);
}

// 埋め込みのブラウザーには、全画面の要求に応えないものがある。この時間で全画面に
// ならなければ、画面いっぱいに広げる方法へ切り替える。
const fullscreenWaitMs = 800;

async function maximize() {
    if (view.requestFullscreen && document.fullscreenEnabled) {
        const timeout = new Promise((resolve) => window.setTimeout(resolve, fullscreenWaitMs));
        try {
            await Promise.race([view.requestFullscreen(), timeout]);
        } catch {
            // 全画面を断られたときは、下で画面いっぱいに広げる方法へ切り替える。
        }
        if (document.fullscreenElement === view) return;
    }
    setFallback(true);
}

function restore() {
    if (document.fullscreenElement === view) {
        document.exitFullscreen();
    } else {
        setFallback(false);
    }
}

function toggleMaximized() {
    if (isMaximized()) {
        restore();
    } else {
        maximize();
    }
}

maximizeButton.addEventListener("click", toggleMaximized);
// 動画と同じく、映像のダブルクリックでも全画面を切り替える。
image.addEventListener("dblclick", toggleMaximized);
document.addEventListener("fullscreenchange", () => {
    if (document.fullscreenElement === view) {
        // 遅れて全画面になったときは、画面いっぱいに広げた表示を外して全画面に任せる。
        setFallback(false);
        showMaximized(true);
    } else if (!view.classList.contains("live-maximized")) {
        showMaximized(false);
    }
});
document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && view.classList.contains("live-maximized")) setFallback(false);
});

function stopStream() {
    image.removeAttribute("src");
    image.hidden = true;
    // 最大化の間は、映像が途切れても元に戻せるようにボタンを残す。
    controls.hidden = !isMaximized();
}

function startStream() {
    if (document.hidden || offscreen || image.hasAttribute("src") || Date.now() < retryAt) return;
    startedAt = Date.now();
    image.src = streamUrl();
}

function showAge(stamp) {
    const age = (Date.now() - new Date(stamp).getTime()) / 1000;
    const stale = age >= STALE_SECONDS;
    ageBadge.textContent = stale ? Math.round(age) + "秒前の映像" : "ライブ";
    ageBadge.toggleAttribute("data-stale", stale);
}

if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
        offscreen = !entry.isIntersecting && !isMaximized();
        if (offscreen) {
            // 映像の枠は隠さずに受け取りだけを止める（枠が縮むと、スクロールの位置がずれる）。
            image.removeAttribute("src");
        } else {
            startStream();
        }
    }).observe(view);
}

image.addEventListener("error", () => {
    if (!image.hasAttribute("src")) return;
    stopStream();
    retryAt = Date.now() + 2000;
    frameStatus.textContent = waiting;
});
document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
        stopStream();
    } else {
        startStream();
        // タブやアプリを切り替えて戻ったときは、手放された Wake Lock を取り直す。
        if (isMaximized()) keepAwake(true);
    }
});
window.addEventListener("pagehide", stopStream);
window.addEventListener("pageshow", startStream);

async function updateStreamStatus() {
    try {
        if (document.hidden || offscreen) return;
        const response = await fetch("/live/status", { cache: "no-store" });
        if (!response.ok) throw new Error();
        const { frame_time: stamp, tray_state: trayState } = await response.json();
        if (document.hidden) return;
        if (!stamp) {
            if (!image.hidden || Date.now() - startedAt >= 4000) stopStream();
            frameStatus.textContent = waiting;
        }
        startStream();
        if (!stamp || !image.hasAttribute("src")) return;
        image.hidden = false;
        controls.hidden = false;
        showAge(stamp);
        const status = "映像更新 " + jst.format(new Date(stamp));
        if (trayState === "searching") {
            frameStatus.textContent = status + " ／ トレーの位置を確認中・判定保留";
        } else if (trayState === "tracking") {
            frameStatus.textContent = status + " ／ トレーを追従中";
        } else {
            frameStatus.textContent = status;
        }
    } catch {
        stopStream();
        frameStatus.textContent = "現在の映像を取得できません。監視の起動とカメラ接続を確認してください。";
    } finally {
        window.setTimeout(updateStreamStatus, 1000);
    }
}

async function updateJudgment() {
    try {
        if (document.hidden) return;
        const response = await fetch("/live/assessment");
        if (!response.ok) throw new Error();
        const data = await response.json();
        judgment.textContent = data
            ? data.label + (data.location ? "（" + data.location + "）" : "")
            : "判定はまだありません";
        reason.textContent = data ? data.reason : "";
        judgmentTime.textContent = data
            ? "動画の保存要求 " + jst.format(new Date(data.requested_at))
                + " ／ 判定 " + jst.format(new Date(data.assessed_at))
                + (data.human_label ? " ／ 手動ラベル " + data.human_label : "")
                + (data.human_location ? "（" + data.human_location + "）" : "")
            : "";
        clipLink.hidden = !data;
        if (data) clipLink.href = "/clips?id=" + encodeURIComponent(data.clip_id);
    } catch {
        judgment.textContent = "判定結果を取得できません";
        reason.textContent = "";
        judgmentTime.textContent = "";
        clipLink.hidden = true;
    } finally {
        window.setTimeout(updateJudgment, 2000);
    }
}

startStream();
updateStreamStatus();
updateJudgment();
