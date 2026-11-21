// 押したことがすぐ分かるようにする。次の画面が届くまで数秒かかる操作（判定器とモデルの画面、
// CLI への問い合わせなど）でも、押したボタンに「処理中」を出し、画面の上端に読み込み中の帯を出す。
// 表示だけで、送信や移動の中身は変えない。
const BAR_DELAY = 150; // これより早く次の画面が届くときは、帯を出さない（ちらつかせない）
const RESET_AFTER = 20000; // 画面が変わらないまま、この時間が過ぎたら表示を戻す

const bar = document.createElement("div");
bar.className = "page-busy";
bar.hidden = true;
bar.setAttribute("role", "progressbar");
bar.setAttribute("aria-label", "読み込み中");
document.body.append(bar);

let barTimer = 0;
let resetTimer = 0;
const busyButtons = [];

function showBar() {
    clearTimeout(barTimer);
    barTimer = setTimeout(() => { bar.hidden = false; }, BAR_DELAY);
    clearTimeout(resetTimer);
    resetTimer = setTimeout(reset, RESET_AFTER);
}

function reset() {
    clearTimeout(barTimer);
    clearTimeout(resetTimer);
    bar.hidden = true;
    for (const { button, label } of busyButtons.splice(0)) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        if (label !== null) button.textContent = label;
    }
}

function markBusy(form, submitter) {
    const buttons = form.querySelectorAll("button:not([type=button]), input[type=submit]");
    // 送信の値（押したボタンの name と value）が消えないよう、止めるのは送信を始めたあと。
    setTimeout(() => {
        for (const button of buttons) {
            const busyLabel = button === submitter ? button.dataset.busyLabel : undefined;
            busyButtons.push({ button, label: busyLabel ? button.textContent : null });
            if (busyLabel) button.textContent = busyLabel;
            if (button === submitter) button.setAttribute("aria-busy", "true");
            button.disabled = true; // 二重の送信を防ぐ
        }
    }, 0);
}

// 送信は、ほかのスクリプトのあと（window まで届いたとき）に見る。
window.addEventListener("submit", (event) => {
    const form = event.target;
    if (event.defaultPrevented || !(form instanceof HTMLFormElement)) return;
    // ダウンロードや別のタブへの送信では、この画面は変わらない。
    if (form.hasAttribute("data-no-busy") || (form.target && form.target !== "_self")) return;
    markBusy(form, event.submitter);
    showBar();
});

window.addEventListener("click", (event) => {
    const link = event.target instanceof Element ? event.target.closest("a[href]") : null;
    if (!link || event.defaultPrevented || event.button !== 0
        || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    if (link.hasAttribute("download") || link.hasAttribute("data-no-busy")
        || (link.target && link.target !== "_self")) return;
    const url = new URL(link.href, location.href);
    // 別のサイトと、同じ画面の中の移動（#…）は除く。
    if (url.origin !== location.origin) return;
    if (url.pathname === location.pathname && url.search === location.search && url.hash) return;
    showBar();
});

// 戻るボタンで画面が戻ったとき（前の表示のまま復元される）は、表示を消す。
window.addEventListener("pageshow", reset);
