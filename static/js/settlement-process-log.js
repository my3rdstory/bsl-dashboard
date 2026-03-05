(function () {
    function formatNow() {
        var now = new Date();
        var hh = String(now.getHours()).padStart(2, "0");
        var mm = String(now.getMinutes()).padStart(2, "0");
        var ss = String(now.getSeconds()).padStart(2, "0");
        return hh + ":" + mm + ":" + ss;
    }

    function notifySettlementsUpdated(activityId) {
        var payload = {
            activityId: String(activityId || ""),
            at: Date.now()
        };

        document.dispatchEvent(
            new CustomEvent("bsl:settlements-updated", { detail: payload })
        );

        try {
            window.localStorage.setItem(
                "bsl:settlements-updated-at",
                String(payload.at)
            );
        } catch (error) {
            void error;
        }

        if ("BroadcastChannel" in window) {
            try {
                var channel = new BroadcastChannel("bsl-settlements");
                channel.postMessage({ type: "updated", payload: payload });
                channel.close();
            } catch (error) {
                void error;
            }
        }
    }

    function initSettlementProcessLogbar() {
        var logbar = document.querySelector("[data-settlement-logbar]");
        if (!(logbar instanceof HTMLElement)) {
            return;
        }
        var titleEl = logbar.querySelector("[data-settlement-logbar-title]");
        var linesEl = logbar.querySelector("[data-settlement-logbar-lines]");
        var closeButton = logbar.querySelector("[data-settlement-logbar-close]");
        if (!(linesEl instanceof HTMLElement)) {
            return;
        }

        function openLogbar() {
            logbar.classList.remove("is-hidden");
        }

        function closeLogbar() {
            logbar.classList.add("is-hidden");
        }

        function appendLine(text, level) {
            var line = document.createElement("p");
            line.className = "settlement-logbar-line";
            if (level === "error") {
                line.classList.add("is-error");
            } else if (level === "success") {
                line.classList.add("is-success");
            }
            line.textContent = "[" + formatNow() + "] " + String(text || "");
            linesEl.appendChild(line);
            linesEl.scrollTop = linesEl.scrollHeight;
        }

        function appendLines(lines) {
            if (!Array.isArray(lines)) {
                return;
            }
            lines.forEach(function (line) {
                appendLine(line);
            });
        }

        if (closeButton instanceof HTMLElement) {
            closeButton.addEventListener("click", function () {
                closeLogbar();
            });
        }

        document.addEventListener("submit", function (event) {
            var target = event.target;
            if (!(target instanceof HTMLFormElement)) {
                return;
            }
            if (!target.matches("[data-settlement-process-form]")) {
                return;
            }
            event.preventDefault();

            var submitButton = target.querySelector("button[type='submit']");
            if (!(submitButton instanceof HTMLButtonElement)) {
                return;
            }
            if (submitButton.disabled) {
                return;
            }

            var activityId = String(target.dataset.activityId || "").trim();
            openLogbar();
            if (titleEl instanceof HTMLElement) {
                titleEl.textContent = activityId
                    ? "정산 처리 로그 (활동 #" + activityId + ")"
                    : "정산 처리 로그";
            }

            linesEl.innerHTML = "";
            appendLine("정산처리 버튼을 눌렀습니다.");
            appendLine("서버에 정산 요청을 전송합니다.");

            submitButton.disabled = true;
            var originalText = submitButton.textContent;
            submitButton.textContent = "처리중...";

            var formData = new FormData(target);
            var csrfInput = target.querySelector("input[name='csrfmiddlewaretoken']");
            var csrfToken = "";
            if (csrfInput instanceof HTMLInputElement) {
                csrfToken = csrfInput.value;
            }

            fetch(target.action, {
                method: "POST",
                body: formData,
                headers: {
                    Accept: "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "X-CSRFToken": csrfToken
                }
            })
                .then(function (response) {
                    return response
                        .json()
                        .catch(function () {
                            return { ok: false, error: "서버 응답을 해석하지 못했습니다." };
                        })
                        .then(function (data) {
                            data.__http_ok = response.ok;
                            return data;
                        });
                })
                .then(function (data) {
                    appendLines(data.logs || []);
                    if (data.ok) {
                        notifySettlementsUpdated(activityId);
                        appendLine(data.message || "정산 처리가 완료되었습니다.", "success");
                        if (data.redirect_url) {
                            appendLine("정산 현황 화면으로 이동합니다.");
                            window.setTimeout(function () {
                                window.location.href = String(data.redirect_url);
                            }, 900);
                            return;
                        }
                        submitButton.disabled = false;
                        submitButton.textContent = originalText;
                        return;
                    }

                    appendLine(data.error || "정산 처리에 실패했습니다.", "error");
                    submitButton.disabled = false;
                    submitButton.textContent = originalText;
                })
                .catch(function (error) {
                    appendLine("네트워크 오류: " + String(error), "error");
                    submitButton.disabled = false;
                    submitButton.textContent = originalText;
                });
        });
    }

    document.addEventListener("DOMContentLoaded", initSettlementProcessLogbar);
})();
