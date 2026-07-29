(function () {
  "use strict";

  const OPEN_DELAY_MS = 260;
  const CLOSE_DELAY_MS = 220;
  const CONFIRM_DELAY_MS = 3500;
  const SEARCH_DEBOUNCE_MS = 220;
  const THEME_KEY = "clip-brain-theme";

  const plate = document.querySelector("#mode-plate");
  const bulk = document.querySelector("#bulk");
  const trigger = document.querySelector("#bulk-trigger");
  const panel = document.querySelector("#bulk-panel");
  const destroyButton = document.querySelector("#bulk-destroy");
  const searchInput = document.querySelector("#search-input");
  const themeToggle = document.querySelector("#theme-toggle");
  const themeLabel = document.querySelector("#theme-label");
  const topicNav = document.querySelector("#topic-nav");
  const typeNav = document.querySelector("#type-nav");
  const quota = document.querySelector("#quota");
  const quotaNum = document.querySelector("#quota-num");
  const quotaFill = document.querySelector("#quota-fill");

  const state = { view: "temporary", type: "", topic: "", query: "" };
  let handlers = {};
  let openTimer;
  let closeTimer;
  let confirmTimer;
  let confirmArmed = false;

  function setOpen(open) {
    panel.hidden = !open;
    trigger.setAttribute("aria-expanded", String(open));
    if (!open) resetDestroy();
  }

  function scheduleOpen() {
    window.clearTimeout(closeTimer);
    window.clearTimeout(openTimer);
    openTimer = window.setTimeout(() => setOpen(true), OPEN_DELAY_MS);
  }

  function scheduleClose() {
    window.clearTimeout(openTimer);
    window.clearTimeout(closeTimer);
    closeTimer = window.setTimeout(() => setOpen(false), CLOSE_DELAY_MS);
  }

  function resetDestroy() {
    confirmArmed = false;
    window.clearTimeout(confirmTimer);
    destroyButton.lastElementChild.textContent = " 全部焚毁";
    destroyButton.classList.remove("is-armed");
  }

  function armDestroy(count) {
    if (confirmArmed) {
      resetDestroy();
      return true;
    }
    confirmArmed = true;
    destroyButton.lastElementChild.textContent = ` 再点一次焚毁 ${count} 条`;
    destroyButton.classList.add("is-armed");
    confirmTimer = window.setTimeout(resetDestroy, CONFIRM_DELAY_MS);
    return false;
  }

  function setCount(selected, total) {
    trigger.textContent = selected > 0 ? `已选 ${selected} / ${total}` : `${total} 条`;
    const empty = total === 0;
    for (const button of panel.querySelectorAll("button")) button.disabled = empty;
  }

  function setQuota(usage) {
    if (!usage || usage.used_bytes <= usage.warn_bytes) {
      quota.hidden = true;
      return;
    }
    quota.hidden = false;
    quotaNum.textContent = `${window.ClipView.formatBytes(usage.used_bytes)} / ${window.ClipView.formatBytes(usage.quota_bytes)}`;
    quotaFill.style.width = `${Math.min(100, (usage.used_bytes / usage.quota_bytes) * 100).toFixed(1)}%`;
  }

  function setTopics(topics) {
    topicNav.replaceChildren();
    const all = [{ topic: "", count: null }, ...topics];
    for (const item of all) {
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.topic = item.topic;
      button.textContent = item.topic || "全部";
      if (item.count !== null) {
        const badge = document.createElement("span");
        badge.className = "n";
        badge.textContent = String(item.count);
        button.append(badge);
      }
      if (item.topic === state.topic) button.classList.add("is-active");
      topicNav.append(button);
    }
  }

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    themeLabel.textContent = theme === "dark" ? "亮色" : "暗色";
    try {
      // The only thing this page ever persists. Never a token, never a blob.
      localStorage.setItem(THEME_KEY, theme);
    } catch (_) {
      // Private mode: the theme simply resets next visit.
    }
  }

  function restoreTheme() {
    let stored = "";
    try {
      stored = localStorage.getItem(THEME_KEY) || "";
    } catch (_) {
      stored = "";
    }
    if (!stored && window.matchMedia) {
      stored = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    }
    applyTheme(stored === "dark" ? "dark" : "light");
  }

  function init(next) {
    handlers = next || {};
    restoreTheme();

    bulk.addEventListener("pointerenter", scheduleOpen);
    bulk.addEventListener("pointerleave", scheduleClose);
    trigger.addEventListener("click", () => {
      window.clearTimeout(openTimer);
      setOpen(panel.hidden);
    });
    document.addEventListener("pointerdown", (event) => {
      if (!bulk.contains(event.target)) setOpen(false);
    });
    panel.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button || button.disabled) return;
      if (button.id === "bulk-copy") handlers.onCopyAll && handlers.onCopyAll();
      if (button.id === "bulk-download") handlers.onDownloadAll && handlers.onDownloadAll();
      if (button.id === "bulk-destroy") handlers.onDestroyAll && handlers.onDestroyAll(armDestroy);
    });

    plate.addEventListener("click", () => {
      state.view = state.view === "temporary" ? "favorite" : "temporary";
      const favorite = state.view === "favorite";
      plate.setAttribute("aria-pressed", String(favorite));
      plate.textContent = favorite ? "★" : "临";
      handlers.onFilters && handlers.onFilters();
    });

    let searchTimer;
    searchInput.addEventListener("input", () => {
      window.clearTimeout(searchTimer);
      searchTimer = window.setTimeout(() => {
        state.query = searchInput.value.trim();
        handlers.onFilters && handlers.onFilters();
      }, SEARCH_DEBOUNCE_MS);
    });
    window.addEventListener("keydown", (event) => {
      if (event.altKey && event.code === "Space") {
        event.preventDefault();
        searchInput.focus();
        searchInput.select();
      }
    });

    typeNav.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      state.type = button.dataset.type || "";
      for (const other of typeNav.querySelectorAll("button")) other.classList.remove("is-active");
      button.classList.add("is-active");
      handlers.onFilters && handlers.onFilters();
    });

    topicNav.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (!button) return;
      state.topic = button.dataset.topic || "";
      for (const other of topicNav.querySelectorAll("button")) other.classList.remove("is-active");
      button.classList.add("is-active");
      handlers.onFilters && handlers.onFilters();
    });

    themeToggle.addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  window.ClipToolbar = Object.freeze({
    init,
    filters: () => ({ ...state }),
    resetDestroy,
    setCount,
    setQuota,
    setTopics,
  });
}());
