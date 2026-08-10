(function () {
  "use strict";

  // Exactly one adapter installs itself. On the real site that is always this
  // one: the local IndexedDB adapter can never become the source of truth.
  if (window.ClipAuth.isLocalDemo()) return;

  const BASE = "/clipboard-api";

  function authHeaders(extra) {
    const token = window.ClipAuth.token();
    if (!token) throw new Error("会话已失效，请重新登录。");
    return Object.assign({ Authorization: `Bearer ${token}` }, extra || {});
  }

  async function request(path, options) {
    const settings = Object.assign({ cache: "no-store", credentials: "omit" }, options || {});
    settings.headers = authHeaders(settings.headers);
    let response;
    try {
      response = await fetch(`${BASE}${path}`, settings);
    } catch (_) {
      throw new Error("后端未连接");
    }
    if (response.status === 204) return null;
    let body = null;
    try {
      body = await response.json();
    } catch (_) {
      body = null;
    }
    if (!response.ok) {
      const error = new Error((body && body.error) || `HTTP ${response.status}`);
      error.code = body && body.error;
      error.status = response.status;
      error.detail = body;
      throw error;
    }
    return body;
  }

  // A Blob-shaped object whose bytes are pulled straight from the server as the
  // ZIP writer consumes them. archive.js only ever calls .stream(), so nothing
  // is buffered: a 900 MiB selection never lands in memory.
  function remoteBlob(url, size) {
    return {
      size,
      stream() {
        let reader = null;
        return new ReadableStream({
          async pull(controller) {
            if (!reader) {
              const response = await fetch(url, {
                cache: "no-store",
                credentials: "omit",
                headers: authHeaders(),
              });
              if (!response.ok) throw new Error(`文件读取失败：HTTP ${response.status}`);
              reader = response.body.getReader();
            }
            const chunk = await reader.read();
            if (chunk.done) controller.close();
            else controller.enqueue(chunk.value);
          },
          cancel() {
            if (reader) reader.cancel();
          },
        });
      },
    };
  }

  function normalize(entry) {
    return {
      id: entry.entry_id,
      text: entry.text || "",
      createdAt: entry.created_at * 1000,
      expiresAt: entry.expires_at === null ? null : entry.expires_at * 1000,
      favorited: Boolean(entry.favorited),
      topic: entry.topic || "",
      totalBytes: entry.total_bytes,
      files: (entry.files || []).map((file) => ({
        fileId: file.file_id,
        name: file.name,
        contentType: file.content_type,
        size: file.size_bytes,
        url: file.url,
        lastModified: entry.created_at * 1000,
        blob: remoteBlob(file.url, file.size_bytes),
      })),
    };
  }

  async function list(filters) {
    const params = new URLSearchParams();
    const options = filters || {};
    params.set("view", options.view || "temporary");
    if (options.topic) params.set("topic", options.topic);
    if (options.type) params.set("type", options.type);
    if (options.q) params.set("q", options.q);
    const body = await request(`/entries?${params.toString()}`);
    return { clips: (body.entries || []).map(normalize), truncated: Boolean(body.truncated) };
  }

  async function create(draft) {
    const form = new FormData();
    form.append("text", draft.text || "");
    for (const file of draft.files || []) form.append("files", file, file.name);
    const body = await request("/entries", {
      method: "POST",
      headers: { Origin: window.location.origin },
      body: form,
    });
    return normalize(body);
  }

  async function patch(id, changes) {
    return normalize(await request(`/entries/${encodeURIComponent(id)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    }));
  }

  async function remove(id) {
    await request(`/entries/${encodeURIComponent(id)}`, { method: "DELETE" });
    return 1;
  }

  async function removeMany(ids) {
    // Never an empty body meaning "everything": the caller names every id.
    const body = await request("/entries/delete-many", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ entry_ids: ids }),
    });
    return body.removed;
  }

  async function removeFile(entryId, fileId) {
    return request(
      `/entries/${encodeURIComponent(entryId)}/files/${encodeURIComponent(fileId)}`,
      { method: "DELETE" },
    );
  }

  async function usage() {
    return request("/usage");
  }

  async function fileBlob(file) {
    const response = await fetch(file.url, {
      cache: "no-store",
      credentials: "omit",
      headers: authHeaders(),
    });
    if (!response.ok) throw new Error(`文件读取失败：HTTP ${response.status}`);
    return response.blob();
  }

  window.ClipClient = Object.freeze({
    mode: "backend",
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
