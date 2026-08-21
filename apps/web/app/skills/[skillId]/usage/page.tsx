import type { Metadata } from "next";
import { SkillStudio } from "@/components/skill-studio";

export const metadata: Metadata = { title: "Skill 使用情况" };

export default async function SkillUsagePage({ params }: { params: Promise<{ skillId: string }> }) {
  const { skillId } = await params;
  return <SkillStudio skillId={skillId} initialView="usage" />;
}
