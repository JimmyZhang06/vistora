import type { Metadata } from "next";
import { ChannelForm } from "@/components/channel-form";

export const metadata: Metadata = { title: "编辑频道" };

export default async function EditChannelPage({ params }: { params: Promise<{ channelId: string }> }) {
  const { channelId } = await params;
  return <ChannelForm channelId={channelId} />;
}
