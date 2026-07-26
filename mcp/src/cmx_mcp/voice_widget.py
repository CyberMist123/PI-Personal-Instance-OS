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

Since v2 the published status carries its own transcript: ✓ first posts the blob
to the same-origin ``/files/transcribe`` (CMX's own endpoint, which verifies that
same page token against the instance), then publishes **one** status whose body
is the transcript and whose audio attachment carries it as alt text. When
transcription is unavailable the body stays empty, exactly like v1, and the
worker's reply remains the fallback.

Plain ES2017, no build step, no external dependency, no backticks (the source
must stay safe to embed in any HTML or config context).
"""

from __future__ import annotations

VOICE_WIDGET_VERSION = "2"

VOICE_WIDGET_JS = """/* CMX voice widget v2 - same-origin, relative API, page session token only. */
(function () {
  "use strict";

  if (window.__piVoiceWidget) {
    return;
  }
  window.__piVoiceWidget = "1";

  var LOG = "[pi-voice]";
  var MIME_CANDIDATES = ["audio/mp4", "audio/webm;codecs=opus", "audio/webm"];
  var POLL_INTERVAL_MS = 1000;
  var POLL_MAX_TRIES = 30;
  var TRANSCRIBE_PATH = "/files/transcribe";
  var TRANSCRIBE_TIMEOUT_MS = 90000;
  var STATUS_MAX_CHARS = 4900;
  var ALT_MAX_CHARS = 1500;

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

  function injectStyle() {
    if (document.getElementById("pi-voice-style")) {
      return;
    }
    var style = document.createElement("style");
    style.id = "pi-voice-style";
    style.textContent = [
      "@keyframes piVoicePulse{0%{transform:scale(1)}50%{transform:scale(1.08)}100%{transform:scale(1)}}",
      "#pi-voice-root{position:fixed;right:18px;bottom:84px;z-index:9999;display:flex;",
      "flex-direction:column;align-items:center;gap:8px;pointer-events:none}",
      "#pi-voice-root>*{pointer-events:auto}",
      "#pi-voice-btn{width:48px;height:48px;border-radius:50%;border:0;cursor:pointer;",
      "font-size:22px;line-height:1;display:flex;align-items:center;justify-content:center;",
      "background:rgba(99,102,241,.25);color:#fff;opacity:.35;transition:.2s;",
      "-webkit-tap-highlight-color:transparent}",
      "#pi-voice-btn:hover,#pi-voice-btn:focus,#pi-voice-root.pi-voice-active #pi-voice-btn{opacity:1}",
      "#pi-voice-root.pi-voice-active #pi-voice-btn{background:rgba(239,68,68,.35);",
      "animation:piVoicePulse 1.4s ease-in-out infinite}",
      ".pi-voice-sat{width:34px;height:34px;border-radius:50%;border:0;cursor:pointer;font-size:15px;",
      "line-height:1;display:flex;align-items:center;justify-content:center;color:#fff;",
      "background:rgba(31,41,55,.72);opacity:.95;transition:.2s}",
      ".pi-voice-sat:hover,.pi-voice-sat:focus{opacity:1}",
      "#pi-voice-ok{background:rgba(34,197,94,.75)}",
      "#pi-voice-row{display:flex;gap:8px}",
      "#pi-voice-chip{font:12px/1.4 system-ui,sans-serif;color:#f9fafb;background:rgba(17,24,39,.78);",
      "border-radius:10px;padding:3px 9px;white-space:nowrap;transition:.2s}",
      "#pi-voice-root:not(.pi-voice-active) #pi-voice-row{display:none}"
    ].join("");
    (document.head || document.documentElement).appendChild(style);
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
    var authHeader = "Bearer " + token;

    injectStyle();

    var root = document.createElement("div");
    root.id = "pi-voice-root";

    var chip = document.createElement("div");
    chip.id = "pi-voice-chip";
    chip.style.display = "none";

    var row = document.createElement("div");
    row.id = "pi-voice-row";

    var okButton = document.createElement("button");
    okButton.id = "pi-voice-ok";
    okButton.type = "button";
    okButton.className = "pi-voice-sat";
    okButton.textContent = "\\u2713";
    okButton.setAttribute("aria-label", "\\u53d1\\u5e03\\u8bed\\u97f3");

    var dropButton = document.createElement("button");
    dropButton.id = "pi-voice-drop";
    dropButton.type = "button";
    dropButton.className = "pi-voice-sat";
    dropButton.textContent = "\\u2715";
    dropButton.setAttribute("aria-label", "\\u4e22\\u5f03\\u5f55\\u97f3");

    var micButton = document.createElement("button");
    micButton.id = "pi-voice-btn";
    micButton.type = "button";
    micButton.textContent = "\\ud83c\\udf99\\ufe0f";
    micButton.setAttribute("aria-label", "\\u8bed\\u97f3\\u4fbf\\u7b7e");
    micButton.title = "\\u8bed\\u97f3\\u4fbf\\u7b7e";

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
    var mimeType = "";

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
      root.classList.remove("pi-voice-active");
      stopTimer();
      elapsed = 0;
      chunks = [];
      recorder = null;
      releaseStream();
    }

    function beginRecording() {
      navigator.mediaDevices.getUserMedia({ audio: true }).then(function (granted) {
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
        root.classList.add("pi-voice-active");
        elapsed = 0;
        setChip(mmss(0));
        timerId = window.setInterval(function () {
          elapsed += 1;
          setChip(mmss(elapsed));
        }, 1000);
      }, function (error) {
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

    function clipName(blob) {
      var isMp4 = (blob.type || mimeType).indexOf("mp4") >= 0;
      return isMp4 ? "voice.m4a" : "voice.webm";
    }

    function transcribe(blob) {
      /* CMX's own same-origin endpoint: it re-verifies this page token against
         the instance, transcribes locally and stores nothing. Any failure is
         non-fatal - the status is then published with an empty body and the
         worker's reply stays as the fallback. */
      var form = new FormData();
      form.append("file", blob, clipName(blob));
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
          warn("transcription unavailable; posting voice only", error);
          return "";
        });
    }

    function upload(blob, description) {
      var form = new FormData();
      form.append("file", blob, clipName(blob));
      if (description) {
        /* Alt text keeps the audio accessible and gives MCP a compact media alt. */
        form.append("description", description);
      }
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

    function publish(mediaId, statusText) {
      return fetch("/api/v1/statuses", {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey()
        },
        body: JSON.stringify({
          status: statusText || "",
          media_ids: [mediaId],
          visibility: visibility
        })
      }).then(function (response) {
        if (!response.ok) {
          throw new Error("status publish HTTP " + response.status);
        }
        return response.json();
      });
    }

    micButton.addEventListener("click", function () {
      if (busy) {
        return;
      }
      if (root.classList.contains("pi-voice-active")) {
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
      if (busy) {
        return;
      }
      busy = true;
      stopTimer();
      setChip("\\u23f3");
      stopRecorder()
        .then(function (parts) {
          releaseStream();
          if (!parts.length) {
            throw new Error("empty recording");
          }
          return new Blob(parts, { type: mimeType || parts[0].type || "audio/webm" });
        })
        .then(function (blob) {
          /* One status carries both the audio bubble and its transcript. */
          return transcribe(blob).then(function (text) {
            return upload(blob, clip(text, ALT_MAX_CHARS)).then(function (media) {
              return { media: media, text: clip(text, STATUS_MAX_CHARS) };
            });
          });
        })
        .then(function (bundle) {
          if (!bundle.media.id) {
            throw new Error("media upload returned no id");
          }
          var ready = bundle.media.pending
            ? waitForMedia(bundle.media.id)
            : Promise.resolve(bundle.media.id);
          return ready.then(function (mediaId) {
            return publish(mediaId, bundle.text);
          });
        })
        .then(function () {
          busy = false;
          resetUi();
          flash("\\u5df2\\u53d1\\u5e03 \\ud83c\\udf99\\ufe0f");
        })
        .catch(function (error) {
          warn("publish failed; recording discarded", error);
          busy = false;
          resetUi();
          flash("\\u53d1\\u5e03\\u5931\\u8d25");
        });
    });
  }

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
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""
