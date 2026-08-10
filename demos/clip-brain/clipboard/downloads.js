(function () {
  "use strict";

  async function copyText(value) {
    if (!value) throw new Error("没有可复制的文字。");
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

  // A plain <a href> cannot carry the Authorization header the download route
  // requires, so a single file is fetched with the session bearer and handed to
  // the browser as an object URL. Bulk ZIPs still stream (see clipboard-client).
  async function downloadFile(file) {
    if (!file) throw new Error("文件不存在或已经被删除。");
    const blob = await window.ClipClient.fileBlob(file);
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name || "unnamed-file";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 60000);
  }

  window.ClipDownloads = Object.freeze({
    copyText,
    downloadFile,
  });
}());
