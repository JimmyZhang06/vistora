import type { Metadata } from "next";
import { FullAiVideoStudio } from "@/components/full-ai-video-studio";

export const metadata: Metadata = { title: "全 AI 影片" };

export default function FullAiVideoPage() {
  return <FullAiVideoStudio />;
}
