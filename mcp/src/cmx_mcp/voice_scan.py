"""Finding the Owner's voice statuses and keeping the player applied.

The hard part of decorating someone else's SPA is not building the UI, it is
staying applied while the SPA rebuilds around you. Three rules earned the hard
way, each verified against docs/clip-brain/design/flicker-harness/:

* claim the media **inside** the MutationObserver callback. That runs after the
  mutation and before the frame is painted, so Mastodon's own player is never
  drawn. Hiding early but inserting later still left a visible blank;
* coalesce the sweep with requestAnimationFrame **and** a timeout. A hidden or
  backgrounded tab never runs rAF, and with rAF alone the in-flight flag would
  stay set and the observer would be dead for the rest of the session;
* one resize listener for the whole page. One per player leaked without bound,
  because a virtualised timeline remounts rows every time you scroll.
"""

from __future__ import annotations

VOICE_SCAN_JS = """
  /* ---------------- voice player: staying applied ---------------- */
  /* A single resize handler for every player on the page. One per decorate
     leaked: a virtualised timeline remounts rows as they cross the viewport, so
     scrolling added handlers without bound, each pinning a detached subtree. */
  var relayoutBound = false;
  function bindRelayout() {
    if (relayoutBound) {
      return;
    }
    relayoutBound = true;
    window.addEventListener("resize", function () {
      var players = document.querySelectorAll("audio, video");
      for (var i = 0; i < players.length; i += 1) {
        if (typeof players[i]._piLayout === "function") {
          players[i]._piLayout();
        }
      }
    });
  }

  /* Replaced once an account resolves. Until then the answer to "why is
     nothing claimed" is this note, which is itself the diagnosis. */
  window.__piVoiceDebug = function () {
    return {
      version: window.__piVoicePlayer,
      acct: "",
      note: "no signed-in account resolved yet"
    };
  };

  /* One console call that says which link in the chain is broken, because the
     desktop timeline has never been claimed and the offline harness cannot
     answer it: its markup is a hand-made imitation, so it can prove there is no
     loop and no leak but never that a selector matches the real thing. */
  function installDebug(acct) {
    window.__piVoiceDebug = function () {
      var media = document.querySelectorAll("audio, video");
      var report = {
        version: window.__piVoicePlayer,
        acct: acct,
        media: media.length,
        inStatus: 0,
        own: 0,
        claimed: 0,
        samples: []
      };
      for (var i = 0; i < media.length; i += 1) {
        var element = media[i];
        var status = statusOf(element);
        if (status) {
          report.inStatus += 1;
        }
        if (isOwn(status, acct)) {
          report.own += 1;
        }
        if (element.getAttribute(PLAYER_MARK) === "1") {
          report.claimed += 1;
        }
        if (report.samples.length < 3) {
          report.samples.push({
            tag: element.tagName,
            src: String(mediaSource(element)).slice(0, 90),
            status: status ? (status.className || status.tagName) : null,
            acctLinks: status ? status.querySelectorAll('a[href*="/@"]').length : 0
          });
        }
      }
      return report;
    };
  }

  function scanForVoice(acct) {
    bindRelayout();
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
    installDebug(acct);
    scanForVoice(acct);
    var pending = false;
    function flush() {
      if (!pending) {
        return;
      }
      pending = false;
      scanForVoice(acct);
    }
    /* Runs synchronously inside the observer callback, which fires after the
       mutation but before the frame is painted, so Mastodon's player is never
       drawn. It only *hides*.

       v14 built the whole replacement here and broke two things at once: the
       element has no chosen source this early, so the waveform sampling was
       skipped and never retried, and driving a player React had not finished
       wiring left it silent. Hiding is the only part that has to beat the
       paint; the rest belongs one frame later, which is where v13 had it. */
    function claimEarly(records) {
      for (var r = 0; r < records.length; r += 1) {
        var added = records[r].addedNodes;
        for (var a = 0; a < added.length; a += 1) {
          var node = added[a];
          if (!node || node.nodeType !== 1) {
            continue;
          }
          var media = node.querySelectorAll
            ? node.querySelectorAll("audio, video")
            : [];
          for (var m = 0; m < media.length; m += 1) {
            var element = media[m];
            if (element.getAttribute(PLAYER_MARK) === "1") {
              continue;
            }
            claimQuietly(element, acct);
          }
        }
      }
    }

    var observer = new MutationObserver(function (records) {
      try {
        claimEarly(records);
      } catch (error) {
        warn("could not claim a player before paint", error);
      }
      /* Coalesce to the next frame so the swap happens before Mastodon's own
         player is ever painted — a 120 ms timer was long enough to see it.
         The timeout is not a duplicate: a hidden or backgrounded tab never runs
         requestAnimationFrame, and with rAF alone the in-flight flag would stay set and
         the observer would be dead for the rest of the session. Whichever fires
         first wins; the flag makes the other a no-op. */
      if (pending) {
        return;
      }
      pending = true;
      if (window.requestAnimationFrame) {
        window.requestAnimationFrame(flush);
      }
      window.setTimeout(flush, 100);
    });
    /* childList only. Observing attributes would make our own restyling
       re-trigger the observer, which is the loop this feature has to avoid. */
    observer.observe(document.body, { childList: true, subtree: true });
  }
"""
