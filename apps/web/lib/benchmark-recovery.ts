import type { ApiProblem, BenchmarkAccountReport, BenchmarkNote } from "./api/contracts";

/** Canonical identity only. Platform tokens must never enter requests or saved selections. */
export function benchmarkProfile(value: string): { url: string; userId: string } | null {
  try {
    const url = new URL(value.trim());
    const match = /^\/user\/profile\/([a-f0-9]{24})\/?$/.exec(url.pathname);
    if (url.protocol !== "https:" || url.hostname !== "www.xiaohongshu.com" || url.port || url.username || url.password || !match) return null;
    return { url: `https://www.xiaohongshu.com/user/profile/${match[1]}`, userId: match[1] };
  } catch {
    return null;
  }
}

export function verifiedBenchmarkNote(note?: BenchmarkNote | null): note is BenchmarkNote & { noteId: string } {
  return note?.identityStatus === "verified" && /^[a-f0-9]{24}$/.test(note.noteId ?? "");
}

/** A refreshed sample has new local indexes; only a verified ID can preserve selection. */
export function refreshedBenchmarkSelection(report: BenchmarkAccountReport, profileUserId: string, noteId?: string): number | null {
  if (report.snapshot.profile.userId !== profileUserId || !noteId) return null;
  return report.snapshot.notes.find((note) => verifiedBenchmarkNote(note) && note.noteId === noteId)?.sampleIndex ?? null;
}

export function benchmarkRecoveryMessage(problem: Pick<ApiProblem, "code" | "message">): string {
  const messages: Record<string, string> = {
    BENCHMARK_AUTHENTICATION_REQUIRED: "小红书登录已失效，请点击页面上方的“连接小红书”并扫码登录，完成后将继续当前主页或详情获取。",
    BENCHMARK_NOTE_UNAVAILABLE: "笔记不可访问、已下架或当前账号无权限。请在原主页核验，或选择另一篇可访问笔记。",
    BENCHMARK_NOTE_INACCESSIBLE: "笔记不可访问、已下架或当前账号无权限。请在原主页核验，或选择另一篇可访问笔记。",
    BENCHMARK_NOTE_NOT_ACCESSIBLE: "当前登录账号无法访问这篇笔记，请在原主页核验访问权限，或选择其他笔记。",
    BENCHMARK_NOTE_NOT_IN_SAMPLE: "当前主页已找不到这篇笔记，请补全笔记信息后重新选择。",
    BENCHMARK_SOURCE_CHANGED: "平台页面结构发生变化，尚不能核验笔记归属。请补全笔记信息；仍失败时保留错误码联系维护者。",
    BENCHMARK_NOTE_IDENTITY_CONFLICT: "笔记身份来源冲突，本次补全已停止。请核验原主页并联系维护者。",
    BENCHMARK_NOTE_IDENTITY_INVALID: "页面笔记标识未通过格式或来源核验。请补全笔记信息；仍失败时保留错误码联系维护者。",
    BENCHMARK_NOTE_FIELDS_INSUFFICIENT: "页面字段不足，暂时不能核验笔记身份。请补全笔记信息后重新选择已核验笔记。",
    BENCHMARK_PROFILE_MISMATCH: "采集结果的作者与当前主页不一致，本次操作已停止。请核验原主页后重试。",
    BENCHMARK_NOTE_IDENTITY_INCOMPLETE: "主页信息可展示，但部分笔记缺少可核验身份。请补全笔记信息，完成后重新选择已核验笔记。",
    BENCHMARK_IDENTITY_INCOMPLETE: "主页信息可展示，但部分笔记缺少可核验身份。请补全笔记信息，完成后重新选择已核验笔记。",
    BENCHMARK_PROVIDER_UNAVAILABLE: "本机采集浏览器暂时不可用。请恢复采集服务后补全笔记信息。",
    BENCHMARK_PROVIDER_BUSY: "采集浏览器正在处理其他请求，请稍后补全笔记信息。",
    BENCHMARK_REFRESH_RATE_LIMITED: "笔记信息刚刚补采过，请至少等待 10 秒后重试。",
    FORBIDDEN: "当前工作区没有研究采集权限，请切换到有权限的工作区后重试。",
  };
  return messages[problem.code] ?? (problem.message || "操作暂时不可用，请重试。");
}
