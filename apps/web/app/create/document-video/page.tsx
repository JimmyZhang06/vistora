import type { Metadata } from "next";
import { DocumentVideoPilotStudio } from "@/components/document-video-pilot-studio";

export const metadata: Metadata = { title: "文件讲解视频" };

export default function DocumentVideoCreatePage() {
  return <DocumentVideoPilotStudio />;
}
