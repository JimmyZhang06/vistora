import type { Metadata } from "next";
import { WebpageVideoStudio } from "@/components/webpage-video-studio";

export const metadata: Metadata = { title: "网页截图成片" };

export default function WebpageVideoCreatePage() {
  return <WebpageVideoStudio />;
}
