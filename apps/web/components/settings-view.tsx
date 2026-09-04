"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import {
  createFrameFactoryAdapter,
  type AccountApiKey,
  type AccountCapabilities,
  type AccountProfile,
  type AccountSession,
  type ApiKeyScope,
  type CreatedApiKey,
  type CreationPreferences,
  type SessionContext,
} from "@/lib/api";
import { Badge, PageHeading, StatePanel } from "@/components/page-heading";
import { useConfirmDialog } from "@/components/confirm-dialog";
import { UiSelect } from "@/components/ui-select";
import { useI18n } from "@/lib/i18n";
import { useTheme, type ThemePreference } from "@/lib/theme";

const themeOptions = [
  { value: "system", icon: "AU", label: "settings.appearance.system", help: "settings.appearance.systemHelp" },
  { value: "light", icon: "LT", label: "settings.appearance.light", help: "settings.appearance.lightHelp" },
  { value: "dark", icon: "DK", label: "settings.appearance.dark", help: "settings.appearance.darkHelp" },
] as const satisfies ReadonlyArray<{ value: ThemePreference; icon: string }>;

export function SettingsView() {
  const adapter = useMemo(() => createFrameFactoryAdapter(), []);
  const { locale, setLocale, t } = useI18n();
  const { preference: themePreference, resolvedTheme, setPreference: setThemePreference } = useTheme();
  const [session, setSession] = useState<SessionContext | null>(null);
  const [capabilities, setCapabilities] = useState<AccountCapabilities | null>(null);
  const [profile, setProfile] = useState<AccountProfile | null>(null);
  const [profileEtag, setProfileEtag] = useState("");
  const [preferences, setPreferences] = useState<CreationPreferences | null>(null);
  const [preferencesEtag, setPreferencesEtag] = useState("");
  const [sessions, setSessions] = useState<AccountSession[]>([]);
  const [apiKeys, setApiKeys] = useState<AccountApiKey[]>([]);
  const [createdKey, setCreatedKey] = useState<CreatedApiKey | null>(null);
  const [newKeyName, setNewKeyName] = useState("");
  const [newKeyScopes, setNewKeyScopes] = useState<ApiKeyScope[]>(["skills:read"]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const { confirm, confirmationDialog } = useConfirmDialog();

  const availableScopes = useMemo<Array<{ value: ApiKeyScope; label: string }>>(() => [
    { value: "account:read", label: t("settings.scope.accountRead") },
    { value: "skills:read", label: t("settings.scope.skillsRead") },
    { value: "skills:write", label: t("settings.scope.skillsWrite") },
    { value: "runs:read", label: t("settings.scope.runsRead") },
    { value: "runs:write", label: t("settings.scope.runsWrite") },
  ], [t]);

  const dateLabel = useCallback((value?: string) => {
    if (!value) return t("common.never");
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? t("common.unknown") : new Intl.DateTimeFormat(locale, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(date);
  }, [locale, t]);

  const deviceLabel = useCallback((userAgent?: string) => {
    if (!userAgent) return t("settings.device.unknown");
    if (/Windows/i.test(userAgent)) return t("settings.device.windows");
    if (/Mac/i.test(userAgent)) return t("settings.device.mac");
    if (/iPhone|iPad/i.test(userAgent)) return t("settings.device.ios");
    if (/Android/i.test(userAgent)) return t("settings.device.android");
    return userAgent.slice(0, 58);
  }, [t]);

  const loadAccount = useCallback(async () => {
    setLoading(true);
    setError("");
    setNotice("");
    const [sessionResult, capabilitiesResult, profileResult, preferencesResult, sessionsResult, keysResult] = await Promise.all([
      adapter.getSession(),
      adapter.getAccountCapabilities(),
      adapter.getAccountProfile(),
      adapter.getCreationPreferences(),
      adapter.listAccountSessions(),
      adapter.listApiKeys(),
    ]);
    const failed = [sessionResult, capabilitiesResult, profileResult, preferencesResult, sessionsResult, keysResult]
      .find((result) => !result.ok);
    if (failed && !failed.ok) {
      setError(failed.error.message || t("settings.offline.title"));
      setLoading(false);
      return;
    }
    if (sessionResult.ok) setSession(sessionResult.data);
    if (capabilitiesResult.ok) setCapabilities(capabilitiesResult.data);
    if (profileResult.ok) {
      setProfile({ ...profileResult.data.value, locale: "zh-CN" });
      setProfileEtag(profileResult.data.etag);
    }
    if (preferencesResult.ok) {
      setPreferences({ ...preferencesResult.data.value, defaultVisibility: "private" });
      setPreferencesEtag(preferencesResult.data.etag);
    }
    if (sessionsResult.ok) setSessions(sessionsResult.data);
    if (keysResult.ok) setApiKeys(keysResult.data);
    setLoading(false);
  }, [adapter, t]);

  useEffect(() => {
    const timer = window.setTimeout(() => void loadAccount(), 0);
    return () => window.clearTimeout(timer);
  }, [loadAccount]);

  async function saveProfile(event: FormEvent) {
    event.preventDefault();
    if (!profile || !profileEtag || !profile.displayName.trim()) return;
    setBusy("profile");
    setNotice("");
    const result = await adapter.replaceAccountProfile({
      displayName: profile.displayName.trim(), avatarUrl: profile.avatarUrl,
      locale: profile.locale, timezone: profile.timezone,
    }, profileEtag);
    setBusy("");
    if (!result.ok) {
      setNotice(result.error.code.toLowerCase().includes("conflict") ? t("settings.profileConflict") : result.error.message);
      return;
    }
    setProfile(result.data.value);
    setProfileEtag(result.data.etag);
    setLocale(result.data.value.locale);
    setNotice(t("settings.profile.saved"));
  }

  async function savePreferences(event: FormEvent) {
    event.preventDefault();
    if (!preferences || !preferencesEtag) return;
    setBusy("preferences");
    setNotice("");
    const result = await adapter.replaceCreationPreferences({
      defaultLanguage: preferences.defaultLanguage,
      defaultAspectRatio: preferences.defaultAspectRatio,
      defaultDurationSeconds: preferences.defaultDurationSeconds,
      defaultVisibility: "private",
      autoQualityCheck: true,
    }, preferencesEtag);
    setBusy("");
    if (!result.ok) {
      setNotice(result.error.code.toLowerCase().includes("conflict") ? t("settings.preferencesConflict") : result.error.message);
      return;
    }
    setPreferences({ ...result.data.value, defaultVisibility: "private", autoQualityCheck: true });
    setPreferencesEtag(result.data.etag);
    setNotice(t("settings.defaults.saved"));
  }

  async function revokeSession(item: AccountSession) {
    const device = deviceLabel(item.userAgent);
    if (!await confirm({
      title: t("settings.security.sessionRevoke"),
      description: t("settings.security.sessionRevokeConfirm", { device }),
      confirmLabel: t("settings.security.sessionRevoke"),
      tone: "danger",
    })) return;
    setBusy(`session:${item.id}`);
    const result = await adapter.revokeAccountSession(item.id);
    setBusy("");
    if (!result.ok) {
      setNotice(result.error.message);
      return;
    }
    setSessions((current) => current.map((sessionItem) => sessionItem.id === item.id
      ? { ...sessionItem, revokedAt: new Date().toISOString() }
      : sessionItem));
    setNotice(t("settings.security.sessionRevokedNotice"));
  }

  function toggleScope(scope: ApiKeyScope) {
    setNewKeyScopes((current) => current.includes(scope)
      ? current.filter((item) => item !== scope)
      : [...current, scope]);
  }

  async function createKey(event: FormEvent) {
    event.preventDefault();
    if (!newKeyName.trim() || newKeyScopes.length === 0) {
      setNotice(t("settings.api.scopeRequired"));
      return;
    }
    setBusy("create-key");
    const result = await adapter.createApiKey({ name: newKeyName.trim(), scopes: newKeyScopes });
    setBusy("");
    if (!result.ok) {
      setNotice(result.error.message);
      return;
    }
    setCreatedKey(result.data);
    setApiKeys((current) => [result.data.key, ...current]);
    setNewKeyName("");
    setNewKeyScopes(["skills:read"]);
    setNotice(t("settings.api.created"));
  }

  async function revokeKey(item: AccountApiKey) {
    if (!await confirm({
      title: t("settings.api.revoke"),
      description: t("settings.api.revokeConfirm", { name: item.name }),
      confirmLabel: t("settings.api.revoke"),
      tone: "danger",
    })) return;
    setBusy(`key:${item.id}`);
    const result = await adapter.revokeApiKey(item.id);
    setBusy("");
    if (!result.ok) {
      setNotice(result.error.message);
      return;
    }
    setApiKeys((current) => current.map((key) => key.id === item.id
      ? { ...key, revokedAt: new Date().toISOString() }
      : key));
    setNotice(t("settings.api.revoked"));
  }

  async function copyCreatedKey() {
    if (!createdKey) return;
    try {
      await navigator.clipboard.writeText(createdKey.plaintext);
      setNotice(t("settings.api.copied"));
    } catch {
      setNotice(t("settings.api.copyFailed"));
    }
  }

  const workspace = session?.workspaces.find((item) => item.id === session.activeWorkspaceId);
  const activeSessions = sessions.filter((item) => !item.revokedAt);
  const activeKeys = apiKeys.filter((item) => !item.revokedAt);
  const sessionTrackingAvailable = capabilities?.sessions === "available";
  const apiKeyAuthenticationAvailable = capabilities?.apiKeys === "available";
  const apiKeyManagementOnly = capabilities?.apiKeys === "management_only";

  return (
    <div className="page settings-page">
      <PageHeading
        eyebrow="07 / ACCOUNT & SETTINGS"
        title={t("settings.title")}
        description={t("settings.description")}
        actions={<button className="button-ghost" type="button" onClick={() => void loadAccount()} disabled={loading}>{loading ? t("common.refreshing") : t("common.refresh")}</button>}
      />

      <div className={`settings-connection settings-connection--${error ? "offline" : loading ? "checking" : "online"}`} role="status" aria-live="polite">
        <span aria-hidden="true" />
        <div>
          <strong>{error ? t("settings.connection.failed") : loading ? t("settings.connection.syncing") : t("settings.connection.connected")}</strong>
          <p>{error || (loading ? t("settings.connection.loading") : `${profile?.email ?? t("common.account")} · ${workspace?.name ?? t("shell.workspace")}`)}</p>
        </div>
        {!loading && error ? <button className="button-small button-secondary" type="button" onClick={() => void loadAccount()}>{t("common.retry")}</button> : null}
      </div>

      {!loading && error ? (
        <StatePanel code="OFFLINE" title={t("settings.offline.title")} description={t("settings.offline.body")} error />
      ) : null}

      <section className="settings-section settings-appearance" aria-labelledby="appearance-title">
        <div className="settings-section-heading">
          <div><p className="eyebrow">APPEARANCE</p><h2 id="appearance-title">{t("settings.appearance.title")}</h2></div>
          <Badge>{resolvedTheme === "dark" ? t("settings.appearance.activeDark") : t("settings.appearance.activeLight")}</Badge>
        </div>
        <div className="panel settings-appearance-card">
          <div className="settings-appearance-copy">
            <span className="settings-icon" aria-hidden="true">UI</span>
            <div><h3>{t("settings.appearance.theme")}</h3><p>{t("settings.appearance.help")}</p></div>
          </div>
          <div className="theme-preference-control" role="radiogroup" aria-label={t("settings.appearance.theme")}>
            {themeOptions.map((option) => (
              <label key={option.value} data-selected={themePreference === option.value ? "true" : undefined}>
                <input
                  type="radio"
                  name="theme-preference"
                  value={option.value}
                  checked={themePreference === option.value}
                  onChange={() => setThemePreference(option.value)}
                />
                <span aria-hidden="true">{option.icon}</span>
                <strong>{t(option.label)}</strong>
                <small>{t(option.help)}</small>
              </label>
            ))}
          </div>
          <p className="settings-appearance-storage">{t("settings.appearance.storage")}</p>
        </div>
      </section>

      {!loading && !error && profile && preferences ? (
        <>
          <section className="settings-section" aria-labelledby="account-profile-title">
            <div className="settings-section-heading">
              <div><p className="eyebrow">PROFILE & DEFAULTS</p><h2 id="account-profile-title">{t("settings.section.profile")}</h2></div>
              <Badge tone="success">{t("common.available")}</Badge>
            </div>
            <div className="settings-editor-grid">
              <form className="panel settings-form" onSubmit={saveProfile}>
                <div className="settings-card-heading">
                  <span className="settings-icon" aria-hidden="true">ID</span>
                  <span className="resource-revision">{t("common.revision", { revision: profile.revision })}</span>
                </div>
                <h3>{t("settings.profile.title")}</h3>
                <p className="muted">{t("settings.profile.help")}</p>
                <div className="form-grid settings-form-grid">
                  <label className="field"><span className="field-label">{t("settings.profile.displayName")}</span><input className="input" required maxLength={160} value={profile.displayName} onChange={(event) => setProfile({ ...profile, displayName: event.target.value })} /></label>
                  <label className="field"><span className="field-label">{t("settings.profile.email")}</span><input className="input" value={profile.email} readOnly aria-describedby="email-readonly" /><small id="email-readonly" className="field-help">{t("settings.profile.emailReadonly")}</small></label>
                  <div className="field"><span className="field-label">{t("settings.interfaceLanguage")}</span><div className="settings-locked-value"><strong>简体中文</strong><small>完整界面语言</small></div><small className="field-help">英文界面将在所有创作与审核页面完成翻译后开放。</small></div>
                  <div className="field"><span className="field-label">{t("settings.profile.timezone")}</span><UiSelect ariaLabel={t("settings.profile.timezone")} value={profile.timezone} onChange={(timezone) => setProfile({ ...profile, timezone })}><option value="Asia/Shanghai">Asia / Shanghai</option><option value="UTC">UTC</option><option value="America/Los_Angeles">America / Los Angeles</option></UiSelect></div>
                </div>
                <div className="settings-form-actions">
                  <small>{t("settings.profile.persisted")}</small>
                  <button className="button" type="submit" disabled={busy === "profile"}>{busy === "profile" ? t("settings.profile.saving") : t("settings.profile.save")}</button>
                </div>
              </form>

              <form className="panel settings-form" onSubmit={savePreferences}>
                <div className="settings-card-heading">
                  <span className="settings-icon" aria-hidden="true">CR</span>
                  <span className="resource-revision">{t("common.revision", { revision: preferences.revision })}</span>
                </div>
                <h3>{t("settings.defaults.title")}</h3>
                <p className="muted">{t("settings.defaults.help")}</p>
                <div className="form-grid settings-form-grid">
                  <div className="field"><span className="field-label">{t("settings.contentLanguage")}</span><UiSelect ariaLabel={t("settings.contentLanguage")} value={preferences.defaultLanguage} onChange={(defaultLanguage) => setPreferences({ ...preferences, defaultLanguage })}><option value="zh-CN">简体中文</option><option value="en-US">English</option></UiSelect><small className="field-help">{t("settings.contentLanguageHelp")}</small></div>
                  <div className="field"><span className="field-label">{t("settings.defaults.aspect")}</span><UiSelect ariaLabel={t("settings.defaults.aspect")} value={preferences.defaultAspectRatio} onChange={(defaultAspectRatio) => setPreferences({ ...preferences, defaultAspectRatio: defaultAspectRatio as CreationPreferences["defaultAspectRatio"] })}><option value="16:9">16:9 Landscape</option><option value="9:16">9:16 Portrait</option><option value="1:1">1:1 Square</option><option value="4:3">4:3 Classic</option></UiSelect></div>
                  <label className="field"><span className="field-label">{t("settings.defaults.duration")}</span><input className="input" type="number" min={15} max={3600} value={preferences.defaultDurationSeconds} onChange={(event) => setPreferences({ ...preferences, defaultDurationSeconds: Number(event.target.value) })} /></label>
                  <div className="field"><span className="field-label">{t("settings.visibility.label")}</span><div className="settings-locked-value" role="status"><strong>{t("settings.visibility.private")}</strong><span>LOCKED</span></div><small className="field-help">{t("settings.visibility.help")}</small></div>
                </div>
                <div className="settings-policy-note" role="note" aria-label={t("settings.defaults.qcAria")}><span className="settings-policy-mark" aria-hidden="true">✓</span><span><strong>{t("settings.defaults.qc")}</strong><small>{t("settings.defaults.qcHelp")}</small></span><Badge>{t("settings.defaults.qcPolicy")}</Badge></div>
                <div className="settings-form-actions">
                  <small>{t("settings.defaults.persisted")}</small>
                  <button className="button" type="submit" disabled={busy === "preferences"}>{busy === "preferences" ? t("settings.defaults.saving") : t("settings.defaults.save")}</button>
                </div>
              </form>
            </div>
          </section>

          <section className="settings-section" aria-labelledby="security-title">
            <div className="settings-section-heading">
              <div><p className="eyebrow">LOGIN & SECURITY</p><h2 id="security-title">{t("settings.security.title")}</h2></div>
              <Badge tone={sessionTrackingAvailable && activeSessions.length ? "success" : "neutral"}>{sessionTrackingAvailable ? t("settings.security.activeSessions", { count: activeSessions.length }) : t("settings.security.authUnavailable")}</Badge>
            </div>
            <div className="security-capability-grid">
              <div className="panel security-capability-card">
                <div className="security-capability-head"><span className="settings-icon" aria-hidden="true">SE</span><Badge tone={sessionTrackingAvailable ? "success" : "neutral"}>{sessionTrackingAvailable ? t("settings.security.sessionActive") : t("settings.security.notRecorded")}</Badge></div>
                <div><p className="eyebrow">ACTIVE SESSIONS</p><h3>{t("settings.security.sessions")}</h3></div>
                {sessionTrackingAvailable ? (
                  <div className="account-list security-session-list">
                    {sessions.length ? sessions.map((item) => (
                      <article key={item.id} className={item.revokedAt ? "is-revoked" : ""}>
                        <div><h3>{deviceLabel(item.userAgent)}</h3><p>{t("settings.security.sessionLastSeen", { date: dateLabel(item.lastSeenAt) })} · {t("settings.security.sessionExpiry", { date: dateLabel(item.expiresAt) })}</p></div>
                        {item.revokedAt ? <Badge>{t("settings.security.sessionRevoked")}</Badge> : <button className="button-small button-ghost" type="button" disabled={busy === `session:${item.id}`} onClick={() => void revokeSession(item)}>{busy === `session:${item.id}` ? t("settings.security.sessionRevoking") : t("settings.security.sessionRevoke")}</button>}
                      </article>
                    )) : <div className="settings-empty-state"><strong>{t("settings.security.noSessions")}</strong><p>{t("settings.security.noSessionsHelp")}</p></div>}
                  </div>
                ) : <div className="settings-empty-state"><strong>{t("settings.security.sessionAbsent")}</strong><p>{t("settings.security.sessionAbsentHelp")}</p></div>}
              </div>
              <div className="panel security-capability-card">
                <div className="security-capability-head"><span className="settings-icon" aria-hidden="true">2F</span><Badge>{t("settings.security.authDependency")}</Badge></div>
                <div><p className="eyebrow">TWO-FACTOR AUTHENTICATION</p><h3>{t("settings.security.twoFactor")}</h3></div>
                <div className="settings-empty-state"><strong>{t("settings.security.twoFactorUnavailable")}</strong><p>{t("settings.security.twoFactorBody")}</p></div>
                <button className="button-secondary" type="button" disabled={capabilities?.twoFactorAuthentication !== "available"}>{capabilities?.twoFactorAuthentication === "available" ? t("settings.security.configure2fa") : t("settings.security.authPending")}</button>
              </div>
            </div>
          </section>

          <section className="settings-section" aria-labelledby="api-keys-title">
            <div className="settings-section-heading">
              <div><p className="eyebrow">DEVELOPER ACCESS</p><h2 id="api-keys-title">{t("settings.api.title")}</h2></div>
              <Badge tone={apiKeyAuthenticationAvailable && activeKeys.length ? "accent" : "neutral"}>{apiKeyAuthenticationAvailable ? t("settings.api.validKeys", { count: activeKeys.length }) : t("settings.api.managementOnly")}</Badge>
            </div>

            {apiKeyManagementOnly ? <div className="settings-capability-note" role="note"><span aria-hidden="true">!</span><div><strong>{t("settings.api.managementOnlyTitle")}</strong><p>{t("settings.api.managementOnlyBody")}</p></div></div> : null}

            {createdKey ? (
              <div className="one-time-secret" role="alert">
                <div><strong>{t("settings.api.oneTimeTitle")}</strong><p>{t("settings.api.oneTimeBody")}</p></div>
                <code>{createdKey.plaintext}</code>
                <div className="button-row"><button className="button" type="button" onClick={() => void copyCreatedKey()}>{t("settings.api.copy")}</button><button className="button-ghost" type="button" onClick={() => setCreatedKey(null)}>{t("settings.api.oneTimeClose")}</button></div>
              </div>
            ) : null}

            <div className="api-key-layout">
              <form className={`panel api-key-create${apiKeyAuthenticationAvailable ? "" : " is-disabled"}`} onSubmit={createKey}>
                <div className="api-key-create-heading"><div><h3>{t("settings.api.createTitle")}</h3><p className="muted">{t("settings.api.createHelp")}</p></div><Badge>{apiKeyAuthenticationAvailable ? t("settings.api.available") : t("settings.api.authPending")}</Badge></div>
                <label className="field"><span className="field-label">{t("settings.api.keyName")}</span><input className="input" required maxLength={100} disabled={!apiKeyAuthenticationAvailable} placeholder={t("settings.api.keyPlaceholder")} value={newKeyName} onChange={(event) => setNewKeyName(event.target.value)} /></label>
                <fieldset className="scope-fieldset" disabled={!apiKeyAuthenticationAvailable}><legend>{t("settings.api.scopes")}</legend>{availableScopes.map((scope) => <label key={scope.value}><input type="checkbox" checked={newKeyScopes.includes(scope.value)} onChange={() => toggleScope(scope.value)} /><span>{scope.label}</span><code>{scope.value}</code></label>)}</fieldset>
                <button className="button" type="submit" disabled={!apiKeyAuthenticationAvailable || busy === "create-key"}>{busy === "create-key" ? t("settings.defaults.saving") : apiKeyAuthenticationAvailable ? t("settings.api.create") : t("settings.api.authRequired")}</button>
              </form>
              <div className="account-list panel api-key-list">
                {apiKeys.length ? apiKeys.map((item) => (
                  <article key={item.id} className={item.revokedAt ? "is-revoked" : ""}>
                    <span className="settings-icon" aria-hidden="true">KEY</span>
                    <div><h3>{item.name}</h3><p><code>{item.keyPrefix}…</code> · {t("settings.api.scopeCount", { count: item.scopes.length })} · {t("settings.api.lastUsed", { date: dateLabel(item.lastUsedAt) })}</p></div>
                    {item.revokedAt ? <Badge>{t("settings.api.revokedState")}</Badge> : <button className="button-small button-ghost" type="button" disabled={busy === `key:${item.id}`} onClick={() => void revokeKey(item)}>{busy === `key:${item.id}` ? t("settings.api.revoking") : t("settings.api.revoke")}</button>}
                  </article>
                )) : <div className="settings-empty-state settings-empty-state--list"><strong>{t("settings.api.empty")}</strong><p>{apiKeyAuthenticationAvailable ? t("settings.api.emptyAvailableHelp") : t("settings.api.emptyUnavailableHelp")}</p></div>}
              </div>
            </div>
          </section>
        </>
      ) : null}

      <p className="settings-save-status" role="status" aria-live="polite">{notice}</p>
      {confirmationDialog}
    </div>
  );
}
