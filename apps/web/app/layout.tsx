import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import { AppShell } from "@/components/app-shell";
import { I18nProvider } from "@/lib/i18n";
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

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="zh-CN">
      <body
        className={`${geistSans.variable} ${geistMono.variable} antialiased`}
      >
        <I18nProvider initialLocale="zh-CN">
          <AppShell>{children}</AppShell>
        </I18nProvider>
      </body>
    </html>
  );
}
