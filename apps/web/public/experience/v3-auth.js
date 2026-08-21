import {initTheme} from "./shared/theme.js";

const DEFAULT_DESTINATION = "/create";

function safeReturnTo(search = globalThis.location?.search ?? "") {
  const value = new URLSearchParams(search).get("returnTo");
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) {
    return DEFAULT_DESTINATION;
  }

  try {
    const base = globalThis.location?.origin ?? "https://framefactory.local";
    const destination = new URL(value, base);
    if (destination.origin !== new URL(base).origin) return DEFAULT_DESTINATION;
    if (["/login", "/signin-with-chatgpt", "/signout-with-chatgpt", "/callback"].includes(destination.pathname)) {
      return DEFAULT_DESTINATION;
    }
    return `${destination.pathname}${destination.search}${destination.hash}`;
  } catch {
    return DEFAULT_DESTINATION;
  }
}

initTheme(document);

const signinLink = document.querySelector("[data-signin-link]");
if (signinLink) {
  const returnTo = safeReturnTo();
  signinLink.href = `/signin-with-chatgpt?return_to=${encodeURIComponent(returnTo)}`;
}
