(function () {
  "use strict";

  const BROWSER_DOWNLOAD_BYTES = 256 * 1024 ** 2;

  function downloadBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = name;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  async function saveThroughBrowser(entries, totalBytes, options) {
    const chunks = [];
    await window.ClipArchive.writeZip(entries, {
      async write(bytes) {
        chunks.push(bytes.slice());
      },
    }, totalBytes, options && options.onProgress);

    const blob = new Blob(chunks, { type: "application/zip" });
    downloadBlob(blob, options.name);
    return {
      delivery: "browser",
      fileCount: entries.length,
      outputBytes: blob.size,
      totalBytes,
    };
  }

  async function saveDirectly(entries, totalBytes, options) {
    if (typeof window.showSaveFilePicker !== "function") {
      throw new Error("内容超过 256 MiB，当前浏览器又不支持直接流式保存。请使用最新版 Chrome 或 Edge。");
    }

    const handle = await window.showSaveFilePicker({
      suggestedName: options.name,
      types: [{
        description: "ZIP archive",
        accept: { "application/zip": [".zip"] },
      }],
    });
    const writable = await handle.createWritable();

    try {
      await window.ClipArchive.writeZip(
        entries,
        writable,
        totalBytes,
        options && options.onProgress,
      );
      await writable.close();
    } catch (error) {
      try {
        await writable.abort();
      } catch (_) {
        // Preserve the original write error.
      }
      throw error;
    }

    return {
      delivery: "direct",
      fileCount: entries.length,
      outputBytes: null,
      totalBytes,
    };
  }

  async function saveClipsAsZip(clips, options) {
    const settings = {
      name: "clipbrain.zip",
      ...(options || {}),
    };
    const { entries, totalBytes } = window.ClipArchive.buildEntries(clips);

    if (totalBytes <= BROWSER_DOWNLOAD_BYTES) {
      return saveThroughBrowser(entries, totalBytes, settings);
    }
    return saveDirectly(entries, totalBytes, settings);
  }

  window.ClipArchiveOutput = Object.freeze({
    BROWSER_DOWNLOAD_BYTES,
    saveClipsAsZip,
  });
}());
