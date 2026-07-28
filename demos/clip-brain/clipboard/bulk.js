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

  function downloadMessage(result) {
    const size = window.ClipView.formatBytes(result.totalBytes);
    if (result.delivery === "browser") {
      return `ZIP 已加入浏览器下载：${result.fileCount} 个文件，${size}。`;
    }
    return `ZIP 已直接保存到磁盘：${result.fileCount} 个文件，${size}；不会出现在浏览器下载栏。`;
  }

  async function downloadZip(clips, prefix, callbacks) {
    assertSelectionSize(clips);
    return window.ClipArchiveOutput.saveClipsAsZip(clips, {
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
        message: downloadMessage(result),
        result,
      };
    }

    await window.ClipDownloads.copyText(combineText(clips));
    return {
      mode: "copy",
      message: `已复制 ${clips.length} 条文本。`,
    };
  }

  async function destroy(clips) {
    if (!clips.length) throw new Error("请先选择至少一条内容。");
    await window.ClipStore.removeMany(clips.map((clip) => clip.id));
    return { removed: clips.length };
  }

  window.ClipBulk = Object.freeze({
    assertSelectionSize,
    combineText,
    copyOrDownload,
    destroy,
    downloadMessage,
    downloadZip,
    hasFiles,
  });
}());
