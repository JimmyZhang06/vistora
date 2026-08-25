"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";

export const themePreferenceCookie = "vistora_theme";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = Exclude<ThemePreference, "system">;

type ThemeContextValue = {
  preference: ThemePreference;
  resolvedTheme: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
  toggleResolvedTheme: () => void;
};

const ThemeContext = createContext<ThemeContextValue | null>(null);

function normalizePreference(value?: string | null): ThemePreference {
  return value === "light" || value === "dark" || value === "system" ? value : "system";
}

function preferenceFromCookie(): ThemePreference {
  const match = document.cookie
    .split(";")
    .map((item) => item.trim())
    .find((item) => item.startsWith(`${themePreferenceCookie}=`));
  return normalizePreference(match ? decodeURIComponent(match.slice(match.indexOf("=") + 1)) : null);
}

function systemTheme(): ResolvedTheme {
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function resolveTheme(preference: ThemePreference): ResolvedTheme {
  return preference === "system" ? systemTheme() : preference;
}

function applyTheme(preference: ThemePreference): ResolvedTheme {
  const resolved = resolveTheme(preference);
  document.documentElement.dataset.theme = resolved;
  document.documentElement.dataset.themePreference = preference;
  return resolved;
}

function persistPreference(preference: ThemePreference) {
  const secure = window.location.protocol === "https:" ? "; Secure" : "";
  document.cookie = `${themePreferenceCookie}=${preference}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`;
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [preference, updatePreference] = useState<ThemePreference>("system");
  const [resolvedTheme, setResolvedTheme] = useState<ResolvedTheme>("dark");
  const [restored, setRestored] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      const storedPreference = preferenceFromCookie();
      updatePreference(storedPreference);
      setResolvedTheme(applyTheme(storedPreference));
      setRestored(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!restored || preference !== "system") return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handleChange = () => setResolvedTheme(applyTheme("system"));
    media.addEventListener("change", handleChange);
    return () => media.removeEventListener("change", handleChange);
  }, [preference, restored]);

  const setPreference = useCallback((nextPreference: ThemePreference) => {
    updatePreference(nextPreference);
    persistPreference(nextPreference);
    setResolvedTheme(applyTheme(nextPreference));
  }, []);

  const toggleResolvedTheme = useCallback(() => {
    setPreference(resolvedTheme === "dark" ? "light" : "dark");
  }, [resolvedTheme, setPreference]);

  const value = useMemo<ThemeContextValue>(() => ({
    preference,
    resolvedTheme,
    setPreference,
    toggleResolvedTheme,
  }), [preference, resolvedTheme, setPreference, toggleResolvedTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside ThemeProvider");
  return value;
}
