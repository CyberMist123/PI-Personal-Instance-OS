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
  const bulkAction = document.querySelector("#bulk-action");
  const bulkDestroy = document.querySelector("#bulk-destroy");
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL_NAME) : null;
  const state = {
    clips: [],
    selected: new Set(),
    expanded: new Set(),
    page: 1,
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
    const selected = selectedClips();
    const result = window.ClipView.renderBoard({
      clips: state.clips,
      selected: state.selected,
      expanded: state.expanded,
      page: state.page,
      selectedBytes: window.ClipArchive.measureClips(selected),
      selectedHasFiles: window.ClipBulk.hasFiles(selected),
    });
    state.page = result.page;
  }

  async function refresh() {
    await window.ClipStore.purgeExpired(Date.now());
    state.clips = await window.ClipStore.list();
    const existingIds = new Set(state.clips.map((clip) => clip.id));
    for (const id of state.selected) if (!existingIds.has(id)) state.selected.delete(id);
    for (const id of state.expanded) if (!existingIds.has(id)) state.expanded.delete(id);
    render();
  }

  function zipCallbacks() {
    return {
      onProgress(percent) {
        window.ClipSelectionMenu.setProgress(percent);
        showMessage(statusBox, `正在本地打包：${percent}%`);
      },
    };
  }

  async function downloadEntry(clip) {
    clearMessages();
    try {
      const result = await window.ClipBulk.downloadZip([clip], "clipbrain-entry", {
        onProgress(percent) {
          showMessage(statusBox, `正在本地打包本条：${percent}%`);
        },
      });
      showMessage(statusBox, window.ClipBulk.downloadMessage(result));
    } catch (error) {
      if (error && error.name === "AbortError") showMessage(statusBox, "已取消本地打包。");
      else showMessage(errorBox, `打包失败：${error.message || "未知错误"}`);
    }
  }

  async function runBulkAction() {
    clearMessages();
    const clips = selectedClips();
    const createsZip = window.ClipBulk.hasFiles(clips);
    if (createsZip) window.ClipSelectionMenu.setProgress(0);
    try {
      const result = await window.ClipBulk.copyOrDownload(clips, zipCallbacks());
      showMessage(statusBox, result.message);
    } catch (error) {
      if (error && error.name === "AbortError") showMessage(statusBox, "已取消本地打包。");
      else showMessage(errorBox, `批量操作失败：${error.message || "未知错误"}`);
    } finally {
      if (createsZip) window.ClipSelectionMenu.clearProgress();
    }
  }

  async function runBulkDestroy() {
    clearMessages();
    const clips = selectedClips();
    if (!window.ClipSelectionMenu.armDestroy(clips.length)) return;
    try {
      const result = await window.ClipBulk.destroy(clips);
      state.selected.clear();
      announceSync();
      await refresh();
      showMessage(statusBox, `已焚毁 ${result.removed} 条记录。`);
    } catch (error) {
      window.ClipSelectionMenu.resetDestroy();
      showMessage(errorBox, `批量焚毁失败：${error.message || "未知错误"}`);
    }
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
    if (copy) {
      const clip = state.clips.find((item) => item.id === copy.dataset.id);
      return window.ClipDownloads.copyText(clip && clip.text)
        .then(() => showMessage(statusBox, "文本已复制。"))
        .catch((error) => showMessage(errorBox, `复制失败：${error.message}`));
    }

    const fileDownload = event.target.closest(".file-download");
    if (fileDownload) {
      const clip = state.clips.find((item) => item.id === fileDownload.dataset.clipId);
      const file = clip && (clip.files || [])[Number(fileDownload.dataset.fileIndex)];
      return file ? window.ClipDownloads.downloadFile(file) : undefined;
    }

    const fileDelete = event.target.closest(".file-delete");
    if (fileDelete) {
      const clip = state.clips.find((item) => item.id === fileDelete.dataset.clipId);
      const removed = await window.ClipDestructive.deleteFile(fileDelete, clip);
      if (removed) {
        announceSync();
        await refresh();
      }
      return;
    }

    const more = event.target.closest(".more-files");
    if (more) {
      if (state.expanded.has(more.dataset.id)) state.expanded.delete(more.dataset.id);
      else state.expanded.add(more.dataset.id);
      return render();
    }

    const entryDownload = event.target.closest(".entry-download");
    if (entryDownload) {
      const clip = state.clips.find((item) => item.id === entryDownload.dataset.id);
      return clip ? downloadEntry(clip) : undefined;
    }

    const remove = event.target.closest(".delete-button");
    if (remove) {
      const removed = await window.ClipDestructive.deleteEntry(remove);
      if (removed) {
        announceSync();
        await refresh();
      }
    }
  });

  bulkAction.addEventListener("click", runBulkAction);
  bulkDestroy.addEventListener("click", runBulkDestroy);
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
