(function () {
  "use strict";

  const POLL_MS = 5000;
  const CHANNEL_NAME = "clip-brain-sync";

  const clipList = document.querySelector("#clip-list");
  const channel = "BroadcastChannel" in window ? new BroadcastChannel(CHANNEL_NAME) : null;

  const state = {
    clips: [],
    selected: new Set(),
    expanded: new Set(),
    topics: [],
    offline: false,
  };
  let pollTimer = null;
  let refreshing = null;

  function message(text) {
    window.ClipCompose.show(window.ClipCompose.statusBox, text);
  }

  function failure(text) {
    window.ClipCompose.show(window.ClipCompose.errorBox, text);
  }

  // No selection means "everything currently listed" — never an empty request
  // that the server would have to interpret as "all".
  function targets() {
    if (!state.selected.size) return state.clips;
    return state.clips.filter((clip) => state.selected.has(clip.id));
  }

  function deriveTopics(filters) {
    if (filters.topic) return state.topics;
    const counts = new Map();
    for (const clip of state.clips) {
      if (!clip.topic) continue;
      counts.set(clip.topic, (counts.get(clip.topic) || 0) + 1);
    }
    return [...counts.entries()].map(([topic, count]) => ({ topic, count }));
  }

  function render() {
    window.ClipView.renderBoard(state);
    window.ClipToolbar.setCount(state.selected.size, state.clips.length);
  }

  async function refresh() {
    if (refreshing) return refreshing;
    const filters = window.ClipToolbar.filters();
    refreshing = (async () => {
      try {
        const result = await window.ClipClient.list({
          view: filters.view,
          topic: filters.topic,
          type: filters.type,
          q: filters.query,
        });
        state.clips = result.clips;
        state.offline = false;
        for (const id of [...state.selected]) {
          if (!state.clips.some((clip) => clip.id === id)) state.selected.delete(id);
        }
        for (const id of [...state.expanded]) {
          if (!state.clips.some((clip) => clip.id === id)) state.expanded.delete(id);
        }
        state.topics = deriveTopics(filters);
        window.ClipToolbar.setTopics(state.topics);
        try {
          window.ClipToolbar.setQuota(await window.ClipClient.usage());
        } catch (_) {
          window.ClipToolbar.setQuota(null);
        }
      } catch (error) {
        // Show the truth instead of a stale list that looks live.
        state.clips = [];
        state.offline = true;
        failure(`读取失败：${error.message || "后端未连接"}`);
      } finally {
        render();
        refreshing = null;
      }
    })();
    return refreshing;
  }

  function announce() {
    if (channel) channel.postMessage({ type: "changed" });
  }

  async function mutate(work, done) {
    window.ClipCompose.clearMessages();
    try {
      const text = await work();
      announce();
      await refresh();
      if (text) message(text);
      if (done) done();
    } catch (error) {
      if (error && error.name === "AbortError") return message("已取消。");
      failure(error.message || "操作失败");
    }
  }

  function findClip(id) {
    return state.clips.find((clip) => clip.id === id);
  }

  clipList.addEventListener("click", (event) => {
    const tick = event.target.closest(".tick");
    if (tick) {
      const id = tick.dataset.id;
      if (state.selected.has(id)) state.selected.delete(id);
      else state.selected.add(id);
      window.ClipToolbar.resetDestroy();
      return render();
    }

    const more = event.target.closest(".more");
    if (more) {
      const id = more.dataset.id;
      if (state.expanded.has(id)) state.expanded.delete(id);
      else state.expanded.add(id);
      return render();
    }

    const open = event.target.closest(".file-open");
    if (open) {
      const clip = findClip(open.dataset.clipId);
      const file = clip && clip.files.find((f) => f.fileId === open.dataset.fileId);
      if (!file) return;
      return window.ClipDownloads.downloadFile(file)
        .catch((error) => failure(`下载失败：${error.message}`));
    }

    const removeFile = event.target.closest(".file-delete");
    if (removeFile) {
      if (!window.ClipDestructive.arm(removeFile, "×?")) return;
      return mutate(async () => {
        await window.ClipClient.removeFile(removeFile.dataset.clipId, removeFile.dataset.fileId);
        return "已删除该文件。";
      });
    }

    const star = event.target.closest(".mini-star");
    if (star) {
      const clip = findClip(star.dataset.id);
      if (!clip) return;
      return mutate(async () => {
        await window.ClipClient.patch(clip.id, { favorite: !clip.favorited });
        return clip.favorited ? "已取消收藏，重新开始 24 小时倒计时。" : "已收藏，不再自动焚毁。";
      });
    }

    const download = event.target.closest(".entry-download");
    if (download) {
      const clip = findClip(download.dataset.id);
      if (!clip) return;
      return mutate(async () => {
        const result = await window.ClipBulk.downloadZip([clip], "clipbrain-entry", {
          onProgress: (percent) => message(`正在本地打包：${percent}%`),
        });
        return window.ClipBulk.downloadMessage(result);
      });
    }

    const destroy = event.target.closest(".delete-button");
    if (destroy) {
      if (!window.ClipDestructive.arm(destroy, "再点一次焚毁")) return;
      return mutate(async () => {
        await window.ClipClient.remove(destroy.dataset.id);
        return "已焚毁 1 条。";
      });
    }
  });

  // Double-click copies the body; there is no copy button anywhere.
  clipList.addEventListener("dblclick", (event) => {
    const body = event.target.closest(".clip-text");
    if (!body) return;
    const clip = findClip(body.dataset.id);
    if (!clip || !clip.text) return;
    window.ClipDownloads.copyText(clip.text)
      .then(() => message("文本已复制。"))
      .catch((error) => failure(`复制失败：${error.message}`));
  });

  function startPolling() {
    if (pollTimer) return;
    pollTimer = window.setInterval(refresh, POLL_MS);
  }

  function stopPolling() {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return stopPolling();
    startPolling();
    refresh();
  });
  window.addEventListener("focus", refresh);
  if (channel) channel.addEventListener("message", refresh);

  window.setInterval(() => window.ClipView.updateCountdowns(state.clips), 1000);

  window.ClipToolbar.init({
    onFilters: () => {
      state.selected.clear();
      refresh();
    },
    onCopyAll: () => mutate(async () => window.ClipBulk.copyAll(targets())),
    onDownloadAll: () => mutate(async () => window.ClipBulk.downloadAll(targets(), {
      onProgress: (percent) => message(`正在本地打包：${percent}%`),
    })),
    onDestroyAll: (arm) => {
      const chosen = targets();
      if (!chosen.length) return failure("这里还没有可操作的内容。");
      if (!arm(chosen.length)) return;
      mutate(async () => {
        const result = await window.ClipBulk.destroy(chosen);
        state.selected.clear();
        return `已焚毁 ${result.removed} 条。`;
      });
    },
  });

  window.ClipCompose.init({ onSaved: async () => { announce(); await refresh(); } });

  window.ClipAuth.ready().then((ok) => {
    if (!ok) return;
    startPolling();
    refresh();
  });
}());
