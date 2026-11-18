"use strict";

const image = document.querySelector("#live-frame");
const frameStatus = document.querySelector("#live-frame-status");
const judgment = document.querySelector("#live-judgment");
const reason = document.querySelector("#live-reason");
const judgmentTime = document.querySelector("#live-judgment-time");
const clipLink = document.querySelector("#live-clip-link");
const view = document.querySelector("#live-view");
const maximizeButton = document.querySelector("#live-maximize");
const jst = new Intl.DateTimeFormat("ja-JP", {
    dateStyle: "short",
    timeStyle: "medium",
    timeZone: "Asia/Tokyo",
});
const waiting = "監視からの映像を待っています。";
let retryAt = 0;
let startedAt = 0;

// 映像の最大化。全画面の API があればそれを使い、ない端末（iPhone の Safari）では
// 画面いっぱいに固定して広げる。
function isMaximized() {
    return view.hasAttribute("data-maximized");
}

function showMaximized(maximized) {
    view.toggleAttribute("data-maximized", maximized);
    maximizeButton.setAttribute("aria-pressed", String(maximized));
    if (!maximized && image.hidden) maximizeButton.hidden = true;
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

maximizeButton.addEventListener("click", () => {
    if (isMaximized()) {
        restore();
    } else {
        maximize();
    }
});
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
    maximizeButton.hidden = !isMaximized();
}

function startStream() {
    if (document.hidden || image.hasAttribute("src") || Date.now() < retryAt) return;
    startedAt = Date.now();
    image.src = "/live/stream";
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
    }
});
window.addEventListener("pagehide", stopStream);
window.addEventListener("pageshow", startStream);

async function updateStreamStatus() {
    try {
        if (document.hidden) return;
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
        maximizeButton.hidden = false;
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
