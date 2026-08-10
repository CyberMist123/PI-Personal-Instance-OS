"""Deciding whether a status belongs to the signed-in account.

The player only ever touches the Owner's own voice notes, so this question gates
everything else. Split out of voice_player to keep both files under the stop
line; the two share one JavaScript scope at runtime.
"""

from __future__ import annotations

VOICE_OWNER_JS = """
  /* ---------------- voice player: whose status is this ---------------- */

  function ownAccount(state) {
    try {
      var me = state && state.meta && state.meta.me;
      var account = me && state.accounts && state.accounts[me];
      return account && account.acct ? String(account.acct) : "";
    } catch (ignored) {
      return "";
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
"""
