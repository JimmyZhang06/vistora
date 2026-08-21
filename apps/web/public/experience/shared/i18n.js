import {locales} from "./locales.js";

export const supportedLocales = ["zh-CN", "en"];
export const localeStorageKey = "ponder-locale";

export function flattenLocale(value, prefix = "", result = {}) {
  for (const [key, child] of Object.entries(value)) {
    const nextKey = prefix ? `${prefix}.${key}` : key;
    if (child && typeof child === "object" && !Array.isArray(child)) {
      flattenLocale(child, nextKey, result);
    } else {
      result[nextKey] = child;
    }
  }
  return result;
}

const flatLocales = Object.fromEntries(
  supportedLocales.map((locale) => [locale, flattenLocale(locales[locale])])
);

export function resolveLocale({saved, browser} = {}) {
  if (supportedLocales.includes(saved)) return saved;
  return String(browser || "").toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

export function formatDuration([minimum, maximum], locale) {
  return locale === "zh-CN" ? `${minimum}–${maximum} 分钟` : `${minimum}–${maximum} min`;
}

export function text(locale, key) {
  return flatLocales[locale]?.[key] ?? flatLocales.en[key] ?? key;
}

export function translateDocument(root = document, locale = getCurrentLocale()) {
  root.documentElement?.setAttribute("lang", locale);

  for (const element of root.querySelectorAll("[data-i18n]")) {
    element.textContent = text(locale, element.dataset.i18n);
  }
  for (const element of root.querySelectorAll("[data-i18n-placeholder]")) {
    element.setAttribute("placeholder", text(locale, element.dataset.i18nPlaceholder));
  }
  for (const element of root.querySelectorAll("[data-i18n-aria-label]")) {
    element.setAttribute("aria-label", text(locale, element.dataset.i18nAriaLabel));
  }
  for (const element of root.querySelectorAll("[data-locale-set]")) {
    element.setAttribute("aria-current", String(element.dataset.localeSet === locale));
  }

  const titleKey = root.body?.dataset.titleKey;
  if (titleKey) root.title = text(locale, titleKey);
  const description = root.querySelector('meta[name="description"]');
  const descriptionKey = root.body?.dataset.descriptionKey;
  if (description && descriptionKey) description.content = text(locale, descriptionKey);

  return locale;
}

export function getCurrentLocale() {
  if (typeof window === "undefined") return "en";
  return resolveLocale({
    saved: window.localStorage?.getItem(localeStorageKey),
    browser: window.navigator?.language,
  });
}

export function setLocale(locale, root = document) {
  if (!supportedLocales.includes(locale)) return getCurrentLocale();
  if (typeof window !== "undefined") {
    window.localStorage?.setItem(localeStorageKey, locale);
  }
  translateDocument(root, locale);
  root.dispatchEvent?.(new CustomEvent("ponder:locale-change", {detail: {locale}}));
  return locale;
}

export function initI18n(root = document) {
  const locale = translateDocument(root, getCurrentLocale());
  root.addEventListener("click", (event) => {
    const trigger = event.target.closest?.("[data-locale-set]");
    if (trigger) setLocale(trigger.dataset.localeSet, root);
  });
  return locale;
}
