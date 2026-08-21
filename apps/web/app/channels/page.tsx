import type { Metadata } from "next";
import { ChannelsView } from "@/components/channels-view";

export const metadata: Metadata = { title: "频道" };

export default function ChannelsPage() {
  return <ChannelsView />;
}
