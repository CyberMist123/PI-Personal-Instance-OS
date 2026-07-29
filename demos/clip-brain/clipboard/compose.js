(function () {
  "use strict";

  const TEXT_LIMIT = 10000;
  const FILE_LIMIT = 20;
  const ENTRY_BYTE_LIMIT = 1024 ** 3;

  const form = document.querySelector("#clip-form");
  const textInput = document.querySelector("#clip-text");
  const fileInput = document.querySelector("#clip-files");
  const pickButton = document.querySelector("#pick-files");
  const textCount = document.querySelector("#text-count");
  const saveButton = document.querySelector("#save-button");
  const draftList = document.querySelector("#draft-files");
  const errorBox = document.querySelector("#form-error");
  const statusBox = document.querySelector("#form-status");

  let draftFiles = [];
  let onSaved = null;

  function countUnicode(value) {
    return Array.from(value).length;
  }

  function show(node, message) {
    node.textContent = message;
    node.hidden = !message;
  }

  function clearMessages() {
    show(errorBox, "");
    show(statusBox, "");
  }

  function refreshDraft() {
    const characters = countUnicode(textInput.value);
    textCount.textContent = `${characters} / ${TEXT_LIMIT}`;
    textCount.classList.toggle("is-over", characters > TEXT_LIMIT);
    window.ClipView.renderDraft(draftFiles);
  }

  function totalBytes(files, text) {
    return files.reduce((sum, file) => sum + file.size, 0) + new Blob([text]).size;
  }

  function validate(text, files) {
    if (!text.trim() && files.length === 0) return "至少粘贴一段文字或选择一个文件。";
    if (countUnicode(text) > TEXT_LIMIT) return `文字超过 ${TEXT_LIMIT} 个 Unicode 字符。`;
    if (files.length > FILE_LIMIT) return `每条最多允许 ${FILE_LIMIT} 个文件。`;
    if (totalBytes(files, text) >= ENTRY_BYTE_LIMIT) return "每条合计必须严格小于 1 GiB。";
    return "";
  }

  function addFiles(incoming) {
    clearMessages();
    const next = [...draftFiles, ...incoming];
    const problem = validate(textInput.value, next);
    if (problem && next.length > draftFiles.length) {
      const fatal = next.length > FILE_LIMIT || totalBytes(next, textInput.value) >= ENTRY_BYTE_LIMIT;
      if (fatal) return show(errorBox, problem);
    }
    draftFiles = next;
    refreshDraft();
  }

  function describe(error) {
    const codes = {
      quota_exceeded: "空间已满（2 GB 上限）。先焚毁一些内容再上传。",
      entry_too_large: "这一条超过 1 GiB 上限。",
      too_many_files: `每条最多允许 ${FILE_LIMIT} 个文件。`,
      text_too_long: `文字超过 ${TEXT_LIMIT} 个 Unicode 字符。`,
      invalid_origin: "来源校验失败，请刷新页面后重试。",
      unauthorized: "登录态已失效，请刷新页面。",
    };
    return codes[error && error.code] || (error && error.message) || "未知错误";
  }

  async function submit(event) {
    event.preventDefault();
    clearMessages();
    const text = textInput.value;
    const problem = validate(text, draftFiles);
    if (problem) return show(errorBox, problem);

    saveButton.disabled = true;
    try {
      await window.ClipClient.create({ text, files: draftFiles });
      form.reset();
      draftFiles = [];
      refreshDraft();
      show(statusBox, "已上传。24 小时后自动焚毁。");
      if (onSaved) await onSaved();
    } catch (error) {
      // Never a fake success: the entry only exists if the server said so.
      show(errorBox, `上传失败：${describe(error)}`);
    } finally {
      saveButton.disabled = false;
    }
  }

  function init(handlers) {
    onSaved = handlers && handlers.onSaved;
    form.addEventListener("submit", submit);
    textInput.addEventListener("input", refreshDraft);
    pickButton.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => {
      addFiles(Array.from(fileInput.files || []));
      fileInput.value = "";
    });
    draftList.addEventListener("click", (event) => {
      const button = event.target.closest(".chip-x");
      if (!button) return;
      draftFiles.splice(Number(button.dataset.index), 1);
      refreshDraft();
    });

    // No big dashed dropzone: the text area itself is the drop target.
    for (const type of ["dragenter", "dragover"]) {
      textInput.addEventListener(type, (event) => {
        event.preventDefault();
        textInput.classList.add("is-dropping");
      });
    }
    for (const type of ["dragleave", "drop"]) {
      textInput.addEventListener(type, () => textInput.classList.remove("is-dropping"));
    }
    textInput.addEventListener("drop", (event) => {
      const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
      if (!files.length) return;
      event.preventDefault();
      addFiles(files);
    });

    refreshDraft();
  }

  window.ClipCompose = Object.freeze({ init, clearMessages, show, errorBox, statusBox });
}());
