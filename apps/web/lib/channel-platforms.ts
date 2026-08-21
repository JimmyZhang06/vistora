export const CHANNEL_PLATFORM_OPTIONS = [
  { value: "youtube", label: "YouTube" },
  { value: "bilibili", label: "哔哩哔哩" },
  { value: "douyin", label: "抖音" },
  { value: "xiaohongshu", label: "小红书" },
  { value: "wechat_channels", label: "微信视频号" },
] as const;

export function normalizeChannelPlatform(value?: string): string {
  return value?.trim().toLocaleLowerCase("en-US") ?? "";
}

export function channelPlatformLabel(value?: string): string {
  const normalized = normalizeChannelPlatform(value);
  return CHANNEL_PLATFORM_OPTIONS.find((option) => option.value === normalized)?.label
    ?? value?.trim()
    ?? "";
}
