"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { catalogs, normalizeLocale, type InterfaceLocale, type MessageKey } from "./catalogs";

type MessageParameters = Record<string, number | string>;

type I18nContextValue = {
  locale: InterfaceLocale;
  setLocale: (locale: string) => void;
  t: (key: MessageKey, parameters?: MessageParameters) => string;
};

const I18nContext = createContext<I18nContextValue | null>(null);

export const interfaceLocaleCookie = "vistora_locale";

function localeFromCookie(): InterfaceLocale | null {
  const match = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(`${interfaceLocaleCookie}=`));
  return match ? normalizeLocale(decodeURIComponent(match.slice(match.indexOf("=") + 1))) : null;
}

function interpolate(message: string, parameters?: MessageParameters) {
  if (!parameters) return message;
  return message.replace(/\{([a-zA-Z0-9_]+)\}/g, (token, key: string) => (
    Object.prototype.hasOwnProperty.call(parameters, key) ? String(parameters[key]) : token
  ));
}

export function I18nProvider({ children, initialLocale }: {
  children: React.ReactNode;
  initialLocale: InterfaceLocale;
}) {
  const [locale, updateLocale] = useState<InterfaceLocale>(initialLocale);
  const cookieRestored = useRef(false);

  const setLocale = useCallback((nextLocale: string) => {
    updateLocale(normalizeLocale(nextLocale));
  }, []);

  useEffect(() => {
    if (!cookieRestored.current) {
      cookieRestored.current = true;
      const storedLocale = localeFromCookie();
      if (storedLocale && storedLocale !== locale) {
        const timer = window.setTimeout(() => updateLocale(storedLocale), 0);
        return () => window.clearTimeout(timer);
      }
    }
    document.documentElement.lang = locale;
    document.cookie = `${interfaceLocaleCookie}=${locale}; Path=/; Max-Age=31536000; SameSite=Lax`;
  }, [locale]);

  const value = useMemo<I18nContextValue>(() => ({
    locale,
    setLocale,
    t: (key, parameters) => interpolate(catalogs[locale][key], parameters),
  }), [locale, setLocale]);

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n() {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}
