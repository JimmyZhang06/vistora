import type { Metadata } from "next";
import { AssetLibraryWorkbench } from "@/components/asset-library-workbench";

export const metadata: Metadata = { title: "素材库管理" };

export default async function AssetLibraryPage({ params }: { params: Promise<{ libraryId: string }> }) {
  const { libraryId } = await params;
  return <AssetLibraryWorkbench libraryId={libraryId} />;
}
