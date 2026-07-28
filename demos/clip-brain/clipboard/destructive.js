(function () {
  "use strict";

  const armedButtons = new WeakMap();

  function restore(button) {
    const armed = armedButtons.get(button);
    if (!armed) return;
    window.clearTimeout(armed.timer);
    button.textContent = armed.label;
    button.classList.remove("is-armed");
    armedButtons.delete(button);
  }

  function arm(button, label) {
    if (armedButtons.has(button)) {
      restore(button);
      return true;
    }

    const original = button.textContent;
    button.textContent = label;
    button.classList.add("is-armed");
    const timer = window.setTimeout(() => restore(button), 3200);
    armedButtons.set(button, { label: original, timer });
    return false;
  }

  async function deleteFile(button, clip) {
    if (!arm(button, "再点一次删除")) return false;
    const index = Number(button.dataset.fileIndex);
    const file = clip && (clip.files || [])[index];
    if (!file) return false;

    const files = clip.files.filter((_, fileIndex) => fileIndex !== index);
    if (!clip.text && files.length === 0) {
      await window.ClipStore.remove(clip.id);
    } else {
      await window.ClipStore.put({ ...clip, files });
    }
    return true;
  }

  async function deleteEntry(button) {
    if (!arm(button, "再点一次焚毁")) return false;
    await window.ClipStore.remove(button.dataset.id);
    return true;
  }

  window.ClipDestructive = Object.freeze({
    arm,
    deleteEntry,
    deleteFile,
    restore,
  });
}());
