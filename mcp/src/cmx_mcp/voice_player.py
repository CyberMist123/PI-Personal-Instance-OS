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

And one thing it must never do: relocate a node React owns. Moving
``.status__content`` under the player made React put it back, which tripped the
observer, which moved it again — the timeline strobed. The player is inserted
*before* the text instead, so the reading order is the same and no node ever
changes parent.

Colours are a single ink that flips with the theme — the light value is the
slate the Owner picked, deliberately softer than black. The transcript is set in
Kai where the platform has it.
"""

from __future__ import annotations

VOICE_PLAYER_JS = """
  /* ---------------- voice player: DOM wiring ---------------- */

  /* Where the audio actually is, asked in the order the answers appear.
     currentSrc alone is empty until the element has selected a resource, which
     on a freshly inserted node has not happened yet — that emptiness is what
     silently skipped the sampling in v14 and left every bar at placeholder
     height. */
  function mediaSource(audio) {
    var direct = audio.currentSrc || audio.getAttribute("src") || "";
    if (direct) {
      return direct;
    }
    var source = audio.querySelector ? audio.querySelector("source[src]") : null;
    return source ? (source.getAttribute("src") || "") : "";
  }

  /* Hide Mastodon's chrome without taking the element out of the render tree.
     display:none did take it out, and a media element in a subtree that is not
     rendered will not play on iOS — the phone showed a player that did
     nothing. Clipping keeps it rendered, keeps Mastodon owning the media
     session, and still shows nothing. */
  function hideNativeChrome(audio) {
    var status = statusOf(audio);
    var original = audio.parentElement;
    if (!original || original === status) {
      return null;
    }
    if (original.getAttribute(HIDDEN_MARK) === "1") {
      return original;
    }
    original.setAttribute(HIDDEN_MARK, "1");
    original.setAttribute("aria-hidden", "true");
    setStyle(original, {
      position: "absolute", width: "1px", height: "1px", margin: "-1px",
      padding: "0", border: "0", overflow: "hidden", opacity: "0",
      pointerEvents: "none", clip: "rect(0 0 0 0)", clipPath: "inset(50%)"
    });
    return original;
  }

  /* The first half of the swap, run synchronously inside the observer callback
     so Mastodon's player is never painted. Only hiding happens here: building
     the replacement this early is what broke the waveform and the sound, since
     React has not finished with the element yet. */
  function claimQuietly(audio, acct) {
    if (!isOwn(statusOf(audio), acct)) {
      return;
    }
    hideNativeChrome(audio);
  }

  function decorate(audio, acct) {
    /* Done already *and* still on the page. React can drop our host during a
       re-render; when it does, fall through and put it back rather than leaving
       the status with a hidden player and nothing in its place. */
    if (audio.getAttribute(PLAYER_MARK) === "1" && audio._piHost && audio._piHost.isConnected) {
      return;
    }
    if (audio._piHost && !audio._piHost.isConnected) {
      audio.removeAttribute(PLAYER_MARK);
      audio._piHost = null;
    }
    if (audio.getAttribute(PLAYER_MARK) === "1") {
      return;
    }
    var status = statusOf(audio);
    if (!isOwn(status, acct)) {
      return;
    }
    audio.setAttribute(PLAYER_MARK, "1");

    var colours = palette();
    /* Usually already hidden by claimQuietly one frame earlier; this covers
       nodes that were moved rather than added, which the observer reports
       without an addedNodes entry. */
    var original = hideNativeChrome(audio);

    var host = document.createElement("div");
    host.setAttribute("data-pi-host", "1");
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
    play.innerHTML = playGlyph(true);
    play.addEventListener("mouseenter", function () { play.style.background = colours.hover; });
    play.addEventListener("mouseleave", function () { play.style.background = "none"; });

    var wave = document.createElement("div");
    setStyle(wave, {
      position: "relative", flex: "1", minWidth: "0", height: WAVE_HEIGHT + "px",
      display: "flex", alignItems: "center", gap: BAR_GAP + "px", cursor: "pointer"
    });

    var clock = document.createElement("span");
    setStyle(clock, {
      flex: "none", minWidth: "42px", textAlign: "right",
      fontSize: "12.5px", color: colours.muted,
      fontVariantNumeric: "tabular-nums",
      fontFamily: '"SF Mono","Cascadia Mono",Consolas,monospace'
    });
    clock.textContent = "0:00";

    row.appendChild(play);
    row.appendChild(wave);
    row.appendChild(clock);
    host.appendChild(row);

    /* Style Mastodon's text where it stands. Never relocate it: that node
       belongs to React, React puts it back, the observer sees the change and we
       move it again — which is exactly what made the timeline strobe. Styles are
       safe because only childList is observed, so restyling cannot re-trigger a
       pass. */
    var content = status ? status.querySelector(".status__content") : null;
    if (content) {
      setStyle(content, {
        paddingTop: "2px",
        marginTop: "0",
        fontSize: "17px",
        lineHeight: "1.85"
      });
      applyKai(content);
    }

    /* Sit above the text instead: player, then transcript, with Mastodon's own
       player hidden below. One sibling insert, and no node changes parent. */
    var anchor = content && content.parentElement ? content : original;
    if (anchor && anchor.parentElement) {
      anchor.parentElement.insertBefore(host, anchor);
    } else if (status) {
      status.appendChild(host);
    }
    audio._piHost = host;

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
      /* Duration while idle, elapsed while playing — one value, so the clock
         never changes width and the bars keep the space they were sized for. */
      var showing = audio.paused && !audio.currentTime
        ? (audio.duration && isFinite(audio.duration) ? audio.duration : 0)
        : (audio.currentTime || 0);
      clock.textContent = mmssClock(showing);
    }

    function layout() {
      var width = wave.clientWidth || 300;
      var count = Math.max(20, Math.floor(width / (BAR_WIDTH + BAR_GAP)));
      if (!peaks || peaks.length !== count) {
        peaks = peaks && peaks.length
          ? resamplePeaks(peaks, count)
          : new Array(count).fill(0.4);
        buildBars(wave, peaks, colours);
      }
      paint();
    }

    layout();
    audio._piLayout = layout;

    /* Real amplitudes, fetched same-origin and decoded once. If anything fails
       the flat placeholder bars stay and playback still works.

       Deliberately not a one-shot at decorate time: the source is often not
       chosen yet at the moment we claim the element, and decorate runs once per
       element, so a single early attempt meant the waveform never appeared at
       all. Try now, and try again on the events that fire once the element has
       a resource. */
    var sampleAttempts = 0;
    var sampled = false;
    function sampleWaveform() {
      if (sampled || sampleAttempts >= 3 || !window.AudioContext) {
        return;
      }
      var url = mediaSource(audio);
      if (!url) {
        return;
      }
      sampleAttempts += 1;
      fetch(url, { credentials: "same-origin" })
        .then(function (response) { return response.arrayBuffer(); })
        .then(function (bytes) {
          var context = new window.AudioContext();
          return context.decodeAudioData(bytes).then(function (buffer) {
            context.close();
            return buffer;
          });
        })
        .then(function (buffer) {
          sampled = true;
          var count = wave.children.length || 40;
          peaks = peaksFrom(buffer, count);
          buildBars(wave, peaks, colours);
          paint();
        })
        .catch(function (error) {
          warn("waveform unavailable; using flat bars", error);
        });
    }
    sampleWaveform();

    play.addEventListener("click", function () {
      if (!audio.paused) {
        audio.pause();
        return;
      }
      /* play() rejects rather than throws when a browser refuses — an unhandled
         rejection is exactly how "the button does nothing" stays a mystery. */
      var started = audio.play();
      if (started && typeof started.catch === "function") {
        started.catch(function (error) {
          warn("playback was refused by the browser", error);
        });
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
    audio.addEventListener("loadedmetadata", function () {
      layout();
      paint();
      sampleWaveform();
    });
    /* canplay and the first play are the two later moments where a source is
       guaranteed to exist, whatever the element looked like when we claimed it. */
    audio.addEventListener("canplay", sampleWaveform);
    audio.addEventListener("play", function () {
      play.innerHTML = playGlyph(false);
      sampleWaveform();
    });
    audio.addEventListener("pause", function () {
      play.innerHTML = playGlyph(true);
    });
    paint();
  }
"""
