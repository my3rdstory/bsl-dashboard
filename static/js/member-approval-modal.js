(function () {
    var modalConfigs = [
        {
            openTriggerSelector: "[data-open-approve-modal]",
            closeTriggerSelector: "[data-close-approve-modal]",
            modalSelector: "[data-approve-modal]",
            formSelector: "[data-approve-form]",
            memberLabelSelector: "[data-approve-member-label]",
            firstReviewedAtSelector: "[data-approve-first-reviewed-at]",
            noteSelector: "[data-approve-note]",
            actionAttr: "data-approve-url"
        },
        {
            openTriggerSelector: "[data-open-reject-modal]",
            closeTriggerSelector: "[data-close-reject-modal]",
            modalSelector: "[data-reject-modal]",
            formSelector: "[data-reject-form]",
            memberLabelSelector: "[data-reject-member-label]",
            firstReviewedAtSelector: "[data-reject-first-reviewed-at]",
            noteSelector: "[data-reject-note]",
            actionAttr: "data-reject-url"
        },
        {
            openTriggerSelector: "[data-open-withdraw-modal]",
            closeTriggerSelector: "[data-close-withdraw-modal]",
            modalSelector: "[data-withdraw-modal]",
            formSelector: "[data-withdraw-form]",
            memberLabelSelector: "[data-withdraw-member-label]",
            firstReviewedAtSelector: "[data-withdraw-first-reviewed-at]",
            noteSelector: "[data-withdraw-note]",
            actionAttr: "data-withdraw-url"
        },
        {
            openTriggerSelector: "[data-open-confirm-dues-modal]",
            closeTriggerSelector: "[data-close-confirm-dues-modal]",
            modalSelector: "[data-confirm-dues-modal]",
            formSelector: "[data-confirm-dues-form]",
            memberLabelSelector: "[data-confirm-dues-member-label]",
            firstReviewedAtSelector: "[data-confirm-dues-first-reviewed-at]",
            noteSelector: "[data-confirm-dues-note]",
            actionAttr: "data-confirm-dues-url"
        }
    ];

    function formatNowToInputValue() {
        var now = new Date();
        var pad = function (value) {
            return String(value).padStart(2, "0");
        };
        return (
            now.getFullYear() +
            "-" +
            pad(now.getMonth() + 1) +
            "-" +
            pad(now.getDate()) +
            "T" +
            pad(now.getHours()) +
            ":" +
            pad(now.getMinutes()) +
            ":" +
            pad(now.getSeconds())
        );
    }

    function closeModal(modal) {
        if (!(modal instanceof HTMLElement)) {
            return;
        }
        modal.classList.remove("is-active");
    }

    function openModal(trigger, config) {
        var modal = document.querySelector(config.modalSelector);
        if (!(modal instanceof HTMLElement)) {
            return;
        }

        var form = modal.querySelector(config.formSelector);
        var memberLabel = modal.querySelector(config.memberLabelSelector);
        var firstReviewedAtInput = modal.querySelector(config.firstReviewedAtSelector);
        var noteInput = modal.querySelector(config.noteSelector);
        if (!(form instanceof HTMLFormElement)) {
            return;
        }

        var actionUrl = trigger.getAttribute(config.actionAttr) || "";
        form.setAttribute("action", actionUrl);

        if (memberLabel instanceof HTMLElement) {
            memberLabel.textContent = trigger.getAttribute("data-member-label") || "-";
        }

        if (firstReviewedAtInput instanceof HTMLInputElement) {
            var sourceSelector = trigger.getAttribute("data-first-check-target") || "";
            var sourceInput = sourceSelector
                ? document.querySelector(sourceSelector)
                : null;
            if (sourceInput instanceof HTMLInputElement && sourceInput.value) {
                firstReviewedAtInput.value = sourceInput.value;
            } else if (!firstReviewedAtInput.value) {
                firstReviewedAtInput.value = formatNowToInputValue();
            }
        }

        if (noteInput instanceof HTMLTextAreaElement) {
            noteInput.value = "";
        }

        modal.classList.add("is-active");
        if (noteInput instanceof HTMLTextAreaElement) {
            noteInput.focus();
        }
    }

    function resolveConfigByOpenTrigger(target) {
        for (var i = 0; i < modalConfigs.length; i += 1) {
            var config = modalConfigs[i];
            var trigger = target.closest(config.openTriggerSelector);
            if (trigger instanceof HTMLElement) {
                return { config: config, trigger: trigger };
            }
        }
        return null;
    }

    function resolveConfigByCloseTrigger(target) {
        for (var i = 0; i < modalConfigs.length; i += 1) {
            var config = modalConfigs[i];
            var trigger = target.closest(config.closeTriggerSelector);
            if (trigger instanceof HTMLElement) {
                return { config: config, trigger: trigger };
            }
        }
        return null;
    }

    function resolveConfigByForm(target) {
        for (var i = 0; i < modalConfigs.length; i += 1) {
            var config = modalConfigs[i];
            if (target.matches(config.formSelector)) {
                return config;
            }
        }
        return null;
    }

    document.body.addEventListener("click", function (event) {
        var target = event.target;
        if (!(target instanceof Element)) {
            return;
        }

        var openResolved = resolveConfigByOpenTrigger(target);
        if (openResolved) {
            event.preventDefault();
            openModal(openResolved.trigger, openResolved.config);
            return;
        }

        var closeResolved = resolveConfigByCloseTrigger(target);
        if (closeResolved) {
            var modalToClose = closeResolved.trigger.closest(closeResolved.config.modalSelector);
            closeModal(modalToClose);
        }
    });

    document.addEventListener("keydown", function (event) {
        if (event.key !== "Escape") {
            return;
        }
        for (var i = 0; i < modalConfigs.length; i += 1) {
            var modal = document.querySelector(modalConfigs[i].modalSelector + ".is-active");
            closeModal(modal);
        }
    });

    document.body.addEventListener("submit", function (event) {
        var target = event.target;
        if (!(target instanceof HTMLFormElement)) {
            return;
        }

        var config = resolveConfigByForm(target);
        if (!config) {
            return;
        }

        var noteInput = target.querySelector(config.noteSelector);
        if (noteInput instanceof HTMLTextAreaElement && !noteInput.value.trim()) {
            event.preventDefault();
            noteInput.focus();
            return;
        }

        var modal = target.closest(config.modalSelector);
        closeModal(modal);
    });
})();
