import type { Metadata } from "next";
import { SkillStudio } from "@/components/skill-studio";

export const metadata: Metadata = { title: "Skill 编辑器" };

export default async function SkillEditorPage({ params }: { params: Promise<{ skillId: string }> }) {
  const { skillId } = await params;
  return <SkillStudio skillId={skillId} initialView="editor" />;
}
