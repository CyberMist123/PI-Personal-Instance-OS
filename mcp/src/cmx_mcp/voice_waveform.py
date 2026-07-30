"""The drawing half of the voice player: theme, amplitudes, bars.

Shipped inside ``/files/voice.js`` ahead of voice_player, which does the DOM
wiring. Split purely so neither file crosses the stop line; the two halves share
one JavaScript scope at runtime.
"""

from __future__ import annotations

VOICE_WAVEFORM_JS = """
  /* ---------------- voice player: drawing ---------------- */

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
      /* fall through to the dark default */
    }
    return true;
  }

  function palette() {
    /* One ink, flipped by theme. The light value is a slate rather than black:
       pure black reads as heavy against Mastodon's white cards. */
    return isDarkTheme()
      ? { ink: "#eef1f5", off: "rgba(238,241,245,.22)", hair: "rgba(238,241,245,.13)",
          hover: "rgba(238,241,245,.09)", muted: "rgba(238,241,245,.55)" }
      : { ink: "#4d535f", off: "rgba(77,83,95,.24)", hair: "rgba(77,83,95,.16)",
          hover: "rgba(77,83,95,.08)", muted: "rgba(77,83,95,.75)" };
  }

  /* Bucket the decoded samples into one RMS value per bar. RMS, not peak: a
     single loud moment makes every peak-based bar hit the ceiling, and the
     waveform stops describing the speech. */
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

  function resamplePeaks(source, count) {
    var out = [];
    for (var i = 0; i < count; i += 1) {
      out.push(source[Math.floor((i / count) * source.length)] || 0.1);
    }
    return out;
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

  function playGlyph(paused) {
    return paused
      ? '<svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26">'
        + '<path d="M8 5.2v13.6L19 12z"></path></svg>'
      : '<svg viewBox="0 0 24 24" fill="currentColor" width="26" height="26">'
        + '<path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.4z"></path></svg>';
  }
"""
