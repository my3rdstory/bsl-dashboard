(function () {
    function initMessageShareForm(root) {
        if (!(root instanceof HTMLElement)) {
            return;
        }
        if (root.dataset.messageShareReady === "1") {
            return;
        }

        var form = root.querySelector("form[data-message-share-form]");
        var editor = root.querySelector("[data-message-share-editor]");
        var contentInput = root.querySelector("input[name='recruiting_message_html']");
        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        if (!(editor instanceof HTMLElement) || !(contentInput instanceof HTMLInputElement)) {
            return;
        }
        if (typeof window.Quill !== "function") {
            return;
        }

        var quill = new window.Quill(editor, {
            theme: "snow",
            modules: {
                toolbar: [
                    [{ header: [1, 2, 3, false] }],
                    ["bold", "italic", "underline", "strike"],
                    [{ list: "ordered" }, { list: "bullet" }],
                    ["blockquote", "code-block", "link"],
                    ["clean"]
                ]
            },
            placeholder: "월간 회원 모집 메시지를 입력하세요"
        });

        if (contentInput.value) {
            quill.root.innerHTML = contentInput.value;
        }

        form.addEventListener("submit", function () {
            contentInput.value = quill.root.innerHTML.trim();
        });

        root.dataset.messageShareReady = "1";
    }

    function initAll() {
        var roots = Array.from(document.querySelectorAll("[data-message-share-form-root]"));
        roots.forEach(initMessageShareForm);
    }

    document.addEventListener("DOMContentLoaded", initAll);
})();
