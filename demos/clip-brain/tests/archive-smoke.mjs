import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, "../clipboard");
globalThis.window = globalThis;
vm.runInThisContext(fs.readFileSync(path.join(root, "archive.js"), "utf8"), {
  filename: "archive.js",
});

const output = process.argv[2];
if (!output) throw new Error("output zip path is required");

const clips = [{
  id: "smoke-clip",
  createdAt: Date.now(),
  text: "你好，Clip Brain\nhello",
  files: [
    { name: "report.txt", size: 3, lastModified: Date.now(), blob: new Blob(["abc"]) },
    { name: "../unsafe?.bin", size: 4, lastModified: Date.now(), blob: new Blob([new Uint8Array([1, 2, 3, 4])]) },
  ],
}];

const { entries, totalBytes } = ClipArchive.buildEntries(clips);
const chunks = [];
await ClipArchive.writeZip(entries, {
  async write(bytes) {
    chunks.push(Buffer.from(bytes));
  },
}, totalBytes);
fs.writeFileSync(output, Buffer.concat(chunks));

let strictLimitPassed = false;
try {
  ClipArchive.buildEntries([{
    id: "limit",
    text: "",
    files: [{ name: "full.bin", size: 1024 ** 3, blob: new Blob([]) }],
  }]);
} catch (error) {
  strictLimitPassed = error instanceof RangeError;
}
if (!strictLimitPassed) throw new Error("1 GiB strict archive limit was not enforced");

console.log(JSON.stringify({
  entries: entries.map((entry) => entry.name),
  totalBytes,
  strictLimitPassed,
}));
