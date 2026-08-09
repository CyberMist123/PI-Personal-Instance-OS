"""Same-origin browser search patch backed by CMX's local SQLite mirror."""

from __future__ import annotations

SEARCH_WIDGET_VERSION = "3"

SEARCH_WIDGET_JS = """/* CMX search widget v3 - patches window.fetch so Mastodon's own search box also surfaces locally mirrored results. */
(function () {
  "use strict";

  if (window.__piSearchWidget) {
    return;
  }
  window.__piSearchWidget = "2";

  var LOG = "[pi-search]";
  var SEARCH_PATH = "/api/v2/search";
  var SITE_SEARCH_PATH = "/files/search";
  var STATUSES_PATH = "/api/v1/statuses";
  var STATUS_CHUNK_SIZE = 20;
  var SITE_SEARCH_LIMIT = 30;
  var TIMEOUT_MS = 8000;

  function warn(message, detail) {
    try {
      if (detail === undefined) {
        console.warn(LOG + " " + message);
      } else {
        console.warn(LOG + " " + message, detail);
      }
    } catch (ignored) {
      /* console may be missing in exotic webviews */
    }
  }

  function readInitialState() {
    var node = document.getElementById("initial-state");
    if (!node) {
      return null;
    }
    try {
      return JSON.parse(node.textContent || node.innerText || "null");
    } catch (error) {
      warn("could not parse #initial-state JSON", error);
      return null;
    }
  }

  function pickToken(state) {
    if (!state || !state.meta || typeof state.meta.access_token !== "string") {
      return "";
    }
    return state.meta.access_token;
  }

  function describeRequest(input, init) {
    /* input can be a string, a URL, or a Request; only a Request also carries
       its own method and that is only used when init does not override it. */
    var url = "";
    var method = (init && init.method) || "GET";
    if (typeof input === "string") {
      url = input;
    } else if (typeof URL !== "undefined" && input instanceof URL) {
      url = input.href;
    } else if (input && typeof input === "object" && typeof input.url === "string") {
      url = input.url;
      if (!init || !init.method) {
        method = input.method || "GET";
      }
    }
    return { url: url, method: String(method).toUpperCase() };
  }

  function sameOriginSearchQuery(url) {
    /* Resolved against the current page so a bare "/api/v2/search?q=x" string
       is treated the same as an absolute same-origin URL. Anything on another
       origin, any other path, or a call with no q, is left completely alone -
       that is exactly what Mastodon does today, so nothing should change. */
    var parsed;
    try {
      parsed = new URL(url, window.location.href);
    } catch (error) {
      return "";
    }
    if (parsed.origin !== window.location.origin || parsed.pathname !== SEARCH_PATH) {
      return "";
    }
    return (parsed.searchParams.get("q") || "").trim();
  }

  function chunk(items, size) {
    var out = [];
    for (var i = 0; i < items.length; i += size) {
      out.push(items.slice(i, i + size));
    }
    return out;
  }

  function fetchSiteHits(query, token, signal, rawFetch) {
    var url = SITE_SEARCH_PATH + "?q=" + encodeURIComponent(query) + "&limit=" + SITE_SEARCH_LIMIT;
    return rawFetch(url, {
      headers: { Authorization: "Bearer " + token },
      signal: signal
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("site search HTTP " + response.status);
      }
      return response.json();
    }).then(function (payload) {
      var items = payload && Array.isArray(payload.items) ? payload.items : null;
      if (!items) {
        throw new Error("site search returned no items array");
      }
      return items;
    });
  }

  function fetchStatusChunk(ids, token, signal, rawFetch) {
    var params = ids.map(function (id) {
      return "id[]=" + encodeURIComponent(id);
    }).join("&");
    return rawFetch(STATUSES_PATH + "?" + params, {
      headers: { Authorization: "Bearer " + token },
      signal: signal
    }).then(function (response) {
      if (!response.ok) {
        throw new Error("statuses HTTP " + response.status);
      }
      return response.json();
    }).then(function (payload) {
      if (!Array.isArray(payload)) {
        throw new Error("statuses response was not an array");
      }
      return payload;
    });
  }

  function fetchStatusesInOrder(ids, token, signal, rawFetch) {
    if (!ids.length) {
      return Promise.resolve([]);
    }
    var byId = {};
    var chain = Promise.resolve();
    chunk(ids, STATUS_CHUNK_SIZE).forEach(function (part) {
      chain = chain.then(function () {
        return fetchStatusChunk(part, token, signal, rawFetch).then(function (statuses) {
          statuses.forEach(function (status) {
            if (status && status.id !== undefined && status.id !== null) {
              byId[String(status.id)] = status;
            }
          });
        });
      });
    });
    return chain.then(function () {
      /* /api/v1/statuses does not promise to preserve request order, and
         silently omits ids the viewer may not see. Re-derive order from the
         (already newest-first) site-search ids instead of trusting the batch. */
      var ordered = [];
      ids.forEach(function (id) {
        var status = byId[String(id)];
        if (status) {
          ordered.push(status);
        }
      });
      return ordered;
    });
  }

  function fetchCmxStatuses(query, token, rawFetch) {
    var controller = window.AbortController ? new window.AbortController() : null;
    var timer = 0;
    if (controller) {
      timer = window.setTimeout(function () {
        try {
          controller.abort();
        } catch (ignored) {
          /* already settled */
        }
      }, TIMEOUT_MS);
    }
    var signal = controller ? controller.signal : undefined;
    function done() {
      if (timer) {
        window.clearTimeout(timer);
        timer = 0;
      }
    }
    return fetchSiteHits(query, token, signal, rawFetch)
      .then(function (items) {
        var ids = items.map(function (item) {
          return item && item.id;
        }).filter(function (id) {
          return id !== undefined && id !== null && id !== "";
        });
        return fetchStatusesInOrder(ids, token, signal, rawFetch);
      })
      .then(function (statuses) {
        done();
        return statuses;
      }, function (error) {
        done();
        throw error;
      });
  }

  function mergedResponse(nativeJson, statuses) {
    /* accounts and hashtags already work on this instance and are kept as
       Mastodon returned them; only statuses - the one index-limited field -
       is replaced. */
    var accounts = nativeJson && Array.isArray(nativeJson.accounts) ? nativeJson.accounts : [];
    var hashtags = nativeJson && Array.isArray(nativeJson.hashtags) ? nativeJson.hashtags : [];
    var payload = { accounts: accounts, hashtags: hashtags, statuses: statuses };
    return new window.Response(JSON.stringify(payload), {
      status: 200,
      headers: { "Content-Type": "application/json; charset=utf-8" }
    });
  }

  function start(state) {
    var token = pickToken(state);
    if (!token) {
      /* Logged-out page (or a Mastodon build without a web token): do not
         touch window.fetch at all, so search stays completely untouched. */
      return;
    }
    if (typeof window.fetch !== "function" || typeof window.Response !== "function") {
      warn("fetch/Response unavailable in this browser");
      return;
    }

    var originalFetch = window.fetch.bind(window);

    window.fetch = function (input, init) {
      var described = describeRequest(input, init);
      if (described.method !== "GET") {
        return originalFetch(input, init);
      }
      var query = sameOriginSearchQuery(described.url);
      if (!query) {
        return originalFetch(input, init);
      }

      /* Mastodon's own search always runs; the CMX lookup runs alongside it,
         not after it, and a slow or failing CMX side never delays or breaks
         the native call. */
      var nativePromise = originalFetch(input, init);
      var cmxPromise = fetchCmxStatuses(query, token, originalFetch).then(
        function (statuses) {
          return { ok: true, statuses: statuses };
        },
        function (error) {
          return { ok: false, error: error };
        }
      );

      return nativePromise.then(function (native) {
        return cmxPromise.then(function (cmx) {
          if (!native.ok) {
            return native;
          }
          if (!cmx.ok) {
            warn("whole-instance search unavailable; native results only", cmx.error);
            return native;
          }
          return native.clone().json().then(
            function (nativeJson) {
              return mergedResponse(nativeJson, cmx.statuses);
            },
            function (error) {
              warn("native search response was not JSON", error);
              return native;
            }
          );
        });
      });
    };
  }

  function boot() {
    var state = readInitialState();
    if (!state) {
      return;
    }
    try {
      start(state);
    } catch (error) {
      warn("search widget failed to start", error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""
