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
    audio.addEventListener("loadedmetadata", function () { layout(); paint(); });
    audio.addEventListener("play", function () {
      play.innerHTML = playGlyph(false);
    });
    audio.addEventListener("pause", function () {
      play.innerHTML = playGlyph(true);
    });
    paint();
  }

  function scanForVoice(acct) {
    /* audio *and* video: Mastodon files an ambiguous container as video,
       and the element still exposes the same play/seek surface. */
    if (!acct) {
      return;
    }
    var players = document.querySelectorAll("audio, video");
    for (var i = 0; i < players.length; i += 1) {
      try {
        decorate(players[i], acct);
      } catch (error) {
        warn("could not restyle a voice status", error);
      }
    }
  }

  function watchTimeline(state) {
    resolveAcct(state).then(function (acct) {
      if (!acct) {
        return;
      }
      startWatching(acct);
    });
  }

  function startWatching(acct) {
    ensureKaiFont();
    scanForVoice(acct);
    var pending = false;
    var schedule = window.requestAnimationFrame
      ? function (fn) { window.requestAnimationFrame(fn); }
      : function (fn) { window.setTimeout(fn, 16); };
    var observer = new MutationObserver(function () {
      /* React re-renders in bursts, so coalesce — but to the next frame, not to
         a timer. A 120 ms window was long enough to see Mastodon's own player
         appear and then get swapped out. */
      if (pending) {
        return;
      }
      pending = true;
      schedule(function () {
        pending = false;
        scanForVoice(acct);
      });
    });
    /* childList only. Observing attributes would make our own restyling
       re-trigger the observer, which is the loop this feature has to avoid. */
    observer.observe(document.body, { childList: true, subtree: true });
  }
"""
