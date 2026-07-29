(function () {
  "use strict";

  // Development-only adapter for http://127.0.0.1:4173/clipboard/. It exposes
  // exactly the ClipClient interface so app.js never branches on the mode, and
  // it is NEVER the source of truth on the real site.
  if (!window.ClipAuth.isLocalDemo()) return;

  const DB_NAME = "clip-brain-local";
  const DB_VERSION = 1;
  const STORE = "clips";
  const TTL_MS = 24 * 60 * 60 * 1000;
  const QUOTA_BYTES = 2 * 1024 ** 3;
  const WARN_BYTES = 1536 * 1024 ** 2;
  let dbPromise;

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);
      request.onupgradeneeded = () => {
        request.result.createObjectStore(STORE, { keyPath: "id" });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    return dbPromise;
  }

  async function run(mode, work) {
    const db = await open();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE, mode);
      let result;
      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error("事务中止"));
      try {
        result = work(transaction.objectStore(STORE));
      } catch (error) {
        transaction.abort();
        reject(error);
      }
    });
  }

  async function rows() {
    const db = await open();
    return new Promise((resolve, reject) => {
      const request = db.transaction(STORE, "readonly").objectStore(STORE).getAll();
      request.onsuccess = () => resolve(request.result || []);
      request.onerror = () => reject(request.error);
    });
  }

  function present(row) {
    return {
      id: row.id,
      text: row.text || "",
      createdAt: row.createdAt,
      expiresAt: row.favorited ? null : row.expiresAt,
      favorited: Boolean(row.favorited),
      topic: row.topic || "",
      totalBytes: row.totalBytes || 0,
      files: (row.files || []).map((file) => ({
        fileId: file.fileId,
        name: file.name,
        contentType: file.type,
        size: file.size,
        url: "",
        lastModified: file.lastModified || row.createdAt,
        blob: file.blob,
      })),
    };
  }

  async function list(filters) {
    const options = filters || {};
    const now = Date.now();
    let all = (await rows()).filter((row) => row.favorited || row.expiresAt > now);
    all.sort((a, b) => b.createdAt - a.createdAt);
    const wantFavorites = (options.view || "temporary") === "favorite";
    all = all.filter((row) => Boolean(row.favorited) === wantFavorites);
    if (options.topic) all = all.filter((row) => row.topic === options.topic);
    let clips = all.map(present);
    if (options.type === "text") clips = clips.filter((clip) => clip.text);
    if (options.type === "image") {
      clips = clips.filter((clip) => clip.files.some((f) => String(f.contentType).startsWith("image/")));
    }
    if (options.q) {
      const needle = options.q.toLowerCase();
      clips = clips.filter((clip) => {
        const hay = `${clip.text}\n${clip.files.map((f) => f.name).join("\n")}`.toLowerCase();
        return hay.includes(needle);
      });
    }
    return { clips, truncated: false };
  }

  async function create(draft) {
    const createdAt = Date.now();
    const files = (draft.files || []).map((file) => ({
      fileId: `${createdAt}-${Math.random().toString(36).slice(2)}`,
      name: file.name || "unnamed-file",
      type: file.type || "application/octet-stream",
      size: file.size,
      lastModified: file.lastModified || createdAt,
      blob: file,
    }));
    const totalBytes = new Blob([draft.text || ""]).size
      + files.reduce((sum, file) => sum + file.size, 0);
    const row = {
      id: crypto.randomUUID ? crypto.randomUUID() : String(createdAt),
      text: draft.text || "",
      files,
      createdAt,
      expiresAt: createdAt + TTL_MS,
      favorited: false,
      topic: "",
      totalBytes,
    };
    await run("readwrite", (store) => store.put(row));
    return present(row);
  }

  async function findRow(id) {
    const all = await rows();
    return all.find((row) => row.id === id);
  }

  async function patch(id, changes) {
    const row = await findRow(id);
    if (!row) throw new Error("条目不存在");
    if ("favorite" in changes) {
      row.favorited = Boolean(changes.favorite);
      row.expiresAt = row.favorited ? null : Date.now() + TTL_MS;
    }
    if ("topic" in changes) row.topic = changes.topic || "";
    await run("readwrite", (store) => store.put(row));
    return present(row);
  }

  async function remove(id) {
    await run("readwrite", (store) => store.delete(id));
    return 1;
  }

  async function removeMany(ids) {
    const unique = [...new Set(ids.filter(Boolean))];
    if (!unique.length) return 0;
    await run("readwrite", (store) => unique.forEach((id) => store.delete(id)));
    return unique.length;
  }

  async function removeFile(entryId, fileId) {
    const row = await findRow(entryId);
    if (!row) throw new Error("条目不存在");
    row.files = (row.files || []).filter((file) => file.fileId !== fileId);
    if (!row.text && !row.files.length) return remove(entryId);
    row.totalBytes = new Blob([row.text || ""]).size
      + row.files.reduce((sum, file) => sum + file.size, 0);
    await run("readwrite", (store) => store.put(row));
    return { entry_removed: false };
  }

  async function usage() {
    const all = await rows();
    return {
      used_bytes: all.reduce((sum, row) => sum + (row.totalBytes || 0), 0),
      quota_bytes: QUOTA_BYTES,
      warn_bytes: WARN_BYTES,
    };
  }

  async function fileBlob(file) {
    return file.blob;
  }

  window.ClipClient = Object.freeze({
    mode: "local",
    list,
    create,
    patch,
    remove,
    removeMany,
    removeFile,
    usage,
    fileBlob,
  });
}());
