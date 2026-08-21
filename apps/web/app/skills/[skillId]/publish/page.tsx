import type { Metadata } from "next";
import { SkillStudio } from "@/components/skill-studio";

export const metadata: Metadata = { title: "Skill 发布检查" };

export default async function SkillPublishPage({ params }: { params: Promise<{ skillId: string }> }) {
  const { skillId } = await params;
  return <SkillStudio skillId={skillId} initialView="publish" />;
}
