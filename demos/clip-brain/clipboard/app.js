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
  const saveButton = document.querySelector("#save-button");
  const errorBox = document.querySelector("#form-error");
  const statusBox = document.querySelector("#form-status");
  const clipList = document.querySelector("#clip-list");
  const draftList = document.querySelector("#draft-files");
  const prevPage = document.querySelector("#prev-page");
  const nextPage = document.querySelector("#next-page");
  const selectPage = document.querySelector("#select-page");
  const downloadSelected = document.querySelector("#download-selected");
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL_NAME) : null;
  const state = {
    clips: [],
    selected: new Set(),
    expanded: new Set(),
    page: 1,
    currentIds: [],
    draftFiles: [],
  };
  function countUnicode(value) {
    return Array.from(value).length;
  }
  function makeId() {
    return crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
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
  function updateDraft() {
    const characterCount = countUnicode(textInput.value);
    textCount.textContent = `${characterCount} / ${TEXT_LIMIT}`;
    textCount.style.color = characterCount > TEXT_LIMIT ? "var(--danger)" : "";
    window.ClipView.renderDraft(state.draftFiles);
  }
  function validateFiles(files) {
    if (files.length > FILE_LIMIT) return `每条最多允许 ${FILE_LIMIT} 个文件。`;
    if (totalFileBytes(files) > BYTE_LIMIT) return "每条文件总量不能超过 1 GiB。";
    return "";
  }
  function validateDraft(text, files) {
    if (!text.trim() && files.length === 0) return "至少粘贴一段文字或选择一个文件。";
    if (countUnicode(text) > TEXT_LIMIT) return `文字超过 ${TEXT_LIMIT} 个 Unicode 字符。`;
    return validateFiles(files);
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
      // IndexedDB remains the source of truth.
    }
  }
  function selectedClips() {
    return state.clips.filter((clip) => state.selected.has(clip.id));
  }
  function render() {
    const selectedBytes = window.ClipArchive.measureClips(selectedClips());
    const result = window.ClipView.renderBoard({
      clips: state.clips,
      selected: state.selected,
      expanded: state.expanded,
      page: state.page,
      selectedBytes,
    });
    state.page = result.page;
    state.currentIds = result.currentIds;
  }
  async function refresh() {
    await window.ClipStore.purgeExpired(Date.now());
    state.clips = await window.ClipStore.list();
    const existingIds = new Set(state.clips.map((clip) => clip.id));
    for (const id of state.selected) if (!existingIds.has(id)) state.selected.delete(id);
    for (const id of state.expanded) if (!existingIds.has(id)) state.expanded.delete(id);
    render();
  }
  async function copyText(id) {
    const clip = state.clips.find((item) => item.id === id);
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
    showMessage(statusBox, "文本已复制。");
  }
  function downloadFile(clipId, fileIndex) {
    const clip = state.clips.find((item) => item.id === clipId);
    const file = clip && (clip.files || [])[Number(fileIndex)];
    if (!file) return;
    const url = URL.createObjectURL(file.blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name || "unnamed-file";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function downloadZip(clips, prefix) {
    clearMessages();
    const bytes = window.ClipArchive.measureClips(clips);
    if (bytes >= window.ClipArchive.MAX_ARCHIVE_BYTES) {
      return showMessage(errorBox, "选中内容合计必须严格小于 1 GiB。");
    }
    try {
      const result = await window.ClipArchive.saveClipsAsZip(clips, {
        name: `${prefix}-${Date.now()}.zip`,
        onProgress: (done, total) => {
          const percent = total ? Math.floor((done / total) * 100) : 100;
          showMessage(statusBox, `正在本地打包：${percent}%`);
        },
      });
      showMessage(statusBox, `ZIP 已保存：${result.fileCount} 个文件，${window.ClipView.formatBytes(result.totalBytes)}。`);
    } catch (error) {
      if (error && error.name === "AbortError") {
        showMessage(statusBox, "已取消本地打包。");
      } else {
        showMessage(errorBox, `打包失败：${error.message || "未知错误"}`);
      }
    }
  }
  async function deleteFile(clipId, fileIndex) {
    const clip = state.clips.find((item) => item.id === clipId);
    const file = clip && (clip.files || [])[Number(fileIndex)];
    if (!file || !window.confirm(`删除文件“${file.name}”？`)) return;
    const files = clip.files.filter((_, index) => index !== Number(fileIndex));
    if (!clip.text && files.length === 0) await window.ClipStore.remove(clip.id);
    else await window.ClipStore.put({ ...clip, files });
    announceSync();
    await refresh();
  }
  async function deleteEntry(id) {
    if (!window.confirm("立即焚毁整条记录？此操作无法撤销。")) return;
    await window.ClipStore.remove(id);
    announceSync();
    await refresh();
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    clearMessages();
    const text = textInput.value;
    const error = validateDraft(text, state.draftFiles);
    if (error) return showMessage(errorBox, error);
    saveButton.disabled = true;
    const createdAt = Date.now();
    try {
      await window.ClipStore.put({
        id: makeId(),
        text,
        files: fileRecords(state.draftFiles),
        createdAt,
        expiresAt: createdAt + TTL_MS,
      });
      form.reset();
      state.draftFiles = [];
      updateDraft();
      showMessage(statusBox, "已保存。24 小时后自动焚毁。");
      announceSync();
      await refresh();
    } catch (error) {
      const quota = error && (error.name === "QuotaExceededError" || error.name === "NS_ERROR_DOM_QUOTA_REACHED");
      showMessage(errorBox, quota
        ? "浏览器存储配额不足，未保存。请减少文件大小或清理旧记录。"
        : `保存失败：${error.message || "未知存储错误"}`);
    } finally {
      saveButton.disabled = false;
    }
  });
  fileInput.addEventListener("change", () => {
    clearMessages();
    const next = [...state.draftFiles, ...Array.from(fileInput.files || [])];
    const error = validateFiles(next);
    fileInput.value = "";
    if (error) return showMessage(errorBox, error);
    state.draftFiles = next;
    updateDraft();
  });
  draftList.addEventListener("click", (event) => {
    const button = event.target.closest(".draft-remove");
    if (!button) return;
    state.draftFiles.splice(Number(button.dataset.index), 1);
    updateDraft();
  });
  clipList.addEventListener("change", (event) => {
    const checkbox = event.target.closest(".clip-select");
    if (!checkbox) return;
    if (checkbox.checked) state.selected.add(checkbox.dataset.id);
    else state.selected.delete(checkbox.dataset.id);
    render();
  });
  clipList.addEventListener("click", async (event) => {
    const copy = event.target.closest(".clip-text, .copy-button");
    if (copy) return copyText(copy.dataset.id).catch((error) => showMessage(errorBox, `复制失败：${error.message}`));
    const fileDownload = event.target.closest(".file-download");
    if (fileDownload) return downloadFile(fileDownload.dataset.clipId, fileDownload.dataset.fileIndex);
    const fileDelete = event.target.closest(".file-delete");
    if (fileDelete) return deleteFile(fileDelete.dataset.clipId, fileDelete.dataset.fileIndex);
    const more = event.target.closest(".more-files");
    if (more) {
      if (state.expanded.has(more.dataset.id)) state.expanded.delete(more.dataset.id);
      else state.expanded.add(more.dataset.id);
      return render();
    }
    const entryDownload = event.target.closest(".entry-download");
    if (entryDownload) {
      const clip = state.clips.find((item) => item.id === entryDownload.dataset.id);
      return downloadZip(clip ? [clip] : [], "clipbrain-entry");
    }
    const remove = event.target.closest(".delete-button");
    if (remove) return deleteEntry(remove.dataset.id);
  });
  selectPage.addEventListener("change", () => {
    state.currentIds.forEach((id) => {
      if (selectPage.checked) state.selected.add(id);
      else state.selected.delete(id);
    });
    render();
  });
  downloadSelected.addEventListener("click", () => downloadZip(selectedClips(), "clipbrain-selected"));
  prevPage.addEventListener("click", () => { state.page -= 1; render(); });
  nextPage.addEventListener("click", () => { state.page += 1; render(); });
  textInput.addEventListener("input", updateDraft);
  if (channel) channel.addEventListener("message", refresh);
  window.addEventListener("storage", (event) => {
    if (event.key === CHANNEL_NAME) refresh();
  });
  setInterval(() => window.ClipView.updateCountdowns(state.clips), 1000);
  setInterval(async () => {
    const removed = await window.ClipStore.purgeExpired(Date.now());
    if (removed) {
      announceSync();
      await refresh();
    }
  }, 30000);
  updateDraft();
  refresh().catch((error) => showMessage(errorBox, `初始化失败：${error.message}`));
}());
