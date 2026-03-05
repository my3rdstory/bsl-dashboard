(function () {
    function normalizeText(value) {
        return (value || "").toString().toLowerCase().trim();
    }

    function getRows(container) {
        return Array.from(container.querySelectorAll("tbody tr[data-search-row]"));
    }

    function ensureRowOrder(container) {
        var rows = getRows(container);
        rows.forEach(function (row, index) {
            if (!row.dataset.searchOrder) {
                row.dataset.searchOrder = String(index);
            }
        });
    }

    function getSearchKey(row) {
        return normalizeText(row.getAttribute("data-search-key") || row.textContent || "");
    }

    function getSearchOrder(row) {
        var raw = row.dataset.searchOrder || "0";
        var parsed = Number.parseInt(raw, 10);
        return Number.isNaN(parsed) ? 0 : parsed;
    }

    function getEmptyRow(container) {
        return container.querySelector("tbody tr[data-search-empty-row]");
    }

    function sortAndFilter(container) {
        var input = container.querySelector("[data-search-input]");
        var tbody = container.querySelector("tbody[data-search-body]");
        if (!(input instanceof HTMLInputElement) || !(tbody instanceof HTMLElement)) {
            return;
        }

        var query = normalizeText(input.value);
        var rows = getRows(container);
        ensureRowOrder(container);
        var ranked = [];

        if (!query) {
            rows
                .slice()
                .sort(function (left, right) {
                    return getSearchOrder(left) - getSearchOrder(right);
                })
                .forEach(function (row) {
                    row.hidden = false;
                    tbody.appendChild(row);
                });

            var defaultEmptyRow = getEmptyRow(container);
            if (defaultEmptyRow instanceof HTMLElement) {
                defaultEmptyRow.hidden = rows.length !== 0;
            }
            return;
        }

        rows.forEach(function (row) {
            var key = getSearchKey(row);
            var index = key.indexOf(query);
            if (index === -1) {
                row.hidden = true;
                return;
            }

            row.hidden = false;
            ranked.push({ row: row, rank: index, key: key, order: getSearchOrder(row) });
        });

        ranked.sort(function (left, right) {
            if (left.rank !== right.rank) {
                return left.rank - right.rank;
            }
            var keyCompare = left.key.localeCompare(right.key, "ko");
            if (keyCompare !== 0) {
                return keyCompare;
            }
            return left.order - right.order;
        });

        ranked.forEach(function (item) {
            tbody.appendChild(item.row);
        });

        var emptyRow = getEmptyRow(container);
        if (emptyRow instanceof HTMLElement) {
            emptyRow.hidden = ranked.length !== 0;
        }
    }

    function wireTable(container) {
        if (!(container instanceof HTMLElement)) {
            return;
        }
        if (container.dataset.searchableReady === "1") {
            sortAndFilter(container);
            return;
        }

        var input = container.querySelector("[data-search-input]");
        if (!(input instanceof HTMLInputElement)) {
            return;
        }

        input.addEventListener("input", function () {
            sortAndFilter(container);
        });

        ensureRowOrder(container);
        container.dataset.searchableReady = "1";
        sortAndFilter(container);
    }

    function initAll(root) {
        var scope = root instanceof HTMLElement ? root : document;
        var tables = Array.from(scope.querySelectorAll("[data-searchable-table]"));
        if (scope instanceof HTMLElement && scope.matches("[data-searchable-table]")) {
            tables.unshift(scope);
        }
        tables.forEach(wireTable);
    }

    function wireDateTimePicker(input) {
        if (!(input instanceof HTMLInputElement)) {
            return;
        }
        if (input.dataset.datetimePickerReady === "1") {
            return;
        }
        if (typeof window.flatpickr !== "function") {
            return;
        }

        window.flatpickr(input, {
            enableTime: true,
            enableSeconds: true,
            time_24hr: true,
            dateFormat: "Y-m-d\\TH:i:S",
            allowInput: true,
        });
        input.dataset.datetimePickerReady = "1";
    }

    function initDateTimePickers(root) {
        var scope = root instanceof HTMLElement ? root : document;
        var inputs = Array.from(scope.querySelectorAll("input.first-check-input[name='first_reviewed_at']"));
        if (scope instanceof HTMLInputElement && scope.matches("input.first-check-input[name='first_reviewed_at']")) {
            inputs.unshift(scope);
        }
        inputs.forEach(wireDateTimePicker);
    }

    document.addEventListener("DOMContentLoaded", function () {
        initAll(document.body);
        initDateTimePickers(document.body);
    });

    document.body.addEventListener("htmx:afterSwap", function (event) {
        var target = event.target;
        if (!(target instanceof HTMLElement)) {
            return;
        }
        initAll(target);
        initDateTimePickers(target);
        var parentTable = target.closest("[data-searchable-table]");
        if (parentTable instanceof HTMLElement) {
            wireTable(parentTable);
        }
    });

    document.body.addEventListener("searchable-table-refresh", function () {
        initAll(document.body);
        initDateTimePickers(document.body);
    });

    document.body.addEventListener("click", function (event) {
        var target = event.target;
        if (!(target instanceof Element)) {
            return;
        }
        var trigger = target.closest("[data-open-datetime-picker]");
        if (!(trigger instanceof HTMLElement)) {
            return;
        }

        var control = trigger.closest(".first-check-control");
        if (!(control instanceof HTMLElement)) {
            return;
        }

        var input = control.querySelector("input.first-check-input[name='first_reviewed_at']");
        if (!(input instanceof HTMLInputElement)) {
            return;
        }

        if (input._flatpickr && typeof input._flatpickr.open === "function") {
            input._flatpickr.open();
            return;
        }

        if (typeof input.showPicker === "function") {
            input.showPicker();
            return;
        }

        input.focus();
        input.click();
    });
})();
