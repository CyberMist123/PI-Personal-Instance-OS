(function () {
  "use strict";

  // The page reuses the browser's own Mastodon session. The access token is
  // read once from the instance's own HTML and kept in a closure: it is never
  // written to localStorage, IndexedDB, a cookie, or a URL.
  let accessToken = "";
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });

  function isLocalDemo() {
    const loopback = ["127.0.0.1", "localhost", "[::1]"].includes(window.location.hostname);
    return loopback && window.location.port === "4173";
  }

  function readAccessToken(html) {
    const documentCopy = new DOMParser().parseFromString(html, "text/html");
    const stateNode = documentCopy.querySelector("#initial-state");
    if (!stateNode) return "";

    const raw = stateNode.textContent || stateNode.getAttribute("data-props") || "";
    try {
      const state = JSON.parse(raw);
      return String(
        (state.meta && (state.meta.access_token || state.meta.accessToken)) || "",
      );
    } catch (_) {
      return "";
    }
  }

  function deny() {
    document.documentElement.dataset.authState = "denied";
    resolveReady(false);
    window.location.replace("/auth/sign_in");
  }

  async function authorize() {
    if (isLocalDemo()) {
      document.documentElement.dataset.authState = "ready";
      resolveReady(true);
      return;
    }

    try {
      const response = await fetch("/", {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "text/html" },
      });
      if (!response.ok) throw new Error(`Mastodon session check returned ${response.status}`);

      const token = readAccessToken(await response.text());
      if (!token) return deny();

      accessToken = token;
      document.documentElement.dataset.authState = "ready";
      resolveReady(true);
    } catch (_) {
      deny();
    }
  }

  window.ClipAuth = Object.freeze({
    isLocalDemo,
    ready: () => ready,
    token: () => accessToken,
  });

  authorize();
}());
