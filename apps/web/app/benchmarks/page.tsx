import type { Metadata } from "next";
import { BenchmarkAccountDemo } from "@/components/benchmark-account-demo";

export const metadata: Metadata = { title: "对标分析 Demo" };

export default function BenchmarksPage() {
  return <BenchmarkAccountDemo />;
}
