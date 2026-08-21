import type { Metadata } from "next";
import { AssetHub } from "@/components/asset-hub";

export const metadata: Metadata = { title: "素材" };

export default function AssetsPage() {
  return <AssetHub />;
}
