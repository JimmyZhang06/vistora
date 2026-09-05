import type { Metadata } from "next";
import { BenchmarkHistory } from "@/components/benchmark-history";

export const metadata: Metadata = { title: "历史分析记录" };

export default function BenchmarkHistoryPage() {
  return <BenchmarkHistory />;
}
