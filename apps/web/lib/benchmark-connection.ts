import type { FrameFactoryAdapter } from "./api/adapter.ts";
import type { ApiResult, BenchmarkConnection } from "./api/contracts.ts";

type ConnectionAdapter = Pick<FrameFactoryAdapter, "getBenchmarkConnectionStatus" | "startBenchmarkConnection">;
export interface BenchmarkConnectionView {
  connection: BenchmarkConnection | null;
  active: boolean;
  busy: boolean;
  paused: boolean;
  notice: string;
}

// Login material lives only in this short-lived controller and React state.
// Polling never creates a QR code, and cancel never logs out a saved browser session.
export class BenchmarkConnectionFlow {
  private view: BenchmarkConnectionView = { connection: null, active: false, busy: false, paused: false, notice: "" };
  private epoch = 0;
  private request: AbortController | null = null;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private deadline = 0;
  private failures = 0;
  private disposed = false;
  private visible = true;
  private adapter: ConnectionAdapter;
  private onChange: (view: BenchmarkConnectionView) => void;
  private onConnected: () => void;
  private now: () => number;

  constructor(
    adapter: ConnectionAdapter,
    onChange: (view: BenchmarkConnectionView) => void,
    onConnected: () => void,
    now: () => number = Date.now,
  ) {
    this.adapter = adapter;
    this.onChange = onChange;
    this.onConnected = onConnected;
    this.now = now;
  }

  private publish(patch: Partial<BenchmarkConnectionView>) {
    this.view = { ...this.view, ...patch };
    if (!this.disposed) this.onChange(this.view);
  }

  private stopRequest() {
    ++this.epoch;
    this.request?.abort();
    this.request = null;
    clearTimeout(this.timer);
  }

  async check(resumeCollection = false) {
    if (this.disposed || this.view.active || this.view.busy || !this.visible) return;
    await this.fetch(false, resumeCollection);
  }

  async start() {
    if (this.disposed || this.view.active || !this.visible) return;
    this.deadline = this.now() + 180_000;
    this.failures = 0;
    this.publish({ active: true, paused: false, notice: "", connection: null });
    await this.fetch(true);
  }

  cancel() {
    if (this.disposed) return;
    this.stopRequest();
    this.publish({ active: false, busy: false, paused: false, connection: null, notice: "已取消本次连接。再次连接时会先检查已有登录。" });
  }

  qrUnavailable() {
    if (this.disposed) return;
    this.stopRequest();
    this.publish({ active: false, busy: false, paused: false, connection: null, notice: "二维码图像无法显示，请重新连接。" });
  }

  setVisible(visible: boolean) {
    if (this.disposed || visible === this.visible) return;
    this.visible = visible;
    if (!visible) {
      this.stopRequest();
      this.publish({ busy: false, paused: this.view.active });
    } else if (this.view.active) {
      this.publish({ paused: false });
      if (!this.expired()) void this.fetch(false);
    } else {
      void this.check();
    }
  }

  dispose() {
    this.disposed = true;
    this.stopRequest();
    this.view = { connection: null, active: false, busy: false, paused: false, notice: "" };
  }

  private expired() {
    if (this.now() < this.deadline) return false;
    this.stopRequest();
    this.publish({ active: false, busy: false, paused: false, connection: null, notice: "本次连接已过期，请点击“连接小红书”重新获取二维码。" });
    return true;
  }

  private schedule(delaySeconds: number) {
    clearTimeout(this.timer);
    if (!this.view.active || !this.visible || this.disposed || this.expired()) return;
    this.timer = setTimeout(() => {
      if (!this.expired()) void this.fetch(false);
    }, Math.min(delaySeconds * 1000, this.deadline - this.now()));
  }

  private async fetch(createQr: boolean, resumeCollection = false) {
    this.stopRequest();
    const epoch = this.epoch;
    const request = new AbortController();
    this.request = request;
    this.publish({ busy: true });
    let result: ApiResult<BenchmarkConnection>;
    try {
      result = createQr
        ? await this.adapter.startBenchmarkConnection(globalThis.crypto.randomUUID(), request.signal)
        : await this.adapter.getBenchmarkConnectionStatus(request.signal);
    } catch {
      result = { ok: false, error: { code: "NETWORK_ERROR", message: "连接检查暂时不可用。" } };
    }
    if (this.disposed || epoch !== this.epoch) return;
    this.request = null;
    this.publish({ busy: false });
    if (!result.ok) {
      this.failures += 1;
      const retry = this.view.active && !createQr && this.failures < 3 && result.error.status !== 401 && result.error.status !== 403 && result.error.status !== 404;
      this.publish({ active: retry, connection: null, notice: retry
        ? "连接检查暂时中断，正在重新检查。"
        : result.error.status === 404 || result.error.code === "BENCHMARK_AUTH_INVALID_RESPONSE"
          ? "研究 API 尚未支持小红书连接，请更新 API 后重试。已有报告仍可读取。"
          : "暂时无法检查小红书连接，请确认本机采集服务可用后重试。已有报告仍可读取。" });
      if (retry) this.schedule(3);
      return;
    }
    this.failures = 0;
    const connection = result.data;
    const wasActive = this.view.active;
    if (connection.expiresAt && wasActive) this.deadline = Math.min(this.deadline, Date.parse(connection.expiresAt));
    if (wasActive && connection.state !== "authorized" && this.expired()) return;
    const active = wasActive && (connection.state === "checking" || connection.state === "awaiting_scan");
    this.publish({ connection: { ...connection, qrImageDataUrl: active ? connection.qrImageDataUrl : null }, active, notice: "" });
    if ((wasActive || resumeCollection) && connection.state === "authorized") this.onConnected();
    if (active) this.schedule(connection.retryAfterSeconds);
  }
}
