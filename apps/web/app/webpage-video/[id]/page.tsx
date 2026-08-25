import type { Metadata } from "next";
import { WebpageVideoDetail } from "@/components/webpage-video-detail";

export const metadata: Metadata = { title: "网页截图成片任务" };

export default async function WebpageVideoRunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <WebpageVideoDetail runId={id} />;
}
