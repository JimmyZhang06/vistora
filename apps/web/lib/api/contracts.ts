export type IsoTimestamp = string;

export type JsonPrimitive = string | number | boolean | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type JsonObject = { [key: string]: JsonValue };

export interface ApiProblem {
  code: string;
  message: string;
  status?: number;
  field?: string;
  retryable?: boolean;
  details?: JsonObject;
}

export type ApiResult<T> =
  | { ok: true; data: T }
  | { ok: false; error: ApiProblem };

export type WorkspaceKind = "personal" | "team" | "system";
export type WorkspaceRole = "owner" | "admin" | "editor" | "viewer";

export interface User {
  id: string;
  displayName: string;
  email: string;
  avatarUrl?: string;
  locale: string;
  timezone: string;
}

export interface Workspace {
  id: string;
  name: string;
  slug: string;
  kind: WorkspaceKind;
  role: WorkspaceRole;
}

export interface SessionContext {
  user: User;
  activeWorkspaceId: string;
  workspaces: Workspace[];
}

export type AccountCapabilityState =
  | "available"
  | "management_only"
  | "not_recorded"
  | "unavailable";

export interface AccountCapabilities {
  profile: AccountCapabilityState;
  creationPreferences: AccountCapabilityState;
  sessions: AccountCapabilityState;
  apiKeys: AccountCapabilityState;
  twoFactorAuthentication: AccountCapabilityState;
}

export interface VersionedResource<T> {
  value: T;
  etag: string;
}

export interface AccountProfile {
  userId: string;
  workspaceId: string;
  email: string;
  displayName: string;
  avatarUrl?: string;
  locale: string;
  timezone: string;
  revision: number;
  updatedAt: IsoTimestamp;
}

export interface AccountProfileUpdate {
  displayName: string;
  avatarUrl?: string;
  locale: string;
  timezone: string;
}

export type CreationAspectRatio = "16:9" | "9:16" | "1:1" | "4:3";

export interface CreationPreferences {
  userId: string;
  workspaceId: string;
  defaultLanguage: string;
  defaultAspectRatio: CreationAspectRatio;
  defaultDurationSeconds: number;
  defaultVisibility: "private" | "workspace";
  autoQualityCheck: boolean;
  revision: number;
  updatedAt: IsoTimestamp;
}

export interface CreationPreferencesUpdate {
  defaultLanguage: string;
  defaultAspectRatio: CreationAspectRatio;
  defaultDurationSeconds: number;
  defaultVisibility: "private" | "workspace";
  autoQualityCheck: boolean;
}

export interface AccountSession {
  id: string;
  workspaceId: string;
  userId: string;
  userAgent?: string;
  createdAt: IsoTimestamp;
  expiresAt: IsoTimestamp;
  lastSeenAt: IsoTimestamp;
  revokedAt?: IsoTimestamp;
}

export type ApiKeyScope =
  | "account:read"
  | "skills:read"
  | "skills:write"
  | "runs:read"
  | "runs:write";

export interface AccountApiKey {
  id: string;
  workspaceId: string;
  name: string;
  keyPrefix: string;
  scopes: ApiKeyScope[];
  createdAt: IsoTimestamp;
  expiresAt?: IsoTimestamp;
  lastUsedAt?: IsoTimestamp;
  revokedAt?: IsoTimestamp;
}

export interface ApiKeyCreateRequest {
  name: string;
  scopes: ApiKeyScope[];
  expiresAt?: IsoTimestamp;
}

export interface CreatedApiKey {
  key: AccountApiKey;
  plaintext: string;
}

export type PublisherType = "workspace" | "system";

export interface Publisher {
  id: string;
  workspaceId: string;
  type: PublisherType;
  displayName: string;
  verified: boolean;
}

export type SkillVisibility = "private" | "workspace" | "public_readonly";
export type SkillStatus =
  | "draft"
  | "validating"
  | "ready"
  | "published"
  | "deprecated";
export type SkillVersionState =
  | "draft"
  | "validating"
  | "ready"
  | "published"
  | "rejected"
  | "deprecated";

export interface SkillPermissions {
  view: boolean;
  fork: boolean;
  edit: boolean;
  test: boolean;
  publish: boolean;
  deleteDraft: boolean;
  deprecate: boolean;
}

export interface SkillStats {
  runCount: number;
  versionCount: number;
}

export interface Skill {
  id: string;
  workspaceId: string;
  name: string;
  slug: string;
  description: string;
  publisher: Publisher;
  visibility: SkillVisibility;
  status: SkillStatus;
  currentVersionId?: string;
  draftVersionId?: string;
  forkedFrom?: {
    skillId: string;
    versionId: string;
  };
  createdBy: string;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
  permissions: SkillPermissions;
  stats: SkillStats;
}

export interface ResearchPolicy {
  factBoundary: "strict" | "balanced" | "creative";
  requireCitations: boolean;
  preferredSources: string[];
  excludedSources: string[];
}

export interface VisualPolicy {
  direction: string;
  shotGuidance: string[];
  forbiddenTreatments: string[];
}

export interface AssetPolicy {
  strategy: "channel_default" | "workspace_libraries" | "explicit_only";
  requiredTags: string[];
  allowExternalAcquisition: boolean;
}

export interface QualityCriterion {
  id: string;
  label: string;
  description: string;
  minimumScore: number;
}

export interface QualityRubric {
  criteria: QualityCriterion[];
}

export interface OutputContract {
  format: string;
  fields: string[];
  constraints: JsonObject;
}

export interface ModelRequirements {
  capabilities: string[];
  preferredModel?: string;
}

export interface SkillSpec {
  inputSchema: JsonObject;
  researchPolicy: ResearchPolicy;
  writingInstructions: string;
  visualPolicy: VisualPolicy;
  assetPolicy: AssetPolicy;
  qcRubric: QualityRubric;
  outputContract: OutputContract;
  modelRequirements: ModelRequirements;
  defaultPipelineVersionId?: string;
}

export interface SkillVersion {
  id: string;
  skillId: string;
  version: string;
  schemaVersion: string;
  state: SkillVersionState;
  revision: number;
  spec: SkillSpec;
  testTopics: string[];
  contentHash?: string;
  releaseNotes?: string;
  createdBy: string;
  createdAt: IsoTimestamp;
  publishedAt?: IsoTimestamp;
}

export interface SkillDetail {
  skill: Skill;
  versions: SkillVersion[];
}

export type SkillScope = "mine" | "team" | "official" | "all";

export interface SkillQuery {
  scope?: SkillScope;
  workspaceId?: string;
  search?: string;
  status?: SkillStatus;
  visibility?: SkillVisibility;
}

export interface SkillIdentityInput {
  name: string;
  description: string;
  slug?: string;
  visibility?: SkillVisibility;
}

export interface ExampleReference {
  id: string;
  kind: "text" | "url" | "file";
  label: string;
  value: string;
}

export interface SkillPackage {
  schemaVersion: string;
  identity: SkillIdentityInput;
  spec: SkillSpec;
  testTopics?: string[];
  releaseNotes?: string;
}

export type CreateSkillRequest =
  | {
      kind: "blank";
      workspaceId: string;
      identity: SkillIdentityInput;
      initialSpec?: Partial<SkillSpec>;
    }
  | {
      kind: "fork";
      workspaceId: string;
      identity: SkillIdentityInput;
      sourceSkillId: string;
      sourceVersionId: string;
    }
  | {
      kind: "distill";
      workspaceId: string;
      identity: SkillIdentityInput;
      examples: ExampleReference[];
    }
  | {
      kind: "import";
      workspaceId: string;
      package: SkillPackage;
    };

export interface SkillDraftPatch {
  spec?: Partial<SkillSpec>;
  testTopics?: string[];
  releaseNotes?: string;
}

export type ValidationSeverity = "error" | "warning" | "info";

export interface ValidationCheck {
  id: string;
  label: string;
  passed: boolean;
  severity: ValidationSeverity;
  message: string;
  section?: keyof SkillSpec | "testTopics";
}

export interface ValidationReport {
  skillId: string;
  versionId: string;
  ready: boolean;
  /** Revision produced by the mutating validation command. */
  revision: number;
  checks: ValidationCheck[];
  estimatedCost: Money;
}

export interface Money {
  amount: number;
  currency: "CNY" | "USD";
}

export interface EvaluationCheck {
  id: string;
  label: string;
  passed: boolean;
  score: number;
  note: string;
}

export interface ComparisonRequest {
  skillId: string;
  leftVersionId: string;
  rightVersionId: string;
  topic: string;
  inputs?: JsonObject;
}

export interface ComparisonSide {
  versionId: string;
  version: string;
  output: string;
  cost: Money;
  durationMs: number;
  checks: EvaluationCheck[];
}

export interface ComparisonDiff {
  path: string;
  change: "added" | "removed" | "changed";
  left?: string;
  right?: string;
}

export interface ComparisonResult {
  id: string;
  topic: string;
  left: ComparisonSide;
  right: ComparisonSide;
  differences: ComparisonDiff[];
  createdAt: IsoTimestamp;
}

export interface RunComposition {
  skillVersionId: string;
  assetLibraryIds: string[];
  voiceProfileId?: string;
  renderPresetVersionId?: string;
  pipelineVersionId: string;
}

export type ChannelStatus = "active" | "paused" | "archived";

export interface ChannelBrandConfig {
  profile: JsonObject;
  logoAssetId?: string;
  introAssetId?: string;
  outroAssetId?: string;
  accentColor?: string;
  fontFamily?: string;
  guidelines?: string;
}

export interface Channel {
  id: string;
  workspaceId: string;
  slug: string;
  name: string;
  description: string;
  platform?: string;
  handle?: string;
  platformConnectionId?: string;
  status: ChannelStatus;
  defaultComposition: RunComposition;
  brandConfig: ChannelBrandConfig;
  revision: number;
  createdBy: string;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface ChannelDraft {
  id?: string;
  workspaceId: string;
  slug?: string;
  name: string;
  description: string;
  platform?: string;
  handle?: string;
  platformConnectionId?: string;
  status?: Channel["status"];
  defaultComposition: RunComposition;
  brandConfig: ChannelBrandConfig;
}

export interface ChannelQuery {
  workspaceId: string;
  search?: string;
  platform?: string;
  status?: ChannelStatus;
}

export interface AssetLibraryOption {
  id: string;
  workspaceId: string;
  name: string;
  description: string;
  assetCount: number;
  readyAssetCount?: number;
  quarantinedAssetCount?: number;
  processingAssetCount?: number;
  taggedAssetCount?: number;
  copyrightCompleteAssetCount?: number;
}

export type AssetKind = "image" | "video" | "audio" | "document" | "text" | "other";
export type AssetStatus = "processing" | "ready" | "quarantined" | "archived";
export type AssetCopyrightStatus = "unknown" | "owned" | "licensed" | "public_domain" | "restricted";
export type AssetReviewStatus = "pending" | "approved" | "rejected";

export interface AssetTag {
  id?: string;
  name: string;
  source: "manual" | "analysis" | string;
  confidence?: number;
}

export interface AssetFileInfo {
  id: string;
  originalFilename: string;
  mediaType: string;
  byteSize: number;
  contentHash: string;
  scanStatus: "pending" | "clean" | "rejected" | "failed" | string;
  width?: number;
  height?: number;
  durationMs?: number;
}

export interface AssetSource {
  id?: string;
  type: string;
  locator?: string;
  provider?: string;
  attribution?: string;
  license?: string;
  capturedAt?: IsoTimestamp;
}

export interface AssetAnalysis {
  id: string;
  version: number;
  provider: string;
  model: string;
  status: "pending" | "completed" | "failed" | "superseded" | string;
  summary: string;
  language?: string;
  people: string[];
  organizations: string[];
  locations: string[];
  eras: string[];
  sceneTypes: string[];
  actions: string[];
  moods: string[];
  visualStyles: string[];
  keywords: string[];
  hasEmbeddedText: boolean;
  hasWatermark: boolean;
  confidence?: number;
  createdAt: IsoTimestamp;
}

export interface AssetUsageRecord {
  id: string;
  runId?: string;
  runTopic?: string;
  segmentId?: string;
  usedAt: IsoTimestamp;
}

export interface AssetReviewEvent {
  id: string;
  decision: string;
  comment?: string;
  actorName?: string;
  createdAt: IsoTimestamp;
}

export interface AssetProcessingState {
  jobId: string;
  jobStatus: "queued" | "running" | "completed" | "failed" | string;
  pipelineStatus?: string;
  currentStage?: string;
  completedStages: string[];
  attempts?: number;
  maxAttempts?: number;
  error?: string;
  updatedAt?: IsoTimestamp;
}

export interface Asset {
  id: string;
  revision: number;
  workspaceId: string;
  libraryId: string;
  kind: AssetKind;
  title: string;
  description: string;
  status: AssetStatus;
  analysisStatus: "pending" | "running" | "completed" | "failed" | string;
  copyrightStatus: AssetCopyrightStatus;
  reviewStatus: AssetReviewStatus;
  tags: AssetTag[];
  thumbnailUrl?: string;
  posterAvailable: boolean;
  file?: AssetFileInfo;
  source?: AssetSource;
  analysis?: AssetAnalysis;
  processing?: AssetProcessingState;
  usageHistory: AssetUsageRecord[];
  reviewHistory: AssetReviewEvent[];
  deletedAt?: IsoTimestamp;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface AssetQuery {
  cursor?: string;
  limit?: number;
  search?: string;
  status?: AssetStatus | "deleted";
  kind?: AssetKind;
  copyrightStatus?: AssetCopyrightStatus;
  reviewStatus?: AssetReviewStatus;
  tag?: string;
}

export interface AssetPage {
  data: Asset[];
  totalCount: number;
  nextCursor?: string;
}

export interface AssetPatchRequest {
  title?: string;
  description?: string;
  copyrightStatus?: AssetCopyrightStatus;
  tags?: string[];
  rightsConfirmed?: boolean;
  rightsEvidence?: JsonObject;
}

export interface AssetReviewRequest {
  decision: "approve" | "reject";
  comment?: string;
}

export type AssetBulkRequest =
  | { items: { assetId: string; revision: number }[]; action: "review"; decision: "approve" | "reject"; comment?: string }
  | { items: { assetId: string; revision: number }[]; action: "add_tags"; tags: string[] }
  | { items: { assetId: string; revision: number }[]; action: "reanalyze" }
  | { items: { assetId: string; revision: number }[]; action: "disable" };

export interface AssetBulkResult {
  acceptedCount: number;
  jobId?: string;
}

export interface AssetPreview {
  url: string;
  mediaType: string;
  expiresAt?: IsoTimestamp;
  sourceVariant: "preview" | "original";
  isFallback: boolean;
  byteSize?: number;
}

export interface AssetPoster {
  url: string;
  mediaType: string;
  expiresAt?: IsoTimestamp;
}

export interface AssetSegment {
  id: string;
  ordinal: number;
  startMs: number;
  endMs: number;
  description: string;
  people: string[];
  locations: string[];
  keywords: string[];
  sceneType?: string;
  action?: string;
  mood?: string;
  shotType?: string;
  confidence?: number;
  representativeFrameUrl?: string;
  transcript?: string;
  boundaryScore?: number;
  boundaryReasons: string[];
  cutSafe: boolean;
  semanticComplete: boolean;
}

export interface AssetIngestionJob {
  id: string;
  libraryId: string;
  sourceRoot: string;
  status: "pending" | "running" | "completed" | "completed_with_errors" | "failed";
  discoveredCount: number;
  importedCount: number;
  deduplicatedCount: number;
  taggedCount: number;
  rejectedCount: number;
  error?: string;
  startedAt?: IsoTimestamp;
  completedAt?: IsoTimestamp;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface AssetIngestionJobQuery {
  libraryId?: string;
  status?: AssetIngestionJob["status"];
  cursor?: string;
  limit?: number;
}

export interface AssetIngestionJobPage {
  data: AssetIngestionJob[];
  nextCursor?: string;
}

export interface AssetLibraryCreateRequest {
  name: string;
  slug: string;
  description: string;
}

export interface AssetUploadRequest {
  libraryId: string;
  file: File;
  title: string;
  description: string;
  copyrightStatus: "owned" | "licensed" | "public_domain";
  tags: string[];
  relativePath?: string;
}

export interface DocumentVideoCreateRequest {
  file: File;
  topic: string;
  durationSeconds: number;
  aspectRatio: "16:9" | "9:16" | "1:1";
  generatedBackgroundEnabled: boolean;
}

export interface DocumentVideoCreateResult {
  sourceId: string;
  runId: string;
}

export interface RemoteAssetImportRequest {
  libraryId: string;
  sourceUrl: string;
  title?: string;
  description: string;
  copyrightStatus: "owned" | "licensed" | "public_domain";
  tags: string[];
  rightsConfirmed: true;
}

export type LibraryBuildSource = "youtube" | "bilibili" | "wikimedia";
export type LibraryBuildStatus = "queued" | "running" | "completed" | "completed_with_errors" | "failed" | "cancelled";
export type LibraryBuildStage = "discover" | "transfer" | "analyze" | "index";

export interface LibraryBuildJobCreateRequest {
  libraryId: string;
  topic: string;
  queries?: string[];
  sources: LibraryBuildSource[];
  maxAssets: number;
  copyrightStatus: "licensed" | "public_domain";
  rightsConfirmed: true;
}

export interface LibraryBuildJob {
  schemaVersion: string;
  id: string;
  workspaceId: string;
  libraryId: string;
  status: LibraryBuildStatus;
  stage: LibraryBuildStage;
  spec: {
    topic: string;
    queries: string[];
    sources: LibraryBuildSource[];
    maxAssets: number;
    copyrightStatus: "licensed" | "public_domain";
    rightsConfirmed: true;
  };
  progress: {
    assetIds: string[];
    discovered: number;
    transferred: number;
    analyzed: number;
    indexed: number;
    failed: number;
  };
  error?: JsonObject;
  revision: number;
  createdBy: string;
  createdAt: IsoTimestamp;
  startedAt?: IsoTimestamp;
  completedAt?: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface VoiceProfileOption {
  id: string;
  workspaceId: string;
  name: string;
  provider: string;
}

export interface RenderPresetOption {
  id: string;
  workspaceId: string;
  presetId: string;
  name: string;
  version: string;
  aspectRatio: string;
  capabilities: string[];
}

export interface PipelineOption {
  id: string;
  workspaceId: string;
  pipelineId: string;
  name: string;
  version: string;
  capabilities: string[];
}

export interface SkillVersionOption {
  skillId: string;
  skillName: string;
  versionId: string;
  version: string;
  defaultPipelineVersionId?: string;
  publisher: Publisher;
}

export interface ComposerOptions {
  channels: Channel[];
  channelProblem?: ApiProblem;
  skills: SkillVersionOption[];
  assetLibraries: AssetLibraryOption[];
  voiceProfiles: VoiceProfileOption[];
  renderPresets: RenderPresetOption[];
  pipelines: PipelineOption[];
}

/**
 * Contract for the isolated generated-only studio.  The browser only uses its
 * dedicated resource/API and cannot supply Skill, Pipeline, Channel, or Asset
 * Library identifiers; the server owns the scheduler Run binding.
 */
export interface FullAiBlocker {
  code: string;
  message: string;
  retryable: boolean;
}

export interface FullAiProviderOption {
  name?: string;
  modelId?: string;
  status: "ready" | "unconfigured" | "incomplete";
  supportsReconciliation: boolean;
  submitUnknownPolicy: "manual_only";
  continuityModes: Array<"prompt_pack" | "none" | string>;
}

export interface FullAiLimits {
  briefMaxLength: number;
  durationSeconds: number[];
  aspectRatios: string[];
  directions: string[];
  variantsPerScene: number[];
  clipSeconds: number;
}

export interface FullAiOptions {
  schemaVersion: "1.0.0" | string;
  mode: "generated_only";
  status: "ready" | "blocked";
  pipeline: {
    slug: string;
    version: number;
    visualSourceMode: "generated_only";
  };
  provider: FullAiProviderOption;
  limits: FullAiLimits;
  blockers: FullAiBlocker[];
}

export interface FullAiSpec {
  brief: string;
  direction: string;
  aspectRatio: string;
  durationSeconds: number;
  variantsPerScene: number;
  continuity: boolean;
  aiDisclosure: boolean;
}

export interface FullAiQuote {
  currency: "CNY" | "USD";
  amountMinor: number;
  expiresAt: IsoTimestamp;
}

export interface FullAiEstimate {
  schemaVersion: "1.0.0" | string;
  status: "ready" | "blocked";
  requestFingerprint: string;
  plan: {
    sceneCount: number;
    clipSeconds: number;
    candidateCount: number;
    billableSeconds: number;
  };
  quote?: FullAiQuote;
  blockers: FullAiBlocker[];
}

export interface FullAiRunCreateRequest extends FullAiSpec {
  estimateFingerprint: string;
  maxCostMinor: number;
  currency: "CNY" | "USD";
}

export interface FullAiRun {
  schemaVersion: "1.0.0" | string;
  id: string;
  projectRunId: string;
  workspaceId: string;
  status: string;
  mode: "generated_only";
  provider: {
    name?: string;
    modelId?: string;
  };
  spec: FullAiSpec;
  quote: FullAiQuote;
  billing: {
    status: string;
    authorizedAmountMinor: number;
    incurredAmountMinor: number;
    requiresReconciliation: boolean;
  };
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

/**
 * Public contract for the isolated webpage-capture video workflow. The Web
 * client intentionally exposes no cookies, credentials, custom headers,
 * browser scripting, private-network targets, or arbitrary pipeline IDs.
 */
export type WebpageVideoAspectRatio = "16:9" | "9:16" | "1:1" | "4:3";

export interface WebpageVideoBlocker {
  code: string;
  message: string;
  retryable: boolean;
}

export interface WebpageVideoVoiceOption {
  id: string;
  name: string;
  language?: string;
  description?: string;
}

export interface WebpageVideoOptions {
  schemaVersion: string;
  status: "ready" | "blocked";
  pipeline?: { slug: string; version: number };
  limits: {
    urlMaxLength: number;
    topicMaxLength: number;
    aspectRatios: WebpageVideoAspectRatio[];
    durationSeconds: number[];
    crawlMaxPagesDefault: number;
    crawlMaxPagesLimit: number;
    crawlMaxDepthDefault: number;
    crawlMaxDepthLimit: number;
  };
  voices: WebpageVideoVoiceOption[];
  subtitles: {
    supported: boolean;
    defaultEnabled: boolean;
  };
  blockers: WebpageVideoBlocker[];
}

export interface WebpageVideoRunCreateRequest {
  targetUrl: string;
  topic: string;
  aspectRatio: WebpageVideoAspectRatio;
  durationSeconds: number;
  subtitlesEnabled: boolean;
  voiceProfileId?: string;
  publicPageConfirmed: true;
  rightsConfirmed: true;
  crawl: {
    maxPages: number;
    maxDepth: number;
    sameOriginOnly: true;
    includeSitemap: boolean;
  };
}

export type WebpageVideoReviewDecision = "approve" | "recapture" | "reject";
export type WebpageVideoSiteReviewDecision = "approve" | "request_changes" | "reject";

export interface WebpageVideoRegion {
  id: string;
  type: string;
  label: string;
  reason?: string;
  score?: number;
  previewUrl?: string;
  sha256?: string;
  width?: number;
  height?: number;
}

export interface WebpageVideoSitePage {
  id: string;
  url: string;
  finalUrl?: string;
  title?: string;
  pageType?: string;
  reason?: string;
  score?: number;
  selected: boolean;
  status?: string;
  capture?: WebpageVideoCapture;
  regions: WebpageVideoRegion[];
  failure?: { code?: string; message: string; retryable: boolean };
}

export interface WebpageVideoStoryboardShot {
  id: string;
  pageId: string;
  regionId?: string;
  label: string;
  reason?: string;
  previewUrl?: string;
  durationSeconds?: number;
  motion: "static" | "zoom_in" | "zoom_out" | "pan";
  transition: "cut" | "fade_black";
  enabled: boolean;
  order: number;
}

export interface WebpageVideoSitePlan {
  schemaVersion: string;
  mode: "site";
  scope: {
    status: string;
    revision: number;
    sha256: string;
    pages: WebpageVideoSitePage[];
  };
  storyboard?: {
    status: string;
    revision: number;
    sha256: string;
    shots: WebpageVideoStoryboardShot[];
  };
}

export interface WebpageVideoScopeReviewRequest {
  decision: WebpageVideoSiteReviewDecision;
  comment?: string;
  expectedRevision: number;
  expectedSha256: string;
  selectedPageIds: string[];
}

export interface WebpageVideoStoryboardReviewRequest {
  decision: WebpageVideoSiteReviewDecision;
  comment?: string;
  expectedRevision: number;
  expectedSha256: string;
  shots: Array<{
    id: string;
    enabled: boolean;
    order: number;
    motion?: WebpageVideoStoryboardShot["motion"];
    transition?: WebpageVideoStoryboardShot["transition"];
  }>;
}

export interface WebpageVideoCapture {
  previewUrl?: string;
  sha256: string;
  requestedUrl: string;
  finalUrl?: string;
  revision: number;
  width?: number;
  height?: number;
  capturedAt?: IsoTimestamp;
  expiresAt?: IsoTimestamp;
  review?: {
    decision: "approve" | "request_changes" | "reject";
    comment?: string;
    issueCodes: string[];
    reviewedRevision: number;
    decidedAt?: IsoTimestamp;
  };
}

export interface WebpageVideoMedia {
  previewUrl?: string;
  downloadUrl?: string;
  captionsUrl?: string;
  mediaType?: string;
  filename?: string;
  sha256?: string;
  byteSize?: number;
}

export interface WebpageVideoPilotFeedback {
  id: string;
  webpageVideoRunId: string;
  customerSegment: string;
  baselineMinutes: number;
  assistedMinutes: number;
  savedMinutes: number;
  timeReductionPercent: number;
  revisionCount: number;
  outcome: "evaluating" | "adopted" | "rejected";
  satisfactionScore?: number;
  willingnessToPayHkd?: number;
  notes?: string;
  revision: number;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface WebpageVideoPilotFeedbackSaveRequest {
  customerSegment: string;
  baselineMinutes: number;
  assistedMinutes: number;
  revisionCount: number;
  outcome: WebpageVideoPilotFeedback["outcome"];
  satisfactionScore?: number;
  willingnessToPayHkd?: number;
  notes?: string;
  expectedRevision: number;
}

export interface WebpageVideoPilotSummaryItem {
  webpageVideoRunId: string;
  customerSegment: string;
  baselineMinutes: number;
  assistedMinutes: number;
  savedMinutes: number;
  timeReductionPercent: number;
  revisionCount: number;
  outcome: WebpageVideoPilotFeedback["outcome"];
  satisfactionScore?: number;
  willingnessToPayHkd?: number;
  updatedAt: IsoTimestamp;
}

export interface WebpageVideoPilotSegmentSummary {
  customerSegment: string;
  pilotCount: number;
  adoptedCount: number;
  savedMinutes: number;
  averageTimeReductionPercent: number;
}

export interface WebpageVideoPilotSummary {
  schemaVersion: string;
  generatedAt: IsoTimestamp;
  totalRecords: number;
  includedRecords: number;
  truncated: boolean;
  recommendedMinimumPilots: number;
  pilotTargetMet: boolean;
  adoptedCount: number;
  evaluatingCount: number;
  rejectedCount: number;
  baselineMinutesTotal: number;
  assistedMinutesTotal: number;
  savedMinutesTotal: number;
  timeReductionPercent?: number;
  averageSatisfactionScore?: number;
  satisfactionResponseCount: number;
  averageWillingnessToPayHkd?: number;
  willingnessToPayResponseCount: number;
  segments: WebpageVideoPilotSegmentSummary[];
  items: WebpageVideoPilotSummaryItem[];
}

export interface WebpageVideoRun {
  schemaVersion: string;
  id: string;
  projectRunId?: string;
  workspaceId?: string;
  status: string;
  rawStatus: string;
  revision: number;
  targetUrl: string;
  finalUrl?: string;
  captureSha256?: string;
  spec: {
    topic: string;
    aspectRatio: WebpageVideoAspectRatio | string;
    durationSeconds: number;
    subtitlesEnabled: boolean;
    voiceProfileId?: string;
  };
  capture?: WebpageVideoCapture;
  siteMode: boolean;
  finalVideo?: WebpageVideoMedia;
  pilotFeedback?: WebpageVideoPilotFeedback;
  failure?: { code?: string; message: string; retryable: boolean };
  createdAt?: IsoTimestamp;
  updatedAt?: IsoTimestamp;
}

export interface WebpageVideoReviewRequest {
  decision: "approve" | "recapture" | "reject";
  comment?: string;
  expectedRevision: number;
  expectedSha256: string;
}

export interface CapabilityGap {
  capability: string;
  resource: "skill" | "pipeline" | "voice" | "render" | "assets";
  message: string;
}

export interface RunDraft {
  workspaceId: string;
  topic: string;
  researchMode?: "off" | "when_missing" | "required";
  inventoryConcepts?: string[];
  sourceUrls?: string[];
  channelId?: string;
  composition: RunComposition;
  videoSettings?: VideoSettings;
}

export type VideoAspectRatio = "16:9" | "9:16" | "1:1" | "4:3";
export type VideoLayout = "full_frame" | "editorial";
export type VideoMediaFit = "cover" | "contain";

export interface VideoSubtitleSettings {
  enabled: boolean;
  position: "bottom" | "lower_third";
  size: "small" | "medium" | "large";
  maxLines: 1 | 2 | 3;
}

export interface VideoSettings {
  language: string;
  aspectRatio: VideoAspectRatio;
  targetDurationSeconds: number;
  visibility: "private" | "workspace";
  autoQualityCheck: boolean;
  layout: VideoLayout;
  mediaFit: VideoMediaFit;
  frameRate: 24 | 25 | 30 | 50 | 60;
  subtitles: VideoSubtitleSettings;
  assetAcquisition: {
    enabled: boolean;
    sources: ("youtube" | "bilibili" | "wikimedia")[];
    maxAssets: number;
    copyrightStatus: "licensed" | "public_domain";
    rightsConfirmed: boolean;
  };
  noAssetDraft: {
    enabled: boolean;
  };
}

export interface RunEstimate {
  cost: Money;
  durationSeconds: number;
  capabilityGaps: CapabilityGap[];
  capabilitiesKnown?: boolean;
}

export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_review"
  | "retrying"
  | "succeeded"
  | "failed"
  | "cancelled";

export interface RunStep {
  id: string;
  key: string;
  type: string;
  label: string;
  status: RunStatus;
  attemptCount: number;
  maxAttempts: number;
  queueName?: string;
  dependencies: string[];
  requiredCapabilities?: string[];
  inputSnapshot?: JsonObject;
  outputSummary?: JsonObject;
  artifacts?: RunArtifact[];
  revision?: number;
  reviewRequired: boolean;
  review?: RunStepReview;
  error?: ApiProblem;
  startedAt?: IsoTimestamp;
  completedAt?: IsoTimestamp;
  nextAttemptAt?: IsoTimestamp;
  cancellationRequestedAt?: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export type RunReviewDecision = "approve" | "reject" | "revise";

export interface RunStepReview {
  decision?: RunReviewDecision;
  actorId?: string;
  comment?: string;
  issueCodes?: string[];
  reviewedRevision?: number;
  requestedAt?: IsoTimestamp;
  decidedAt?: IsoTimestamp;
}

export interface RunStepReviewRequest {
  decision: RunReviewDecision;
  comment?: string;
  issueCodes?: string[];
  expectedRevision?: number;
}

export interface RunArtifact {
  id: string;
  kind: string;
  filename: string;
  mediaType: string;
  byteSize: number;
  contentHash: string;
  contentUrl: string;
  stepId?: string;
  createdAt: IsoTimestamp;
}

export interface Run {
  id: string;
  workspaceId: string;
  topic: string;
  projectKind?: "standard" | "full_ai" | "webpage_video" | "document_video";
  controlRunId?: string;
  channelId?: string;
  status: RunStatus;
  composition: RunComposition;
  videoSettings?: VideoSettings;
  estimate: RunEstimate;
  /** False when the collection response did not include persisted cost data. */
  costAvailable?: boolean;
  steps: RunStep[];
  /** False when the collection response did not embed durable Worker steps. */
  stepsAvailable?: boolean;
  artifacts: RunArtifact[];
  cancellationRequestedAt?: IsoTimestamp;
  createdBy: string;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface RunQuery {
  workspaceId?: string;
  channelId?: string;
  search?: string;
  status?: RunStatus;
}

export type GenerationBatchStatus =
  | "queued"
  | "running"
  | "awaiting_review"
  | "succeeded"
  | "completed_with_errors";

export interface GenerationBatchStatusCounts {
  queued: number;
  running: number;
  awaitingReview: number;
  succeeded: number;
  failed: number;
  cancelled: number;
}

export interface GenerationBatch {
  id: string;
  workspaceId: string;
  name: string;
  status: GenerationBatchStatus;
  totalCount: number;
  statusCounts: GenerationBatchStatusCounts;
  composition: RunComposition;
  videoSettings?: VideoSettings;
  createdBy: string;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface GenerationBatchItem {
  id: string;
  workspaceId: string;
  batchId: string;
  runId: string;
  ordinal: number;
  label: string;
  input: JsonObject;
  status: RunStatus;
  cancellationRequestedAt?: IsoTimestamp;
  createdAt: IsoTimestamp;
  updatedAt: IsoTimestamp;
}

export interface GenerationBatchCreateRequest {
  workspaceId: string;
  name: string;
  channelId?: string;
  items: Array<{ topic: string; inputs?: JsonObject }>;
  composition: RunComposition;
  videoSettings?: VideoSettings;
  researchMode: "off" | "when_missing" | "required";
}

export interface GenerationBatchItemQuery {
  status?: RunStatus;
  search?: string;
  cursor?: string;
  limit?: number;
}

export interface GenerationBatchItemPage {
  data: GenerationBatchItem[];
  totalCount: number;
  nextCursor?: string;
}
