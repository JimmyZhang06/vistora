import type { Metadata } from "next";
import { SkillStudio } from "@/components/skill-studio";

export const metadata: Metadata = { title: "Skill 版本历史" };

export default async function SkillVersionsPage({ params }: { params: Promise<{ skillId: string }> }) {
  const { skillId } = await params;
  return <SkillStudio skillId={skillId} initialView="versions" />;
}
