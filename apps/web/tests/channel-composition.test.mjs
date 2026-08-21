import assert from "node:assert/strict";
import test from "node:test";

test("channel selection preserves every default asset library and supports accessible toggling", async () => {
  const {
    selectionFromComposerOptions,
    toggleAssetLibrarySelection,
  } = await import("../lib/channel-composition.ts");
  const options = {
    channels: [{
      id: "channel-1",
      workspaceId: "workspace-1",
      slug: "daily-stories",
      name: "Daily Stories",
      description: "",
      status: "active",
      defaultComposition: {
        skillVersionId: "skill-version-1",
        pipelineVersionId: "pipeline-version-1",
        assetLibraryIds: ["library-1", "library-2", "library-1"],
      },
      brandConfig: { profile: {} },
      revision: 1,
      createdBy: "user-1",
      createdAt: "2026-08-20T08:00:00Z",
      updatedAt: "2026-08-20T08:00:00Z",
    }],
    skills: [],
    assetLibraries: [
      { id: "library-1", workspaceId: "workspace-1", name: "One", description: "", assetCount: 1 },
      { id: "library-2", workspaceId: "workspace-1", name: "Two", description: "", assetCount: 1 },
    ],
    voiceProfiles: [],
    renderPresets: [],
    pipelines: [],
  };

  const selected = selectionFromComposerOptions(options, "channel-1");
  assert.deepEqual(selected.assetLibraryIds, ["library-1", "library-2"]);
  assert.deepEqual(toggleAssetLibrarySelection(selected.assetLibraryIds, "library-1"), ["library-2"]);
  assert.deepEqual(toggleAssetLibrarySelection(selected.assetLibraryIds, "library-3"), ["library-1", "library-2", "library-3"]);
  assert.deepEqual(selected.assetLibraryIds, ["library-1", "library-2"], "toggling must not mutate channel defaults");
});
