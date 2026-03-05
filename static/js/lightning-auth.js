document.addEventListener("click", function (event) {
    var button = event.target;
    if (!(button instanceof HTMLElement)) {
        return;
    }
    if (!button.classList.contains("copy-auth-code-button")) {
        return;
    }

    var panel = button.closest(".box");
    if (!(panel instanceof HTMLElement)) {
        return;
    }

    var codeNode = panel.querySelector("[data-auth-code]");
    if (!(codeNode instanceof HTMLElement)) {
        return;
    }

    var code = (codeNode.textContent || "").trim();
    if (!code) {
        return;
    }

    navigator.clipboard.writeText(code);
});
