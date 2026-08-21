import type { Metadata } from "next";
import { SkillLibrary } from "@/components/skill-library";

export const metadata: Metadata = { title: "Skill" };

export default function SkillsPage() {
  return <SkillLibrary />;
}
