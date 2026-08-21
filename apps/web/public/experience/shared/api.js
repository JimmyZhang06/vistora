// FrameFactory 前端数据层：只跟本地 /api 说话，不含任何业务判断。
// 后端合同见仓库根目录 02_交接文档.md 与 frontend/docs/BACKEND_INTEGRATION.md。

async function request(path, options = {}) {
  const response = await fetch(path, {
    headers: options.body ? {"content-type": "application/json"} : undefined,
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `${response.status} ${path}`);
  return payload;
}

export class WalletAuthError extends Error {
  constructor(message) {
    super(message);
    this.name = "WalletAuthError";
  }
}

export const api = {
  authConfig: () => request("/api/auth/config"),
  authSession: () => request("/api/auth/session"),
  authNonce: (address) => request("/api/auth/nonce", {method: "POST", body: {address}}),
  authVerify: (body) => request("/api/auth/verify", {method: "POST", body}),
  authWalletLogout: () => request("/api/auth/wallet/logout", {method: "POST", body: {}}),
  authLogout: () => request("/api/auth/logout", {method: "POST", body: {}}),
  health: () => request("/api/health"),
  accounts: () => request("/api/accounts"),
  updateAccount: (accountId, body) => request(`/api/accounts/${encodeURIComponent(accountId)}`, {method: "PATCH", body}),
  materialStatus: (account, person) => request(`/api/materials/status?${new URLSearchParams({account, person})}`),
  libraries: () => request("/api/libraries"),
  videos: (params) => request(`/api/assets/videos?${new URLSearchParams(params)}`),
  voices: () => request("/api/assets/voices"),
  flywheel: () => request("/api/flywheel"),
  finals: () => request("/api/finals"),
  runs: () => request("/api/runs"),
  run: (slug) => request(`/api/runs/${encodeURIComponent(slug)}`),
  create: (body) => request("/api/runs", {method: "POST", body}),
  advance: (slug, body) => request(`/api/runs/${encodeURIComponent(slug)}/continue`, {method: "POST", body}),
  stop: (slug) => request(`/api/runs/${encodeURIComponent(slug)}/stop`, {method: "POST", body: {}}),
  saveScript: (slug, sentences) => request(`/api/runs/${encodeURIComponent(slug)}/script`, {method: "POST", body: {sentences}}),
  saveEdl: (slug, edl) => request(`/api/runs/${encodeURIComponent(slug)}/edl`, {method: "POST", body: {edl}}),
  agent: (body) => request("/api/agent", {method: "POST", body}),
  // 上传原片：裸字节 PUT，元信息走 query（避免手写 multipart 解析）
  upload: async (file, {library, person}) => {
    const query = new URLSearchParams({library, person, filename: file.name});
    const response = await fetch(`/api/assets/upload?${query}`, {method: "POST", body: file});
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.error || `上传失败 ${response.status}`);
    return payload;
  },
  // 通用 CLI 任务（白名单注册表）
  commands: () => request("/api/commands"),
  blockchainRecords: () => request("/api/blockchain/records"),
  runCommand: (command, params) => request("/api/jobs", {method: "POST", body: {command, params}}),
  jobState: (jobId) => request(`/api/jobs/${encodeURIComponent(jobId)}`),
  stopabort: (jobId) => request(`/api/jobs/${encodeURIComponent(jobId)}/stop`, {method: "POST", body: {}}),
};

export async function connectWallet() {
  if (!window.ethereum) throw new WalletAuthError("未检测到浏览器钱包，请安装 MetaMask 或兼容钱包");
  await api.authConfig();
  // 已授权账号直接复用，避免页面重载或重复点击时再发 wallet_requestPermissions。
  let [address] = await window.ethereum.request({method: "eth_accounts"});
  if (!address) {
    try {
      [address] = await window.ethereum.request({method: "eth_requestAccounts"});
    } catch (error) {
      if (error?.code === -32002 || /already pending|requestPermissions/i.test(error?.message || "")) {
        throw new WalletAuthError("MetaMask 中已有一个连接请求等待处理。请打开 MetaMask，完成或取消该请求后再试。");
      }
      throw error;
    }
  }
  if (!address) throw new WalletAuthError("钱包未返回账号");

  const nonce = await api.authNonce(address);
  const signature = await window.ethereum.request({
    method: "personal_sign",
    params: [nonce.message, address],
  });
  return api.authVerify({address, nonce: nonce.nonce, signature});
}

/** 订阅通用任务的日志流（没有决策链，只有 CLI 输出 + 状态）。 */
export function subscribeJob(jobId, handlers = {}) {
  const source = new EventSource(`/api/jobs/${encodeURIComponent(jobId)}/events`);
  for (const name of ["log", "status", "decision", "snapshot"]) {
    source.addEventListener(name, (event) => {
      try {
        handlers[name]?.(JSON.parse(event.data));
      } catch {
        // 忽略半行
      }
    });
  }
  source.onerror = () => handlers.error?.();
  return source;
}

/** 订阅一条 run 的事件流：decision（决策链）/ log（CLI 输出）/ status / snapshot。 */
export function subscribeRun(slug, handlers = {}) {
  let source;
  const seen = new Set();
  const connect = () => {
    source = new EventSource(`/api/runs/${encodeURIComponent(slug)}/events`);
    for (const name of ["decision", "log", "status", "snapshot"]) {
      source.addEventListener(name, (event) => {
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch {
          return;
        }
        // 重连后 decisions.jsonl 会补发已有行，按 seq 去重
        if (name === "decision" && payload?.seq != null) {
          if (seen.has(payload.seq)) return;
          seen.add(payload.seq);
        }
        handlers[name]?.(payload);
      });
    }
    source.onerror = () => {
      source.close();
      // 后端仍在跑时 5 秒后重连（ handlers.reconnect 控制是否启用）
      if (handlers.reconnect !== false) {
        setTimeout(connect, 5000);
      }
    };
  };
  connect();
  return {
    close: () => { source.close(); },
    source,
  };
}

/** 后端连不上时（没起服务 / 断网现场）走离线演示，不让页面白屏。 */
export async function backendOnline() {
  try {
    const health = await api.health();
    return Boolean(health.ready);
  } catch {
    return false;
  }
}
