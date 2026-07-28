(function () {
  "use strict";

  const OPEN_DELAY_MS = 260;
  const CLOSE_DELAY_MS = 220;
  const CONFIRM_DELAY_MS = 3500;
  const root = document.querySelector("#selection-menu");
  const trigger = document.querySelector("#selection-trigger");
  const selectionText = document.querySelector("#selection-text");
  const actions = document.querySelector("#bulk-actions");
  const actionButton = document.querySelector("#bulk-action");
  const destroyButton = document.querySelector("#bulk-destroy");
  const copyLabel = document.querySelector("#bulk-copy-label");
  const downloadLabel = document.querySelector("#bulk-download-label");

  let openTimer;
  let closeTimer;
  let confirmTimer;
  let hasSelection = false;
  let hasFiles = false;
  let overLimit = false;
  let busy = false;
  let pointerInside = false;
  let confirmArmed = false;

  function clearTimer(name) {
    const timer = name === "open" ? openTimer : closeTimer;
    if (timer) window.clearTimeout(timer);
    if (name === "open") openTimer = undefined;
    else closeTimer = undefined;
  }

  function setOpen(value) {
    const next = Boolean(value && hasSelection);
    root.dataset.open = String(next);
    trigger.setAttribute("aria-expanded", String(next));
    actions.hidden = !next;
  }

  function open() {
    clearTimer("close");
    setOpen(true);
  }

  function close() {
    clearTimer("open");
    if (!busy) setOpen(false);
  }

  function scheduleOpen() {
    clearTimer("close");
    clearTimer("open");
    if (!hasSelection) return;
    openTimer = window.setTimeout(open, OPEN_DELAY_MS);
  }

  function scheduleClose() {
    clearTimer("open");
    clearTimer("close");
    closeTimer = window.setTimeout(close, CLOSE_DELAY_MS);
  }

  function resetDestroy() {
    confirmArmed = false;
    if (confirmTimer) window.clearTimeout(confirmTimer);
    confirmTimer = undefined;
    destroyButton.textContent = "全部焚毁";
    destroyButton.classList.remove("is-armed");
  }

  function armDestroy(count) {
    if (confirmArmed) {
      resetDestroy();
      return true;
    }
    confirmArmed = true;
    destroyButton.textContent = `再点一次焚毁 ${count} 条`;
    destroyButton.classList.add("is-armed");
    confirmTimer = window.setTimeout(resetDestroy, CONFIRM_DELAY_MS);
    return false;
  }

  function updateMode() {
    copyLabel.classList.toggle("is-active", !hasFiles);
    downloadLabel.classList.toggle("is-active", hasFiles);
    actionButton.setAttribute(
      "aria-label",
      hasFiles ? "全部下载为本地 ZIP" : "全部复制所选文本",
    );
    if (!busy) actionButton.disabled = !hasSelection || overLimit;
  }

  function update(options) {
    const nextHasSelection = options.count > 0;
    if (hasSelection && (!nextHasSelection || options.count !== Number(root.dataset.count))) resetDestroy();
    hasSelection = nextHasSelection;
    hasFiles = Boolean(options.hasFiles);
    overLimit = Boolean(options.overLimit);
    root.dataset.count = String(options.count);
    root.hidden = !hasSelection;
    selectionText.textContent = `已选 ${options.count} 条`;
    trigger.title = options.bytesLabel;
    destroyButton.disabled = !hasSelection || busy;
    actionButton.title = overLimit ? "选中内容必须严格小于 1 GiB" : "";
    updateMode();
    if (!hasSelection) setOpen(false);
  }

  function setProgress(percent) {
    busy = true;
    setOpen(true);
    actionButton.disabled = true;
    destroyButton.disabled = true;
    copyLabel.classList.remove("is-active");
    downloadLabel.classList.add("is-active");
    downloadLabel.textContent = percent > 0 ? `下载 ${percent}%` : "准备 ZIP";
  }

  function clearProgress() {
    busy = false;
    downloadLabel.textContent = "下载";
    destroyButton.disabled = !hasSelection;
    updateMode();
    if (!pointerInside && !root.contains(document.activeElement)) scheduleClose();
  }

  trigger.addEventListener("pointerenter", scheduleOpen);
  trigger.addEventListener("focus", scheduleOpen);
  root.addEventListener("pointerenter", () => {
    pointerInside = true;
    clearTimer("close");
  });
  root.addEventListener("pointerleave", () => {
    pointerInside = false;
    scheduleClose();
  });
  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) scheduleClose();
  });
  trigger.addEventListener("click", () => {
    clearTimer("open");
    if (root.dataset.open === "true") close();
    else open();
  });
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    resetDestroy();
    setOpen(false);
    trigger.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });

  window.ClipSelectionMenu = Object.freeze({
    armDestroy,
    clearProgress,
    close,
    open,
    resetDestroy,
    setProgress,
    update,
  });
}());
