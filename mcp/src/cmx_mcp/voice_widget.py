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
text. Nothing is retried: if transcription fails or the page is closed first, the
status simply stays text-less and the worker's reply remains the fallback.

Since v4 the widget never injects a ``<style>`` element: Mastodon 4.6.3 ships a
strict Content-Security-Policy whose ``style-src`` is locked to a per-response
``'nonce-…'``, so a runtime-injected stylesheet (and its keyframes) is refused by
the browser. Every rule is therefore applied as an inline ``element.style.*``
property and the recording "pulse" is driven by a ``setInterval`` toggle instead
of a CSS animation. (The Nginx site also rewrites the CSP on injected pages so
the external script itself is allowed to load — see ``nginx/default.conf``.)

v5 only resizes: this is the owner's private single-user instance, so the mic
no longer has to be discreet - 64px instead of 48px, resting at 50% opacity
instead of 35%, with the check/cross satellites grown to 44px comfortable tap
targets.

Plain ES2017, no build step, no external dependency, no backticks (the source
must stay safe to embed in any HTML or config context).
"""

from __future__ import annotations

from .voice_player import VOICE_PLAYER_JS

VOICE_WIDGET_VERSION = "7"

VOICE_WIDGET_JS = """/* CMX voice widget v7 - same-origin, relative API, page session token only. */
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
  var OGG_NAME = "voice.ogg";
  var TRANSCRIBE_TIMEOUT_MS = 90000;
  var STATUS_MAX_CHARS = 4900;
  var ALT_MAX_CHARS = 1500;
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
    var recording = false;
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
      micButton.style.background = "rgba(239,68,68,.35)";
      micButton._piRest = "1";
      micButton.style.opacity = "1";
      row.style.display = "flex";
      startPulse();
    }

    function exitRecordingLook() {
      recording = false;
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
        enterRecordingLook();
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

    /* MediaRecorder only emits WebM or MP4, and Mastodon rejects both as audio:
       their magic bytes read as video, so the upload either trips Paperclip's
       spoof check or is typed as a video with no video stream. CMX rewraps the
       recording as Ogg/Opus first — a container swap, not a re-encode, unless
       the source is iOS Safari's AAC. */
    function toOgg(blob, filename) {
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

    function publish(mediaId) {
      /* Publish first, with an empty body: the transcript is edited in later so
         that tapping the check mark never waits on transcription. */
      return fetch("/api/v1/statuses", {
        method: "POST",
        headers: {
          Authorization: authHeader,
          "Content-Type": "application/json",
          "Idempotency-Key": idempotencyKey()
        },
        body: JSON.stringify({ status: "", media_ids: [mediaId], visibility: visibility })
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

    function backfill(statusId, mediaId, blob, filename) {
      /* Fire-and-forget, and deliberately state-free: every value it touches was
         captured when its own status was published, so a second recording started
         meanwhile cannot redirect this edit. No retry - a text-less voice post is
         still a complete post, and the worker's reply covers it. */
      if (!statusId || !mediaId) {
        warn("published status carried no id; skipping the transcript edit");
        return;
      }
      transcribe(blob, filename)
        .then(function (text) {
          if (!text) {
            return null;
          }
          return editWithTranscript(statusId, mediaId, text).then(function () {
            quietFlash("\\u6587\\u5b57\\u5df2\\u8865\\u4e0a \\u2713");
            return null;
          });
        })
        .catch(function (error) {
          warn("could not add the transcript to " + statusId, error);
        });
    }

    micButton.addEventListener("click", function () {
      if (busy) {
        return;
      }
      if (recording) {
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
      /* Locals, captured once per tap: the background transcript edit below runs
         long after resetUi() and after a new recording may have started. */
      var clipMime = mimeType;
      var clipBlob = null;
      var clipName = "";
      var clipMediaId = "";
      stopRecorder()
        .then(function (parts) {
          releaseStream();
          if (!parts.length) {
            throw new Error("empty recording");
          }
          var recorded = new Blob(parts, { type: clipMime || parts[0].type || "audio/webm" });
          return toOgg(recorded, blobName(recorded, clipMime));
        })
        .then(function (ogg) {
          /* From here on the Ogg is the recording: Mastodon accepts it and
             whisper reads it just as happily as the original. */
          clipBlob = ogg;
          clipName = OGG_NAME;
          return upload(clipBlob, clipName);
        })
        .then(function (media) {
          if (!media.id) {
            throw new Error("media upload returned no id");
          }
          clipMediaId = String(media.id);
          return media.pending ? waitForMedia(clipMediaId) : clipMediaId;
        })
        .then(publish)
        .then(function (status) {
          var statusId = String((status && status.id) || "");
          busy = false;
          resetUi();
          flash("\\u5df2\\u53d1\\u5e03 \\ud83c\\udf99\\ufe0f");
          /* The voice post is already online; the transcript catches up on its own. */
          backfill(statusId, clipMediaId, clipBlob, clipName);
        })
        .catch(function (error) {
          warn("publish failed; recording discarded", error);
          busy = false;
          resetUi();
          flash("\\u53d1\\u5e03\\u5931\\u8d25");
        });
    });
  }

  /* ---------------- voice player: own statuses only ---------------- */

  var PLAYER_MARK = "data-pi-voice-player";
  var BAR_WIDTH = 3;
  var BAR_GAP = 2;
  var WAVE_HEIGHT = 32;
  var MIN_BAR = 3;
  var KAI = '"Kaiti SC","STKaiti",KaiTi,"\u6977\u4f53","TW-Kai","LXGW WenKai","AR PL UKai CN",serif';

  function isDarkTheme() {
    try {
      var body = document.body;
      if (body && body.classList.contains("theme-mastodon-light")) {
        return false;
      }
      var bg = window.getComputedStyle(body).backgroundColor || "";
      var nums = bg.replace(/[^0-9,.]/g, "").split(",");
      if (nums.length >= 3) {
        var lum = (Number(nums[0]) * 299 + Number(nums[1]) * 587 + Number(nums[2]) * 114) / 1000;
        return lum < 128;
      }
    } catch (ignored) {
      /* fall through */
    }
    return true;
  }

  function palette() {
    /* One ink, flipped. Light is the softer slate rather than black. */
    return isDarkTheme()
      ? { ink: "#eef1f5", off: "rgba(238,241,245,.22)", hair: "rgba(238,241,245,.13)",
          hover: "rgba(238,241,245,.09)", muted: "rgba(238,241,245,.55)" }
      : { ink: "#4d535f", off: "rgba(77,83,95,.24)", hair: "rgba(77,83,95,.16)",
          hover: "rgba(77,83,95,.08)", muted: "rgba(77,83,95,.75)" };
  }

  function ownAccount(state) {
    try {
      var me = state && state.meta && state.meta.me;
      var account = me && state.accounts && state.accounts[me];
      return account && account.acct ? String(account.acct) : "";
    } catch (ignored) {
      return "";
    }
  }

  function statusOf(node) {
    var current = node;
    while (current && current !== document.body) {
      if (current.classList &&
          (current.classList.contains("status") || current.tagName === "ARTICLE")) {
        return current;
      }
      current = current.parentElement;
    }
    return null;
  }

  function isOwn(status, acct) {
    if (!status || !acct) {
      return false;
    }
    var links = status.querySelectorAll('a[href*="/@"]');
    for (var i = 0; i < links.length; i += 1) {
      var path = "";
      try {
        path = new URL(links[i].href, window.location.href).pathname;
      } catch (ignored) {
        path = "";
      }
      if (path === "/@" + acct || path.indexOf("/@" + acct + "/") === 0) {
        return true;
      }
    }
    return false;
  }

  /* Bucket the decoded samples into one RMS value per bar. RMS, not peak: peaks
     make every bar look the same height once there is any loud moment. */
  function peaksFrom(buffer, count) {
    var data = buffer.getChannelData(0);
    var per = Math.floor(data.length / count) || 1;
    var out = [];
    var max = 0;
    for (var i = 0; i < count; i += 1) {
      var sum = 0;
      var start = i * per;
      for (var j = 0; j < per; j += 1) {
        var v = data[start + j] || 0;
        sum += v * v;
      }
      var rms = Math.sqrt(sum / per);
      out.push(rms);
      if (rms > max) {
        max = rms;
      }
    }
    if (max <= 0) {
      return out.map(function () { return 0.12; });
    }
    return out.map(function (v) { return v / max; });
  }

  function buildBars(wave, peaks, colours) {
    while (wave.firstChild) {
      wave.removeChild(wave.firstChild);
    }
    for (var i = 0; i < peaks.length; i += 1) {
      var bar = document.createElement("span");
      setStyle(bar, {
        width: BAR_WIDTH + "px",
        height: Math.max(MIN_BAR, Math.round(peaks[i] * (WAVE_HEIGHT - 6)) + MIN_BAR) + "px",
        borderRadius: "999px",
        background: colours.off,
        flex: "none",
        display: "block"
      });
      wave.appendChild(bar);
    }
  }

  function mmssClock(seconds) {
    if (!isFinite(seconds) || seconds < 0) {
      seconds = 0;
    }
    return mmss(Math.floor(seconds));
  }

  function decorate(audio, acct) {
    if (audio.getAttribute(PLAYER_MARK) === "1") {
      return;
    }
    var status = statusOf(audio);
    if (!isOwn(status, acct)) {
      return;
    }
    audio.setAttribute(PLAYER_MARK, "1");

    var colours = palette();
    var original = audio.parentElement;
    if (original && original !== status) {
      /* Hide Mastodon's own controls but leave the element in place: it still
         owns the media session, and React can keep re-rendering it safely. */
      original.style.display = "none";
    }

    var host = document.createElement("div");
    setStyle(host, { color: colours.ink, margin: "6px 0 0" });

    var row = document.createElement("div");
    setStyle(row, { display: "flex", alignItems: "center", gap: "13px", padding: "4px 0 10px" });

    var play = document.createElement("button");
    play.type = "button";
    play.setAttribute("aria-label", "\u64ad\u653e");
    setStyle(play, {
      width: "46px", height: "46px", flex: "none", border: "0", borderRadius: "50%",
      padding: "0", cursor: "pointer", background: "none", color: colours.ink,
      display: "grid", placeItems: "center"
    });
    play.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26">'
      + '<path d="M8 5.2v13.6L19 12z"></path></svg>';
    play.addEventListener("mouseenter", function () { play.style.background = colours.hover; });
    play.addEventListener("mouseleave", function () { play.style.background = "none"; });

    var wave = document.createElement("div");
    setStyle(wave, {
      position: "relative", flex: "1", minWidth: "0", height: WAVE_HEIGHT + "px",
      display: "flex", alignItems: "center", gap: BAR_GAP + "px", cursor: "pointer"
    });

    var clock = document.createElement("span");
    setStyle(clock, {
      flex: "none", fontSize: "12.5px", color: colours.muted,
      fontVariantNumeric: "tabular-nums",
      fontFamily: '"SF Mono","Cascadia Mono",Consolas,monospace'
    });
    clock.textContent = "0:00";

    row.appendChild(play);
    row.appendChild(wave);
    row.appendChild(clock);
    host.appendChild(row);

    /* Move Mastodon's own status text below the player and set it in Kai: the
       transcript belongs to the recording, so it reads as one block. */
    var content = status ? status.querySelector(".status__content") : null;
    if (content && !content.getAttribute(PLAYER_MARK)) {
      content.setAttribute(PLAYER_MARK, "1");
      setStyle(content, {
        borderTop: "1px solid " + colours.hair,
        paddingTop: "11px",
        marginTop: "0",
        fontFamily: KAI,
        fontSize: "17px",
        lineHeight: "1.85"
      });
      host.appendChild(content);
    }

    var anchor = original && original.parentElement ? original.parentElement : status;
    if (anchor) {
      anchor.appendChild(host);
    }

    var peaks = null;
    function paint() {
      if (!peaks) {
        return;
      }
      var ratio = audio.duration ? audio.currentTime / audio.duration : 0;
      var bars = wave.children;
      for (var i = 0; i < bars.length; i += 1) {
        bars[i].style.background = (i / bars.length) < ratio ? colours.ink : colours.off;
      }
      clock.textContent = mmssClock(audio.currentTime || 0)
        + (audio.duration && isFinite(audio.duration) ? " / " + mmssClock(audio.duration) : "");
    }

    function layout() {
      var width = wave.clientWidth || 300;
      var count = Math.max(20, Math.floor(width / (BAR_WIDTH + BAR_GAP)));
      if (!peaks || peaks.length !== count) {
        peaks = peaks && peaks.length
          ? resample(peaks, count)
          : new Array(count).fill(0.4);
        buildBars(wave, peaks, colours);
      }
      paint();
    }

    function resample(source, count) {
      var out = [];
      for (var i = 0; i < count; i += 1) {
        out.push(source[Math.floor((i / count) * source.length)] || 0.1);
      }
      return out;
    }

    layout();
    window.addEventListener("resize", layout);

    /* Real amplitudes, fetched same-origin and decoded once. If anything fails
       the flat placeholder bars stay and playback still works. */
    if (audio.currentSrc && window.AudioContext) {
      fetch(audio.currentSrc, { credentials: "same-origin" })
        .then(function (response) { return response.arrayBuffer(); })
        .then(function (bytes) {
          var context = new window.AudioContext();
          return context.decodeAudioData(bytes).then(function (buffer) {
            context.close();
            return buffer;
          });
        })
        .then(function (buffer) {
          var count = wave.children.length || 40;
          peaks = peaksFrom(buffer, count);
          buildBars(wave, peaks, colours);
          paint();
        })
        .catch(function (error) {
          warn("waveform unavailable; using flat bars", error);
        });
    }

    play.addEventListener("click", function () {
      if (audio.paused) {
        audio.play();
      } else {
        audio.pause();
      }
    });
    wave.addEventListener("click", function (event) {
      var rect = wave.getBoundingClientRect();
      var ratio = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
      if (audio.duration && isFinite(audio.duration)) {
        audio.currentTime = ratio * audio.duration;
        paint();
      }
    });
    audio.addEventListener("timeupdate", paint);
    audio.addEventListener("loadedmetadata", paint);
    audio.addEventListener("play", function () {
      play.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26">'
        + '<path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"></path></svg>';
    });
    audio.addEventListener("pause", function () {
      play.innerHTML = '<svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26">'
        + '<path d="M8 5.2v13.6L19 12z"></path></svg>';
    });
    paint();
  }

  function scanForVoice(acct) {
    if (!acct) {
      return;
    }
    var players = document.querySelectorAll("audio");
    for (var i = 0; i < players.length; i += 1) {
      try {
        decorate(players[i], acct);
      } catch (error) {
        warn("could not restyle a voice status", error);
      }
    }
  }

  function watchTimeline(state) {
    var acct = ownAccount(state);
    if (!acct) {
      return;
    }
    scanForVoice(acct);
    var pending = 0;
    var observer = new MutationObserver(function () {
      /* React re-renders in bursts; coalesce so we scan once per frame. */
      if (pending) {
        return;
      }
      pending = window.setTimeout(function () {
        pending = 0;
        scanForVoice(acct);
      }, 120);
    });
    observer.observe(document.body, { childList: true, subtree: true });
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
    try {
      /* Independent of the recorder: a page with no microphone still gets its
         own voice statuses restyled. */
      watchTimeline(state);
    } catch (error) {
      warn("voice player failed to start", error);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
"""
