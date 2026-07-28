(function () {
  "use strict";

  const MAX_ARCHIVE_BYTES = 1024 ** 3;
  const encoder = new TextEncoder();
  const crcTable = buildCrcTable();

  function buildCrcTable() {
    const table = new Uint32Array(256);
    for (let index = 0; index < 256; index += 1) {
      let value = index;
      for (let bit = 0; bit < 8; bit += 1) {
        value = (value & 1) ? (0xedb88320 ^ (value >>> 1)) : (value >>> 1);
      }
      table[index] = value >>> 0;
    }
    return table;
  }

  function updateCrc(crc, bytes) {
    let value = crc;
    for (const byte of bytes) {
      value = crcTable[(value ^ byte) & 0xff] ^ (value >>> 8);
    }
    return value >>> 0;
  }

  function view(size) {
    const bytes = new Uint8Array(size);
    return { bytes, data: new DataView(bytes.buffer) };
  }

  function dosDateTime(timestamp) {
    const date = new Date(timestamp || Date.now());
    const year = Math.max(1980, date.getFullYear());
    return {
      time: ((date.getHours() & 31) << 11)
        | ((date.getMinutes() & 63) << 5)
        | ((Math.floor(date.getSeconds() / 2)) & 31),
      date: (((year - 1980) & 127) << 9)
        | (((date.getMonth() + 1) & 15) << 5)
        | (date.getDate() & 31),
    };
  }

  function cleanSegment(value, fallback) {
    const cleaned = String(value || "")
      .replace(/[\u0000-\u001f\u007f<>:"/\\|?*]/g, "_")
      .replace(/[. ]+$/g, "")
      .trim();
    return (cleaned || fallback).slice(0, 180);
  }

  function uniqueName(rawName, used) {
    const safe = cleanSegment(rawName, "unnamed-file");
    const dot = safe.lastIndexOf(".");
    const stem = dot > 0 ? safe.slice(0, dot) : safe;
    const suffix = dot > 0 ? safe.slice(dot) : "";
    let candidate = safe;
    let counter = 2;
    while (used.has(candidate.toLowerCase())) {
      candidate = `${stem} (${counter})${suffix}`;
      counter += 1;
    }
    used.add(candidate.toLowerCase());
    return candidate;
  }

  function folderName(clip, position) {
    const date = new Date(clip.createdAt || Date.now());
    const stamp = [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"),
      "-",
      String(date.getHours()).padStart(2, "0"),
      String(date.getMinutes()).padStart(2, "0"),
      String(date.getSeconds()).padStart(2, "0"),
    ].join("");
    return `clip-${String(position + 1).padStart(2, "0")}-${stamp}-${String(clip.id || "").slice(0, 8)}`;
  }

  function measureClips(clips) {
    return clips.reduce((total, clip) => {
      const textBytes = clip.text ? encoder.encode(clip.text).byteLength : 0;
      const fileBytes = (clip.files || []).reduce((sum, file) => sum + Number(file.size || 0), 0);
      return total + textBytes + fileBytes;
    }, 0);
  }

  function buildEntries(clips) {
    const totalBytes = measureClips(clips);
    if (!clips.length) throw new Error("请先选择至少一条内容。");
    if (totalBytes >= MAX_ARCHIVE_BYTES) {
      throw new RangeError("选中内容合计必须严格小于 1 GiB。");
    }

    const entries = [];
    clips.forEach((clip, position) => {
      const folder = folderName(clip, position);
      const used = new Set();

      if (clip.text) {
        const name = uniqueName("text.txt", used);
        entries.push({
          name: `${folder}/${name}`,
          blob: new Blob([clip.text], { type: "text/plain;charset=utf-8" }),
          lastModified: clip.createdAt || Date.now(),
        });
      }

      for (const file of (clip.files || [])) {
        const name = uniqueName(file.name, used);
        entries.push({
          name: `${folder}/${name}`,
          blob: file.blob,
          lastModified: file.lastModified || clip.createdAt || Date.now(),
        });
      }
    });

    if (!entries.length) throw new Error("选中条目没有可下载内容。");
    if (entries.length > 65535) throw new Error("ZIP 文件数量超过格式上限。");
    return { entries, totalBytes };
  }

  function localHeader(nameBytes, stamp) {
    const { bytes, data } = view(30 + nameBytes.length);
    data.setUint32(0, 0x04034b50, true);
    data.setUint16(4, 20, true);
    data.setUint16(6, 0x0808, true);
    data.setUint16(8, 0, true);
    data.setUint16(10, stamp.time, true);
    data.setUint16(12, stamp.date, true);
    data.setUint16(26, nameBytes.length, true);
    bytes.set(nameBytes, 30);
    return bytes;
  }

  function dataDescriptor(crc, size) {
    const { bytes, data } = view(16);
    data.setUint32(0, 0x08074b50, true);
    data.setUint32(4, crc, true);
    data.setUint32(8, size, true);
    data.setUint32(12, size, true);
    return bytes;
  }

  function centralHeader(entry) {
    const { bytes, data } = view(46 + entry.nameBytes.length);
    data.setUint32(0, 0x02014b50, true);
    data.setUint16(4, 20, true);
    data.setUint16(6, 20, true);
    data.setUint16(8, 0x0808, true);
    data.setUint16(10, 0, true);
    data.setUint16(12, entry.stamp.time, true);
    data.setUint16(14, entry.stamp.date, true);
    data.setUint32(16, entry.crc, true);
    data.setUint32(20, entry.size, true);
    data.setUint32(24, entry.size, true);
    data.setUint16(28, entry.nameBytes.length, true);
    data.setUint32(42, entry.offset, true);
    bytes.set(entry.nameBytes, 46);
    return bytes;
  }

  function endRecord(count, centralSize, centralOffset) {
    const { bytes, data } = view(22);
    data.setUint32(0, 0x06054b50, true);
    data.setUint16(8, count, true);
    data.setUint16(10, count, true);
    data.setUint32(12, centralSize, true);
    data.setUint32(16, centralOffset, true);
    return bytes;
  }

  async function writeZip(entries, writable, totalBytes, onProgress) {
    let offset = 0;
    let completed = 0;
    const centralEntries = [];

    async function write(bytes) {
      await writable.write(bytes);
      offset += bytes.byteLength;
    }

    for (const entry of entries) {
      const nameBytes = encoder.encode(entry.name);
      const stamp = dosDateTime(entry.lastModified);
      const localOffset = offset;
      await write(localHeader(nameBytes, stamp));

      const reader = entry.blob.stream().getReader();
      let crc = 0xffffffff;
      let size = 0;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        crc = updateCrc(crc, value);
        size += value.byteLength;
        completed += value.byteLength;
        await write(value);
        if (onProgress) onProgress(completed, totalBytes);
      }

      const finalCrc = (crc ^ 0xffffffff) >>> 0;
      await write(dataDescriptor(finalCrc, size));
      centralEntries.push({
        nameBytes,
        stamp,
        crc: finalCrc,
        size,
        offset: localOffset,
      });
    }

    const centralOffset = offset;
    for (const entry of centralEntries) await write(centralHeader(entry));
    const centralSize = offset - centralOffset;
    await write(endRecord(centralEntries.length, centralSize, centralOffset));
  }

  function suggestedName(prefix) {
    const date = new Date();
    const stamp = [
      date.getFullYear(),
      String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"),
      "-",
      String(date.getHours()).padStart(2, "0"),
      String(date.getMinutes()).padStart(2, "0"),
    ].join("");
    return `${prefix || "clipbrain"}-${stamp}.zip`;
  }

  async function saveClipsAsZip(clips, options) {
    if (typeof window.showSaveFilePicker !== "function") {
      throw new Error("当前浏览器不支持流式 ZIP 保存，请使用最新版 Chrome 或 Edge。");
    }

    const { entries, totalBytes } = buildEntries(clips);
    const handle = await window.showSaveFilePicker({
      suggestedName: (options && options.name) || suggestedName("clipbrain"),
      types: [{
        description: "ZIP archive",
        accept: { "application/zip": [".zip"] },
      }],
    });
    const writable = await handle.createWritable();

    try {
      await writeZip(entries, writable, totalBytes, options && options.onProgress);
      await writable.close();
    } catch (error) {
      try {
        await writable.abort();
      } catch (_) {
        // Keep the original write error.
      }
      throw error;
    }

    return { totalBytes, fileCount: entries.length };
  }

  window.ClipArchive = Object.freeze({
    MAX_ARCHIVE_BYTES,
    buildEntries,
    measureClips,
    saveClipsAsZip,
    writeZip,
  });
}());
