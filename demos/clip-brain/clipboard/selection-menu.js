(function () {
  "use strict";

  const OPEN_DELAY_MS = 260;
  const CLOSE_DELAY_MS = 220;
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
  let hasSelection = false;
  let hasFiles = false;
  let overLimit = false;
  let busy = false;
  let pointerInside = false;

  function clearOpenTimer() {
    if (openTimer) window.clearTimeout(openTimer);
    openTimer = undefined;
  }

  function clearCloseTimer() {
    if (closeTimer) window.clearTimeout(closeTimer);
    closeTimer = undefined;
  }

  function setOpen(value) {
    const next = Boolean(value && hasSelection);
    root.dataset.open = String(next);
    trigger.setAttribute("aria-expanded", String(next));
    actions.hidden = !next;
  }

  function open() {
    clearCloseTimer();
    setOpen(true);
  }

  function close() {
    clearOpenTimer();
    if (!busy) setOpen(false);
  }

  function scheduleOpen() {
    clearCloseTimer();
    clearOpenTimer();
    if (!hasSelection) return;
    openTimer = window.setTimeout(open, OPEN_DELAY_MS);
  }

  function scheduleClose() {
    clearOpenTimer();
    clearCloseTimer();
    closeTimer = window.setTimeout(close, CLOSE_DELAY_MS);
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
    hasSelection = options.count > 0;
    hasFiles = Boolean(options.hasFiles);
    overLimit = Boolean(options.overLimit);
    root.dataset.hasSelection = String(hasSelection);
    selectionText.textContent = hasSelection
      ? `已选 ${options.count} 条 · ${options.bytesLabel}`
      : "未选择条目";
    trigger.disabled = !hasSelection;
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
    clearCloseTimer();
  });
  root.addEventListener("pointerleave", () => {
    pointerInside = false;
    scheduleClose();
  });
  root.addEventListener("focusout", (event) => {
    if (!root.contains(event.relatedTarget)) scheduleClose();
  });
  trigger.addEventListener("click", () => {
    clearOpenTimer();
    if (root.dataset.open === "true") close();
    else open();
  });
  root.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    setOpen(false);
    trigger.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!root.contains(event.target)) close();
  });

  window.ClipSelectionMenu = Object.freeze({
    clearProgress,
    close,
    open,
    setProgress,
    update,
  });
}());
