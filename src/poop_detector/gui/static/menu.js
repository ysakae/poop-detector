"use strict";

// メニューの操作を助ける。このスクリプトが動かなくても、「その他」の開閉とリンクはそのまま使える。

// スマートフォンの幅の下のタブの「その他」。外側を押す・Esc・項目を選ぶ、のどれでも閉じる。
const more = document.querySelector(".tab-more");
if (more) {
    const close = () => {
        more.open = false;
    };
    document.addEventListener("click", (event) => {
        if (!more.open) return;
        // 「その他」自体を押したときは、ブラウザーの開閉に任せる。
        if (event.target.closest(".tab-more > summary")) return;
        close();
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !more.open) return;
        close();
        more.querySelector("summary").focus();
    });
}

// 管理のメニュー。横に入りきらないとき、開いている項目が見える位置まで送る。
const sub = document.querySelector(".sub-menu");
const current = sub && sub.querySelector('a[aria-current="page"]');
if (current) {
    const view = sub.getBoundingClientRect();
    const item = current.getBoundingClientRect();
    sub.scrollLeft += item.left - view.left - (view.width - item.width) / 2;
}
