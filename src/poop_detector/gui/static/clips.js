"use strict";

const clipList = document.querySelector("#clip-list");
let selection;
history.scrollRestoration = "manual";
// 選んだ再生の速さ。一覧から別の動画を開いても引き継ぐ（画面を開き直すと戻る）。
let playbackRate = 1;

function applyRate(detail) {
    const video = detail?.querySelector("video");
    const button = detail?.querySelector("button[data-video-rate]");
    if (!video || !button) return;
    const fast = playbackRate === Number(button.dataset.videoRate);
    video.defaultPlaybackRate = playbackRate;
    video.playbackRate = playbackRate;
    button.setAttribute("aria-pressed", String(fast));
}

async function showClip(url, push) {
    if (selection) selection.abort();
    const request = new AbortController();
    selection = request;
    try {
        const response = await fetch(url, {signal: request.signal});
        if (!response.ok) throw new Error();
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const detail = page.querySelector("#clip-detail");
        const active = page.querySelector("#clip-list a.active");
        if (!detail) throw new Error();
        if (request.signal.aborted) return;

        document.querySelector("#clip-detail").replaceWith(detail);
        applyRate(detail);
        const activePath = active?.getAttribute("href");
        for (const link of clipList.querySelectorAll("a")) {
            link.classList.toggle("active", link.getAttribute("href") === activePath);
        }
        if (push && url !== location.href) history.pushState(null, "", url);
    } catch {
        if (request.signal.aborted) return;
        if (push) location.assign(url);
        else location.reload();
    }
}

clipList?.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link || event.defaultPrevented || event.button !== 0
        || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showClip(link.href, true);
});

window.addEventListener("popstate", () => showClip(location.href, false));

document.addEventListener("click", (event) => {
    const button = event.target.closest(
        "#clip-detail button[data-video-offset], #clip-detail button[data-video-seek],"
        + " #clip-detail button[data-video-jump], #clip-detail button[data-video-rate]"
    );
    if (!button) return;
    const detail = button.closest("#clip-detail");
    const video = detail.querySelector("video");
    if (button.dataset.videoRate !== undefined) {
        const rate = Number(button.dataset.videoRate);
        playbackRate = playbackRate === rate ? 1 : rate;
        applyRate(detail);
    } else if (button.dataset.videoJump !== undefined) {
        const end = Number.isFinite(video.duration) ? video.duration : Infinity;
        video.currentTime = Math.min(Math.max(video.currentTime + Number(button.dataset.videoJump), 0), end);
    } else if (button.dataset.videoSeek !== undefined) {
        video.currentTime = Number(button.dataset.videoSeek);
    } else if (video.readyState) {
        const input = detail.querySelector(`input[name="${button.dataset.videoOffset}"]`);
        const limit = Math.floor(Number(input.max) * 100 + 1e-7) / 100;
        input.value = Math.min(video.currentTime, limit).toFixed(2);
        input.dispatchEvent(new Event("input", {bubbles: true}));
    }
});

document.addEventListener("change", (event) => {
    const field = event.target.closest('#clip-detail select[name="location"]');
    if (!field) return;
    const destination = field.form.querySelector('select[name="record_id"]');
    if (!destination) return;
    for (const option of destination.options) {
        const location = option.dataset.recordLocation;
        option.disabled = Boolean(
            (location && field.value && location !== field.value)
            || (option.dataset.trayEvent === "1" && field.value !== "outside")
        );
        if (option.selected && option.disabled) destination.value = "";
    }
});

document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!form.matches("#clip-detail form.quick-confirm") || event.submitter?.value === "none") return;

    // 詳細入力と共通の時刻欄を確認し、押されたボタンの送信データに加える。
    const labelForm = form.closest("#clip-detail").querySelector("#clip-label-form");
    const offsets = ["start", "end"].map((name) => labelForm.elements.namedItem(name));
    if (!offsets.every((offset) => offset.reportValidity())) {
        event.preventDefault();
        return;
    }
    form.addEventListener("formdata", ({formData}) => {
        for (const offset of offsets) formData.set(offset.name, offset.value);
    }, {once: true});
});
