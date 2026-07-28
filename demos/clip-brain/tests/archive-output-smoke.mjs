import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";

const root = path.resolve(import.meta.dirname, "../clipboard");
let clicked = false;
let directWrites = 0;
globalThis.window = globalThis;
globalThis.setTimeout = (callback) => { callback(); return 0; };
globalThis.document = {
  body: {
    append() {},
  },
  createElement() {
    return {
      click() { clicked = true; },
      remove() {},
      set href(value) { this._href = value; },
      set download(value) { this._download = value; },
    };
  },
};
globalThis.URL.createObjectURL = () => "blob:test";
globalThis.URL.revokeObjectURL = () => {};
globalThis.ClipArchive = {
  buildEntries(clips) {
    return {
      entries: [{ name: "clip/file.txt", blob: new Blob(["abc"]) }],
      totalBytes: clips[0].bytes,
    };
  },
  async writeZip(entries, writable, totalBytes, onProgress) {
    await writable.write(new Uint8Array([1, 2, 3]));
    if (onProgress) onProgress(totalBytes, totalBytes);
  },
};

vm.runInThisContext(fs.readFileSync(path.join(root, "archive-output.js"), "utf8"), {
  filename: "archive-output.js",
});

const browserResult = await ClipArchiveOutput.saveClipsAsZip(
  [{ bytes: 1024 }],
  { name: "small.zip" },
);
if (!clicked || browserResult.delivery !== "browser") {
  throw new Error("small ZIP did not use browser download");
}

globalThis.showSaveFilePicker = async () => ({
  async createWritable() {
    return {
      async write() { directWrites += 1; },
      async close() {},
      async abort() {},
    };
  },
});

const directResult = await ClipArchiveOutput.saveClipsAsZip(
  [{ bytes: 257 * 1024 ** 2 }],
  { name: "large.zip" },
);
if (directResult.delivery !== "direct" || directWrites !== 1) {
  throw new Error("large ZIP did not use direct streaming save");
}

console.log(JSON.stringify({
  browserDelivery: browserResult.delivery,
  directDelivery: directResult.delivery,
  threshold: ClipArchiveOutput.BROWSER_DOWNLOAD_BYTES,
}));
