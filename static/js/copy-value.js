function emitCopyToast(button, fallbackMessage, level) {
    var message = "";
    if (button instanceof HTMLElement) {
        message = String(button.getAttribute("data-copy-toast") || "").trim();
    }
    if (!message) {
        message = String(fallbackMessage || "").trim();
    }
    if (!message) {
        return;
    }
    document.body.dispatchEvent(
        new CustomEvent("app-toast", {
            detail: {
                message: message,
                level: String(level || "success"),
                duration: 2200
            }
        })
    );
}

document.addEventListener("click", function (event) {
    var trigger = event.target;
    if (!(trigger instanceof HTMLElement)) {
        return;
    }

    var button = trigger.closest("[data-copy-target]");
    if (!(button instanceof HTMLElement)) {
        return;
    }

    var selector = button.getAttribute("data-copy-target") || "";
    if (!selector) {
        return;
    }
    var target = document.querySelector(selector);
    if (!(target instanceof HTMLInputElement) && !(target instanceof HTMLTextAreaElement)) {
        return;
    }
    var value = target.value.trim();
    if (!value) {
        return;
    }
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
        navigator.clipboard
            .writeText(value)
            .then(function () {
                emitCopyToast(button, "복사했습니다.", "success");
            })
            .catch(function () {
                emitCopyToast(button, "복사에 실패했습니다.", "warning");
            });
        return;
    }

    try {
        target.focus();
        target.select();
        var copied = document.execCommand("copy");
        if (copied) {
            emitCopyToast(button, "복사했습니다.", "success");
        } else {
            emitCopyToast(button, "복사에 실패했습니다.", "warning");
        }
    } catch (_error) {
        emitCopyToast(button, "복사에 실패했습니다.", "warning");
    }
});
