import type { Metadata } from "next";
import { ProjectsView } from "@/components/projects-view";

export const metadata: Metadata = { title: "项目" };

export default function ProjectsPage() {
  return <ProjectsView />;
}
