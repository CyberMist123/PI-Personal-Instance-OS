(function () {
  "use strict";

  // Three separate actions, not one adaptive copy/download button: copying a
  // selection that contains files silently dropped them in the old design.
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
    if (!clips.length) throw new Error("这里还没有可操作的内容。");
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
        callbacks.onProgress(total ? Math.floor((done / total) * 100) : 100);
      },
    });
  }

  async function copyAll(clips) {
    if (!clips.length) throw new Error("这里还没有可操作的内容。");
    const text = combineText(clips);
    if (!text) throw new Error("选中的条目没有可复制的文字。");
    await window.ClipDownloads.copyText(text);
    const skipped = clips.filter((clip) => (clip.files || []).length).length;
    return skipped
      ? `已复制 ${clips.length} 条文字；${skipped} 条中的文件请用「全部下载」。`
      : `已复制 ${clips.length} 条文字。`;
  }

  async function downloadAll(clips, callbacks) {
    const result = await downloadZip(clips, "clipbrain-selected", callbacks);
    return downloadMessage(result);
  }

  async function destroy(clips) {
    if (!clips.length) throw new Error("这里还没有可操作的内容。");
    const removed = await window.ClipClient.removeMany(clips.map((clip) => clip.id));
    return { removed };
  }

  window.ClipBulk = Object.freeze({
    assertSelectionSize,
    combineText,
    copyAll,
    destroy,
    downloadAll,
    downloadMessage,
    downloadZip,
    hasFiles,
  });
}());
