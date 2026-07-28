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

  function downloadFile(file) {
    if (!file || !file.blob) throw new Error("文件不存在或已经被删除。");
    const url = URL.createObjectURL(file.blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = file.name || "unnamed-file";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  window.ClipDownloads = Object.freeze({
    copyText,
    downloadFile,
  });
}());
