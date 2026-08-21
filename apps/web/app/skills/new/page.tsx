import type { Metadata } from "next";
import { SkillCreator } from "@/components/skill-creator";

export const metadata: Metadata = { title: "创建 Skill" };

export default function NewSkillPage() {
  return <SkillCreator />;
}
