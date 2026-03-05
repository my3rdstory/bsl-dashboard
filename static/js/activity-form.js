(function () {
    function formatSize(bytes) {
        var raw = Number.parseInt(String(bytes || 0), 10);
        if (!Number.isFinite(raw) || raw <= 0) {
            return "0 B";
        }
        if (raw >= 1024 * 1024) {
            return (raw / (1024 * 1024)).toFixed(2) + " MB";
        }
        if (raw >= 1024) {
            return (raw / 1024).toFixed(1) + " KB";
        }
        return String(raw) + " B";
    }

    function buildDataTransfer(files) {
        var dataTransfer = new DataTransfer();
        files.forEach(function (file) {
            dataTransfer.items.add(file);
        });
        return dataTransfer;
    }

    function initActivityForm(root) {
        if (!(root instanceof HTMLElement)) {
            return;
        }
        if (root.dataset.activityFormReady === "1") {
            return;
        }

        var form = root.querySelector("form[data-activity-form]");
        var editor = root.querySelector("[data-activity-editor]");
        var contentInput = root.querySelector("input[name='content_html']");
        var contentError = root.querySelector("[data-content-error]");
        var fileInput = root.querySelector("[data-file-input]");
        var fileDropzone = root.querySelector("[data-file-dropzone]");
        var fileList = root.querySelector("[data-file-list]");
        var fileError = root.querySelector("[data-file-error]");
        var contentCharCount = root.querySelector("[data-content-char-count]");

        if (!(form instanceof HTMLFormElement)) {
            return;
        }
        if (!(editor instanceof HTMLElement) || !(contentInput instanceof HTMLInputElement)) {
            return;
        }
        if (!(fileInput instanceof HTMLInputElement) || !(fileDropzone instanceof HTMLElement)) {
            return;
        }

        var maxFiles = Number.parseInt(root.dataset.maxFiles || "10", 10);
        var maxSizeBytes = Number.parseInt(root.dataset.maxSizeBytes || String(10 * 1024 * 1024), 10);
        var maxContentChars = Number.parseInt(root.dataset.maxContentChars || "10000", 10);
        if (!Number.isFinite(maxContentChars) || maxContentChars < 1) {
            maxContentChars = 10000;
        }
        var selectedFiles = [];

        var quill = null;
        if (typeof window.Quill === "function") {
            quill = new window.Quill(editor, {
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
                placeholder: "활동 내용을 입력하세요"
            });

            if (contentInput.value) {
                quill.root.innerHTML = contentInput.value;
            }

            var editorContainer = editor.querySelector(".ql-container");
            if (editorContainer instanceof HTMLElement) {
                editorContainer.addEventListener("click", function (event) {
                    quill.focus();
                    if (event.target === editorContainer) {
                        var cursorIndex = Math.max(0, quill.getLength() - 1);
                        quill.setSelection(cursorIndex, 0, "silent");
                    }
                });
            }
        }

        function getEditorCharLength() {
            if (!quill) {
                return 0;
            }
            var rawLength = quill.getLength() - 1;
            return rawLength > 0 ? rawLength : 0;
        }

        function updateContentCounter(length) {
            if (!(contentCharCount instanceof HTMLElement)) {
                return;
            }
            contentCharCount.textContent = String(length || 0);
        }

        function clampEditorLength() {
            if (!quill) {
                return 0;
            }
            var currentLength = getEditorCharLength();
            if (currentLength <= maxContentChars) {
                return currentLength;
            }
            quill.deleteText(maxContentChars, currentLength - maxContentChars, "silent");
            return maxContentChars;
        }

        function hasNonEmptyEditorText() {
            if (!quill) {
                return false;
            }
            return quill.getText().trim().length > 0;
        }

        function setFileError(message) {
            if (!(fileError instanceof HTMLElement)) {
                return;
            }
            var text = String(message || "").trim();
            fileError.textContent = text;
            fileError.classList.toggle("is-hidden", !text);
        }

        function renderFiles() {
            if (!(fileList instanceof HTMLElement)) {
                return;
            }
            fileList.innerHTML = "";
            selectedFiles.forEach(function (file, index) {
                var item = document.createElement("li");
                item.className = "activity-file-item";

                var meta = document.createElement("span");
                meta.className = "activity-file-meta";
                meta.textContent = file.name + " (" + formatSize(file.size) + ")";

                var remove = document.createElement("button");
                remove.type = "button";
                remove.className = "button is-small activity-file-remove";
                remove.textContent = "제거";
                remove.addEventListener("click", function () {
                    selectedFiles.splice(index, 1);
                    syncInputFiles();
                    renderFiles();
                });

                item.appendChild(meta);
                item.appendChild(remove);
                fileList.appendChild(item);
            });
        }

        function syncInputFiles() {
            var transfer = buildDataTransfer(selectedFiles);
            fileInput.files = transfer.files;
        }

        function appendFiles(files) {
            var incoming = Array.from(files || []);
            if (!incoming.length) {
                return;
            }
            var errorMessages = [];

            incoming.forEach(function (file) {
                if (selectedFiles.length >= maxFiles) {
                    errorMessages.push("첨부파일은 최대 " + String(maxFiles) + "개까지 등록할 수 있습니다.");
                    return;
                }
                if (file.size > maxSizeBytes) {
                    errorMessages.push(file.name + " 파일이 10MB를 초과했습니다.");
                    return;
                }
                selectedFiles.push(file);
            });

            syncInputFiles();
            renderFiles();
            setFileError(errorMessages.join(" "));
        }

        fileInput.addEventListener("change", function () {
            appendFiles(fileInput.files);
        });

        fileDropzone.addEventListener("click", function () {
            fileInput.click();
        });

        fileDropzone.addEventListener("keydown", function (event) {
            if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                fileInput.click();
            }
        });

        ["dragenter", "dragover"].forEach(function (name) {
            fileDropzone.addEventListener(name, function (event) {
                event.preventDefault();
                fileDropzone.classList.add("is-dragover");
            });
        });

        ["dragleave", "dragend", "drop"].forEach(function (name) {
            fileDropzone.addEventListener(name, function (event) {
                event.preventDefault();
                fileDropzone.classList.remove("is-dragover");
            });
        });

        fileDropzone.addEventListener("drop", function (event) {
            var files = event.dataTransfer ? event.dataTransfer.files : null;
            appendFiles(files);
        });

        if (quill) {
            updateContentCounter(clampEditorLength());
            quill.on("text-change", function () {
                updateContentCounter(clampEditorLength());
                if (contentError instanceof HTMLElement && hasNonEmptyEditorText()) {
                    contentError.classList.add("is-hidden");
                }
            });
        } else {
            updateContentCounter(0);
        }

        form.addEventListener("submit", function (event) {
            setFileError("");
            if (quill) {
                updateContentCounter(clampEditorLength());
                contentInput.value = quill.root.innerHTML.trim();
                if (!hasNonEmptyEditorText()) {
                    event.preventDefault();
                    if (contentError instanceof HTMLElement) {
                        contentError.classList.remove("is-hidden");
                    }
                    return;
                }
            }
            if (contentError instanceof HTMLElement) {
                contentError.classList.add("is-hidden");
            }
        });

        root.dataset.activityFormReady = "1";
    }

    function initAll() {
        var roots = Array.from(document.querySelectorAll("[data-activity-form-root]"));
        roots.forEach(initActivityForm);
    }

    document.addEventListener("DOMContentLoaded", initAll);
})();
