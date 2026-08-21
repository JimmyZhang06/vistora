import type { ComposerOptions } from "./api/contracts";

export interface ComposerSelection {
  channelId: string;
  skillVersionId: string;
  assetLibraryIds: string[];
  voiceProfileId: string;
  renderPresetVersionId: string;
  pipelineVersionId: string;
}

export const emptyComposerSelection: ComposerSelection = {
  channelId: "",
  skillVersionId: "",
  assetLibraryIds: [],
  voiceProfileId: "",
  renderPresetVersionId: "",
  pipelineVersionId: "",
};

export function selectionFromComposerOptions(
  options: ComposerOptions,
  requestedChannelId = "",
): ComposerSelection {
  const channel = options.channels.find(
    (item) => item.id === requestedChannelId && item.status === "active",
  );
  return {
    channelId: channel?.id ?? "",
    skillVersionId: channel?.defaultComposition.skillVersionId ?? options.skills[0]?.versionId ?? "",
    assetLibraryIds: channel
      ? [...new Set(channel.defaultComposition.assetLibraryIds)]
      : options.assetLibraries[0]?.id ? [options.assetLibraries[0].id] : [],
    voiceProfileId: channel?.defaultComposition.voiceProfileId ?? options.voiceProfiles[0]?.id ?? "",
    renderPresetVersionId: channel?.defaultComposition.renderPresetVersionId ?? options.renderPresets[0]?.id ?? "",
    pipelineVersionId: channel?.defaultComposition.pipelineVersionId ?? options.pipelines[0]?.id ?? "",
  };
}

export function toggleAssetLibrarySelection(
  selectedIds: readonly string[],
  libraryId: string,
): string[] {
  return selectedIds.includes(libraryId)
    ? selectedIds.filter((id) => id !== libraryId)
    : [...selectedIds, libraryId];
}
