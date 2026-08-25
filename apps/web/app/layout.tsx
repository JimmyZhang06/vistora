import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { AppShell } from "@/components/app-shell";
import { I18nProvider } from "@/lib/i18n";
import { ThemeProvider } from "@/lib/theme";
import "./globals.css";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  title: {
    default: "Vistora Studio",
    template: "%s · Vistora",
  },
  description:
    "创建、测试并复用属于你的内容生产 Skill。",
  icons: {
    icon: "/brand/vistora-logo.png",
    shortcut: "/brand/vistora-logo.png",
  },
};

const restoreThemeScript = `
(() => {
  try {
    const match = document.cookie.match(/(?:^|; )vistora_theme=([^;]*)/);
    const stored = match ? decodeURIComponent(match[1]) : "system";
    const preference = stored === "light" || stored === "dark" ? stored : "system";
    const resolved = preference === "system"
      ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : preference;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.themePreference = preference;
  } catch (_) {
    document.documentElement.dataset.theme = "dark";
    document.documentElement.dataset.themePreference = "system";
  }
})();`;

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head><script dangerouslySetInnerHTML={{ __html: restoreThemeScript }} /></head>
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <ThemeProvider>
          <I18nProvider initialLocale="zh-CN">
            <AppShell>{children}</AppShell>
          </I18nProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
