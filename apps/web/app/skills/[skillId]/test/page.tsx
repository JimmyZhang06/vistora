import type { Metadata } from "next";
import { SkillStudio } from "@/components/skill-studio";

export const metadata: Metadata = { title: "Skill 测试台" };

export default async function SkillTestPage({ params }: { params: Promise<{ skillId: string }> }) {
  const { skillId } = await params;
  return <SkillStudio skillId={skillId} initialView="test" />;
}
