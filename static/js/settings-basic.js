(function () {
    function setNextDuesVisibility(form) {
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        var endYearSelect = form.querySelector("[data-end-year-select]");
        var nextDuesField = form.querySelector("[data-next-dues-field]");
        if (!(endYearSelect instanceof HTMLSelectElement) || !(nextDuesField instanceof HTMLElement)) {
            return;
        }

        var hasEndYear = Boolean((endYearSelect.value || "").trim());
        var nextDuesInput = form.querySelector("[data-next-dues-input]");
        if (hasEndYear) {
            nextDuesField.classList.remove("is-hidden");
            if (nextDuesInput instanceof HTMLInputElement) {
                nextDuesInput.disabled = false;
            }
            return;
        }

        nextDuesField.classList.add("is-hidden");
        if (nextDuesInput instanceof HTMLInputElement) {
            nextDuesInput.value = "";
            nextDuesInput.disabled = true;
        }
    }

    function wireSettingsBasicForm(root) {
        var scope = root instanceof HTMLElement ? root : document;
        var forms = Array.from(scope.querySelectorAll("form"));
        if (scope instanceof HTMLFormElement) {
            forms.unshift(scope);
        }
        forms.forEach(function (form) {
            if (!(form instanceof HTMLFormElement)) {
                return;
            }
            var endYearSelect = form.querySelector("[data-end-year-select]");
            if (!(endYearSelect instanceof HTMLSelectElement)) {
                return;
            }
            if (endYearSelect.dataset.nextDuesWired === "1") {
                setNextDuesVisibility(form);
                return;
            }
            endYearSelect.addEventListener("change", function () {
                setNextDuesVisibility(form);
            });
            endYearSelect.dataset.nextDuesWired = "1";
            setNextDuesVisibility(form);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        wireSettingsBasicForm(document.body);
    });
})();
