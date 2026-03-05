(function () {
    function patchCanvasGetContextForReadback() {
        function patchPrototype(prototype, markerKey) {
            if (!prototype) {
                return;
            }
            if (prototype[markerKey] === "1") {
                return;
            }
            var originalGetContext = prototype.getContext;
            if (typeof originalGetContext !== "function") {
                return;
            }

            prototype.getContext = function (contextType, contextAttributes) {
                if (contextType !== "2d") {
                    return originalGetContext.call(this, contextType, contextAttributes);
                }

                var attrs = contextAttributes;
                if (!attrs || typeof attrs !== "object") {
                    attrs = {};
                }
                if (!Object.prototype.hasOwnProperty.call(attrs, "willReadFrequently")) {
                    attrs = Object.assign({}, attrs, { willReadFrequently: true });
                }
                return originalGetContext.call(this, contextType, attrs);
            };
            prototype[markerKey] = "1";
        }

        if (window.HTMLCanvasElement && window.HTMLCanvasElement.prototype) {
            patchPrototype(window.HTMLCanvasElement.prototype, "__activityModalCanvasPatched");
        }
        if (window.OffscreenCanvas && window.OffscreenCanvas.prototype) {
            patchPrototype(window.OffscreenCanvas.prototype, "__activityModalOffscreenPatched");
        }
    }

    function isPdfType(contentType, filename) {
        var type = String(contentType || "").toLowerCase();
        if (type === "application/pdf") {
            return true;
        }
        return String(filename || "").toLowerCase().endsWith(".pdf");
    }

    function isImageType(contentType, filename) {
        var type = String(contentType || "").toLowerCase();
        if (type.startsWith("image/")) {
            return true;
        }
        var lower = String(filename || "").toLowerCase();
        return /\.(png|jpe?g|gif|bmp|webp|svg)$/i.test(lower);
    }

    function hideElement(element, hidden) {
        if (!(element instanceof HTMLElement)) {
            return;
        }
        element.classList.toggle("is-hidden", hidden);
    }

    function setText(element, value) {
        if (!(element instanceof HTMLElement)) {
            return;
        }
        element.textContent = String(value || "");
    }

    function initAttachmentModal() {
        patchCanvasGetContextForReadback();
        var modal = document.querySelector("[data-attachment-preview-modal]");
        if (!(modal instanceof HTMLElement)) {
            return;
        }
        if (modal.dataset.attachmentPreviewReady === "1") {
            return;
        }

        var titleEl = modal.querySelector("[data-attachment-preview-title]");
        var loadingEl = modal.querySelector("[data-attachment-preview-loading]");
        var errorEl = modal.querySelector("[data-attachment-preview-error]");
        var imageWrap = modal.querySelector("[data-attachment-preview-image-wrap]");
        var imageEl = modal.querySelector("[data-attachment-preview-image]");
        var pdfWrap = modal.querySelector("[data-attachment-preview-pdf-wrap]");
        var pdfPages = modal.querySelector("[data-attachment-preview-pdf-pages]");
        var fallbackWrap = modal.querySelector("[data-attachment-preview-fallback]");
        var pdfWorkerUrl = String(modal.dataset.pdfWorkerUrl || "").trim();
        var openNewLinks = Array.from(
            modal.querySelectorAll("[data-attachment-preview-open-new]")
        );

        var pdfWorkerConfigured = false;

        function setOpenNewUrl(url) {
            openNewLinks.forEach(function (link) {
                if (link instanceof HTMLAnchorElement) {
                    link.href = String(url || "");
                }
            });
        }

        function clearPreview() {
            hideElement(loadingEl, true);
            hideElement(errorEl, true);
            setText(errorEl, "");
            hideElement(imageWrap, true);
            hideElement(pdfWrap, true);
            hideElement(fallbackWrap, true);
            if (imageEl instanceof HTMLImageElement) {
                imageEl.removeAttribute("src");
            }
            if (pdfPages instanceof HTMLElement) {
                pdfPages.innerHTML = "";
            }
        }

        function openModal() {
            modal.classList.add("is-active");
            document.documentElement.classList.add("is-clipped");
        }

        function closeModal() {
            modal.classList.remove("is-active");
            document.documentElement.classList.remove("is-clipped");
            clearPreview();
        }

        function showError(message) {
            hideElement(loadingEl, true);
            hideElement(errorEl, false);
            setText(errorEl, message || "미리보기를 불러오지 못했습니다.");
            hideElement(fallbackWrap, false);
        }

        function showPdfIframeFallback(previewUrl) {
            if (!(pdfPages instanceof HTMLElement)) {
                showError("PDF 미리보기를 불러오지 못했습니다.");
                return;
            }
            hideElement(pdfWrap, false);
            hideElement(loadingEl, true);
            pdfPages.innerHTML = "";

            var iframe = document.createElement("iframe");
            iframe.className = "attachment-preview-pdf-iframe";
            iframe.src = previewUrl;
            iframe.loading = "lazy";
            iframe.referrerPolicy = "no-referrer";
            pdfPages.appendChild(iframe);
        }

        function renderImage(previewUrl) {
            if (!(imageEl instanceof HTMLImageElement)) {
                showError("이미지 영역을 불러오지 못했습니다.");
                return;
            }
            hideElement(imageWrap, false);
            hideElement(loadingEl, false);
            imageEl.onload = function () {
                hideElement(loadingEl, true);
            };
            imageEl.onerror = function () {
                showError("이미지 미리보기를 불러오지 못했습니다.");
            };
            imageEl.src = previewUrl;
        }

        async function renderPdf(previewUrl) {
            if (!(pdfPages instanceof HTMLElement)) {
                showError("PDF 영역을 불러오지 못했습니다.");
                return;
            }
            if (!(window.pdfjsLib && typeof window.pdfjsLib.getDocument === "function")) {
                showPdfIframeFallback(previewUrl);
                return;
            }

            try {
                if (!pdfWorkerConfigured && window.pdfjsLib.GlobalWorkerOptions) {
                    if (pdfWorkerUrl) {
                        window.pdfjsLib.GlobalWorkerOptions.workerSrc = pdfWorkerUrl;
                    }
                    pdfWorkerConfigured = true;
                }
                hideElement(pdfWrap, false);
                hideElement(loadingEl, false);

                var loadingTask = window.pdfjsLib.getDocument(previewUrl);
                var pdf = await loadingTask.promise;
                var maxWidth = Math.max(320, pdfPages.clientWidth || 900);

                for (var pageNo = 1; pageNo <= pdf.numPages; pageNo += 1) {
                    var page = await pdf.getPage(pageNo);
                    var baseViewport = page.getViewport({ scale: 1 });
                    var scale = Math.min(2, maxWidth / baseViewport.width);
                    var viewport = page.getViewport({ scale: scale });

                    var wrapper = document.createElement("div");
                    wrapper.className = "attachment-preview-pdf-page";

                    var pageLabel = document.createElement("p");
                    pageLabel.className = "attachment-preview-pdf-page-label";
                    pageLabel.textContent = "페이지 " + String(pageNo) + " / " + String(pdf.numPages);

                    var canvas = document.createElement("canvas");
                    canvas.className = "attachment-preview-pdf-canvas";
                    canvas.width = Math.floor(viewport.width);
                    canvas.height = Math.floor(viewport.height);

                    wrapper.appendChild(pageLabel);
                    wrapper.appendChild(canvas);
                    pdfPages.appendChild(wrapper);

                    var context = canvas.getContext("2d", {
                        alpha: false,
                        willReadFrequently: true,
                    });
                    await page.render({
                        canvasContext: context,
                        viewport: viewport,
                    }).promise;
                }

                hideElement(loadingEl, true);
            } catch (error) {
                showPdfIframeFallback(previewUrl);
            }
        }

        function openPreview(trigger) {
            if (!(trigger instanceof HTMLElement)) {
                return;
            }
            var previewUrl = String(trigger.dataset.previewUrl || trigger.getAttribute("href") || "");
            var downloadUrl = String(trigger.dataset.downloadUrl || previewUrl);
            var contentType = String(trigger.dataset.contentType || "");
            var filename = String(trigger.dataset.filename || "첨부 미리보기");
            if (!previewUrl) {
                return;
            }

            clearPreview();
            setText(titleEl, filename);
            setOpenNewUrl(downloadUrl);
            openModal();

            if (isImageType(contentType, filename)) {
                renderImage(previewUrl);
                return;
            }

            if (isPdfType(contentType, filename)) {
                renderPdf(previewUrl);
                return;
            }

            hideElement(loadingEl, true);
            hideElement(fallbackWrap, false);
        }

        document.addEventListener("click", function (event) {
            var target = event.target;
            if (!(target instanceof Element)) {
                return;
            }

            var openTrigger = target.closest("[data-open-attachment-preview]");
            if (openTrigger instanceof HTMLElement) {
                event.preventDefault();
                openPreview(openTrigger);
                return;
            }

            var closeTrigger = target.closest("[data-close-attachment-preview]");
            if (closeTrigger instanceof HTMLElement) {
                event.preventDefault();
                closeModal();
            }
        });

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && modal.classList.contains("is-active")) {
                closeModal();
            }
        });

        modal.dataset.attachmentPreviewReady = "1";
    }

    document.addEventListener("DOMContentLoaded", initAttachmentModal);
})();
