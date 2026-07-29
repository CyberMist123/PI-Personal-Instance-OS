(function () {
  "use strict";

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

  async function authorize() {
    if (isLocalDemo()) {
      document.documentElement.dataset.authState = "ready";
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
      if (!token) {
        document.documentElement.dataset.authState = "denied";
        window.location.replace("/auth/sign_in");
        return;
      }

      document.documentElement.dataset.authState = "ready";
    } catch (_) {
      document.documentElement.dataset.authState = "denied";
      window.location.replace("/auth/sign_in");
    }
  }

  authorize();
}());
