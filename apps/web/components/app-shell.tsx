"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { createFrameFactoryAdapter, type SessionContext } from "@/lib/api";
import { useI18n, type MessageKey } from "@/lib/i18n";
import { useTheme } from "@/lib/theme";

const navigation: ReadonlyArray<{ href: string; label: MessageKey; eyebrow: string }> = [
  { href: "/create", label: "shell.nav.create", eyebrow: "01" },
  { href: "/projects", label: "shell.nav.projects", eyebrow: "02" },
  { href: "/batches", label: "shell.nav.batches", eyebrow: "03" },
  { href: "/skills", label: "shell.nav.skills", eyebrow: "04" },
  { href: "/assets", label: "shell.nav.assets", eyebrow: "05" },
  { href: "/channels", label: "shell.nav.channels", eyebrow: "06" },
  { href: "/benchmarks", label: "shell.nav.benchmarks", eyebrow: "07" },
  { href: "/settings", label: "shell.nav.settings", eyebrow: "08" },
];

type ConnectionState = "checking" | "online" | "offline";

function isCurrent(pathname: string, href: string) {
  return pathname === href || pathname.startsWith(`${href}/`);
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { setLocale, t } = useI18n();
  const { resolvedTheme, toggleResolvedTheme } = useTheme();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const moreButtonRef = useRef<HTMLButtonElement>(null);
  const [menuOpen, setMenuOpen] = useState(false);
  const [session, setSession] = useState<SessionContext | null>(null);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [connectionError, setConnectionError] = useState("");
  const adapterRef = useRef(createFrameFactoryAdapter());

  const refreshSession = useCallback(async () => {
    setConnection("checking");
    setConnectionError("");
    const result = await adapterRef.current.getSession();
    if (result.ok) {
      setSession(result.data);
      // The application workspaces are currently complete in Simplified Chinese.
      // Keep the interface language honest until the remaining routes are translated.
      setLocale("zh-CN");
      setConnection("online");
      return;
    }
    setSession(null);
    setConnectionError(result.error.message);
    setConnection("offline");
  }, [setLocale]);

  useEffect(() => {
    const timer = window.setTimeout(() => void refreshSession(), 0);
    return () => window.clearTimeout(timer);
  }, [refreshSession]);

  useEffect(() => {
    document.querySelector<HTMLElement>("main h1")?.focus({ preventScroll: true });
    dialogRef.current?.close();
  }, [pathname]);

  function openMenu() {
    setMenuOpen(true);
    dialogRef.current?.showModal();
    window.requestAnimationFrame(() => dialogRef.current?.querySelector<HTMLAnchorElement>("a")?.focus());
  }

  function closeMenu() {
    dialogRef.current?.close();
    setMenuOpen(false);
    moreButtonRef.current?.focus();
  }

  const workspace = session?.workspaces.find((item) => item.id === session.activeWorkspaceId);
  const identityLabel = session?.user.displayName || t("shell.notConnected");
  const currentItem = navigation.find((item) => isCurrent(pathname, item.href));
  const currentSection = currentItem ? t(currentItem.label) : t("shell.productNavigation");
  const connectionMessage = connection === "online"
    ? t("shell.connectionServiceOnline")
    : connection === "offline"
      ? connectionError || t("shell.connectionUnavailable")
      : t("shell.startingConnection");
  const connectionLabel = connection === "online"
    ? t("shell.connectionOnline")
    : connection === "offline"
      ? t("shell.connectionOffline")
      : t("shell.connectionChecking");

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">{t("shell.skip")}</a>

      <header className="topbar">
        <Link href="/create" className="wordmark" aria-label="Vistora home">
          <span className="wordmark-logo" aria-hidden="true" />
          <span className="wordmark-copy" aria-hidden="true">
            <strong>Vistora</strong>
            <small>AI VIDEO CREATION PLATFORM</small>
          </span>
          <span className="sr-only">Vistora AI Video Creation Platform</span>
        </Link>

        <div className="identity-strip" aria-label={t("shell.identity")}>
          <div className="topbar-context">
            <span>{t("shell.currentLocation")}</span>
            <strong>{currentSection}</strong>
          </div>
          <div className="workspace-switcher">
            <i aria-hidden="true" />
            <span>
              <small>{t("shell.workspace")}</small>
              <strong>{workspace?.name ?? (connection === "checking" ? t("common.loading") : t("common.unavailable"))}</strong>
            </span>
          </div>
        </div>

        <div className="topbar-actions">
          <button
            className="theme-toggle"
            type="button"
            onClick={toggleResolvedTheme}
            aria-label={resolvedTheme === "dark" ? t("shell.theme.switchToLight") : t("shell.theme.switchToDark")}
            title={resolvedTheme === "dark" ? t("shell.theme.switchToLight") : t("shell.theme.switchToDark")}
          >
            <span className="theme-toggle-icon" aria-hidden="true">{resolvedTheme === "dark" ? "☼" : "◐"}</span>
            <span className="theme-toggle-label">{resolvedTheme === "dark" ? t("shell.theme.dark") : t("shell.theme.light")}</span>
          </button>
          <button
            className={`sync-state sync-state--${connection}`}
            type="button"
            title={connectionMessage}
            onClick={() => void refreshSession()}
            disabled={connection === "checking"}
            aria-label={t("shell.recheck", { message: connectionMessage })}
          >
            <i aria-hidden="true" />
            {connection === "online" ? t("shell.connectionServiceOnline") : connection === "offline" ? t("shell.connectionFailed") : t("shell.connectionChecking")}
          </button>
          <Link className="avatar" href="/settings" aria-label={t("shell.accountSettings", { name: identityLabel })}>
            {session?.user.displayName.trim().slice(0, 1) || t("common.account").slice(0, 1)}
          </Link>
        </div>
      </header>

      <aside className="sidebar">
        <div>
          <Link className="sidebar-create" href="/create">
            <span aria-hidden="true">＋</span><strong>{t("shell.newCreation")}</strong>
          </Link>
          <nav aria-label={t("shell.mainNavigation")}>
            <p className="nav-label">{t("shell.productNavigation")}</p>
            <ul>
              {navigation.map((item) => {
                const current = isCurrent(pathname, item.href);
                return (
                  <li key={item.href}>
                    <Link href={item.href} aria-current={current ? "page" : undefined}>
                      <span className="nav-index">{item.eyebrow}</span><span>{t(item.label)}</span>
                    </Link>
                  </li>
                );
              })}
            </ul>
          </nav>
        </div>

      </aside>

      <main id="main-content" className="main-content">{children}</main>

      <nav className="mobile-nav" aria-label={t("shell.mobileNavigation")}>
        {navigation.slice(0, 4).map((item) => {
          const current = isCurrent(pathname, item.href);
          const label = t(item.label);
          return (
            <Link key={item.href} href={item.href} aria-current={current ? "page" : undefined}>
              <span aria-hidden="true">{label.slice(0, 1)}</span>{label}
            </Link>
          );
        })}
        <button ref={moreButtonRef} type="button" aria-expanded={menuOpen} aria-controls="mobile-menu" onClick={openMenu}>
          <span aria-hidden="true">•••</span>{t("shell.more")}
        </button>
      </nav>

      <dialog id="mobile-menu" ref={dialogRef} className="mobile-menu" aria-labelledby="mobile-menu-title" onClose={() => setMenuOpen(false)}>
        <div className="mobile-menu-head">
          <div><span className="eyebrow">NAVIGATION</span><h2 id="mobile-menu-title">{t("shell.allNavigation")}</h2></div>
          <button className="icon-button" type="button" onClick={closeMenu} aria-label={t("shell.closeNavigation")}>×</button>
        </div>
        <nav aria-label={t("shell.allNavigation")}>
          {navigation.map((item) => (
            <Link key={item.href} href={item.href} aria-current={isCurrent(pathname, item.href) ? "page" : undefined} onClick={() => dialogRef.current?.close()}>
              <span>{item.eyebrow}</span><strong>{t(item.label)}</strong>
            </Link>
          ))}
        </nav>
        <div className="mobile-identity">
          <span>{t("common.account")}: {identityLabel}</span>
          <span>{t("shell.workspace")}: {workspace?.name ?? t("common.unavailable")}</span>
          <span>{t("shell.service")}: {connectionLabel}</span>
        </div>
      </dialog>
    </div>
  );
}
