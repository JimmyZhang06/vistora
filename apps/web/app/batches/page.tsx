import type { Metadata } from "next";
import { BatchConsole } from "@/components/batch-console";

export const metadata: Metadata = { title: "批量生产" };

export default function BatchesPage() {
  return <BatchConsole />;
}
