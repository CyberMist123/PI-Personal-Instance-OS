(function () {
  "use strict";

  const TEXT_LIMIT = 10000;
  const FILE_LIMIT = 30;
  const BYTE_LIMIT = 1024 ** 3;
  const TTL_MS = 24 * 60 * 60 * 1000;
  const CHANNEL_NAME = "clip-brain-demo-sync";

  const form = document.querySelector("#clip-form");
  const textInput = document.querySelector("#clip-text");
  const fileInput = document.querySelector("#clip-files");
  const textCount = document.querySelector("#text-count");
  const fileSummary = document.querySelector("#file-summary");
  const saveButton = document.querySelector("#save-button");
  const errorBox = document.querySelector("#form-error");
  const statusBox = document.querySelector("#form-status");
  const clipList = document.querySelector("#clip-list");
  const clipTotal = document.querySelector("#clip-total");
  const emptyState = document.querySelector("#empty-state");
  const template = document.querySelector("#clip-template");
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL_NAME) : null;

  let clips = [];

  function countUnicode(value) {
    return Array.from(value).length;
  }

  function makeId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    const units = ["KiB", "MiB", "GiB"];
    let value = bytes;
    let unit = "B";
    for (const next of units) {
      value /= 1024;
      unit = next;
      if (value < 1024) break;
    }
    return `${value.toFixed(value >= 10 ? 1 : 2)} ${unit}`;
  }

  function totalFileBytes(files) {
    return files.reduce((sum, file) => sum + file.size, 0);
  }

  function showMessage(node, message) {
    node.textContent = message;
    node.hidden = !message;
  }

  function clearMessages() {
    showMessage(errorBox, "");
    showMessage(statusBox, "");
  }

  function updateDraftSummary() {
    const characterCount = countUnicode(textInput.value);
    const files = Array.from(fileInput.files || []);
    const bytes = totalFileBytes(files);
    textCount.textContent = `${characterCount} / ${TEXT_LIMIT}`;
    textCount.style.color = characterCount > TEXT_LIMIT ? "var(--danger)" : "";
    fileSummary.textContent = files.length
      ? `${files.length} 个文件 · ${formatBytes(bytes)}`
      : "尚未选择文件";
  }

  function validateDraft(text, files) {
    const length = countUnicode(text);
    const bytes = totalFileBytes(files);
    if (!text.trim() && files.length === 0) return "至少粘贴一段文字或选择一个文件。";
    if (length > TEXT_LIMIT) return `文字超过 ${TEXT_LIMIT} 个 Unicode 字符。`;
    if (files.length > FILE_LIMIT) return `每条最多允许 ${FILE_LIMIT} 个文件。`;
    if (bytes > BYTE_LIMIT) return "每条文件总量不能超过 1 GiB。";
    return "";
  }

  function fileRecords(files) {
    return files.map((file) => ({
      name: file.name || "unnamed-file",
      type: file.type || "application/octet-stream",
      size: file.size,
      lastModified: file.lastModified || Date.now(),
      blob: file,
    }));
  }

  function announceSync() {
    if (channel) channel.postMessage({ type: "changed", at: Date.now() });
    try {
      localStorage.setItem(CHANNEL_NAME, String(Date.now()));
    } catch (_) {
      // IndexedDB remains the source of truth; localStorage is only a fallback signal.
    }
  }

  async function refresh() {
    await window.ClipStore.purgeExpired(Date.now());
    clips = await window.ClipStore.list();
    render();
  }

  function formatCreated(timestamp) {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    }).format(new Date(timestamp));
  }

  function formatRemaining(expiresAt) {
    const remaining = Math.max(0, expiresAt - Date.now());
    const hours = Math.floor(remaining / 3600000);
    const minutes = Math.floor((remaining % 3600000) / 60000);
    const seconds = Math.floor((remaining % 60000) / 1000);
    return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
  }

  function renderFiles(list, clip) {
    list.replaceChildren();
    for (const [fileIndex, file] of (clip.files || []).entries()) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      const name = document.createElement("span");
      const size = document.createElement("span");
      button.type = "button";
      button.className = "file-button";
      button.dataset.clipId = clip.id;
      button.dataset.fileIndex = String(fileIndex);
      name.className = "file-name";
      name.textContent = file.name;
      size.className = "file-size";
      size.textContent = formatBytes(file.size);
      button.append(name, size);
      item.append(button);
      list.append(item);
    }
  }

  function render() {
    clipList.replaceChildren();
    emptyState.hidden = clips.length > 0;
    clipTotal.textContent = `${clips.length} 条`;

    for (const clip of clips) {
      const node = template.content.firstElementChild.cloneNode(true);
      node.dataset.id = clip.id;
      node.querySelector(".clip-created").textContent = formatCreated(clip.createdAt);
      node.querySelector(".clip-countdown").textContent = formatRemaining(clip.expiresAt);

      const textButton = node.querySelector(".clip-text");
      const copyButton = node.querySelector(".copy-button");
      textButton.textContent = clip.text || "";
      textButton.dataset.id = clip.id;
      copyButton.dataset.id = clip.id;
      copyButton.hidden = !clip.text;

      const deleteButton = node.querySelector(".delete-button");
      deleteButton.dataset.id = clip.id;
      renderFiles(node.querySelector(".clip-files"), clip);
      clipList.append(node);
    }
  }

  function updateCountdowns() {
    for (const card of clipList.querySelectorAll(".clip-card")) {
      const clip = clips.find((item) => item.id === card.dataset.id);
      if (clip) card.querySelector(".clip-countdown").textContent = formatRemaining(clip.expiresAt);
    }
  }

  async function copyText(id) {
    const clip = clips.find((item) => item.id === id);
    if (!clip || !clip.text) return;
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(clip.text);
    } else {
      const fallback = document.createElement("textarea");
      fallback.value = clip.text;
      fallback.style.position = "fixed";
      fallback.style.opacity = "0";
      document.body.append(fallback);
      fallback.select();
      if (!document.execCommand("copy")) throw new Error("浏览器拒绝复制");
      fallback.remove();
    }
    showMessage(statusBox, "文本已复制。其余内容未改变。");
  }

  function downloadFile(clipId, fileIndex) {
    const clip = clips.find((item) => item.id === clipId);
    const file = clip && (clip.files || [])[Number(fileIndex)];
    if (!file) return;
    const url = URL.createObjectURL(file.blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessages();
    const text = textInput.value;
    const files = Array.from(fileInput.files || []);
    const error = validateDraft(text, files);
    if (error) return showMessage(errorBox, error);

    saveButton.disabled = true;
    const createdAt = Date.now();
    try {
      await window.ClipStore.put({
        id: makeId(),
        text,
        files: fileRecords(files),
        createdAt,
        expiresAt: createdAt + TTL_MS,
      });
      form.reset();
      updateDraftSummary();
      showMessage(statusBox, "已保存。24 小时后自动焚毁。");
      announceSync();
      await refresh();
    } catch (storageError) {
      const quota = storageError && (storageError.name === "QuotaExceededError" || storageError.name === "NS_ERROR_DOM_QUOTA_REACHED");
      showMessage(errorBox, quota
        ? "浏览器存储配额不足，未保存这条内容。请减少文件大小或清理旧记录。"
        : `保存失败：${storageError.message || "未知存储错误"}`);
    } finally {
      saveButton.disabled = false;
    }
  });

  clipList.addEventListener("click", async (event) => {
    const textButton = event.target.closest(".clip-text, .copy-button");
    if (textButton) return copyText(textButton.dataset.id).catch((error) => showMessage(errorBox, `复制失败：${error.message}`));

    const fileButton = event.target.closest(".file-button");
    if (fileButton) return downloadFile(fileButton.dataset.clipId, fileButton.dataset.fileIndex);

    const deleteButton = event.target.closest(".delete-button");
    if (!deleteButton) return;
    await window.ClipStore.remove(deleteButton.dataset.id);
    announceSync();
    await refresh();
  });

  textInput.addEventListener("input", updateDraftSummary);
  fileInput.addEventListener("change", updateDraftSummary);
  if (channel) channel.addEventListener("message", refresh);
  window.addEventListener("storage", (event) => {
    if (event.key === CHANNEL_NAME) refresh();
  });

  setInterval(updateCountdowns, 1000);
  setInterval(async () => {
    const removed = await window.ClipStore.purgeExpired(Date.now());
    if (removed) {
      announceSync();
      await refresh();
    }
  }, 30000);

  updateDraftSummary();
  refresh().catch((error) => showMessage(errorBox, `初始化失败：${error.message}`));
}());
