import type { Metadata } from "next";
import { WebpageVideoStudio } from "@/components/webpage-video-studio";

export const metadata: Metadata = { title: "申请评审演示" };

export default function ApplicationDemoPage() {
  return <WebpageVideoStudio mode="application-demo" />;
}
