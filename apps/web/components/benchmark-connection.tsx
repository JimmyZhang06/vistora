"use client";

import { useEffect, useRef, useState } from "react";
import { type FrameFactoryAdapter, type BenchmarkConnectionState } from "@/lib/api";
import { BenchmarkConnectionFlow, type BenchmarkConnectionView } from "@/lib/benchmark-connection";
import { Badge } from "@/components/page-heading";

const LABELS: Record<BenchmarkConnectionState, string> = {
  not_configured: "本机采集登录尚未配置", provider_unavailable: "本机采集服务暂时不可用",
  checking: "正在检查小红书登录", login_required: "需要登录小红书",
  awaiting_scan: "请用小红书 App 扫码并确认登录", authorized: "小红书已连接",
  expired: "二维码已过期，请重新连接", error: "连接未完成，请重试",
};

export function BenchmarkConnectionPanel({ adapter, required, onConnected }: {
  adapter: FrameFactoryAdapter; required: boolean; onConnected: () => void;
}) {
  const [view, setView] = useState<BenchmarkConnectionView>({ connection: null, active: false, busy: false, paused: false, notice: "" });
  const flow = useRef<BenchmarkConnectionFlow | null>(null);
  const onConnectedRef = useRef(onConnected);
  useEffect(() => { onConnectedRef.current = onConnected; }, [onConnected]);
  useEffect(() => {
    const current = new BenchmarkConnectionFlow(adapter, setView, () => onConnectedRef.current());
    flow.current = current;
    const visibility = () => current.setVisible(!document.hidden);
    visibility();
    void current.check();
    document.addEventListener("visibilitychange", visibility);
    return () => {
      document.removeEventListener("visibilitychange", visibility);
      current.dispose();
      if (flow.current === current) flow.current = null;
    };
  }, [adapter]);

  const state = view.connection?.state;
  const connected = state === "authorized";
  const statusLabel = !view.active && state === "awaiting_scan" ? "已有待完成的登录，点击连接后查看二维码"
    : !view.active && state === "checking" ? "连接状态正在更新，可稍后再次检查"
      : state ? LABELS[state] : view.busy ? "正在检查连接状态…" : "尚未检查连接";
  return <section id="benchmark-connection" className="panel benchmark-source" aria-label="连接小红书">
    <div>
      <p className="eyebrow">本机采集登录</p>
      <h2>连接小红书</h2>
      <p className="muted">在本机采集浏览器中登录，用于获取你选择的公开主页和笔记详情。点击连接后生成二维码，请用小红书 App 扫码确认。</p>
      <p className="muted">连接成功后可继续当前主页发现或详情获取；完整视频分析可能调用付费模型，仍需你再次点击开始或重试。</p>
      {required ? <p role="status">当前采集需要登录。请完成连接，随后继续当前操作。</p> : null}
      <p role="status" aria-live="polite"><Badge tone={connected ? "success" : required ? "warning" : "neutral"}>{view.paused ? "页面已隐藏，连接检查暂停" : view.notice || statusLabel}</Badge></p>
      {state === "not_configured" ? <p className="muted">请在运行 Vistora 的电脑上配置并启动小红书采集服务，再检查登录状态。</p> : null}
      {view.active && view.connection?.qrImageDataUrl ? <div>
        {/* A strict PNG data URL is validated by the adapter; never load a remote login URL. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={view.connection.qrImageDataUrl} alt="小红书本机采集登录二维码" width={220} height={220} onError={() => flow.current?.qrUnavailable()} style={{ objectFit: "contain", background: "white", borderRadius: 8 }} />
        <p className="muted">请勿转发二维码。二维码过期后需重新点击连接。</p>
      </div> : null}
    </div>
    <div className="benchmark-deep-buttons">
      {view.active ? <button className="button-secondary" type="button" onClick={() => flow.current?.cancel()}>取消连接</button>
        : <button className="button" type="button" disabled={view.busy} onClick={() => void flow.current?.start()}>{view.busy ? "正在检查…" : connected ? required ? "检查连接并继续" : "检查已有连接" : "连接小红书"}</button>}
      {!view.active && !connected ? <button className="button-ghost" type="button" disabled={view.busy} onClick={() => void flow.current?.check()}>检查登录状态</button> : null}
      <a className="button-ghost" href="/benchmarks/history">读取历史报告</a>
    </div>
  </section>;
}
