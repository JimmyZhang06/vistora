import type { Metadata } from "next";
import { ApplicationEvidenceDashboard } from "@/components/application-evidence-dashboard";

export const metadata: Metadata = { title: "申请证据中心" };

export default function ApplicationEvidencePage() {
  return <ApplicationEvidenceDashboard />;
}
