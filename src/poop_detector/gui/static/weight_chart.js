"use strict";

// タップでも詳細を表示し、Escape でフォーカスを保ったまま閉じる。
for (const chart of document.querySelectorAll("[data-weight-chart]")) {
    chart.addEventListener("click", (event) => {
        const point = event.target.closest(".weight-data-point");
        if (point) {
            point.removeAttribute("data-dismissed");
            point.focus();
        }
    });
    chart.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && event.target.matches(".weight-data-point")) {
            event.target.setAttribute("data-dismissed", "");
        }
    });
    chart.addEventListener("focusout", (event) => {
        event.target.removeAttribute("data-dismissed");
    });
}
