(function () {
  "use strict";

  function hasFiles(clips) {
    return clips.some((clip) => (clip.files || []).length > 0);
  }

  function combineText(clips) {
    return clips
      .filter((clip) => clip.text)
      .map((clip, index) => {
        const divider = clips.length > 1 ? `--- Clip ${index + 1} ---\n` : "";
        return `${divider}${clip.text}`;
      })
      .join("\n\n");
  }

  function assertSelectionSize(clips) {
    if (!clips.length) throw new Error("请先选择至少一条内容。");
    const bytes = window.ClipArchive.measureClips(clips);
    if (bytes >= window.ClipArchive.MAX_ARCHIVE_BYTES) {
      throw new RangeError("选中内容合计必须严格小于 1 GiB。");
    }
    return bytes;
  }

  async function copyText(value) {
    if (!value) throw new Error("所选条目没有可复制的文字。");
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
      return;
    }

    const fallback = document.createElement("textarea");
    fallback.value = value;
    fallback.style.position = "fixed";
    fallback.style.opacity = "0";
    document.body.append(fallback);
    fallback.select();
    const copied = document.execCommand("copy");
    fallback.remove();
    if (!copied) throw new Error("浏览器拒绝复制。");
  }

  async function downloadZip(clips, prefix, callbacks) {
    assertSelectionSize(clips);
    return window.ClipArchive.saveClipsAsZip(clips, {
      name: `${prefix}-${Date.now()}.zip`,
      onProgress(done, total) {
        if (!callbacks || !callbacks.onProgress) return;
        const percent = total ? Math.floor((done / total) * 100) : 100;
        callbacks.onProgress(percent);
      },
    });
  }

  async function copyOrDownload(clips, callbacks) {
    assertSelectionSize(clips);
    if (hasFiles(clips)) {
      const result = await downloadZip(clips, "clipbrain-selected", callbacks);
      return {
        mode: "download",
        message: `ZIP 已保存：${result.fileCount} 个文件，${window.ClipView.formatBytes(result.totalBytes)}。`,
      };
    }

    await copyText(combineText(clips));
    return {
      mode: "copy",
      message: `已复制 ${clips.length} 条文本。`,
    };
  }

  async function destroy(clips) {
    if (!clips.length) throw new Error("请先选择至少一条内容。");
    const confirmed = window.confirm(`立即焚毁选中的 ${clips.length} 条记录？此操作无法撤销。`);
    if (!confirmed) return { cancelled: true, removed: 0 };
    await window.ClipStore.removeMany(clips.map((clip) => clip.id));
    return { cancelled: false, removed: clips.length };
  }

  window.ClipBulk = Object.freeze({
    assertSelectionSize,
    combineText,
    copyOrDownload,
    destroy,
    downloadZip,
    hasFiles,
  });
}());
