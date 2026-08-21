import type { Metadata } from "next";
import { ChannelDetail } from "@/components/channel-detail";

export const metadata: Metadata = { title: "频道详情" };

export default async function ChannelDetailPage({ params }: { params: Promise<{ channelId: string }> }) {
  const { channelId } = await params;
  return <ChannelDetail channelId={channelId} />;
}
