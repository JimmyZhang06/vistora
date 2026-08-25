import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const readSource = (path) => readFile(new URL(path, import.meta.url), "utf8");

test("theme preference is restored before paint and exposed through the shared shell", async () => {
  const [layout, shell, theme] = await Promise.all([
    readSource("../app/layout.tsx"),
    readSource("../components/app-shell.tsx"),
    readSource("../lib/theme/context.tsx"),
  ]);

  assert.match(layout, /vistora_theme/);
  assert.match(layout, /documentElement\.dataset\.theme/);
  assert.match(layout, /<ThemeProvider>/);
  assert.match(shell, /className="theme-toggle"/);
  assert.match(shell, /toggleResolvedTheme/);
  assert.match(theme, /"system" \| "light" \| "dark"/);
  assert.match(theme, /prefers-color-scheme: dark/);
  assert.match(theme, /SameSite=Lax/);
});

test("settings and global styles provide a complete three-state appearance control", async () => {
  const [settings, styles] = await Promise.all([
    readSource("../components/settings-view.tsx"),
    readSource("../app/globals.css"),
  ]);

  for (const preference of ["system", "light", "dark"]) {
    assert.match(settings, new RegExp(`value: "${preference}"`));
  }
  assert.match(settings, /role="radiogroup"/);
  assert.match(styles, /:root\[data-theme="light"\]/);
  assert.match(styles, /color-scheme: light/);
  assert.match(styles, /prefers-reduced-motion: reduce/);
});

test("shared controls and intentional dark surfaces preserve readable theme boundaries", async () => {
  const [styles, composer, batches, channelForm, skillStudio] = await Promise.all([
    readSource("../app/globals.css"),
    readSource("../components/create-composer.tsx"),
    readSource("../components/batch-console.tsx"),
    readSource("../components/channel-form.tsx"),
    readSource("../components/skill-studio.tsx"),
  ]);

  assert.match(styles, /\.theme-inverse\s*\{[\s\S]*?--ink:\s*var\(--inverse-ink\)/);
  assert.match(styles, /\.select:disabled\s*\{[\s\S]*?opacity:\s*1/);
  assert.match(styles, /\.ui-select-popover\s*\{[\s\S]*?var\(--panel\)/);
  assert.match(styles, /\.composition-library-trigger\s*\{[\s\S]*?background:\s*var\(--raised\)/);
  assert.match(styles, /\.method-card:disabled\s*\{[\s\S]*?opacity:\s*1/);
  for (const source of [composer, batches, channelForm, skillStudio]) {
    assert.match(source, /theme-inverse/);
  }
});
