(function () {
  "use strict";

  const VISIBLE_FILES = 4;
  const TTL_MS = 24 * 60 * 60 * 1000;
  const LOW_FRACTION = 0.1;

  const clipList = document.querySelector("#clip-list");
  const emptyState = document.querySelector("#empty-state");
  const offlineState = document.querySelector("#offline-state");
  const draftList = document.querySelector("#draft-files");
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
    if (dot <= 0 || dot === clean.length - 1) return "文件";
    return clean.slice(dot + 1).toUpperCase().slice(0, 10);
  }

  // The badge already states the type, so the name drops its extension.
  function stem(name) {
    const clean = String(name || "unnamed");
    const dot = clean.lastIndexOf(".");
    return dot <= 0 ? clean : clean.slice(0, dot);
  }

  function formatRemaining(expiresAt) {
    if (expiresAt === null) return "不焚毁";
    const remaining = Math.max(0, expiresAt - Date.now());
    const hours = Math.floor(remaining / 3600000);
    const minutes = Math.floor((remaining % 3600000) / 60000);
    const seconds = Math.floor((remaining % 60000) / 1000);
    return [hours, minutes, seconds].map((v) => String(v).padStart(2, "0")).join(":");
  }

  function fileRow(clip, file) {
    const item = document.createElement("li");
    item.className = "filerow";
    const open = document.createElement("button");
    open.type = "button";
    open.className = "file-open";
    open.dataset.clipId = clip.id;
    open.dataset.fileId = file.fileId;
    const kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = extension(file.name);
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = stem(file.name);
    name.title = file.name;
    const size = document.createElement("span");
    size.className = "size";
    size.textContent = formatBytes(file.size);
    open.append(kind, name, size);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "file-delete";
    remove.dataset.clipId = clip.id;
    remove.dataset.fileId = file.fileId;
    remove.setAttribute("aria-label", "删除文件");
    remove.textContent = "×";
    item.append(open, remove);
    return item;
  }

  function renderFiles(list, clip, expanded) {
    list.replaceChildren();
    const files = clip.files || [];
    const shown = expanded ? files : files.slice(0, VISIBLE_FILES);
    for (const file of shown) list.append(fileRow(clip, file));
    if (files.length > VISIBLE_FILES) {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "more";
      button.dataset.id = clip.id;
      button.textContent = expanded
        ? "收起文件"
        : `还有 ${files.length - VISIBLE_FILES} 个文件 ↓`;
      item.append(button);
      list.append(item);
    }
  }

  function renderCard(clip, selected, expanded) {
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.id = clip.id;
    node.classList.toggle("is-favorite", clip.favorited);

    const tick = node.querySelector(".tick");
    tick.dataset.id = clip.id;
    tick.setAttribute("aria-checked", String(selected));

    node.querySelector(".badge").textContent = clip.favorited ? "★ 收藏" : "临时";

    const countdown = node.querySelector(".countdown");
    countdown.textContent = formatRemaining(clip.expiresAt);

    const ttl = node.querySelector(".ttl");
    if (clip.expiresAt === null) {
      ttl.hidden = true;
    } else {
      const fraction = Math.max(0, Math.min(1, (clip.expiresAt - Date.now()) / TTL_MS));
      ttl.classList.toggle("is-low", fraction <= LOW_FRACTION);
      ttl.firstElementChild.style.width = `${(fraction * 100).toFixed(1)}%`;
    }

    const text = node.querySelector(".clip-text");
    text.textContent = clip.text || "";
    text.dataset.id = clip.id;

    const star = node.querySelector(".mini-star");
    star.dataset.id = clip.id;
    star.setAttribute("aria-pressed", String(clip.favorited));
    star.textContent = clip.favorited ? "★ 已收藏" : "★ 收藏";

    const download = node.querySelector(".entry-download");
    download.dataset.id = clip.id;
    download.textContent = (clip.files || []).length ? "下载本条 ZIP" : "下载";

    node.querySelector(".delete-button").dataset.id = clip.id;
    renderFiles(node.querySelector(".clip-files"), clip, expanded);
    return node;
  }

  function renderBoard(state) {
    clipList.replaceChildren();
    for (const clip of state.clips) {
      clipList.append(renderCard(clip, state.selected.has(clip.id), state.expanded.has(clip.id)));
    }
    offlineState.hidden = !state.offline;
    emptyState.hidden = state.offline || state.clips.length > 0;
  }

  function renderDraft(files) {
    draftList.replaceChildren();
    files.forEach((file, index) => {
      const chip = document.createElement("span");
      chip.className = "chip";
      const kind = document.createElement("span");
      kind.className = "kind";
      kind.textContent = extension(file.name);
      const name = document.createElement("span");
      name.className = "name";
      name.textContent = stem(file.name || "unnamed");
      name.title = file.name || "unnamed";
      const size = document.createElement("span");
      size.className = "size";
      size.textContent = formatBytes(file.size);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chip-x";
      remove.dataset.index = String(index);
      remove.setAttribute("aria-label", "移除待上传文件");
      remove.textContent = "×";
      chip.append(kind, name, size, remove);
      draftList.append(chip);
    });
  }

  function updateCountdowns(clips) {
    for (const card of clipList.querySelectorAll(".card")) {
      const clip = clips.find((item) => item.id === card.dataset.id);
      if (clip) card.querySelector(".countdown").textContent = formatRemaining(clip.expiresAt);
    }
  }

  window.ClipView = Object.freeze({
    formatBytes,
    renderBoard,
    renderDraft,
    updateCountdowns,
  });
}());
