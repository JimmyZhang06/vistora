import type { Metadata } from "next";
import { RunDetail } from "@/components/run-detail";

export const metadata: Metadata = { title: "项目执行状态" };

export default async function RunDetailPage({ params }: { params: Promise<{ runId: string }> }) {
  const { runId } = await params;
  return <RunDetail runId={runId} />;
}
