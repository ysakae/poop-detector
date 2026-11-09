"use strict";

const clipList = document.querySelector("#clip-list");
let selection;
history.scrollRestoration = "manual";

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

clipList.addEventListener("click", (event) => {
    const link = event.target.closest("a");
    if (!link || event.defaultPrevented || event.button !== 0
        || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    showClip(link.href, true);
});

window.addEventListener("popstate", () => showClip(location.href, false));
