import type {
  AccountApiKey,
  AccountCapabilities,
  AccountProfile,
  AccountProfileUpdate,
  AccountSession,
  ApiResult,
  ApiKeyCreateRequest,
  AssetLibraryCreateRequest,
  AssetLibraryOption,
  Asset,
  AssetBulkRequest,
  AssetBulkResult,
  AssetIngestionJob,
  AssetIngestionJobPage,
  AssetIngestionJobQuery,
  AssetPage,
  AssetPatchRequest,
  AssetPreview,
  AssetPoster,
  AssetQuery,
  AssetReviewRequest,
  AssetSegment,
  AssetUploadRequest,
  RemoteAssetImportRequest,
  Channel,
  ChannelDraft,
  ChannelQuery,
  ComparisonRequest,
  ComparisonResult,
  ComposerOptions,
  CreatedApiKey,
  CreationPreferences,
  CreationPreferencesUpdate,
  CreateSkillRequest,
  GenerationBatch,
  GenerationBatchCreateRequest,
  GenerationBatchItemPage,
  GenerationBatchItemQuery,
  LibraryBuildJob,
  LibraryBuildJobCreateRequest,
  FullAiEstimate,
  FullAiOptions,
  FullAiRun,
  FullAiRunCreateRequest,
  FullAiSpec,
  WebpageVideoCapture,
  WebpageVideoOptions,
  WebpageVideoReviewRequest,
  WebpageVideoScopeReviewRequest,
  WebpageVideoSitePlan,
  WebpageVideoStoryboardReviewRequest,
  WebpageVideoRun,
  WebpageVideoRunCreateRequest,
  Run,
  RunDraft,
  RunEstimate,
  RunQuery,
  RunStep,
  RunStepReviewRequest,
  SessionContext,
  Skill,
  SkillDetail,
  SkillDraftPatch,
  SkillQuery,
  SkillVersion,
  ValidationReport,
  VersionedResource,
} from "./contracts";

/**
 * UI-facing data boundary. Page and component code should depend on this
 * interface, never directly on mock fixtures or a transport implementation.
 */
export interface FrameFactoryAdapter {
  getSession(): Promise<ApiResult<SessionContext>>;
  getAccountCapabilities(): Promise<ApiResult<AccountCapabilities>>;
  getAccountProfile(): Promise<ApiResult<VersionedResource<AccountProfile>>>;
  replaceAccountProfile(
    profile: AccountProfileUpdate,
    etag: string,
  ): Promise<ApiResult<VersionedResource<AccountProfile>>>;
  getCreationPreferences(): Promise<ApiResult<VersionedResource<CreationPreferences>>>;
  replaceCreationPreferences(
    preferences: CreationPreferencesUpdate,
    etag: string,
  ): Promise<ApiResult<VersionedResource<CreationPreferences>>>;
  listAccountSessions(): Promise<ApiResult<AccountSession[]>>;
  revokeAccountSession(sessionId: string): Promise<ApiResult<void>>;
  listApiKeys(): Promise<ApiResult<AccountApiKey[]>>;
  createApiKey(request: ApiKeyCreateRequest): Promise<ApiResult<CreatedApiKey>>;
  revokeApiKey(keyId: string): Promise<ApiResult<void>>;

  listSkills(query?: SkillQuery): Promise<ApiResult<Skill[]>>;
  getSkill(skillId: string): Promise<ApiResult<SkillDetail>>;
  createSkill(request: CreateSkillRequest): Promise<ApiResult<Skill>>;
  createDraft(skillId: string, sourceVersionId: string): Promise<ApiResult<SkillVersion>>;
  saveDraft(
    skillId: string,
    versionId: string,
    patch: SkillDraftPatch,
    expectedRevision: number,
  ): Promise<ApiResult<SkillVersion>>;
  validateVersion(skillId: string, versionId: string): Promise<ApiResult<ValidationReport>>;
  publishVersion(
    skillId: string,
    versionId: string,
    releaseNotes: string,
  ): Promise<ApiResult<SkillVersion>>;
  compareVersions(request: ComparisonRequest): Promise<ApiResult<ComparisonResult>>;
  rollbackSkill(skillId: string, versionId: string): Promise<ApiResult<Skill>>;
  deprecateVersion(skillId: string, versionId: string): Promise<ApiResult<SkillVersion>>;
  deleteDraft(skillId: string): Promise<ApiResult<void>>;

  getComposerOptions(workspaceId: string): Promise<ApiResult<ComposerOptions>>;
  getFullAiOptions(): Promise<ApiResult<FullAiOptions>>;
  estimateFullAiRun(spec: FullAiSpec): Promise<ApiResult<FullAiEstimate>>;
  createFullAiRun(
    request: FullAiRunCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<FullAiRun>>;
  getFullAiRun(runId: string): Promise<ApiResult<FullAiRun>>;
  getWebpageVideoOptions(): Promise<ApiResult<WebpageVideoOptions>>;
  createWebpageVideoRun(
    request: WebpageVideoRunCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<WebpageVideoRun>>;
  getWebpageVideoRun(runId: string): Promise<ApiResult<WebpageVideoRun>>;
  getWebpageVideoCapture(runId: string): Promise<ApiResult<WebpageVideoCapture>>;
  getWebpageVideoSite(runId: string): Promise<ApiResult<WebpageVideoSitePlan>>;
  reviewWebpageVideoRun(
    runId: string,
    review: WebpageVideoReviewRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<void>>;
  reviewWebpageVideoScope(runId: string, review: WebpageVideoScopeReviewRequest, idempotencyKey: string): Promise<ApiResult<void>>;
  reviewWebpageVideoStoryboard(runId: string, review: WebpageVideoStoryboardReviewRequest, idempotencyKey: string): Promise<ApiResult<void>>;
  cancelWebpageVideoRun(runId: string, idempotencyKey: string): Promise<ApiResult<void>>;
  createAssetLibrary(
    request: AssetLibraryCreateRequest,
  ): Promise<ApiResult<AssetLibraryOption>>;
  createLibraryBuildJob(
    request: LibraryBuildJobCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<LibraryBuildJob>>;
  getLibraryBuildJob(jobId: string): Promise<ApiResult<LibraryBuildJob>>;
  cancelLibraryBuildJob(jobId: string, revision: number): Promise<ApiResult<LibraryBuildJob>>;
  uploadAsset(request: AssetUploadRequest): Promise<ApiResult<void>>;
  importRemoteAsset(request: RemoteAssetImportRequest): Promise<ApiResult<void>>;
  listAssets(libraryId: string, query?: AssetQuery): Promise<ApiResult<AssetPage>>;
  getAsset(assetId: string): Promise<ApiResult<Asset>>;
  updateAsset(assetId: string, revision: number, patch: AssetPatchRequest, idempotencyKey: string): Promise<ApiResult<Asset>>;
  reviewAsset(assetId: string, revision: number, review: AssetReviewRequest, idempotencyKey: string): Promise<ApiResult<Asset>>;
  reanalyzeAsset(assetId: string, revision: number, idempotencyKey: string): Promise<ApiResult<AssetIngestionJob>>;
  deleteAsset(assetId: string, revision: number, idempotencyKey: string): Promise<ApiResult<void>>;
  restoreAsset(assetId: string, revision: number, idempotencyKey: string): Promise<ApiResult<Asset>>;
  bulkUpdateAssets(request: AssetBulkRequest, idempotencyKey: string): Promise<ApiResult<AssetBulkResult>>;
  getAssetPreview(assetId: string): Promise<ApiResult<AssetPreview>>;
  getAssetPoster(assetId: string): Promise<ApiResult<AssetPoster>>;
  listAssetSegments(assetId: string): Promise<ApiResult<AssetSegment[]>>;
  listAssetIngestionJobs(query?: AssetIngestionJobQuery): Promise<ApiResult<AssetIngestionJobPage>>;
  estimateRun(draft: RunDraft): Promise<ApiResult<RunEstimate>>;
  createRun(draft: RunDraft, idempotencyKey: string): Promise<ApiResult<Run>>;
  listRuns(query?: RunQuery): Promise<ApiResult<Run[]>>;
  getRun(runId: string): Promise<ApiResult<Run>>;
  cancelRun(runId: string, idempotencyKey: string): Promise<ApiResult<Run>>;
  retryRunStep(stepId: string, idempotencyKey: string): Promise<ApiResult<RunStep>>;
  reviewRunStep(
    stepId: string,
    review: RunStepReviewRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<RunStep>>;

  listGenerationBatches(): Promise<ApiResult<GenerationBatch[]>>;
  getGenerationBatch(batchId: string): Promise<ApiResult<GenerationBatch>>;
  listGenerationBatchItems(
    batchId: string,
    query?: GenerationBatchItemQuery,
  ): Promise<ApiResult<GenerationBatchItemPage>>;
  createGenerationBatch(
    request: GenerationBatchCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<GenerationBatch>>;
  cancelGenerationBatch(
    batchId: string,
    idempotencyKey: string,
  ): Promise<ApiResult<GenerationBatch>>;
  retryFailedGenerationBatch(
    batchId: string,
    idempotencyKey: string,
  ): Promise<ApiResult<GenerationBatch>>;

  listChannels(query: ChannelQuery): Promise<ApiResult<Channel[]>>;
  getChannel(channelId: string): Promise<ApiResult<VersionedResource<Channel>>>;
  saveChannel(
    draft: ChannelDraft,
    etag?: string,
    idempotencyKey?: string,
  ): Promise<ApiResult<VersionedResource<Channel>>>;
  archiveChannel(channelId: string, etag: string): Promise<ApiResult<void>>;
}
