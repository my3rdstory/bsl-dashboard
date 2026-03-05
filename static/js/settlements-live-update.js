(function () {
    function getRowsBody() {
        var el = document.getElementById("activity-settlements-body");
        if (!(el instanceof HTMLElement)) {
            return null;
        }
        return el;
    }

    function refreshSettlementRows() {
        var rowsBody = getRowsBody();
        if (!(rowsBody instanceof HTMLElement)) {
            return;
        }
        if (rowsBody.dataset.refreshing === "1") {
            return;
        }

        var rowsUrl = String(rowsBody.dataset.rowsUrl || "").trim();
        if (!rowsUrl) {
            return;
        }

        rowsBody.dataset.refreshing = "1";

        if (window.htmx && typeof window.htmx.ajax === "function") {
            window.htmx.ajax("GET", rowsUrl, {
                target: "#activity-settlements-body",
                swap: "innerHTML"
            });
            window.setTimeout(function () {
                var currentBody = getRowsBody();
                if (currentBody instanceof HTMLElement) {
                    currentBody.dataset.refreshing = "0";
                }
            }, 400);
            return;
        }

        fetch(rowsUrl, {
            method: "GET",
            headers: { "X-Requested-With": "XMLHttpRequest" }
        })
            .then(function (response) {
                return response.text();
            })
            .then(function (html) {
                var currentBody = getRowsBody();
                if (!(currentBody instanceof HTMLElement)) {
                    return;
                }
                currentBody.innerHTML = html;
                document.body.dispatchEvent(new Event("searchable-table-refresh"));
            })
            .finally(function () {
                var currentBody = getRowsBody();
                if (currentBody instanceof HTMLElement) {
                    currentBody.dataset.refreshing = "0";
                }
            });
    }

    function pollSettlementState() {
        var rowsBody = getRowsBody();
        if (!(rowsBody instanceof HTMLElement)) {
            return;
        }
        if (document.hidden) {
            return;
        }
        if (rowsBody.dataset.statePolling === "1") {
            return;
        }
        var stateUrl = String(rowsBody.dataset.stateUrl || "").trim();
        if (!stateUrl) {
            return;
        }

        rowsBody.dataset.statePolling = "1";
        fetch(stateUrl, {
            method: "GET",
            headers: { "X-Requested-With": "XMLHttpRequest" }
        })
            .then(function (response) {
                return response.json();
            })
            .then(function (data) {
                var currentBody = getRowsBody();
                if (!(currentBody instanceof HTMLElement)) {
                    return;
                }
                var nextRevision = String((data && data.revision) || "");
                if (!nextRevision) {
                    return;
                }
                var currentRevision = String(currentBody.dataset.stateRevision || "");
                if (!currentRevision) {
                    currentBody.dataset.stateRevision = nextRevision;
                    return;
                }
                if (currentRevision === nextRevision) {
                    return;
                }
                currentBody.dataset.stateRevision = nextRevision;
                refreshSettlementRows();
            })
            .finally(function () {
                var currentBody = getRowsBody();
                if (currentBody instanceof HTMLElement) {
                    currentBody.dataset.statePolling = "0";
                }
            });
    }

    function initLiveUpdate() {
        if (!getRowsBody()) {
            return;
        }

        document.addEventListener("bsl:settlements-updated", function () {
            refreshSettlementRows();
        });

        window.addEventListener("storage", function (event) {
            if (event.key !== "bsl:settlements-updated-at") {
                return;
            }
            refreshSettlementRows();
        });

        if ("BroadcastChannel" in window) {
            var channel = new BroadcastChannel("bsl-settlements");
            channel.addEventListener("message", function (event) {
                var data = event && event.data ? event.data : {};
                if (data.type !== "updated") {
                    return;
                }
                refreshSettlementRows();
            });
        }

        window.setInterval(pollSettlementState, 5000);
    }

    document.addEventListener("DOMContentLoaded", initLiveUpdate);
})();
