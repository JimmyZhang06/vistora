// 渲染辅助：真数据来自后端 LLM 产物，含引号尖括号，进 innerHTML 前一律转义。
// 出处教训：kaleido MVP critic review 第 2 条（app.js 双层转义防内联 XSS）。

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function escapeAttribute(value) {
  return escapeHtml(value).replaceAll("\n", " ");
}

export function pad2(value) {
  return String(value).padStart(2, "0");
}

export function seconds(value) {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  const total = Math.round(value);
  return `${pad2(Math.floor(total / 60))}:${pad2(total % 60)}`;
}

export function fixed(value, digits = 1) {
  return typeof value === "number" && !Number.isNaN(value) ? value.toFixed(digits) : "—";
}

/** 素材切片相对路径 → 本地媒体 URL（后端仓库内白名单目录）。 */
export function clipUrl(libraryRoot, file) {
  if (!file) return null;
  const relative = `${libraryRoot}/${file}`.replaceAll("\\", "/");
  return `/media/${relative.split("/").map(encodeURIComponent).join("/")}`;
}

export function runFileUrl(relative) {
  if (!relative) return null;
  return `/media/${relative.replaceAll("\\", "/").split("/").map(encodeURIComponent).join("/")}`;
}

export function clipName(file) {
  return String(file || "").split("/").pop() || "—";
}

const IMAGE_EXTENSIONS = new Set(["jpg", "jpeg", "png", "webp", "avif", "gif"]);
const AUDIO_EXTENSIONS = new Set(["mp3", "wav", "m4a", "aac", "flac"]);

/** 书单产线用静图 Ken Burns、音效等非视频素材，按扩展名分派渲染。 */
export function mediaKind(file, declared) {
  if (declared && declared !== "video") return declared;
  const extension = String(file || "").split(".").pop()?.toLowerCase();
  if (IMAGE_EXTENSIONS.has(extension)) return "image";
  if (AUDIO_EXTENSIONS.has(extension)) return "audio";
  return "video";
}

/** 后端给静图的 dur 是 9999 哨兵值（可无限拉伸），不是真实时长，别当秒数显示。 */
export function clipDuration(value, locale = "zh-CN") {
  if (typeof value !== "number" || Number.isNaN(value)) return "—";
  if (value >= 9999) return locale === "zh-CN" ? "静图" : "still";
  return `${value.toFixed(1)}s`;
}
