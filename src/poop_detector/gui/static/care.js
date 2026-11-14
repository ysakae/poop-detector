"use strict";

const historyField = document.querySelector("#feeding-history");
if (historyField) {
    const select = document.querySelector("#feeding-history-select");
    const food = document.querySelector("#feeding-food");
    historyField.hidden = false;
    select.addEventListener("change", () => {
        if (select.value) food.value = select.value;
        select.selectedIndex = 0;
    });
}
