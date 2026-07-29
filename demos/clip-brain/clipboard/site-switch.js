(function () {
  "use strict";

  const TARGET = "/clipboard/";
  const MAX_WATCH_MS = 15000;
  const BOUND_ATTRIBUTE = "data-clip-brain-linked";

  function hasSignedInSession() {
    const node = document.querySelector("#initial-state");
    if (!node) return false;
    try {
      const state = JSON.parse(node.textContent || node.getAttribute("data-props") || "{}");
      return Boolean(state.meta && (state.meta.access_token || state.meta.accessToken));
    } catch (_) {
      return false;
    }
  }

  function looksLikeBrand(anchor) {
    const label = String(anchor.getAttribute("aria-label") || "").toLowerCase();
    const text = String(anchor.textContent || "").trim().toLowerCase();
    const image = anchor.querySelector('img[alt*="mastodon" i]');
    let path = "";
    try {
      path = new URL(anchor.href, window.location.href).pathname;
    } catch (_) {
      return false;
    }
    const homeLike = path === "/" || path === "/home" || path === "/web/home";
    return homeLike && (label.includes("mastodon") || text === "mastodon" || Boolean(image));
  }

  function bindBrand() {
    if (!hasSignedInSession()) return false;
    const brand = Array.from(document.querySelectorAll("a[href]")).find(looksLikeBrand);
    if (!brand) return false;
    if (brand.getAttribute(BOUND_ATTRIBUTE) === "true") return true;

    brand.href = TARGET;
    brand.title = "打开 Clip Brain";
    brand.setAttribute(BOUND_ATTRIBUTE, "true");
    brand.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      window.location.assign(TARGET);
    }, true);
    return true;
  }

  if (bindBrand()) return;

  const observer = new MutationObserver(() => {
    if (!bindBrand()) return;
    observer.disconnect();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.setTimeout(() => observer.disconnect(), MAX_WATCH_MS);
}());
