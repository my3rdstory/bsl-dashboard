(function () {
    function ensureStack() {
        var stack = document.querySelector("[data-app-toast-stack]");
        if (stack instanceof HTMLElement) {
            return stack;
        }
        var created = document.createElement("div");
        created.className = "app-toast-stack";
        created.setAttribute("data-app-toast-stack", "");
        document.body.appendChild(created);
        return created;
    }

    function normalizeLevel(level) {
        var raw = String(level || "").toLowerCase();
        if (raw === "error" || raw === "danger") {
            return "is-danger";
        }
        if (raw === "warning" || raw === "warn") {
            return "is-warning";
        }
        if (raw === "success") {
            return "is-success";
        }
        return "is-info";
    }

    function addToast(message, level, duration) {
        var text = String(message || "").trim();
        if (!text) {
            return;
        }
        var stack = ensureStack();
        var toast = document.createElement("div");
        toast.className = "app-toast " + normalizeLevel(level);
        toast.setAttribute("data-app-toast", "");
        toast.setAttribute("data-toast-duration", String(duration || 3000));
        toast.textContent = text;
        stack.appendChild(toast);
        scheduleDismiss(toast);
    }

    function scheduleDismiss(toast) {
        if (!(toast instanceof HTMLElement)) {
            return;
        }
        var isPersistent = String(toast.getAttribute("data-toast-persistent") || "")
            .toLowerCase()
            .trim();
        if (isPersistent === "1" || isPersistent === "true" || isPersistent === "yes") {
            return;
        }
        if (toast.dataset.toastReady === "1") {
            return;
        }
        var rawDuration = toast.getAttribute("data-toast-duration") || "3000";
        var duration = Number.parseInt(rawDuration, 10);
        var delay = Number.isNaN(duration) ? 3000 : duration;
        if (delay <= 0) {
            return;
        }

        toast.dataset.toastReady = "1";
        window.setTimeout(function () {
            toast.classList.add("is-hide");
            window.setTimeout(function () {
                toast.remove();
            }, 260);
        }, delay);
    }

    function applyToastLevel(toast) {
        if (!(toast instanceof HTMLElement)) {
            return;
        }
        var level = String(toast.getAttribute("data-toast-level") || "").trim();
        if (!level) {
            return;
        }
        if (!toast.classList.contains("is-success") && !toast.classList.contains("is-warning") && !toast.classList.contains("is-danger")) {
            toast.classList.add(normalizeLevel(level));
        }
    }

    function initToasts(root) {
        var scope = root instanceof HTMLElement ? root : document;
        var toasts = Array.from(scope.querySelectorAll("[data-app-toast]"));
        if (scope instanceof HTMLElement && scope.matches("[data-app-toast]")) {
            toasts.unshift(scope);
        }
        toasts.forEach(function (toast) {
            applyToastLevel(toast);
            scheduleDismiss(toast);
        });
    }

    document.addEventListener("DOMContentLoaded", function () {
        initToasts(document.body);
        document.body.addEventListener("app-toast", function (event) {
            var detail = event && event.detail ? event.detail : {};
            var message = typeof detail === "string" ? detail : detail.message;
            var level = typeof detail === "string" ? "info" : detail.level;
            var duration = typeof detail === "string" ? 3000 : detail.duration;
            addToast(message, level, duration);
        });
    });

    document.body.addEventListener("htmx:afterSwap", function (event) {
        var target = event && event.detail ? event.detail.target : null;
        if (target instanceof HTMLElement) {
            initToasts(target);
        }
    });
})();
