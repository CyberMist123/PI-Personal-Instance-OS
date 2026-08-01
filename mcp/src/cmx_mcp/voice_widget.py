"""The floating voice-recorder button injected into the owner's own Mastodon web UI.

Nginx ``sub_filter`` appends a single ``<script src="/files/voice.js" defer>`` tag
to Mastodon's HTML; this module is the only place that script lives. Two hard
rules keep the increment same-origin and credential-free:

* the JavaScript only ever calls **relative** Mastodon API paths, so it inherits
  whatever origin the browser is already on;
* the bearer token is read out of the page's own ``#initial-state`` blob
  (``meta.access_token``, the token Mastodon's own web client uses) and is never
  stored, copied or transmitted anywhere else. Logged-out pages simply have no
  token, and the widget then removes itself silently.

Since v3 recording is **publish-first, transcribe-later**: ✓ uploads the audio and
immediately posts an empty-bodied voice status, so the user waits for nothing but
the upload. The blob then stays in memory while the page posts it to the
same-origin ``/files/transcribe`` (CMX's own endpoint, which re-verifies that
same page token against the instance) and, when the local transcript comes back,
**edits the status it just created** (``PUT /api/v1/statuses/<id>`` with
``media_attributes``) so the body becomes the transcript and the audio gains alt
text.

Since v17 tapping the large microphone again ends the recording and starts the
upload immediately (the check button remains as an equivalent accessible
target). Before the first network request, the completed Blob and its stable
idempotency key are saved in IndexedDB. Upload/publish/transcript failures remain
there and resume when the same browser opens CMX again or comes back online. The
page token is never persisted. This gives both mobile browsers and Windows a
device-local outbox without weakening the same-origin credential boundary.

Since v4 the widget never injects a ``<style>`` element: Mastodon 4.6.3 ships a
strict Content-Security-Policy whose ``style-src`` is locked to a per-response
``'nonce-…'``, so a runtime-injected stylesheet (and its keyframes) is refused by
the browser. Every rule is therefore applied as an inline ``element.style.*``
property and the recording "pulse" is driven by a ``setInterval`` toggle instead
of a CSS animation. (The Nginx site also rewrites the CSP on injected pages so
the external script itself is allowed to load — see ``nginx/default.conf``.)

v5 resized the controls for the owner's private single-user instance. v17 keeps
those sizes and adds tap-to-finish plus the durable outbox described above.

Plain ES2017, no build step, no external dependency, no backticks (the source
must stay safe to embed in any HTML or config context).
"""

from __future__ import annotations

from .image_widget import IMAGE_WIDGET_JS
from .voice_owner import VOICE_OWNER_JS
from .voice_player import VOICE_PLAYER_JS
from .voice_scan import VOICE_SCAN_JS
from .voice_waveform import VOICE_WAVEFORM_JS

VOICE_WIDGET_VERSION = "20"

VOICE_WIDGET_JS = """/* CMX voice widget v20 - voice outbox plus passive image recognition. */
(function () {
  "use strict";

  if (window.__piVoiceWidget) {
    return;
  }
  window.__piVoiceWidget = "1";

  var LOG = "[pi-voice]";
  /* WebM/Opus first, MP4 last. An .m4a shares its container with MP4 video, so
     Mastodon's magic-byte detection reports video/quicktime, the extension no
     longer matches the contents, and Paperclip's spoof check rejects the upload
     with a 422. Desktop Chrome supports audio/mp4, so listing it first sent
     every desktop recording down that path. MP4 stays as the last resort
     because iOS Safari records nothing else. */
  var MIME_CANDIDATES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  var POLL_INTERVAL_MS = 1000;
  var POLL_MAX_TRIES = 30;
  var TRANSCRIBE_PATH = "/files/transcribe";
  var REMUX_PATH = "/files/voice-remux";
  var MP3_NAME = "voice.mp3";
  var TRANSCRIBE_TIMEOUT_MS = 90000;
  var STATUS_MAX_CHARS = 4900;
  var ALT_MAX_CHARS = 1500;
  var OUTBOX_DB = "cmx-voice-outbox";
  var OUTBOX_VERSION = 1;
  var OUTBOX_STORE = "recordings";
  var MIC_RESTING = "0.5";
  var SAT_BUTTON_STYLE = { width: "44px", height: "44px", fontSize: "19px" };
  var MIC_BUTTON_STYLE = { width: "64px", height: "64px", fontSize: "29px" };

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

  function pickVisibility(state) {
    if (state && state.compose && state.compose.default_privacy) {
      return state.compose.default_privacy;
    }
    if (state && state.meta && state.meta.default_privacy) {
      return state.meta.default_privacy;
    }
    return "private";
  }

  function pickOwnerId(state) {
    if (state && state.meta && (typeof state.meta.me === "string" || typeof state.meta.me === "number")) {
      return String(state.meta.me);
    }
    return "";
  }

  function pickMime() {
    if (!window.MediaRecorder) {
      return "";
    }
    if (typeof window.MediaRecorder.isTypeSupported !== "function") {
      /* Older MediaRecorder builds: let the browser choose its own default. */
      return "";
    }
    for (var i = 0; i < MIME_CANDIDATES.length; i += 1) {
      if (window.MediaRecorder.isTypeSupported(MIME_CANDIDATES[i])) {
        return MIME_CANDIDATES[i];
      }
    }
    return "";
  }

  function idempotencyKey() {
    try {
      if (window.crypto && typeof window.crypto.randomUUID === "function") {
        return window.crypto.randomUUID();
      }
      if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        var bytes = new Uint8Array(16);
        window.crypto.getRandomValues(bytes);
        var out = "";
        for (var i = 0; i < bytes.length; i += 1) {
          out += (bytes[i] + 256).toString(16).slice(-2);
        }
        return out;
      }
    } catch (ignored) {
      /* fall through to Math.random */
    }
    return "pv-" + Date.now().toString(16) + "-" + Math.random().toString(16).slice(2);
  }

  function clip(text, limit) {
    if (typeof text !== "string" || !text) {
      return "";
    }
    if (text.length <= limit) {
      return text;
    }
    return text.slice(0, limit - 1) + "\\u2026";
  }

  function mmss(totalSeconds) {
    var minutes = Math.floor(totalSeconds / 60);
    var seconds = totalSeconds % 60;
    return (minutes < 10 ? "0" : "") + minutes + ":" + (seconds < 10 ? "0" : "") + seconds;
  }

  function sleep(ms) {
    return new Promise(function (resolve) {
      window.setTimeout(resolve, ms);
    });
  }

  function openOutbox() {
    return new Promise(function (resolve, reject) {
      if (!window.indexedDB) {
        reject(new Error("IndexedDB unavailable"));
        return;
      }
      var request = window.indexedDB.open(OUTBOX_DB, OUTBOX_VERSION);
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(OUTBOX_STORE)) {
          db.createObjectStore(OUTBOX_STORE, { keyPath: "id" });
        }
      };
      request.onsuccess = function () {
        resolve(request.result);
      };
      request.onerror = function () {
        reject(request.error || new Error("could not open voice outbox"));
      };
      request.onblocked = function () {
        reject(new Error("voice outbox upgrade blocked"));
      };
    });
  }

  function outboxWrite(mode, operation) {
    return openOutbox().then(function (db) {
      return new Promise(function (resolve, reject) {
        var tx;
        try {
          tx = db.transaction(OUTBOX_STORE, mode);
          operation(tx.objectStore(OUTBOX_STORE));
        } catch (error) {
          db.close();
          reject(error);
          return;
        }
        tx.oncomplete = function () {
          db.close();
          resolve();
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error || new Error("voice outbox transaction failed"));
        };
        tx.onabort = tx.onerror;
      });
    });
  }

  function outboxPut(entry) {
    return outboxWrite("readwrite", function (store) {
      store.put(entry);
    });
  }

  function outboxDelete(id) {
    return outboxWrite("readwrite", function (store) {
      store.delete(id);
    });
  }

  function outboxList() {
    return openOutbox().then(function (db) {
      return new Promise(function (resolve, reject) {
        var values = [];
        var tx = db.transaction(OUTBOX_STORE, "readonly");
        var request = tx.objectStore(OUTBOX_STORE).openCursor();
        request.onsuccess = function () {
          var cursor = request.result;
          if (cursor) {
            values.push(cursor.value);
            cursor.continue();
          }
        };
        request.onerror = function () {
          reject(request.error || new Error("could not read voice outbox"));
        };
        tx.oncomplete = function () {
          db.close();
          resolve(values);
        };
        tx.onerror = function () {
          db.close();
          reject(tx.error || new Error("voice outbox transaction failed"));
        };
      });
    });
  }

  function setStyle(element, styles) {
    /* Inline styles only: Mastodon's CSP style-src is nonce-locked, so an
       injected stylesheet element (with :hover or CSS animation) is refused. */
    var keys = Object.keys(styles);
    for (var i = 0; i < keys.length; i += 1) {
      element.style[keys[i]] = styles[keys[i]];
    }
  }

  function makeHoverable(element, restingOpacity) {
    /* Replaces the CSS :hover / :focus opacity bump. element._piRest holds the
       current resting opacity so the mic can rest at 1 while recording. */
    element._piRest = restingOpacity;
    element.style.opacity = restingOpacity;
    function lift() {
      element.style.opacity = "1";
    }
    function settle() {
      element.style.opacity = element._piRest;
    }
    element.addEventListener("mouseenter", lift);
    element.addEventListener("focus", lift);
    element.addEventListener("mouseleave", settle);
    element.addEventListener("blur", settle);
  }

  function start(state) {
    var token = pickToken(state);
    if (!token) {
      /* Logged-out page (or a Mastodon build without a web token): stay invisible. */
      return;
    }
    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
      warn("getUserMedia unavailable (needs HTTPS and a modern browser)");
      return;
    }
    if (!window.MediaRecorder) {
      warn("MediaRecorder unavailable in this browser");
      return;
    }

    var visibility = pickVisibility(state);
    var ownerId = pickOwnerId(state);
    var authHeader = "Bearer " + token;

    var root = document.createElement("div");
    root.id = "pi-voice-root";
    setStyle(root, {
      position: "fixed",
      right: "18px",
      bottom: "84px",
      zIndex: "9999",
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      gap: "8px",
      pointerEvents: "none"
    });

    var chip = document.createElement("div");
    chip.id = "pi-voice-chip";
    setStyle(chip, {
      pointerEvents: "auto",
      font: "12px/1.4 system-ui,sans-serif",
      color: "#f9fafb",
      background: "rgba(17,24,39,.78)",
      borderRadius: "10px",
      padding: "3px 9px",
      whiteSpace: "nowrap",
      transition: "opacity .2s",
      display: "none"
    });

    var row = document.createElement("div");
    row.id = "pi-voice-row";
    setStyle(row, {
      pointerEvents: "auto",
      display: "none",
      gap: "8px"
    });

    var okButton = document.createElement("button");
    okButton.id = "pi-voice-ok";
    okButton.type = "button";
    okButton.textContent = "\\u2713";
    okButton.setAttribute("aria-label", "\\u53d1\\u5e03\\u8bed\\u97f3");
    setStyle(okButton, SAT_BUTTON_STYLE);
    okButton.style.background = "rgba(34,197,94,.75)";
    makeHoverable(okButton, "0.95");

    var dropButton = document.createElement("button");
    dropButton.id = "pi-voice-drop";
    dropButton.type = "button";
    dropButton.textContent = "\\u2715";
    dropButton.setAttribute("aria-label", "\\u4e22\\u5f03\\u5f55\\u97f3");
    setStyle(dropButton, SAT_BUTTON_STYLE);
    dropButton.style.background = "rgba(31,41,55,.72)";
    makeHoverable(dropButton, "0.95");

    var micButton = document.createElement("button");
    micButton.id = "pi-voice-btn";
    micButton.type = "button";
    micButton.textContent = "\\ud83c\\udf99\\ufe0f";
    micButton.setAttribute("aria-label", "\\u8bed\\u97f3\\u4fbf\\u7b7e");
    micButton.title = "\\u8bed\\u97f3\\u4fbf\\u7b7e";
    setStyle(micButton, {
      pointerEvents: "auto",
      width: "64px",
      height: "64px",
      borderRadius: "50%",
      border: "0",
      cursor: "pointer",
      fontSize: "29px",
      lineHeight: "1",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      background: "rgba(99,102,241,.25)",
      color: "#fff",
      boxShadow: "none",
      transition: "opacity .2s, background .2s, box-shadow .2s",
      WebkitTapHighlightColor: "transparent"
    });
    makeHoverable(micButton, MIC_RESTING);

    row.appendChild(okButton);
    row.appendChild(dropButton);
    root.appendChild(chip);
    root.appendChild(row);
    root.appendChild(micButton);
    document.body.appendChild(root);

    var recorder = null;
    var stream = null;
    var chunks = [];
    var timerId = 0;
    var elapsed = 0;
    var busy = false;
    var requesting = false;
    var recording = false;
    var retrying = false;
    var mimeType = "";
    var pulseId = 0;
    var pulseOn = false;

    function startPulse() {
      /* JS-driven pulse: no CSS keyframes, so nothing to nonce. */
      stopPulse();
      pulseId = window.setInterval(function () {
        pulseOn = !pulseOn;
        micButton.style.boxShadow = pulseOn
          ? "0 0 0 8px rgba(239,68,68,.28)"
          : "0 0 0 0 rgba(239,68,68,0)";
      }, 700);
    }

    function stopPulse() {
      if (pulseId) {
        window.clearInterval(pulseId);
        pulseId = 0;
      }
      pulseOn = false;
      micButton.style.boxShadow = "none";
    }

    function enterRecordingLook() {
      recording = true;
      micButton.setAttribute("aria-label", "\u7ed3\u675f\u5e76\u4e0a\u4f20\u5f55\u97f3");
      micButton.title = "\u518d\u70b9\u4e00\u6b21\u7ed3\u675f\u5e76\u4e0a\u4f20";
      micButton.style.background = "rgba(239,68,68,.35)";
      micButton._piRest = "1";
      micButton.style.opacity = "1";
      row.style.display = "flex";
      startPulse();
    }

    function exitRecordingLook() {
      recording = false;
      micButton.setAttribute("aria-label", "\u8bed\u97f3\u4fbf\u7b7e");
      micButton.title = "\u8bed\u97f3\u4fbf\u7b7e";
      stopPulse();
      micButton.style.background = "rgba(99,102,241,.25)";
      micButton._piRest = MIC_RESTING;
      micButton.style.opacity = MIC_RESTING;
      row.style.display = "none";
    }

    function setChip(text) {
      if (!text) {
        chip.style.display = "none";
        chip.textContent = "";
        return;
      }
      chip.textContent = text;
      chip.style.display = "block";
    }

    function flash(text) {
      setChip(text);
      window.setTimeout(function () {
        if (chip.textContent === text) {
          setChip("");
        }
      }, 2000);
    }

    function quietFlash(text) {
      /* Background notices must never paint over a running mm:ss timer. */
      if (recording || busy) {
        return;
      }
      flash(text);
    }

    function stopTimer() {
      if (timerId) {
        window.clearInterval(timerId);
        timerId = 0;
      }
    }

    function releaseStream() {
      if (stream) {
        try {
          var tracks = stream.getTracks();
          for (var i = 0; i < tracks.length; i += 1) {
            tracks[i].stop();
          }
        } catch (error) {
          warn("could not stop microphone tracks", error);
        }
      }
      stream = null;
    }

    function resetUi() {
      exitRecordingLook();
      stopTimer();
      elapsed = 0;
      chunks = [];
      recorder = null;
      releaseStream();
    }

    function beginRecording() {
      if (requesting) {
        return;
      }
      requesting = true;
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (granted) {
        requesting = false;
        stream = granted;
        mimeType = pickMime();
        try {
          recorder = mimeType
            ? new window.MediaRecorder(stream, { mimeType: mimeType })
            : new window.MediaRecorder(stream);
        } catch (error) {
          warn("MediaRecorder could not start", error);
          releaseStream();
          flash("\\u5f55\\u97f3\\u5931\\u8d25");
          return;
        }
        chunks = [];
        recorder.ondataavailable = function (event) {
          if (event.data && event.data.size > 0) {
            chunks.push(event.data);
          }
        };
        recorder.onerror = function (event) {
          warn("MediaRecorder error", event);
        };
        recorder.start();
        enterRecordingLook();
        elapsed = 0;
        setChip(mmss(0));
        timerId = window.setInterval(function () {
          elapsed += 1;
          setChip(mmss(elapsed));
        }, 1000);
      }, function (error) {
        requesting = false;
        warn("microphone permission denied or unavailable", error);
        flash("\\u65e0\\u9ea6\\u514b\\u98ce\\u6743\\u9650");
      });
    }

    function stopRecorder() {
      return new Promise(function (resolve) {
        if (!recorder || recorder.state === "inactive") {
          resolve(chunks.slice());
          return;
        }
        recorder.onstop = function () {
          resolve(chunks.slice());
        };
        try {
          recorder.stop();
        } catch (error) {
          warn("recorder stop failed", error);
          resolve(chunks.slice());
        }
      });
    }

    function blobName(blob, mime) {
      /* Pure on purpose: a background transcribe must not read the shared
         mimeType, which a newly started recording may already have replaced. */
      var isMp4 = String(blob.type || mime || "").indexOf("mp4") >= 0;
      return isMp4 ? "voice.m4a" : "voice.webm";
    }

    function transcribe(blob, filename) {
      /* CMX's own same-origin endpoint: it re-verifies this page token against
         the instance, transcribes locally and stores nothing. Runs only after
         the status is already published, so any failure just leaves that status
         text-less and the worker's reply stays as the fallback. */
      var form = new FormData();
      form.append("file", blob, filename);
      var options = {
        method: "POST",
        headers: { Authorization: authHeader },
        body: form
      };
      var controller = null;
      var timer = 0;
      if (window.AbortController) {
        controller = new window.AbortController();
        options.signal = controller.signal;
        timer = window.setTimeout(function () {
          try {
            controller.abort();
          } catch (ignored) {
            /* already settled */
          }
        }, TRANSCRIBE_TIMEOUT_MS);
      }
      function done() {
        if (timer) {
          window.clearTimeout(timer);
          timer = 0;
        }
      }
      return fetch(TRANSCRIBE_PATH, options)
        .then(function (response) {
          done();
          if (response.status !== 200) {
            throw new Error("transcribe HTTP " + response.status);
          }
          return response.json();
        })
        .then(function (payload) {
          var text = payload && typeof payload.text === "string" ? payload.text : "";
          return text.trim();
        })
        .catch(function (error) {
          done();
          warn("transcription unavailable; the voice post stays text-less", error);
          return "";
        });
    }

    /* MediaRecorder only emits WebM or MP4, whose magic bytes read as video,
       so Mastodon refuses them as audio. Ogg fixes that but will not play on
       iOS, where every browser is WebKit. CMX converts to MP3, the one format
       both ends accept. */
    function toMp3(blob, filename) {
      var form = new FormData();
      form.append("file", blob, filename);
      return fetch(REMUX_PATH, {
        method: "POST",
        headers: { Authorization: authHeader },
        body: form
      }).then(function (response) {
        if (response.status !== 200) {
          throw new Error("remux HTTP " + response.status);
        }
        return response.blob();
      });
    }

    function upload(blob, filename) {
      var form = new FormData();
      form.append("file", blob, filename);
      return fetch("/api/v2/media", {
        method: "POST",
        headers: { Authorization: authHeader },
        body: form
      }).then(function (response) {
        if (response.status !== 200 && response.status !== 202) {
          throw new Error("media upload HTTP " + response.status);
        }
        return response.json().then(function (payload) {
          return { id: payload && payload.id, pending: response.status === 202 };
        });
      });
    }

    function waitForMedia(mediaId) {
      var tries = 0;
      function attempt() {
        if (tries >= POLL_MAX_TRIES) {
          throw new Error("media processing timed out after 30s");
        }
        tries += 1;
        return sleep(POLL_INTERVAL_MS)
          .then(function () {
            return fetch("/api/v1/media/" + encodeURIComponent(mediaId), {
              headers: { Authorization: authHeader }
            });
          })
          .then(function (response) {
            if (response.status === 200) {
              return mediaId;
            }
            return attempt();
          });
      }
      return attempt();
    }

    function publish(mediaId, stableKey, entryVisibility) {
      /* Publish first, with an empty body: the transcript is edited in later so
         that tapping the check mark never waits on transcription. */
      return fetch("/api/v1/statuses", {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
          "Idempotency-Key": stableKey
        },
        body: JSON.stringify({
          status: "",
          media_ids: [mediaId],
          visibility: entryVisibility || visibility
        })
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("status publish HTTP " + response.status);
        }
        return response.json();
      });
    }

    function editWithTranscript(statusId, mediaId, text) {
      /* Mastodon's edit API takes media_attributes, so one PUT fills in both the
         body and the audio alt text of the status that is already online. */
      return fetch("/api/v1/statuses/" + encodeURIComponent(statusId), {
        method: "PUT",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          status: clip(text, STATUS_MAX_CHARS),
          media_ids: [mediaId],
          media_attributes: [{ id: mediaId, description: clip(text, ALT_MAX_CHARS) }]
        })
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("status edit HTTP " + response.status);
        }
        return response.json();
      });
    }

    function persistEntry(entry) {
      return outboxPut(entry).then(function () {
        return true;
      }).catch(function (error) {
        warn("could not save recording to the local outbox", error);
        return false;
      });
    }

    function backfill(entry) {
      /* A published entry stays in IndexedDB until the transcript edit succeeds.
         Reopening the page can therefore finish a local transcription that was
         interrupted by navigation, sleep or a temporary model failure. */
      if (!entry.statusId || !entry.mediaId) {
        warn("published status carried no id; skipping the transcript edit");
        return Promise.resolve(false);
      }
      return transcribe(entry.blob, entry.filename)
        .then(function (text) {
          if (!text) {
            return false;
          }
          return editWithTranscript(entry.statusId, entry.mediaId, text).then(function () {
            return outboxDelete(entry.id).catch(function (error) {
              warn("transcript succeeded but the local outbox entry could not be removed", error);
            });
          }).then(function () {
            quietFlash("\\u6587\\u5b57\\u5df2\\u8865\\u4e0a \\u2713");
            return true;
          });
        })
        .catch(function (error) {
          warn("could not add the transcript to " + entry.statusId, error);
          return false;
        });
    }

    function ensureMedia(entry) {
      function markReady() {
        entry.mediaPending = false;
        entry.phase = "media-ready";
        return persistEntry(entry).then(function () {
          return entry.mediaId;
        });
      }

      if (entry.mediaId) {
        return entry.mediaPending
          ? waitForMedia(entry.mediaId).then(markReady)
          : Promise.resolve(entry.mediaId);
      }
      return upload(entry.blob, entry.filename).then(function (media) {
        if (!media.id) {
          throw new Error("media upload returned no id");
        }
        entry.mediaId = String(media.id);
        entry.mediaPending = Boolean(media.pending);
        entry.phase = "uploaded";
        return persistEntry(entry).then(function () {
          return entry.mediaPending ? waitForMedia(entry.mediaId).then(markReady) : markReady();
        });
      });
    }

    function prepareEntry(entry) {
      if (entry.phase !== "recorded" || entry.filename === MP3_NAME) {
        return Promise.resolve(entry);
      }
      return toMp3(entry.blob, entry.filename).then(function (mp3) {
        entry.blob = mp3;
        entry.filename = MP3_NAME;
        entry.mimeType = mp3.type || "audio/mpeg";
        entry.phase = "converted";
        return persistEntry(entry).then(function () {
          return entry;
        });
      });
    }

    function publishEntry(entry) {
      if (entry.statusId) {
        return Promise.resolve(entry);
      }
      return prepareEntry(entry)
        .then(ensureMedia)
        .then(function () {
          return publish(entry.mediaId, entry.idempotencyKey, entry.visibility);
        })
        .then(function (status) {
          entry.statusId = String((status && status.id) || "");
          if (!entry.statusId) {
            throw new Error("status publish returned no id");
          }
          entry.phase = "published";
          return persistEntry(entry).then(function () {
            return entry;
          });
        });
    }

    function makeEntry(blob, filename) {
      var stableKey = idempotencyKey();
      return {
        id: "voice-" + stableKey,
        ownerId: ownerId,
        createdAt: Date.now(),
        visibility: visibility,
        idempotencyKey: stableKey,
        filename: filename,
        mimeType: blob.type || "audio/webm",
        blob: blob,
        phase: "recorded",
        mediaId: "",
        mediaPending: false,
        statusId: ""
      };
    }

    function finishAndPublish() {
      if (busy || !recording) {
        return;
      }
      busy = true;
      stopTimer();
      setChip("\\u4e0a\\u4f20\\u4e2d\\u2026");
      var clipMime = mimeType;
      var entry = null;
      var stored = false;
      stopRecorder()
        .then(function (parts) {
          releaseStream();
          if (!parts.length) {
            throw new Error("empty recording");
          }
          var blob = new Blob(parts, { type: clipMime || parts[0].type || "audio/webm" });
          entry = makeEntry(blob, blobName(blob, clipMime));
          return persistEntry(entry);
        })
        .then(function (saved) {
          stored = saved;
          /* Release the microphone and controls as soon as the recording is
             safely in the local outbox. Remux/upload/publish/transcribe continue
             in the background, so the next recording can start immediately. */
          busy = false;
          resetUi();
          flash("\\u5df2\\u4fdd\\u5b58\\uff0c\\u53d1\\u9001\\u4e2d \\ud83c\\udf99\\ufe0f");
          return publishEntry(entry);
        })
        .then(function (publishedEntry) {
          quietFlash("\\u5df2\\u53d1\\u5e03 \\ud83c\\udf99\\ufe0f");
          /* Publishing is complete; transcription can finish in this page or a
             later page load using the durable entry and no stored credential. */
          backfill(publishedEntry);
        })
        .catch(function (error) {
          warn("publish failed; recording kept in the local outbox", error);
          if (busy) {
            busy = false;
            resetUi();
          }
          quietFlash(stored
            ? "\\u5df2\\u4fdd\\u5b58\\uff0c\\u8054\\u7f51\\u540e\\u91cd\\u4f20"
            : "\\u53d1\\u5e03\\u5931\\u8d25");
        });
    }

    function retryOutbox() {
      if (retrying || busy || recording || navigator.onLine === false) {
        return;
      }
      retrying = true;
      outboxList()
        .then(function (allEntries) {
          var entries = allEntries.filter(function (entry) {
            return entry && entry.blob && (!entry.ownerId || !ownerId || entry.ownerId === ownerId);
          });
          if (!entries.length) {
            return { total: 0, failed: 0 };
          }
          quietFlash("\\u6b63\\u5728\\u91cd\\u4f20 " + entries.length + " \\u6761\\u2026");
          var result = { total: entries.length, failed: 0 };
          var chain = Promise.resolve();
          entries.forEach(function (entry) {
            chain = chain.then(function () {
              return publishEntry(entry)
                .then(backfill)
                .then(function (complete) {
                  if (!complete) {
                    result.failed += 1;
                  }
                })
                .catch(function (error) {
                  result.failed += 1;
                  warn("outbox retry failed for " + entry.id, error);
                });
            });
          });
          return chain.then(function () {
            return result;
          });
        })
        .then(function (result) {
          retrying = false;
          if (result.total && !result.failed) {
            quietFlash("\\u91cd\\u4f20\\u5b8c\\u6210 \\u2713");
          } else if (result.failed) {
            quietFlash("\\u5f85\\u91cd\\u4f20 " + result.failed + " \\u6761");
          }
        })
        .catch(function (error) {
          retrying = false;
          warn("could not resume the local voice outbox", error);
        });
    }

    micButton.addEventListener("click", function () {
      if (busy) {
        return;
      }
      if (recording) {
        finishAndPublish();
        return;
      }
      beginRecording();
    });

    dropButton.addEventListener("click", function () {
      if (busy) {
        return;
      }
      stopRecorder().then(function () {
        resetUi();
        setChip("");
      });
    });

    okButton.addEventListener("click", function () {
      finishAndPublish();
    });

    window.addEventListener("online", retryOutbox);
    window.setTimeout(retryOutbox, 500);
  }

/*__VOICE_PLAYER__*/
/*__IMAGE_RECOGNITION__*/

  function boot() {
    var state = readInitialState();
    if (!state) {
      return;
    }
    try {
      start(state);
    } catch (error) {
      warn("widget failed to start", error);
    }
    try {
      /* Independent of the recorder: a page with no microphone still gets its
         own voice statuses restyled. */
      watchTimeline(state);
    } catch (error) {
      warn("voice player failed to start", error);
    }
    try {
      startImageRecognition(state);
    } catch (error) {
      warn("image recognition hook failed to start", error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""

# The served script is one file; the source is five, so no module carries the
# whole widget. Order is only cosmetic — function declarations hoist — but it
# reads as the pipeline does: draw, decide whose status it is, build the player,
# then keep it applied while the SPA rebuilds underneath.
_PLAYER_SOURCE = "\n".join([
    VOICE_WAVEFORM_JS.strip("\n"),
    VOICE_OWNER_JS.strip("\n"),
    VOICE_PLAYER_JS.strip("\n"),
    VOICE_SCAN_JS.strip("\n"),
])
VOICE_WIDGET_JS = VOICE_WIDGET_JS.replace("/*__VOICE_PLAYER__*/", _PLAYER_SOURCE)
VOICE_WIDGET_JS = VOICE_WIDGET_JS.replace("/*__IMAGE_RECOGNITION__*/", IMAGE_WIDGET_JS.strip("\n"))
