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

VOICE_PLAYER_JS = """
  /* ---------------- voice player: DOM wiring ---------------- */

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

    /* Move Mastodon's own status text below the player and set it in Kai: the
       transcript belongs to the recording, so it reads as one block. */
    var content = status ? status.querySelector(".status__content") : null;
    if (content && !content.getAttribute(PLAYER_MARK)) {
      content.setAttribute(PLAYER_MARK, "1");
      /* No rule between player and transcript: the two are one utterance, and
         spacing already says so. */
      setStyle(content, {
        paddingTop: "2px",
        marginTop: "0",
        fontSize: "17px",
        lineHeight: "1.85"
      });
      applyKai(content);
      host.appendChild(content);
    }

    /* Put the player exactly where Mastodon's was, not at the end of the
       status: appending pushed it below the reply/boost row. */
    if (original && original.parentElement) {
      original.parentElement.insertBefore(host, original.nextSibling);
    } else if (status) {
      status.appendChild(host);
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

  function resolveAcct(state) {
    var acct = ownAccount(state);
    if (acct) {
      return Promise.resolve(acct);
    }
    /* #initial-state does not always carry the full account object — it is
       populated differently across views, which is why this worked on the phone
       and not on the desktop timeline. Ask the instance instead of giving up:
       one request, the page's own token, nothing stored. */
    var token = pickToken(state);
    if (!token) {
      return Promise.resolve("");
    }
    return fetch("/api/v1/accounts/verify_credentials", {
      cache: "no-store",
      headers: { Authorization: "Bearer " + token, Accept: "application/json" }
    }).then(function (response) {
      return response.ok ? response.json() : null;
    }).then(function (payload) {
      return payload && payload.acct ? String(payload.acct) : "";
    }).catch(function (error) {
      warn("could not resolve the signed-in account", error);
      return "";
    });
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
