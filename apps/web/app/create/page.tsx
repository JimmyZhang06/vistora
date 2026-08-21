import type { Metadata } from "next";
import { CreateComposer } from "@/components/create-composer";

export const metadata: Metadata = { title: "创作" };

export default function CreatePage() {
  return <CreateComposer />;
}
