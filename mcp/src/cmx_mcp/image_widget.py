"""Passive native-compose image recognition hook shipped inside ``voice.js``.

The hook observes Mastodon's existing XHR requests without changing their
payloads or responses. Image blobs are kept in a small IndexedDB outbox from
``POST /api/v2/media`` until ``POST /api/v1/statuses`` supplies the status id.
Recognition then runs in the background through the caller's own page bearer.
"""

IMAGE_WIDGET_JS = r"""
  function startImageRecognition(state) {
    var token = pickToken(state);
    if (!token || !window.XMLHttpRequest || !window.indexedDB) {
      return;
    }

    var DB_NAME = "cmx-image-recognition-outbox";
    var DB_VERSION = 1;
    var STORE_NAME = "images";
    var proto = window.XMLHttpRequest.prototype;

    function openDb() {
      return new Promise(function (resolve, reject) {
        var request = window.indexedDB.open(DB_NAME, DB_VERSION);
        request.onupgradeneeded = function () {
          var db = request.result;
          if (!db.objectStoreNames.contains(STORE_NAME)) {
            db.createObjectStore(STORE_NAME, { keyPath: "mediaId" });
          }
        };
        request.onsuccess = function () { resolve(request.result); };
        request.onerror = function () { reject(request.error); };
      });
    }

    function withStore(mode, operation) {
      return openDb().then(function (db) {
        return new Promise(function (resolve, reject) {
          var tx = db.transaction(STORE_NAME, mode);
          var store = tx.objectStore(STORE_NAME);
          var request = operation(store);
          request.onsuccess = function () { resolve(request.result); };
          request.onerror = function () { reject(request.error); };
          tx.oncomplete = function () { db.close(); };
          tx.onabort = function () { db.close(); };
        });
      });
    }

    function saveUpload(mediaId, file) {
      return withStore("readwrite", function (store) {
        return store.put({
          mediaId: String(mediaId),
          blob: file,
          name: String(file.name || "image"),
          type: String(file.type || "image/jpeg"),
          statusId: ""
        });
      });
    }

    function getUpload(mediaId) {
      return withStore("readonly", function (store) {
        return store.get(String(mediaId));
      });
    }

    function saveRecord(record) {
      return withStore("readwrite", function (store) { return store.put(record); });
    }

    function deleteRecord(mediaId) {
      return withStore("readwrite", function (store) {
        return store.delete(String(mediaId));
      });
    }

    function allRecords() {
      return withStore("readonly", function (store) { return store.getAll(); });
    }

    function parseJson(xhr) {
      try { return JSON.parse(xhr.responseText || "null"); }
      catch (ignored) { return null; }
    }

    function requestPath(url) {
      try { return new URL(String(url), window.location.href).pathname; }
      catch (ignored) { return ""; }
    }

    function recognizeRecord(record) {
      if (!record || !record.statusId || !record.blob) { return Promise.resolve(); }
      var form = new FormData();
      form.append("file", record.blob, record.name || "image");
      form.append("status_id", record.statusId);
      form.append("media_id", record.mediaId);
      return fetch("/files/recognize", {
        method: "POST",
        headers: { Authorization: "Bearer " + token },
        body: form,
        credentials: "same-origin"
      }).then(function (response) {
        if (!response.ok) { throw new Error("recognition HTTP " + response.status); }
        return response.json();
      }).then(function (result) {
        if (result && result.alt_error) { throw new Error(result.alt_error); }
        return deleteRecord(record.mediaId);
      }).catch(function (error) {
        warn("image recognition will retry later", error);
      });
    }

    function attachStatus(status) {
      if (!status || !status.id || !Array.isArray(status.media_attachments)) { return; }
      status.media_attachments.forEach(function (attachment) {
        if (!attachment || attachment.type !== "image" || !attachment.id) { return; }
        getUpload(attachment.id).then(function (record) {
          if (!record) { return; }
          record.statusId = String(status.id);
          return saveRecord(record).then(function () { return recognizeRecord(record); });
        }).catch(function (error) { warn("could not attach image recognition", error); });
      });
    }

    if (!proto.__cmxImageRecognitionHook) {
      var originalOpen = proto.open;
      var originalSend = proto.send;
      proto.open = function (method, url) {
        this.__cmxMethod = String(method || "").toUpperCase();
        this.__cmxPath = requestPath(url);
        return originalOpen.apply(this, arguments);
      };
      proto.send = function (body) {
        var xhr = this;
        var file = null;
        if (xhr.__cmxMethod === "POST" && xhr.__cmxPath === "/api/v2/media" &&
            body && typeof body.get === "function") {
          file = body.get("file");
        }
        xhr.addEventListener("load", function () {
          if (xhr.status < 200 || xhr.status >= 300) { return; }
          var response = parseJson(xhr);
          if (file && response && response.id && String(file.type || "").indexOf("image/") === 0) {
            saveUpload(response.id, file).catch(function (error) {
              warn("could not save image recognition outbox", error);
            });
          }
          if (xhr.__cmxMethod === "POST" && xhr.__cmxPath === "/api/v1/statuses") {
            attachStatus(response);
          }
        });
        return originalSend.apply(this, arguments);
      };
      proto.__cmxImageRecognitionHook = true;
    }

    function retryOutbox() {
      allRecords().then(function (records) {
        records.filter(function (record) { return Boolean(record.statusId); })
          .forEach(function (record) { recognizeRecord(record); });
      }).catch(function (error) { warn("could not resume image recognition outbox", error); });
    }

    window.addEventListener("online", retryOutbox);
    window.setTimeout(retryOutbox, 1500);
  }
"""
