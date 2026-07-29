import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../clipboard");
const removedIds = [];
let copied = "";
globalThis.window = globalThis;
globalThis.confirm = () => true;
globalThis.ClipArchive = {
  MAX_ARCHIVE_BYTES: 1024 ** 3,
  measureClips(clips) {
    return clips.reduce((sum, clip) => sum + Number(clip.bytes || 0), 0);
  },
};
globalThis.ClipClient = {
  async removeMany(ids) {
    removedIds.push(...ids);
    return ids.length;
  },
};
globalThis.ClipDownloads = {
  async copyText(value) {
    copied = value;
  },
};
globalThis.ClipView = {
  formatBytes(bytes) {
    return `${bytes} B`;
  },
};

vm.runInThisContext(fs.readFileSync(path.join(root, "bulk.js"), "utf8"), {
  filename: "bulk.js",
});

const textOnly = [
  { id: "a", text: "alpha", files: [], bytes: 5 },
  { id: "b", text: "beta", files: [], bytes: 4 },
];
const withFile = [{ id: "c", text: "note", files: [{ name: "x.bin" }], bytes: 8 }];

const combined = ClipBulk.combineText(textOnly);
if (!combined.includes("alpha") || !combined.includes("beta")) {
  throw new Error("bulk text combination failed");
}
if (ClipBulk.hasFiles(textOnly)) throw new Error("text-only selection detected files");
if (!ClipBulk.hasFiles(withFile)) throw new Error("file selection was not detected");
ClipBulk.assertSelectionSize(textOnly);

let strictLimitPassed = false;
try {
  ClipBulk.assertSelectionSize([{ id: "limit", bytes: 1024 ** 3 }]);
} catch (error) {
  strictLimitPassed = error instanceof RangeError;
}
if (!strictLimitPassed) throw new Error("bulk 1 GiB strict limit was not enforced");

// Copy is its own action now, and it must say so when it leaves files behind
// rather than silently dropping them the way the merged button used to.
const copyMessage = await ClipBulk.copyAll(withFile);
if (copied !== "note") throw new Error("copyAll did not hand the text to the clipboard");
if (!copyMessage.includes("全部下载")) {
  throw new Error("copyAll did not warn that files were skipped");
}

const destroyed = await ClipBulk.destroy(textOnly);
if (destroyed.removed !== 2 || removedIds.join(",") !== "a,b") {
  throw new Error("bulk destroy did not submit the selected ids once");
}

console.log(JSON.stringify({
  combined,
  copyMessage,
  hasFilesTextOnly: ClipBulk.hasFiles(textOnly),
  hasFilesWithFile: ClipBulk.hasFiles(withFile),
  removedIds,
  strictLimitPassed,
}));
