import type { Metadata } from "next";
import { ChannelForm } from "@/components/channel-form";

export const metadata: Metadata = { title: "新建频道" };

export default function NewChannelPage() {
  return <ChannelForm />;
}
