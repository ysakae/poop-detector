"use strict";

(() => {
    const interval = 5000;
    const main = document.querySelector("main");
    const {clipRevision, reviewRevision} = main.dataset;
    const endpoint = clipRevision ? "/page-status?clips=1" : "/page-status";
    let timer;
    let checking = false;
    let edited = false;
    let submitting = false;
    let notice;
    let pending;

    function updateCount(count) {
        if (count === pending) return;
        pending = count;
        const remaining = main.querySelector("[data-review-remaining]");
        if (remaining) remaining.textContent = `確認待ち 残り${count}件`;
        for (const link of document.querySelectorAll('.site-menu a[href="/review"], .tab-bar a[href="/review"]')) {
            const badge = link.querySelector(".badge");
            if (!count) {
                badge?.remove();
            } else if (badge) {
                badge.textContent = count;
            } else {
                const newBadge = document.createElement("span");
                newBadge.className = "badge";
                newBadge.textContent = count;
                link.append(newBadge);
            }
        }
        const home = main.querySelector("[data-home-review]");
        if (home) {
            home.querySelector("strong").textContent = count;
            home.querySelector("a").textContent = `確認待ち ${count}件を確認する`;
            home.querySelector("[data-review-waiting]").hidden = !count;
            home.querySelector("[data-review-empty]").hidden = Boolean(count);
        }
    }

    function busy() {
        return edited || submitting || document.activeElement?.closest("form")
            || [...main.querySelectorAll("video")].some((video) => !video.paused && !video.ended);
    }

    function announce() {
        if (notice) return;
        notice = document.createElement("p");
        notice.className = "message message-info";
        notice.setAttribute("role", "status");
        const link = document.createElement("a");
        link.href = location.href;
        link.addEventListener("click", () => { link.href = location.href; });
        link.textContent = "更新";
        notice.append("動画または確認待ちが更新されました。", link, "すると最新の状態を表示します。");
        main.prepend(notice);
    }

    function schedule(delay = interval) {
        if (timer !== undefined || checking || document.hidden) return;
        timer = window.setTimeout(check, delay);
    }

    async function check() {
        timer = undefined;
        if (document.hidden) return;
        checking = true;
        try {
            const response = await fetch(endpoint, {signal: AbortSignal.timeout(interval)});
            if (response.status === 401 || response.status === 403) return;
            if (!response.ok) throw new Error("HTTP " + response.status);
            const state = await response.json();
            if (!document.hidden) {
                updateCount(state.pending);
                const changed = (reviewRevision && reviewRevision !== state.review)
                    || (clipRevision && clipRevision !== state.clips);
                if (changed) {
                    if (busy()) announce();
                    else {
                        location.reload();
                        return;
                    }
                } else if (notice) {
                    notice.remove();
                    notice = undefined;
                }
            }
        } catch (error) {
            console.warn("画面の更新を確認できませんでした。次の回で試します。", error);
        } finally {
            checking = false;
        }
        schedule();
    }

    if (reviewRevision || clipRevision) {
        main.addEventListener("input", (event) => {
            if (event.target.closest("form")) edited = true;
        });
        window.addEventListener("submit", (event) => {
            if (!event.defaultPrevented) submitting = true;
        });
        window.addEventListener("pageshow", () => { submitting = false; });
    }
    document.addEventListener("visibilitychange", () => {
        window.clearTimeout(timer);
        timer = undefined;
        schedule(0);
    });
    schedule();
})();
