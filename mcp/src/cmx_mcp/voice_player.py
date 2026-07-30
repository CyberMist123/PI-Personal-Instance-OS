"""Replaces Mastodon's audio player on the Owner's own voice statuses.

Shipped as part of the same injected ``/files/voice.js`` (see voice_widget), but
kept in its own module so neither file grows past the stop line.

Three things make this safe to do to someone else's SPA:

* it never removes Mastodon's nodes, it only hides the original player and
  drives that same ``<audio>`` element, so pausing, seeking and cleanup all stay
  Mastodon's;
* it finds work by looking for ``<audio>`` rather than by class name, because
  Mastodon's markup changes between releases and ours should not care;
* React re-renders the timeline constantly, so every insertion is marked and a
  MutationObserver re-applies it. Running twice over the same status is a no-op.

Colours are a single ink that flips with the theme — the light value is the
slate the Owner picked, deliberately softer than black. The transcript is set in
Kai where the platform has it.
"""

from __future__ import annotations

VOICE_PLAYER_VERSION = "1"

VOICE_PLAYER_JS = """
  /* ---------------- voice player: own statuses only ---------------- */

  var PLAYER_MARK = "data-pi-voice-player";
  var BAR_WIDTH = 3;
  var BAR_GAP = 2;
  var WAVE_HEIGHT = 32;
  var MIN_BAR = 3;
  var KAI = '"Kaiti SC","STKaiti",KaiTi,"\\u6977\\u4f53","TW-Kai","LXGW WenKai","AR PL UKai CN",serif';

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
    play.setAttribute("aria-label", "\\u64ad\\u653e");
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
"""
