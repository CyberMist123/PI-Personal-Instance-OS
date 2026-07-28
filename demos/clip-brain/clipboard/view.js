(function () {
  "use strict";

  const PAGE_SIZE = 12;
  const clipList = document.querySelector("#clip-list");
  const clipTotal = document.querySelector("#clip-total");
  const emptyState = document.querySelector("#empty-state");
  const pageLabel = document.querySelector("#page-label");
  const prevPage = document.querySelector("#prev-page");
  const nextPage = document.querySelector("#next-page");
  const selectPage = document.querySelector("#select-page");
  const selectionText = document.querySelector("#selection-text");
  const downloadSelected = document.querySelector("#download-selected");
  const draftList = document.querySelector("#draft-files");
  const fileSummary = document.querySelector("#file-summary");
  const template = document.querySelector("#clip-template");

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

  function extension(name) {
    const clean = String(name || "");
    const dot = clean.lastIndexOf(".");
    if (dot <= 0 || dot === clean.length - 1) return "无后缀";
    return clean.slice(dot + 1).toUpperCase().slice(0, 10);
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

  function renderDraft(files) {
    draftList.replaceChildren();
    const total = files.reduce((sum, file) => sum + file.size, 0);
    fileSummary.textContent = files.length
      ? `${files.length} 个文件 · ${formatBytes(total)}`
      : "尚未选择文件";

    files.forEach((file, index) => {
      const item = document.createElement("li");
      item.className = "draft-file";
      item.innerHTML = `
        <span class="file-kind">${extension(file.name)}</span>
        <span class="file-name" title=""></span>
        <span class="file-size">${formatBytes(file.size)}</span>
        <button class="draft-remove" type="button" data-index="${index}" aria-label="移除待上传文件">×</button>
      `;
      item.querySelector(".file-name").textContent = file.name || "unnamed-file";
      item.querySelector(".file-name").title = file.name || "unnamed-file";
      draftList.append(item);
    });
  }

  function renderFileRows(list, clip, expanded) {
    list.replaceChildren();
    const files = clip.files || [];
    const visibleFiles = expanded ? files : files.slice(0, 4);

    visibleFiles.forEach((file, index) => {
      const item = document.createElement("li");
      item.className = "clip-file";
      item.innerHTML = `
        <button class="file-download" type="button" data-clip-id="" data-file-index="${index}">
          <span class="file-kind">${extension(file.name)}</span>
          <span class="file-name"></span>
          <span class="file-size">${formatBytes(file.size)}</span>
        </button>
        <details class="file-menu">
          <summary aria-label="文件操作">•••</summary>
          <button class="file-delete" type="button" data-clip-id="" data-file-index="${index}">删除文件</button>
        </details>
      `;
      const download = item.querySelector(".file-download");
      const remove = item.querySelector(".file-delete");
      download.dataset.clipId = clip.id;
      remove.dataset.clipId = clip.id;
      item.querySelector(".file-name").textContent = file.name || "unnamed-file";
      list.append(item);
    });

    if (files.length > 4) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "more-files";
      button.dataset.id = clip.id;
      button.textContent = expanded ? "收起文件" : `还有 ${files.length - 4} 个文件`;
      item.append(button);
      list.append(item);
    }
  }

  function renderCard(clip, selected, expanded) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.id = clip.id;
    node.querySelector(".clip-select").checked = selected;
    node.querySelector(".clip-select").dataset.id = clip.id;
    node.querySelector(".clip-created").textContent = formatCreated(clip.createdAt);
    node.querySelector(".clip-countdown").textContent = formatRemaining(clip.expiresAt);

    const textButton = node.querySelector(".clip-text");
    textButton.textContent = clip.text || "";
    textButton.dataset.id = clip.id;

    const copyButton = node.querySelector(".copy-button");
    copyButton.dataset.id = clip.id;
    copyButton.hidden = !clip.text;

    node.querySelector(".entry-download").dataset.id = clip.id;
    node.querySelector(".delete-button").dataset.id = clip.id;
    renderFileRows(node.querySelector(".clip-files"), clip, expanded);
    return node;
  }

  function renderBoard(state) {
    const pageCount = Math.max(1, Math.ceil(state.clips.length / PAGE_SIZE));
    const page = Math.min(Math.max(1, state.page), pageCount);
    const start = (page - 1) * PAGE_SIZE;
    const current = state.clips.slice(start, start + PAGE_SIZE);
    clipList.replaceChildren();

    current.forEach((clip) => {
      clipList.append(renderCard(
        clip,
        state.selected.has(clip.id),
        state.expanded.has(clip.id),
      ));
    });

    emptyState.hidden = state.clips.length > 0;
    clipTotal.textContent = `${state.clips.length} 条`;
    pageLabel.textContent = `${page} / ${pageCount}`;
    prevPage.disabled = page <= 1;
    nextPage.disabled = page >= pageCount;

    const currentIds = current.map((clip) => clip.id);
    const selectedHere = currentIds.filter((id) => state.selected.has(id)).length;
    selectPage.checked = currentIds.length > 0 && selectedHere === currentIds.length;
    selectPage.indeterminate = selectedHere > 0 && selectedHere < currentIds.length;

    selectionText.textContent = state.selected.size
      ? `已选 ${state.selected.size} 条 · ${formatBytes(state.selectedBytes)}`
      : "未选择条目";
    downloadSelected.disabled = !state.selected.size || state.selectedBytes >= window.ClipArchive.MAX_ARCHIVE_BYTES;
    downloadSelected.title = state.selectedBytes >= window.ClipArchive.MAX_ARCHIVE_BYTES
      ? "选中内容必须严格小于 1 GiB"
      : "";
    return { page, pageCount, currentIds };
  }

  function updateCountdowns(clips) {
    for (const card of clipList.querySelectorAll(".clip-card")) {
      const clip = clips.find((item) => item.id === card.dataset.id);
      if (clip) card.querySelector(".clip-countdown").textContent = formatRemaining(clip.expiresAt);
    }
  }

  window.ClipView = Object.freeze({
    PAGE_SIZE,
    formatBytes,
    renderBoard,
    renderDraft,
    updateCountdowns,
  });
}());
