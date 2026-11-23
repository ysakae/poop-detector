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

const defaultNote = document.querySelector("#feeding-default-note");
if (defaultNote) {
    const mealType = document.querySelector("#feeding-meal-type");
    mealType.addEventListener("change", () => {
        if (mealType.value !== "treat" || defaultNote.hidden) return;
        for (const field of ["#feeding-food", "#feeding-grams"]) {
            const input = document.querySelector(field);
            if (input.value === input.defaultValue) input.value = "";
        }
        defaultNote.hidden = true;
    });
}
