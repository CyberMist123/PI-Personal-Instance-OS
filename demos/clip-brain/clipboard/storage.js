(function () {
  "use strict";

  const DB_NAME = "clip-brain-demo";
  const DB_VERSION = 1;
  const STORE_NAME = "clips";
  let dbPromise;

  function openDatabase() {
    if (dbPromise) return dbPromise;

    dbPromise = new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, DB_VERSION);

      request.onupgradeneeded = () => {
        const db = request.result;
        const store = db.createObjectStore(STORE_NAME, { keyPath: "id" });
        store.createIndex("expiresAt", "expiresAt");
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.onblocked = () => reject(new Error("IndexedDB upgrade is blocked by another tab."));
    });

    return dbPromise;
  }

  async function run(mode, work) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, mode);
      const store = transaction.objectStore(STORE_NAME);
      let result;

      transaction.oncomplete = () => resolve(result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error("IndexedDB transaction aborted."));

      try {
        result = work(store, transaction);
      } catch (error) {
        transaction.abort();
        reject(error);
      }
    });
  }

  async function list() {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readonly");
      const request = transaction.objectStore(STORE_NAME).getAll();

      request.onsuccess = () => {
        const rows = request.result || [];
        rows.sort((a, b) => b.createdAt - a.createdAt);
        resolve(rows);
      };
      request.onerror = () => reject(request.error);
    });
  }

  async function put(entry) {
    return run("readwrite", (store) => store.put(entry));
  }

  async function remove(id) {
    return run("readwrite", (store) => store.delete(id));
  }

  async function purgeExpired(now) {
    const db = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(STORE_NAME, "readwrite");
      const index = transaction.objectStore(STORE_NAME).index("expiresAt");
      const request = index.openCursor(IDBKeyRange.upperBound(now));
      let removed = 0;

      request.onsuccess = () => {
        const cursor = request.result;
        if (!cursor) return;
        cursor.delete();
        removed += 1;
        cursor.continue();
      };
      transaction.oncomplete = () => resolve(removed);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error || new Error("Expiry cleanup aborted."));
    });
  }

  window.ClipStore = Object.freeze({
    list,
    put,
    remove,
    purgeExpired,
  });
}());
