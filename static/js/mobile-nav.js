(function () {
    function initMobileNav() {
        var toggleButton = document.querySelector("[data-mobile-nav-toggle]");
        var closeButton = document.querySelector("[data-mobile-nav-close]");
        var panel = document.querySelector("[data-mobile-nav-panel]");
        var backdrop = document.querySelector("[data-mobile-nav-backdrop]");

        if (!(toggleButton instanceof HTMLButtonElement) || !(panel instanceof HTMLElement)) {
            return;
        }

        var navOpen = false;

        function updateAria(isOpen) {
            toggleButton.setAttribute("aria-expanded", isOpen ? "true" : "false");
            panel.setAttribute("aria-hidden", isOpen ? "false" : "true");
            if (backdrop instanceof HTMLElement) {
                backdrop.setAttribute("aria-hidden", isOpen ? "false" : "true");
            }
        }

        function openNav() {
            if (navOpen) {
                return;
            }
            navOpen = true;
            document.body.classList.add("is-mobile-nav-open");
            panel.classList.add("is-open");
            if (backdrop instanceof HTMLElement) {
                backdrop.classList.add("is-open");
            }
            updateAria(true);
        }

        function closeNav() {
            if (!navOpen) {
                return;
            }
            navOpen = false;
            document.body.classList.remove("is-mobile-nav-open");
            panel.classList.remove("is-open");
            if (backdrop instanceof HTMLElement) {
                backdrop.classList.remove("is-open");
            }
            updateAria(false);
        }

        toggleButton.addEventListener("click", function () {
            if (navOpen) {
                closeNav();
                return;
            }
            openNav();
        });

        if (closeButton instanceof HTMLButtonElement) {
            closeButton.addEventListener("click", closeNav);
        }

        if (backdrop instanceof HTMLElement) {
            backdrop.addEventListener("click", closeNav);
        }

        document.addEventListener("keydown", function (event) {
            if (event.key === "Escape" && navOpen) {
                closeNav();
            }
        });

        panel.addEventListener("click", function (event) {
            if (!(event.target instanceof HTMLElement)) {
                return;
            }
            var link = event.target.closest(".sidebar-link");
            if (link instanceof HTMLElement && link.closest("nav")) {
                closeNav();
            }
        });

        window.addEventListener("resize", function () {
            if (window.innerWidth > 768 && navOpen) {
                closeNav();
            }
        });

        updateAria(false);
    }

    document.addEventListener("DOMContentLoaded", initMobileNav);
})();
