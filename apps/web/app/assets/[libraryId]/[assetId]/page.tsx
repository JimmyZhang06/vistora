import type { Metadata } from "next";
import { AssetDetail } from "@/components/asset-detail";

export const metadata: Metadata = { title: "素材详情" };

export default async function AssetDetailPage({ params }: { params: Promise<{ libraryId: string; assetId: string }> }) {
  const { libraryId, assetId } = await params;
  return <AssetDetail libraryId={libraryId} assetId={assetId} />;
}
