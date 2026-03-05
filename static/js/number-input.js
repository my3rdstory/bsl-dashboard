(function () {
    function sanitizeNumberInput(input) {
        if (!(input instanceof HTMLInputElement)) {
            return;
        }
        if (!input.classList.contains("number-spin-input")) {
            return;
        }

        var raw = String(input.value || "");
        var digitsOnly = raw.replace(/[^\d]/g, "");
        if (digitsOnly !== raw) {
            input.value = digitsOnly;
        }
    }

    function clampMinValue(input) {
        if (!(input instanceof HTMLInputElement)) {
            return;
        }
        if (!input.classList.contains("number-spin-input")) {
            return;
        }
        if (!input.value) {
            return;
        }

        var minAttr = input.getAttribute("min");
        if (!minAttr) {
            return;
        }
        var minValue = parseInt(minAttr, 10);
        var value = parseInt(input.value, 10);
        if (Number.isNaN(minValue) || Number.isNaN(value)) {
            return;
        }
        if (value < minValue) {
            input.value = String(minValue);
        }
    }

    document.addEventListener("input", function (event) {
        sanitizeNumberInput(event.target);
    });

    document.addEventListener(
        "blur",
        function (event) {
            sanitizeNumberInput(event.target);
            clampMinValue(event.target);
        },
        true
    );
})();
