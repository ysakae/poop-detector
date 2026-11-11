"use strict";

const form = document.querySelector("#cleanup-form");
const all = document.querySelector("#cleanup-all");
const summary = document.querySelector("#cleanup-selection");
const candidates = form ? [...form.querySelectorAll('input[name="id"]')] : [];

form?.addEventListener("change", (event) => {
    if (event.target === all) {
        for (const candidate of candidates) candidate.checked = all.checked;
    }
    const selected = candidates.filter((candidate) => candidate.checked);
    const bytes = selected.reduce((sum, candidate) => sum + Number(candidate.dataset.bytes), 0);
    all.checked = selected.length === candidates.length;
    all.indeterminate = selected.length > 0 && !all.checked;
    summary.textContent = "選択済み " + selected.length + "本 ／ 空く容量の目安 "
        + (bytes / 1024 ** 2).toFixed(2) + " MiB";
});

const capacity = document.querySelector("#recording-capacity");
async function refreshCapacity() {
    try {
        if (!document.hidden) {
            const response = await fetch("/clips/capacity");
            if (response.ok) {
                const html = await response.text();
                if (capacity.innerHTML !== html) capacity.innerHTML = html;
            }
        }
    } catch {
        // 直前の表示を維持し、次回の更新を待つ。
    } finally {
        window.setTimeout(refreshCapacity, 30000);
    }
}
if (capacity) window.setTimeout(refreshCapacity, 30000);
