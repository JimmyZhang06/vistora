import type { FrameFactoryAdapter } from "./adapter";
import type {
  AccountApiKey,
  AccountCapabilities,
  AccountProfile,
  AccountProfileUpdate,
  AccountSession,
  ApiProblem,
  ApiResult,
  ApiKeyCreateRequest,
  AssetLibraryCreateRequest,
  AssetLibraryOption,
  Asset,
  AssetAnalysis,
  AssetBulkRequest,
  AssetBulkResult,
  AssetIngestionJob,
  AssetIngestionJobPage,
  AssetIngestionJobQuery,
  AssetPage,
  AssetPatchRequest,
  AssetPoster,
  AssetPreview,
  AssetQuery,
  AssetReviewRequest,
  AssetSegment,
  AssetTag,
  AssetUploadRequest,
  BenchmarkAccountSnapshot,
  BenchmarkAccountPreviewRequest,
  BenchmarkAccountReport,
  BenchmarkConnection,
  BenchmarkConnectionState,
  BenchmarkHistoryPage,
  BenchmarkHistoryDetail,
  BenchmarkHistoryQuery,
  BenchmarkHistorySummary,
  BenchmarkAccountStrategyReport,
  BenchmarkAnalysisJob,
  BenchmarkVideoAnalysis,
  BenchmarkDeepFinding,
  BenchmarkDeepMetric,
  BenchmarkDeepNoteReport,
  BenchmarkDeepTimelineItem,
  BenchmarkInsight,
  BenchmarkNotePerformance,
  BenchmarkNoteReport,
  BenchmarkNote,
  BenchmarkNoteSourceEvidence,
  BenchmarkNoteSourceRequest,
  BenchmarkStrategySignal,
  BenchmarkTitlePattern,
  DocumentVideoCreateRequest,
  DocumentVideoCreateResult,
  RemoteAssetImportRequest,
  Channel,
  ChannelDraft,
  ChannelQuery,
  ComparisonRequest,
  ComparisonResult,
  ComposerOptions,
  CreateSkillRequest,
  CreatedApiKey,
  CreationPreferences,
  CreationPreferencesUpdate,
  GenerationBatch,
  GenerationBatchCreateRequest,
  GenerationBatchItem,
  GenerationBatchItemPage,
  GenerationBatchItemQuery,
  LibraryBuildJob,
  LibraryBuildJobCreateRequest,
  FullAiBlocker,
  FullAiEstimate,
  FullAiOptions,
  FullAiRun,
  FullAiRunCreateRequest,
  FullAiSpec,
  JsonObject,
  WebpageVideoAspectRatio,
  WebpageVideoBlocker,
  WebpageVideoCapture,
  WebpageVideoMedia,
  WebpageVideoOptions,
  WebpageVideoPilotFeedback,
  WebpageVideoPilotFeedbackSaveRequest,
  WebpageVideoPilotSummary,
  WebpageVideoReviewRequest,
  WebpageVideoScopeReviewRequest,
  WebpageVideoSitePage,
  WebpageVideoSitePlan,
  WebpageVideoStoryboardReviewRequest,
  WebpageVideoStoryboardShot,
  WebpageVideoRun,
  WebpageVideoRunCreateRequest,
  Run,
  RunArtifact,
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
  SkillSpec,
  SkillVersion,
  ValidationReport,
  VideoSettings,
  VersionedResource,
} from "./contracts";
import { normalizeChannelPlatform } from "../channel-platforms.ts";
import { benchmarkProfile } from "../benchmark-recovery.ts";

type JsonRecord = Record<string, unknown>;

export interface HttpAdapterOptions {
  baseUrl?: string;
  benchmarkBaseUrl?: string;
  fetch?: typeof globalThis.fetch;
  testPollIntervalMs?: number;
  testPollAttempts?: number;
}

interface Page<T> { data: T[]; page?: { has_more?: boolean; next_cursor?: string | null } }

const DEFAULT_API_URL = "http://127.0.0.1:8200";

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" ? value as JsonRecord : {};
}

function text(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function number(value: unknown, fallback = 0): number {
  return typeof value === "number" ? value : fallback;
}

function optionalNumber(value: unknown): number | undefined {
  return typeof value === "number" ? value : undefined;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}

function numbers(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === "number") : [];
}

function records(value: unknown): JsonRecord[] {
  return Array.isArray(value) ? value.map(record) : [];
}

function nonEmptyRecord(value: unknown): JsonRecord | undefined {
  const mapped = record(value);
  return Object.keys(mapped).length ? mapped : undefined;
}

function normalizeWebpageVideoStatus(value: unknown): string {
  const status = text(value, "queued").toLowerCase();
  const aliases: Record<string, string> = {
    pending: "queued",
    validating: "validating_url",
    url_validating: "validating_url",
    screenshotting: "capturing",
    awaiting_review: "awaiting_capture_review",
    promoting: "promoting_asset",
    materializing: "promoting_asset",
    analyzing: "analyzing_asset",
    writing: "composing",
    synthesizing: "composing",
    timeline: "composing",
    qc: "quality_check",
    awaiting_quality_review: "quality_review_required",
    awaiting_qc_review: "quality_review_required",
    completed: "succeeded",
    complete: "succeeded",
    canceled: "cancelled",
  };
  return aliases[status] ?? status;
}

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
}

function benchmarkRequestSignal(signal?: AbortSignal, timeoutMs = 20_000): AbortSignal {
  const timeout = AbortSignal.timeout(timeoutMs);
  return signal ? AbortSignal.any([signal, timeout]) : timeout;
}

function slugify(value: string): string {
  const slug = value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  return slug || `skill-${Date.now().toString(36)}`;
}

async function sha256Hex(file: File): Promise<string> {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return Array.from(new Uint8Array(digest), (value) => value.toString(16).padStart(2, "0")).join("");
}

function defaultCanonicalSpec(spec: Partial<SkillSpec> = {}): JsonRecord {
  const defaults: JsonRecord = {
    input_schema: {
      type: "object", title: "Skill input", description: "",
      properties: {}, required: [], additionalProperties: false,
    },
    research_policy: {
      mode: "required", allowed_source_types: ["primary", "official"], require_https: true,
      fact_treatment: "cite",
      citation_style: "artifact_only", minimum_sources: 1, maximum_sources: 10,
      freshness_days: 365, blocked_domains: [],
    },
    writing_policy: {
      language: "zh-CN", target_duration_seconds: 90, tone_tags: ["clear"],
      structure: [{ name: "main", purpose: "完整表达核心观点", target_share: 1 }],
      markdown_instructions: "围绕主题建立清晰论点。", prohibited_content: [],
    },
    visual_policy: {
      aspect_ratio: "9:16", frame_rate: 30,
      visual_tags: ["editorial"],
      shot_duration_seconds: { minimum: 1, target: 3, maximum: 8 }, text_safe_area_percent: 10,
      generated_media_allowed: false,
    },
    asset_policy: {
      library_binding: "run_composition", allowed_kinds: ["image", "video"], minimum_assets: 0,
      license_required: true, fallback: "fail", deduplicate_by_hash: true,
    },
    qc_policy: {
      rules: [{ id: "clarity", category: "writing", description: "输出必须清晰完整", severity: "error", action: "fail" }],
      human_review: "on_warning", minimum_pass_rate: 1,
    },
    capability_requirements: [],
    output_contract: {
      artifacts: [{ name: "script", kind: "script", media_types: ["text/markdown"], required: true, maximum_bytes: 10485760 }],
      allow_undeclared_artifacts: false,
    },
    default_pipeline_version_id: null,
  };
  return { ...defaults, ...skillSpecToCanonicalPatch(spec, defaults) };
}

/**
 * Convert the closed canonical SkillVersion contract into the smaller editor view.
 * Canonical fields that have no honest editor control stay in rawVersions and are
 * merged back by skillSpecToCanonicalPatch instead of being replaced with defaults.
 */
export function canonicalVersionToSkillSpec(raw: JsonRecord): SkillSpec {
  const research = record(raw.research_policy);
  const writing = record(raw.writing_policy);
  const visual = record(raw.visual_policy);
  const asset = record(raw.asset_policy);
  const qc = record(raw.qc_policy);
  const output = record(raw.output_contract);
  const artifacts = Array.isArray(output.artifacts) ? output.artifacts.map(record) : [];
  const rules = Array.isArray(qc.rules) ? qc.rules.map(record) : [];
  const requirements = Array.isArray(raw.capability_requirements) ? raw.capability_requirements.map(record) : [];
  const visualTags = strings(visual.visual_tags);
  const libraryBinding = text(asset.library_binding, "run_composition");
  return {
    inputSchema: record(raw.input_schema) as JsonObject,
    researchPolicy: {
      factBoundary: text(research.fact_treatment) === "exclude_unverified" ? "strict" : "balanced",
      requireCitations: text(research.fact_treatment) === "cite",
      preferredSources: strings(research.allowed_source_types), excludedSources: strings(research.blocked_domains),
    },
    writingInstructions: text(writing.markdown_instructions),
    visualPolicy: { direction: visualTags[0] ?? "", shotGuidance: visualTags.slice(1), forbiddenTreatments: [] },
    assetPolicy: {
      strategy: libraryBinding === "channel_default" ? "channel_default" : libraryBinding === "none" ? "explicit_only" : "workspace_libraries",
      requiredTags: strings(asset.allowed_kinds), allowExternalAcquisition: text(asset.fallback) !== "fail",
    },
    qcRubric: { criteria: rules.map((rule, index) => ({ id: text(rule.id, `rule-${index + 1}`), label: text(rule.id, "质量规则"), description: text(rule.description), minimumScore: number(qc.minimum_pass_rate, 1) })) },
    outputContract: { format: text(artifacts[0]?.kind, "script"), fields: artifacts.map((item) => text(item.name)).filter(Boolean), constraints: output as JsonObject },
    modelRequirements: { capabilities: requirements.map((item) => text(item.name)).filter(Boolean) },
    defaultPipelineVersionId: typeof raw.default_pipeline_version_id === "string" ? raw.default_pipeline_version_id : undefined,
  };
}

/** Merge editor-supported values into a canonical patch without erasing fields the UI cannot express. */
export function skillSpecToCanonicalPatch(spec: Partial<SkillSpec>, raw: JsonRecord): JsonRecord {
  const output: JsonRecord = {};

  if (spec.inputSchema) {
    const input = record(spec.inputSchema);
    const properties = record(input.properties);
    output.input_schema = {
      ...record(raw.input_schema),
      ...input,
      type: "object",
      title: text(input.title, text(record(raw.input_schema).title, "Skill input")),
      properties: Object.fromEntries(Object.entries(properties).map(([key, value]) => {
        const field = record(value);
        return [key, { ...field, type: text(field.type, "string"), title: text(field.title, key) }];
      })),
      required: strings(input.required),
      additionalProperties: false,
    };
  }

  if (spec.researchPolicy) {
    const source = record(raw.research_policy);
    output.research_policy = {
      ...source,
      allowed_source_types: spec.researchPolicy.preferredSources,
      blocked_domains: spec.researchPolicy.excludedSources,
      fact_treatment: spec.researchPolicy.factBoundary === "strict"
        ? "exclude_unverified"
        : spec.researchPolicy.requireCitations ? "cite" : "attribute",
    };
  }
  if (spec.writingInstructions !== undefined) {
    output.writing_policy = { ...record(raw.writing_policy), markdown_instructions: spec.writingInstructions };
  }
  if (spec.visualPolicy) {
    output.visual_policy = {
      ...record(raw.visual_policy),
      visual_tags: Array.from(new Set([spec.visualPolicy.direction, ...spec.visualPolicy.shotGuidance].map((item) => item.trim()).filter(Boolean))),
    };
  }
  if (spec.assetPolicy) {
    const source = record(raw.asset_policy);
    const previous = canonicalVersionToSkillSpec({ asset_policy: source }).assetPolicy;
    const requestedKinds = spec.assetPolicy.requiredTags.map((item) => item.trim()).filter(Boolean);
    const fallback = spec.assetPolicy.allowExternalAcquisition === previous.allowExternalAcquisition
      ? text(source.fallback, "fail")
      : spec.assetPolicy.allowExternalAcquisition ? "licensed_stock" : "fail";
    output.asset_policy = {
      ...source,
      library_binding: spec.assetPolicy.strategy === "channel_default" ? "channel_default" : spec.assetPolicy.strategy === "explicit_only" ? "none" : "run_composition",
      allowed_kinds: requestedKinds.length ? requestedKinds : previous.requiredTags.length ? previous.requiredTags : ["image", "video"],
      fallback,
    };
  }
  if (spec.qcRubric) {
    const source = record(raw.qc_policy);
    const sourceRules = Array.isArray(source.rules) ? source.rules.map(record) : [];
    output.qc_policy = {
      ...source,
      rules: spec.qcRubric.criteria.map((criterion, index) => {
        const existing = sourceRules.find((rule) => text(rule.id) === criterion.id) ?? sourceRules[index] ?? {};
        return {
          ...existing,
          id: slugify(criterion.id),
          category: text(existing.category, "writing"),
          description: criterion.description,
          severity: text(existing.severity, "error"),
          action: text(existing.action, "fail"),
        };
      }),
    };
  }
  if (spec.outputContract) {
    const contract = spec.outputContract;
    const rawContract = record(raw.output_contract);
    const source = Object.keys(contract.constraints).length ? record(contract.constraints) : rawContract;
    const sourceArtifacts = Array.isArray(source.artifacts) ? source.artifacts.map(record) : [];
    output.output_contract = {
      ...rawContract,
      ...source,
      artifacts: contract.fields.map((name, index) => {
        const existing = sourceArtifacts.find((artifact) => text(artifact.name) === name) ?? sourceArtifacts[index] ?? {};
        return {
          ...existing,
          name: slugify(name),
          kind: index === 0 ? contract.format : text(existing.kind, "script"),
          media_types: Array.isArray(existing.media_types) ? existing.media_types : ["text/markdown"],
          required: typeof existing.required === "boolean" ? existing.required : true,
          maximum_bytes: number(existing.maximum_bytes, 10485760),
        };
      }),
    };
  }
  if (spec.modelRequirements) {
    const source = Array.isArray(raw.capability_requirements) ? raw.capability_requirements.map(record) : [];
    output.capability_requirements = spec.modelRequirements.capabilities.map((name, index) => {
      const existing = source.find((requirement) => text(requirement.name) === name) ?? source[index] ?? {};
      return { ...existing, name, level: text(existing.level, "required"), minimum_version: text(existing.minimum_version, "1.0.0") };
    });
  }
  if (spec.defaultPipelineVersionId !== undefined) output.default_pipeline_version_id = spec.defaultPipelineVersionId || null;
  return output;
}

export class HttpFrameFactoryAdapter implements FrameFactoryAdapter {
  private readonly baseUrl: string;
  private readonly benchmarkBaseUrl: string;
  private readonly fetcher: typeof globalThis.fetch;
  private readonly pollIntervalMs: number;
  private readonly pollAttempts: number;
  private readonly rawVersions = new Map<string, JsonRecord>();

  constructor(options: HttpAdapterOptions = {}) {
    this.baseUrl = (options.baseUrl ?? process.env.NEXT_PUBLIC_FRAMEFACTORY_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
    this.benchmarkBaseUrl = (options.benchmarkBaseUrl ?? process.env.NEXT_PUBLIC_FRAMEFACTORY_BENCHMARK_API_URL ?? this.baseUrl).replace(/\/$/, "");
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.pollIntervalMs = options.testPollIntervalMs ?? 500;
    this.pollAttempts = options.testPollAttempts ?? 120;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<ApiResult<T>> {
    try {
      const baseUrl = path.startsWith("/v1/benchmark-") ? this.benchmarkBaseUrl : this.baseUrl;
      const response = await this.fetcher(`${baseUrl}${path}`, {
        ...init,
        headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
      });
      if (!response.ok) return { ok: false, error: await this.problem(response) };
      if (response.status === 204) return { ok: true, data: undefined as T };
      return { ok: true, data: await response.json() as T };
    } catch (cause) {
      return { ok: false, error: { code: "NETWORK_ERROR", message: cause instanceof Error ? cause.message : "无法连接 Vistora API", retryable: true } };
    }
  }

  private async requestVersioned<T>(path: string, init: RequestInit = {}): Promise<ApiResult<VersionedResource<T>>> {
    try {
      const response = await this.fetcher(`${this.baseUrl}${path}`, {
        ...init,
        headers: { Accept: "application/json", ...(init.body ? { "Content-Type": "application/json" } : {}), ...init.headers },
      });
      if (!response.ok) return { ok: false, error: await this.problem(response) };
      return {
        ok: true,
        data: {
          value: await response.json() as T,
          etag: response.headers.get("ETag") ?? "",
        },
      };
    } catch (cause) {
      return { ok: false, error: { code: "NETWORK_ERROR", message: cause instanceof Error ? cause.message : "无法连接 Vistora API", retryable: true } };
    }
  }

  private async problem(response: Response): Promise<ApiProblem> {
    const body = record(await response.json().catch(() => null));
    const details = record(body.details);
    const errors = Array.isArray(details.errors) ? details.errors.map(record) : [];
    const first = errors[0];
    const location = Array.isArray(first?.loc) ? first.loc.filter((part) => typeof part === "string").join(".") : text(details.path);
    return {
      code: text(body.code, `HTTP_${response.status}`), message: text(body.message, response.statusText || "API 请求失败"), status: response.status,
      field: location || undefined, retryable: response.status === 429 || response.status >= 500,
      details: Object.keys(details).length ? details as JsonObject : undefined,
    };
  }

  private async page(path: string): Promise<ApiResult<JsonRecord[]>> {
    const output: JsonRecord[] = [];
    let cursor: string | null = null;
    do {
      const separator = path.includes("?") ? "&" : "?";
      const result: ApiResult<Page<JsonRecord>> = await this.request<Page<JsonRecord>>(`${path}${separator}limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (!result.ok) return result;
      output.push(...result.data.data);
      cursor = result.data.page?.has_more ? result.data.page.next_cursor ?? null : null;
    } while (cursor);
    return { ok: true, data: output };
  }

  async getSession(): Promise<ApiResult<SessionContext>> {
    const result = await this.request<JsonRecord>("/v1/context");
    if (!result.ok) return result;
    const profileResult = await this.getAccountProfile();
    if (!profileResult.ok) return profileResult;
    const userId = text(result.data.user_id);
    const workspaceId = text(result.data.workspace_id);
    const profile = profileResult.data.value;
    return { ok: true, data: {
      user: {
        id: userId, displayName: profile.displayName, email: profile.email,
        avatarUrl: profile.avatarUrl, locale: profile.locale, timezone: profile.timezone,
      },
      activeWorkspaceId: workspaceId,
      workspaces: [{ id: workspaceId, name: text(result.data.workspace_name, "默认创作空间"), slug: "default", kind: "personal", role: "owner" }],
    } };
  }

  async getAccountCapabilities(): Promise<ApiResult<AccountCapabilities>> {
    const result = await this.request<JsonRecord>("/v1/account/capabilities");
    return result.ok ? { ok: true, data: {
      profile: text(result.data.profile, "unavailable") as AccountCapabilities["profile"],
      creationPreferences: text(result.data.creation_preferences, "unavailable") as AccountCapabilities["creationPreferences"],
      sessions: text(result.data.sessions, "unavailable") as AccountCapabilities["sessions"],
      apiKeys: text(result.data.api_keys, "unavailable") as AccountCapabilities["apiKeys"],
      twoFactorAuthentication: text(result.data.two_factor_authentication, "unavailable") as AccountCapabilities["twoFactorAuthentication"],
    } } : result;
  }

  private mapAccountProfile(raw: JsonRecord): AccountProfile {
    return {
      userId: text(raw.user_id), workspaceId: text(raw.workspace_id), email: text(raw.email),
      displayName: text(raw.display_name), avatarUrl: text(raw.avatar_url) || undefined,
      locale: text(raw.locale, "zh-CN"), timezone: text(raw.timezone, "Asia/Shanghai"),
      revision: number(raw.revision, 1), updatedAt: text(raw.updated_at),
    };
  }

  async getAccountProfile(): Promise<ApiResult<VersionedResource<AccountProfile>>> {
    const result = await this.requestVersioned<JsonRecord>("/v1/account/profile");
    return result.ok ? { ok: true, data: { value: this.mapAccountProfile(result.data.value), etag: result.data.etag } } : result;
  }

  async replaceAccountProfile(profile: AccountProfileUpdate, etag: string): Promise<ApiResult<VersionedResource<AccountProfile>>> {
    const result = await this.requestVersioned<JsonRecord>("/v1/account/profile", {
      method: "PUT", headers: { "If-Match": etag }, body: JSON.stringify({
        display_name: profile.displayName, avatar_url: profile.avatarUrl ?? null,
        locale: profile.locale, timezone: profile.timezone,
      }),
    });
    return result.ok ? { ok: true, data: { value: this.mapAccountProfile(result.data.value), etag: result.data.etag } } : result;
  }

  private mapCreationPreferences(raw: JsonRecord): CreationPreferences {
    return {
      userId: text(raw.user_id), workspaceId: text(raw.workspace_id),
      defaultLanguage: text(raw.default_language, "zh-CN"),
      defaultAspectRatio: text(raw.default_aspect_ratio, "16:9") as CreationPreferences["defaultAspectRatio"],
      defaultDurationSeconds: number(raw.default_duration_seconds, 180),
      defaultVisibility: text(raw.default_visibility, "private") as CreationPreferences["defaultVisibility"],
      autoQualityCheck: Boolean(raw.auto_quality_check), revision: number(raw.revision, 1), updatedAt: text(raw.updated_at),
    };
  }

  async getCreationPreferences(): Promise<ApiResult<VersionedResource<CreationPreferences>>> {
    const result = await this.requestVersioned<JsonRecord>("/v1/account/creation-preferences");
    return result.ok ? { ok: true, data: { value: this.mapCreationPreferences(result.data.value), etag: result.data.etag } } : result;
  }

  async replaceCreationPreferences(preferences: CreationPreferencesUpdate, etag: string): Promise<ApiResult<VersionedResource<CreationPreferences>>> {
    const result = await this.requestVersioned<JsonRecord>("/v1/account/creation-preferences", {
      method: "PUT", headers: { "If-Match": etag }, body: JSON.stringify({
        default_language: preferences.defaultLanguage, default_aspect_ratio: preferences.defaultAspectRatio,
        default_duration_seconds: preferences.defaultDurationSeconds, default_visibility: preferences.defaultVisibility,
        auto_quality_check: preferences.autoQualityCheck,
      }),
    });
    return result.ok ? { ok: true, data: { value: this.mapCreationPreferences(result.data.value), etag: result.data.etag } } : result;
  }

  private mapAccountSession(raw: JsonRecord): AccountSession {
    return {
      id: text(raw.id), workspaceId: text(raw.workspace_id), userId: text(raw.user_id),
      userAgent: text(raw.user_agent) || undefined, createdAt: text(raw.created_at), expiresAt: text(raw.expires_at),
      lastSeenAt: text(raw.last_seen_at), revokedAt: text(raw.revoked_at) || undefined,
    };
  }

  async listAccountSessions(): Promise<ApiResult<AccountSession[]>> {
    const result = await this.request<JsonRecord[]>("/v1/account/sessions");
    return result.ok ? { ok: true, data: result.data.map((item) => this.mapAccountSession(item)) } : result;
  }

  revokeAccountSession(sessionId: string): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/account/sessions/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  }

  private mapApiKey(raw: JsonRecord): AccountApiKey {
    return {
      id: text(raw.id), workspaceId: text(raw.workspace_id), name: text(raw.name), keyPrefix: text(raw.key_prefix),
      scopes: strings(raw.scopes) as AccountApiKey["scopes"], createdAt: text(raw.created_at),
      expiresAt: text(raw.expires_at) || undefined, lastUsedAt: text(raw.last_used_at) || undefined,
      revokedAt: text(raw.revoked_at) || undefined,
    };
  }

  async listApiKeys(): Promise<ApiResult<AccountApiKey[]>> {
    const result = await this.request<JsonRecord[]>("/v1/account/api-keys");
    return result.ok ? { ok: true, data: result.data.map((item) => this.mapApiKey(item)) } : result;
  }

  async createApiKey(input: ApiKeyCreateRequest): Promise<ApiResult<CreatedApiKey>> {
    const result = await this.request<JsonRecord>("/v1/account/api-keys", {
      method: "POST", body: JSON.stringify({ name: input.name, scopes: input.scopes, expires_at: input.expiresAt ?? null }),
    });
    return result.ok ? { ok: true, data: { key: this.mapApiKey(record(result.data.key)), plaintext: text(result.data.api_key) } } : result;
  }

  revokeApiKey(keyId: string): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/account/api-keys/${encodeURIComponent(keyId)}`, { method: "DELETE" });
  }

  private mapVersion(raw: JsonRecord): SkillVersion {
    const id = text(raw.id);
    this.rawVersions.set(id, raw);
    return {
      id, skillId: text(raw.skill_id), version: text(raw.version), schemaVersion: text(raw.schema_version, "1.0.0"),
      state: text(raw.state, "draft") as SkillVersion["state"], revision: number(raw.revision, 1), spec: canonicalVersionToSkillSpec(raw),
      testTopics: strings(raw.test_topics), contentHash: text(raw.content_hash) || undefined,
      releaseNotes: text(raw.release_notes) || undefined, createdBy: text(raw.created_by), createdAt: text(raw.created_at),
      publishedAt: text(raw.published_at) || undefined,
    };
  }

  private mapSkill(raw: JsonRecord, versions: SkillVersion[] = [], runCount = 0): Skill {
    const system = text(raw.ownership_type) === "system" || text(raw.publisher_type) === "system";
    const currentVersionId = text(raw.current_version_id) || undefined;
    const draftVersionId = versions.find((version) => ["draft", "ready", "rejected"].includes(version.state))?.id;
    const canEdit = !system;
    return {
      id: text(raw.id), workspaceId: text(raw.workspace_id), name: text(raw.name), slug: text(raw.slug), description: text(raw.description),
      publisher: { id: system ? "system" : text(raw.workspace_id), workspaceId: text(raw.workspace_id), type: system ? "system" : "workspace", displayName: text(raw.publisher_name), verified: system },
      visibility: text(raw.visibility, "private") as Skill["visibility"],
      status: (text(raw.status) === "active" ? "published" : text(raw.status, "draft")) as Skill["status"],
      currentVersionId, draftVersionId,
      forkedFrom: text(raw.forked_from_skill_id) ? { skillId: text(raw.forked_from_skill_id), versionId: "" } : undefined,
      createdBy: text(raw.created_by), createdAt: text(raw.created_at), updatedAt: text(raw.updated_at),
      permissions: { view: true, fork: true, edit: canEdit, test: true, publish: canEdit, deleteDraft: canEdit, deprecate: canEdit },
      stats: { runCount, versionCount: versions.length },
    };
  }

  async listSkills(query: SkillQuery = {}): Promise<ApiResult<Skill[]>> {
    const [skillsResult, versionsResult, runsResult] = await Promise.all([this.page("/v1/skills"), this.page("/v1/skill-versions"), this.page("/v1/runs")]);
    if (!skillsResult.ok) return skillsResult;
    if (!versionsResult.ok) return versionsResult;
    if (!runsResult.ok) return runsResult;
    const versions = versionsResult.data.map((item) => this.mapVersion(item));
    const versionOwners = new Map(versions.map((version) => [version.id, version.skillId]));
    const runCounts = new Map<string, number>();
    for (const run of runsResult.data) {
      const skillId = versionOwners.get(text(record(record(run.composition_snapshot).skill_version).id));
      if (skillId) runCounts.set(skillId, (runCounts.get(skillId) ?? 0) + 1);
    }
    let skills = skillsResult.data.map((item) => this.mapSkill(item, versions.filter((version) => version.skillId === text(item.id)), runCounts.get(text(item.id)) ?? 0));
    if (query.scope === "mine") skills = skills.filter((item) => item.publisher.type !== "system");
    if (query.scope === "official") skills = skills.filter((item) => item.publisher.type === "system");
    if (query.search) { const needle = query.search.toLowerCase(); skills = skills.filter((item) => `${item.name} ${item.description} ${item.publisher.displayName}`.toLowerCase().includes(needle)); }
    if (query.status) skills = skills.filter((item) => item.status === query.status);
    if (query.visibility) skills = skills.filter((item) => item.visibility === query.visibility);
    return { ok: true, data: skills };
  }

  async getSkill(skillId: string): Promise<ApiResult<SkillDetail>> {
    const [skillResult, versionsResult, runsResult] = await Promise.all([
      this.request<JsonRecord>(`/v1/skills/${encodeURIComponent(skillId)}`),
      this.page(`/v1/skill-versions?skill_id=${encodeURIComponent(skillId)}`),
      this.page("/v1/runs"),
    ]);
    if (!skillResult.ok) return skillResult;
    if (!versionsResult.ok) return versionsResult;
    if (!runsResult.ok) return runsResult;
    const versions = versionsResult.data.map((item) => this.mapVersion(item));
    const versionIds = new Set(versions.map((version) => version.id));
    const runCount = runsResult.data.filter((run) => versionIds.has(text(record(record(run.composition_snapshot).skill_version).id))).length;
    return { ok: true, data: { skill: this.mapSkill(skillResult.data, versions, runCount), versions } };
  }

  async createSkill(request: CreateSkillRequest): Promise<ApiResult<Skill>> {
    const identity = request.kind === "import" ? request.package.identity : request.identity;
    if (request.kind === "distill") {
      return { ok: false, error: { code: "distill_not_available", message: "示例蒸馏服务尚未接入，未创建空白替代草稿。", status: 501 } };
    }
    if (request.kind === "fork") {
      const forked = await this.request<{ skill: JsonRecord; version: JsonRecord }>(`/v1/skills/${encodeURIComponent(request.sourceSkillId)}/fork`, {
        method: "POST", headers: { "Idempotency-Key": idempotencyKey("fork-skill") }, body: JSON.stringify({
          source_version_id: request.sourceVersionId, name: identity.name, slug: identity.slug || slugify(identity.name),
          description: identity.description, visibility: identity.visibility ?? "private",
        }),
      });
      if (!forked.ok) return forked;
      const version = this.mapVersion(forked.data.version);
      return { ok: true, data: this.mapSkill(forked.data.skill, [version]) };
    }
    const created = await this.request<JsonRecord>("/v1/skills", { method: "POST", headers: { "Idempotency-Key": idempotencyKey("create-skill") }, body: JSON.stringify({
      name: identity.name, slug: identity.slug || slugify(identity.name), description: identity.description,
      visibility: identity.visibility ?? "private", publisher_name: "My Vistora",
    }) });
    if (!created.ok) return created;
    const spec = request.kind === "blank" ? request.initialSpec : request.package.spec;
    const version = await this.request<JsonRecord>("/v1/skill-versions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey("create-version") }, body: JSON.stringify({
      skill_id: text(created.data.id), version: "0.1.0", ...defaultCanonicalSpec(spec),
      test_topics: request.kind === "import" ? request.package.testTopics ?? [] : [],
      release_notes: request.kind === "import" ? request.package.releaseNotes ?? "" : "",
    }) });
    if (!version.ok) {
      await this.request<void>(`/v1/skills/${encodeURIComponent(text(created.data.id))}`, { method: "DELETE" });
      return version;
    }
    return { ok: true, data: this.mapSkill(created.data, [this.mapVersion(version.data)]) };
  }

  async createDraft(skillId: string, sourceVersionId: string): Promise<ApiResult<SkillVersion>> {
    const detail = await this.getSkill(skillId);
    if (!detail.ok) return detail;
    const highest = detail.data.versions.reduce((current, item) => {
      const left = current.split(".").map(Number);
      const right = item.version.split(".").map(Number);
      return (right[0] ?? 0) > (left[0] ?? 0)
        || ((right[0] ?? 0) === (left[0] ?? 0) && (right[1] ?? 0) > (left[1] ?? 0))
        || ((right[0] ?? 0) === (left[0] ?? 0) && (right[1] ?? 0) === (left[1] ?? 0) && (right[2] ?? 0) > (left[2] ?? 0))
        ? item.version : current;
    }, "0.0.0");
    const parts = highest.split(".").map(Number);
    const version = `${parts[0] || 0}.${parts[1] || 0}.${(parts[2] || 0) + 1}`;
    const result = await this.request<JsonRecord>("/v1/skill-versions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey("create-draft") }, body: JSON.stringify({ skill_id: skillId, version, source_version_id: sourceVersionId }) });
    return result.ok ? { ok: true, data: this.mapVersion(result.data) } : result;
  }

  private draftPatch(patch: SkillDraftPatch, raw: JsonRecord): JsonRecord {
    const output: JsonRecord = patch.spec ? skillSpecToCanonicalPatch(patch.spec, raw) : {};
    if (patch.testTopics) output.test_topics = patch.testTopics;
    if (patch.releaseNotes !== undefined) output.release_notes = patch.releaseNotes;
    return output;
  }

  async saveDraft(_skillId: string, versionId: string, patch: SkillDraftPatch, expectedRevision: number): Promise<ApiResult<SkillVersion>> {
    const raw = this.rawVersions.get(versionId) ?? {};
    const result = await this.request<JsonRecord>(`/v1/skill-versions/${encodeURIComponent(versionId)}`, { method: "PATCH", headers: { "If-Match": String(expectedRevision), "Idempotency-Key": idempotencyKey("save-draft") }, body: JSON.stringify(this.draftPatch(patch, raw)) });
    return result.ok ? { ok: true, data: this.mapVersion(result.data) } : result;
  }

  async validateVersion(skillId: string, versionId: string): Promise<ApiResult<ValidationReport>> {
    const revision = number(this.rawVersions.get(versionId)?.revision, 1);
    const result = await this.request<JsonRecord>(`/v1/skill-versions/${encodeURIComponent(versionId)}/validate`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey("validate-version"), "If-Match": String(revision) } });
    if (!result.ok) return result;
    const current = this.rawVersions.get(versionId) ?? {};
    this.rawVersions.set(versionId, {
      ...current,
      revision: number(result.data.revision, number(current.revision, 1)),
      state: result.data.ready ? "ready" : "rejected",
    });
    return { ok: true, data: {
      skillId: text(result.data.skill_id, skillId), versionId: text(result.data.version_id, versionId), ready: Boolean(result.data.ready),
      revision: number(result.data.revision, number(current.revision, 1)),
      checks: (Array.isArray(result.data.checks) ? result.data.checks.map(record) : []).map((check) => ({ id: text(check.id), label: text(check.label), passed: Boolean(check.passed), severity: text(check.severity, "error") as "error" | "warning" | "info", message: text(check.message), section: undefined })),
      estimatedCost: { amount: number(record(result.data.estimated_cost).amount), currency: text(record(result.data.estimated_cost).currency, "CNY") as "CNY" | "USD" },
    } };
  }

  async publishVersion(_skillId: string, versionId: string, releaseNotes: string): Promise<ApiResult<SkillVersion>> {
    const cached = this.rawVersions.get(versionId);
    const result = await this.request<JsonRecord>(`/v1/skill-versions/${encodeURIComponent(versionId)}/publish`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("publish-version"), "If-Match": String(number(cached?.revision, 1)) },
      body: JSON.stringify({ release_notes: releaseNotes }),
    });
    return result.ok ? { ok: true, data: this.mapVersion(result.data) } : result;
  }

  async compareVersions(request: ComparisonRequest): Promise<ApiResult<ComparisonResult>> {
    let execution = await this.request<JsonRecord>("/v1/skill-test-executions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey("test-skill") }, body: JSON.stringify({
      skill_id: request.skillId, left_version_id: request.leftVersionId, right_version_id: request.rightVersionId, topic: request.topic, inputs: request.inputs ?? {},
    }) });
    if (!execution.ok) return execution;
    for (let attempt = 0; !["succeeded", "failed", "cancelled"].includes(text(execution.data.status)) && attempt < this.pollAttempts; attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, this.pollIntervalMs));
      execution = await this.request<JsonRecord>(`/v1/skill-test-executions/${encodeURIComponent(text(execution.data.id))}`);
      if (!execution.ok) return execution;
    }
    if (text(execution.data.status) !== "succeeded") return { ok: false, error: { code: text(record(execution.data.error).code, "TEST_EXECUTION_FAILED"), message: text(record(execution.data.error).message, "Skill 测试执行失败"), retryable: text(execution.data.status) !== "cancelled" } };
    const result = record(execution.data.result);
    const side = (value: unknown) => { const item = record(value); return { versionId: text(item.version_id), version: text(item.version), output: text(item.output), cost: { amount: number(record(item.cost).amount), currency: text(record(item.cost).currency, "CNY") as "CNY" | "USD" }, durationMs: number(item.duration_ms), checks: (Array.isArray(item.checks) ? item.checks.map(record) : []).map((check) => ({ id: text(check.id), label: text(check.label), passed: Boolean(check.passed), score: number(check.score), note: text(check.note) })) }; };
    return { ok: true, data: { id: text(execution.data.id), topic: text(result.topic, request.topic), left: side(result.left), right: side(result.right), differences: (Array.isArray(result.differences) ? result.differences.map(record) : []).map((diff) => ({ path: text(diff.path), change: text(diff.change, "changed") as "added" | "removed" | "changed", left: text(diff.left) || undefined, right: text(diff.right) || undefined })), createdAt: text(execution.data.created_at) } };
  }

  rollbackSkill(): Promise<ApiResult<Skill>> { return Promise.resolve({ ok: false, error: { code: "NOT_SUPPORTED", message: "控制 API 暂不支持版本回滚" } }); }
  deprecateVersion(): Promise<ApiResult<SkillVersion>> { return Promise.resolve({ ok: false, error: { code: "NOT_SUPPORTED", message: "控制 API 暂不支持弃用版本" } }); }

  async deleteDraft(skillId: string): Promise<ApiResult<void>> {
    const detail = await this.getSkill(skillId);
    if (!detail.ok) return detail;
    const draftId = detail.data.skill.draftVersionId;
    if (!draftId) return { ok: false, error: { code: "DRAFT_NOT_FOUND", message: "当前 Skill 没有草稿" } };
    const revision = detail.data.versions.find((version) => version.id === draftId)?.revision ?? 1;
    return this.request<void>(`/v1/skill-versions/${encodeURIComponent(draftId)}`, { method: "DELETE", headers: { "Idempotency-Key": idempotencyKey("delete-draft"), "If-Match": String(revision) } });
  }

  async createAssetLibrary(request: AssetLibraryCreateRequest): Promise<ApiResult<AssetLibraryOption>> {
    const result = await this.request<JsonRecord>("/v1/asset-libraries", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("create-asset-library") },
      body: JSON.stringify({ name: request.name, slug: request.slug, description: request.description }),
    });
    if (!result.ok) return result;
    return { ok: true, data: {
      id: text(result.data.id), workspaceId: text(result.data.workspace_id),
      name: text(result.data.name), description: text(result.data.description),
      assetCount: number(result.data.asset_count), readyAssetCount: number(result.data.ready_asset_count),
    } };
  }

  async getBenchmarkConnectionStatus(signal?: AbortSignal): Promise<ApiResult<BenchmarkConnection>> {
    const result = await this.request<JsonRecord>("/v1/benchmark-auth/xiaohongshu/status", {
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    return result.ok ? this.mapBenchmarkConnection(result.data) : result;
  }

  async startBenchmarkConnection(idempotencyKey: string, signal?: AbortSignal): Promise<ApiResult<BenchmarkConnection>> {
    const result = await this.request<JsonRecord>("/v1/benchmark-auth/xiaohongshu/qrcode", {
      method: "POST", body: "{}", headers: { "Idempotency-Key": idempotencyKey },
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    return result.ok ? this.mapBenchmarkConnection(result.data) : result;
  }

  private mapBenchmarkConnection(raw: JsonRecord): ApiResult<BenchmarkConnection> {
    const states: BenchmarkConnectionState[] = ["not_configured", "provider_unavailable", "checking", "login_required", "awaiting_scan", "authorized", "expired", "error"];
    const state = raw.state as BenchmarkConnectionState;
    const qr = raw.qr_image_data_url;
    const validTimestamp = (value: unknown) => value === null || (typeof value === "string" && Number.isFinite(Date.parse(value)));
    if (raw.schema_version !== "1.0.0" || raw.platform !== "xiaohongshu" || raw.mode !== "local_browser" || !states.includes(state)
      || !validTimestamp(raw.expires_at) || !validTimestamp(raw.checked_at)
      || (qr !== null && (typeof qr !== "string" || qr.length > 1_400_000 || !/^data:image\/png;base64,iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/.test(qr)))
      || (state === "awaiting_scan" && (!qr || !raw.expires_at))) {
      return { ok: false, error: { code: "BENCHMARK_AUTH_INVALID_RESPONSE", message: "小红书连接服务返回了不兼容的数据，请更新研究 API 后重试。", retryable: false } };
    }
    return { ok: true, data: {
      platform: "xiaohongshu", mode: "local_browser", state,
      qrImageDataUrl: state === "awaiting_scan" ? qr as string : null,
      expiresAt: raw.expires_at as string | null, checkedAt: raw.checked_at as string | null,
      retryAfterSeconds: typeof raw.retry_after_seconds === "number" && Number.isFinite(raw.retry_after_seconds) ? Math.min(10, Math.max(3, raw.retry_after_seconds)) : 3,
      errorCode: typeof raw.error_code === "string" ? raw.error_code : null,
    } };
  }

  async getBenchmarkAccountDemo(): Promise<ApiResult<BenchmarkAccountSnapshot>> {
    const result = await this.request<JsonRecord>("/v1/benchmark-accounts/demo");
    if (!result.ok) return result;
    const compatibility = this.benchmarkDiscoveryProblem(result.data);
    if (compatibility) return { ok: false, error: compatibility };
    return { ok: true, data: this.mapBenchmarkAccountSnapshot(result.data) };
  }

  async previewBenchmarkAccount(
    request: BenchmarkAccountPreviewRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<BenchmarkAccountSnapshot>> {
    const profile = benchmarkProfile(request.profileUrl);
    if (!profile) return { ok: false, error: { code: "BENCHMARK_INVALID_PROFILE_URL", message: "请输入有效的小红书 HTTPS 博主主页链接。" } };
    const result = await this.request<JsonRecord>("/v1/benchmark-accounts/preview", {
      method: "POST", signal: benchmarkRequestSignal(signal, 60_000),
      body: JSON.stringify({ platform: request.platform, profile_url: profile.url, refresh_note_identity: request.refreshNoteIdentity ?? false }),
    });
    if (!result.ok) return result;
    const compatibility = this.benchmarkDiscoveryProblem(result.data, profile.userId);
    if (compatibility) return { ok: false, error: compatibility };
    return { ok: true, data: this.mapBenchmarkAccountSnapshot(result.data) };
  }

  async generateBenchmarkAccountReport(
    request: BenchmarkAccountPreviewRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<BenchmarkAccountReport>> {
    const profile = benchmarkProfile(request.profileUrl);
    if (!profile) return { ok: false, error: { code: "BENCHMARK_INVALID_PROFILE_URL", message: "请输入有效的小红书 HTTPS 博主主页链接。" } };
    const result = await this.request<JsonRecord>("/v1/benchmark-accounts/report", {
      method: "POST", signal: benchmarkRequestSignal(signal, 60_000),
      body: JSON.stringify({ platform: request.platform, profile_url: profile.url, refresh_note_identity: request.refreshNoteIdentity ?? false }),
    });
    if (!result.ok) return result;
    const compatibility = this.benchmarkDiscoveryProblem(record(result.data.snapshot), profile.userId);
    if (compatibility) return { ok: false, error: compatibility };
    return { ok: true, data: this.mapBenchmarkAccountReport(result.data) };
  }

  async getBenchmarkDeepReportDemo(): Promise<ApiResult<BenchmarkDeepNoteReport>> {
    const result = await this.request<JsonRecord>("/v1/benchmark-notes/deep-report/demo");
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkDeepNoteReport(result.data) };
  }

  async collectBenchmarkNoteSourceEvidence(
    request: BenchmarkNoteSourceRequest,
    signal?: AbortSignal,
  ): Promise<ApiResult<BenchmarkNoteSourceEvidence>> {
    const profile = benchmarkProfile(request.profileUrl);
    if (!profile || !/^[a-f0-9]{24}$/.test(request.noteId)) return { ok: false, error: { code: "BENCHMARK_INVALID_NOTE_IDENTITY", message: "当前笔记身份无效，请补全笔记信息后重新选择。" } };
    const result = await this.request<JsonRecord>("/v1/benchmark-notes/source-evidence", {
      method: "POST", signal: benchmarkRequestSignal(signal, 60_000),
      body: JSON.stringify({
        platform: request.platform,
        profile_url: profile.url,
        note_id: request.noteId,
      }),
    });
    if (!result.ok) return result;
    if (result.data.profile_user_id !== profile.userId || result.data.note_id !== request.noteId || result.data.platform !== "xiaohongshu") {
      return { ok: false, error: { code: "BENCHMARK_NOTE_IDENTITY_CONFLICT", message: "返回的详情不属于当前账号和笔记，已停止展示。" } };
    }
    const media = record(result.data.media);
    if (media.kind !== "video" && media.kind !== "image") return { ok: false, error: { code: "BENCHMARK_SOURCE_CHANGED", message: "详情未提供可核验的媒体类型，请补全笔记信息后重试。" } };
    return { ok: true, data: this.mapBenchmarkNoteSourceEvidence(result.data) };
  }

  private mapBenchmarkNoteSourceEvidence(raw: JsonRecord): BenchmarkNoteSourceEvidence {
    const media = record(raw.media);
    const metric = (value: unknown) => {
      const item = record(value);
      return {
        display: text(item.display, "—"),
        lowerBound: typeof item.lower_bound === "number" ? item.lower_bound : undefined,
        precision: text(item.precision, "unknown") as "exact" | "rounded" | "lower_bound" | "unknown",
      };
    };
    return {
      schemaVersion: "1.0.0",
      platform: "xiaohongshu",
      profileUserId: text(raw.profile_user_id),
      noteId: text(raw.note_id),
      canonicalUrl: text(raw.canonical_url),
      capturedAt: text(raw.captured_at),
      acquisitionMethod: "authenticated_managed_browser",
      title: text(raw.title),
      description: text(raw.description),
      likes: metric(raw.likes),
      collects: metric(raw.collects),
      comments: metric(raw.comments),
      media: {
        kind: text(media.kind) === "image" ? "image" : "video",
        videoAvailable: Boolean(media.video_available),
        imageCount: number(media.image_count),
        durationMs: typeof media.duration_ms === "number" ? media.duration_ms : undefined,
        width: typeof media.width === "number" ? media.width : undefined,
        height: typeof media.height === "number" ? media.height : undefined,
        trustedMediaOrigin: Boolean(media.trusted_media_origin),
      },
      limitations: strings(raw.limitations),
    };
  }

  async createBenchmarkAnalysisJob(
    request: BenchmarkNoteSourceRequest,
    key: string,
    signal?: AbortSignal,
  ): Promise<ApiResult<BenchmarkAnalysisJob>> {
    const profile = benchmarkProfile(request.profileUrl);
    if (!profile || !/^[a-f0-9]{24}$/.test(request.noteId)) return { ok: false, error: { code: "BENCHMARK_INVALID_NOTE_IDENTITY", message: "当前笔记身份无效，请补全笔记信息后重新选择。" } };
    const result = await this.request<JsonRecord>("/v1/benchmark-analysis/jobs", {
      method: "POST", signal: benchmarkRequestSignal(signal),
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({ platform: request.platform, profile_url: profile.url, note_id: request.noteId }),
    });
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkAnalysisJob(result.data) };
  }

  async getBenchmarkAnalysisJob(jobId: string, signal?: AbortSignal): Promise<ApiResult<BenchmarkAnalysisJob>> {
    const result = await this.request<JsonRecord>(`/v1/benchmark-analysis/jobs/${encodeURIComponent(jobId)}`, {
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkAnalysisJob(result.data) };
  }

  async getLatestBenchmarkAnalysisJob(
    profileUserId: string, noteId: string, signal?: AbortSignal,
  ): Promise<ApiResult<BenchmarkAnalysisJob>> {
    const query = new URLSearchParams({ profile_user_id: profileUserId, note_id: noteId });
    const result = await this.request<JsonRecord>(`/v1/benchmark-analysis/latest?${query}`, {
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkAnalysisJob(result.data) };
  }

  async cancelBenchmarkAnalysisJob(jobId: string, signal?: AbortSignal): Promise<ApiResult<BenchmarkAnalysisJob>> {
    const result = await this.request<JsonRecord>(`/v1/benchmark-analysis/jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST", signal: benchmarkRequestSignal(signal),
    });
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkAnalysisJob(result.data) };
  }

  async retryBenchmarkAnalysisJob(jobId: string, signal?: AbortSignal): Promise<ApiResult<BenchmarkAnalysisJob>> {
    const result = await this.request<JsonRecord>(`/v1/benchmark-analysis/jobs/${encodeURIComponent(jobId)}/retry`, {
      method: "POST", signal: benchmarkRequestSignal(signal),
    });
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkAnalysisJob(result.data) };
  }

  async listBenchmarkHistory(query: BenchmarkHistoryQuery, signal?: AbortSignal): Promise<ApiResult<BenchmarkHistoryPage>> {
    const params = new URLSearchParams();
    if (query.kind) params.set("kind", query.kind);
    if (query.q) params.set("q", query.q);
    if (query.cursor) params.set("cursor", query.cursor);
    const result = await this.request<JsonRecord>(`/v1/benchmark-history?${params}`, {
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    if (!result.ok) return result;
    return { ok: true, data: { items: records(result.data.items).map((item) => this.mapHistorySummary(item)), nextCursor: text(result.data.next_cursor) || undefined } };
  }

  async getBenchmarkHistory(recordId: string, signal?: AbortSignal): Promise<ApiResult<BenchmarkHistoryDetail>> {
    if (!/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(recordId)) {
      return { ok: false, error: { code: "BENCHMARK_HISTORY_NOT_FOUND", message: "历史记录链接无效，请从列表重新选择。" } };
    }
    const result = await this.request<JsonRecord>(`/v1/benchmark-history/${encodeURIComponent(recordId)}`, {
      signal: benchmarkRequestSignal(signal), cache: "no-store",
    });
    if (!result.ok) return result;
    const raw = result.data;
    const account = record(raw.account_report);
    const accountProfile = record(record(account.snapshot).profile);
    const video = record(raw.video_job);
    const matches = raw.record_id === recordId && /^[a-f0-9]{24}$/.test(text(raw.profile_user_id))
      && (raw.kind === "account"
        ? nonEmptyRecord(raw.account_report) && !nonEmptyRecord(raw.video_job)
          && accountProfile.platform === "xiaohongshu" && accountProfile.user_id === raw.profile_user_id
          && benchmarkProfile(text(accountProfile.profile_url))?.userId === raw.profile_user_id
        : raw.kind === "video" && nonEmptyRecord(raw.video_job) && !nonEmptyRecord(raw.account_report)
          && video.profile_user_id === raw.profile_user_id && video.note_id === raw.note_id
          && /^[a-f0-9]{24}$/.test(text(raw.note_id)));
    if (!matches) return { ok: false, error: { code: "BENCHMARK_HISTORY_IDENTITY_CONFLICT", message: "保存报告与当前记录的账号或笔记不一致，已停止展示。请返回历史列表重试。" } };
    return { ok: true, data: {
      ...this.mapHistorySummary(result.data),
      accountReport: nonEmptyRecord(result.data.account_report) ? this.mapBenchmarkAccountReport(record(result.data.account_report)) : null,
      videoJob: nonEmptyRecord(result.data.video_job) ? this.mapBenchmarkAnalysisJob(record(result.data.video_job)) : null,
      mediaAvailable: Boolean(result.data.media_available),
    } };
  }

  private mapHistorySummary(raw: JsonRecord): BenchmarkHistorySummary {
    return {
      id: text(raw.record_id), kind: raw.kind === "video" ? "video" : "account", title: text(raw.title),
      profileUserId: text(raw.profile_user_id), noteId: text(raw.note_id) || undefined,
      status: raw.status === "partial" ? "partial" : "ready", savedAt: text(raw.saved_at), analyzedAt: text(raw.analyzed_at),
    };
  }

  private mapBenchmarkAnalysisJob(raw: JsonRecord): BenchmarkAnalysisJob {
    const id = text(raw.job_id);
    const progress = record(raw.progress);
    // Only links for generated files explicitly returned by the artifact manifest are exposed.
    const artifacts = records(raw.artifacts).filter((item) =>
      /^(?:attempt-[1-9][0-9]*\/)?(?:frames\/[A-Za-z0-9_-]+\.(?:jpg|jpeg|png)|[A-Za-z0-9_-]+\.(?:wav|mp3))$/.test(text(item.filename)),
    ).map((item) => ({
      filename: text(item.filename),
      mediaType: text(item.media_type),
      contentUrl: `${this.benchmarkBaseUrl}/v1/benchmark-analysis/jobs/${encodeURIComponent(id)}/artifacts/${text(item.filename).split("/").map(encodeURIComponent).join("/")}`,
    }));
    const analysisRaw = nonEmptyRecord(raw.analysis);
    let analysis: BenchmarkVideoAnalysis | null = null;
    if (analysisRaw) {
      const transcript = record(analysisRaw.transcript);
      const temporal = record(analysisRaw.temporal);
      const technical = record(analysisRaw.technical);
      const audio = record(analysisRaw.audio_analysis);
      const narrative = record(analysisRaw.narrative_analysis);
      analysis = {
        status: text(analysisRaw.status) === "complete" ? "complete" : "partial",
        durationMs: optionalNumber(technical.duration_ms),
        frames: records(analysisRaw.frames).map((frame) => ({
          timestampMs: number(frame.timestamp_ms),
          key: text(frame.key),
          artifactUrl: artifacts.find((artifact) => artifact.filename === text(frame.key))?.contentUrl,
          ocrText: text(record(frame.ocr).text),
          vision: Object.fromEntries(Object.entries(record(frame.vision)).filter((entry): entry is [string, string] => typeof entry[1] === "string")),
        })),
        capabilities: Object.fromEntries(Object.entries(record(analysisRaw.capabilities)).map(([key, value]) => {
          const capability = record(value);
          return [key, {
            status: text(capability.status, "unavailable") as BenchmarkVideoAnalysis["capabilities"][string]["status"],
            provider: text(capability.provider),
            limitations: strings(capability.limitations),
          }];
        })),
        speechStatus: ["present", "absent"].includes(text(temporal.speech_status))
          ? text(temporal.speech_status) as "present" | "absent" : "unknown",
        speechDetectionMethod: text(temporal.speech_detection_method),
        transcript: {
          text: text(transcript.text),
          segments: records(transcript.segments).filter((segment) =>
            typeof segment.start_ms === "number" && typeof segment.end_ms === "number",
          ).map((segment) => ({ startMs: number(segment.start_ms), endMs: number(segment.end_ms), text: text(segment.text) })),
          timestampSource: text(transcript.timestamp_source),
          provider: text(transcript.provider),
        },
        audioAnalysis: {
          rmsDbfs: optionalNumber(audio.rms_dbfs), peakDbfs: optionalNumber(audio.peak_dbfs),
          crestFactorDb: optionalNumber(audio.crest_factor_db),
          clippedSampleRatio: optionalNumber(audio.clipped_sample_ratio),
          lowEnergyRatio: optionalNumber(audio.low_energy_ratio),
          vadSpeechRatio: optionalNumber(audio.vad_speech_ratio),
          transcribedCharactersPerSecond: optionalNumber(audio.transcribed_characters_per_second),
          silences: records(audio.silences).map((silence) => ({ startMs: number(silence.start_ms), endMs: number(silence.end_ms) })),
          pitchAnalysis: {
            status: text(record(audio.pitch_analysis).status, "unavailable"),
            medianHz: optionalNumber(record(audio.pitch_analysis).median_hz),
            rangeHz: optionalNumber(record(audio.pitch_analysis).p90_p10_range_hz),
            limitations: strings(record(audio.pitch_analysis).limitations),
          },
          findings: strings(audio.findings), limitations: strings(audio.limitations),
        },
        creativeInsights: records(analysisRaw.creative_insights).map((insight) => ({
          claim: text(insight.claim), evidenceFrameKeys: strings(insight.evidence_frame_keys), confidence: optionalNumber(insight.confidence),
        })),
        narrativeAnalysis: {
          sections: records(narrative.sections).map((section) => ({
            startMs: number(section.start_ms), endMs: number(section.end_ms),
            role: text(section.role), observation: text(section.observation),
            strategyHypothesis: text(section.strategy_hypothesis),
            evidenceFrameKeys: strings(section.evidence_frame_keys), transcriptQuotes: strings(section.transcript_quotes),
          })),
          voiceoverFindings: records(narrative.voiceover_findings).map((finding) => ({
            claim: text(finding.claim), evidenceKind: text(finding.evidence_kind), transcriptQuote: text(finding.transcript_quote),
          })),
          audioVisualFindings: records(narrative.audio_visual_findings).map((finding) => ({
            claim: text(finding.claim), evidenceFrameKeys: strings(finding.evidence_frame_keys), transcriptQuote: text(finding.transcript_quote),
          })),
          limitations: strings(narrative.limitations),
        },
        limitations: strings(analysisRaw.limitations),
      };
    }
    const problem = nonEmptyRecord(raw.error);
    return {
      id, workspaceId: text(raw.workspace_id), profileUserId: text(raw.profile_user_id),
      noteId: text(raw.note_id), title: text(raw.title),
      status: text(raw.status, "failed") as BenchmarkAnalysisJob["status"],
      progress: { stage: text(progress.stage), percent: Math.min(100, Math.max(0, number(progress.percent))), message: text(progress.message) },
      createdAt: text(raw.created_at), updatedAt: text(raw.updated_at), attempt: number(raw.attempt, 1),
      sourceEvidence: nonEmptyRecord(raw.source_evidence) ? this.mapBenchmarkNoteSourceEvidence(record(raw.source_evidence)) : null,
      analysis,
      report: nonEmptyRecord(raw.report) ? this.mapBenchmarkDeepNoteReport(record(raw.report)) : null,
      error: problem ? { code: text(problem.code), message: text(problem.message), retryable: Boolean(problem.retryable) } : null,
      artifacts,
    };
  }

  async getLatestBenchmarkDeepReport(): Promise<ApiResult<BenchmarkDeepNoteReport>> {
    const result = await this.request<JsonRecord>("/v1/benchmark-notes/deep-report/latest");
    if (!result.ok) return result;
    return { ok: true, data: this.mapBenchmarkDeepNoteReport(result.data) };
  }

  private mapBenchmarkDeepNoteReport(raw: JsonRecord): BenchmarkDeepNoteReport {
    const metrics: BenchmarkDeepMetric[] = (
      Array.isArray(raw.metrics) ? raw.metrics : []
    ).map((value) => {
      const item = record(value);
      return {
        key: text(item.key),
        label: text(item.label),
        value: text(item.value),
        interpretation: text(item.interpretation),
      };
    });
    const findings: BenchmarkDeepFinding[] = (
      Array.isArray(raw.findings) ? raw.findings : []
    ).map((value) => {
      const item = record(value);
      return {
        category: text(item.category, "limitation") as BenchmarkDeepFinding["category"],
        confidence: text(item.confidence, "low") as BenchmarkDeepFinding["confidence"],
        claim: text(item.claim),
        evidence: strings(item.evidence),
        reusableMove: text(item.reusable_move) || undefined,
      };
    });
    const timeline: BenchmarkDeepTimelineItem[] = (
      Array.isArray(raw.timeline) ? raw.timeline : []
    ).map((value) => {
      const item = record(value);
      return {
        startMs: number(item.start_ms),
        endMs: number(item.end_ms),
        label: text(item.label),
        description: text(item.description),
        transcript: text(item.transcript),
        ocrText: strings(item.ocr_text),
        audioEvents: strings(item.audio_events),
        evidenceTypes: strings(item.evidence_types) as BenchmarkDeepTimelineItem["evidenceTypes"],
        confidence: typeof item.confidence === "number" ? item.confidence : undefined,
        representativeFrameKey: text(item.representative_frame_key) || undefined,
      };
    });
    return {
      schemaVersion: "1.0.0",
      sourceKind: text(raw.source_kind) === "worker_asset_analysis"
        ? "worker_asset_analysis"
        : "synthetic_demo",
      sourceLabel: text(raw.source_label),
      evidenceDepth: "multimodal_timeline_v1",
      title: text(raw.title),
      durationMs: number(raw.duration_ms),
      status: text(raw.status) === "ready" ? "ready" : "partial",
      summary: text(raw.summary),
      metrics,
      findings,
      timeline,
      limitations: strings(raw.limitations),
    };
  }

  private mapBenchmarkAccountReport(raw: JsonRecord): BenchmarkAccountReport {
    const signal = (value: unknown): BenchmarkStrategySignal => {
      const item = record(value);
      return {
        key: text(item.key), label: text(item.label), evidence: text(item.evidence),
        likelyEffect: text(item.likely_effect), reusableMove: text(item.reusable_move),
      };
    };
    const insight = (value: unknown): BenchmarkInsight => {
      const item = record(value);
      return {
        level: text(item.level, "limitation") as BenchmarkInsight["level"],
        confidence: text(item.confidence, "low") as BenchmarkInsight["confidence"],
        claim: text(item.claim), evidence: strings(item.evidence),
      };
    };
    const performance = (value: unknown): BenchmarkNotePerformance => {
      const item = record(value);
      return {
        tier: text(item.tier, "unknown") as BenchmarkNotePerformance["tier"],
        percentile: typeof item.percentile === "number" ? item.percentile : undefined,
        relativeToMedian: typeof item.relative_to_median === "number" ? item.relative_to_median : undefined,
        sampleMedianLikesLowerBound: typeof item.sample_median_likes_lower_bound === "number" ? item.sample_median_likes_lower_bound : undefined,
        caveat: text(item.caveat),
      };
    };
    const noteReport = (value: unknown): BenchmarkNoteReport => {
      const item = record(value);
      return {
        sampleIndex: number(item.sample_index), title: text(item.title),
        format: item.format === "image" ? "image" : item.format === "video" ? "video" : "unknown",
        publishedAt: text(item.published_at) || null, likesDisplay: text(item.likes_display),
        likesLowerBound: typeof item.likes_lower_bound === "number" ? item.likes_lower_bound : undefined,
        performance: performance(item.performance),
        strategySignals: (Array.isArray(item.strategy_signals) ? item.strategy_signals : []).map(signal),
        viralMechanisms: (Array.isArray(item.viral_mechanisms) ? item.viral_mechanisms : []).map(insight),
        recommendations: strings(item.recommendations), evidenceDepth: "public_metadata_only",
        limitations: strings(item.limitations),
      };
    };
    const pattern = (value: unknown): BenchmarkTitlePattern => {
      const item = record(value);
      return {
        key: text(item.key), label: text(item.label), matchingNotes: number(item.matching_notes),
        sharePercent: number(item.share_percent), topCandidateMatches: number(item.top_candidate_matches),
        medianLikesLowerBound: typeof item.median_likes_lower_bound === "number" ? item.median_likes_lower_bound : undefined,
        liftVsSampleMedian: typeof item.lift_vs_sample_median === "number" ? item.lift_vs_sample_median : undefined,
      };
    };
    const accountRaw = record(raw.account_report);
    const accountReport: BenchmarkAccountStrategyReport = {
      nickname: text(accountRaw.nickname), sampleSize: number(accountRaw.sample_size),
      evidenceDepth: "public_metadata_only", executiveSummary: text(accountRaw.executive_summary),
      formatStrategy: text(accountRaw.format_strategy), publishingStrategy: text(accountRaw.publishing_strategy),
      titlePatterns: (Array.isArray(accountRaw.title_patterns) ? accountRaw.title_patterns : []).map(pattern),
      topCandidateNoteIndexes: numbers(accountRaw.top_candidate_note_indexes),
      playbook: strings(accountRaw.playbook), limitations: strings(accountRaw.limitations),
    };
    return {
      schemaVersion: "1.0.0", generatedAt: text(raw.generated_at),
      snapshot: this.mapBenchmarkAccountSnapshot(record(raw.snapshot)),
      accountReport,
      noteReports: (Array.isArray(raw.note_reports) ? raw.note_reports : []).map(noteReport),
      historyRecordId: text(raw.history_record_id) || undefined,
      historyWarning: text(raw.history_warning) || undefined,
    };
  }

  private benchmarkDiscoveryProblem(raw: JsonRecord, expectedUserId?: string): ApiProblem | null {
    const acquisition = record(raw.acquisition);
    const profile = record(raw.profile);
    const canonical = benchmarkProfile(text(profile.profile_url));
    const notes = Array.isArray(raw.notes) ? raw.notes.map(record) : null;
    const validNotes = notes?.every((note) =>
      (note.identity_status === "verified" && /^[a-f0-9]{24}$/.test(text(note.note_id)))
      || (note.identity_status === "missing" && !note.note_id),
    );
    const identified = notes?.filter((note) => note.identity_status === "verified").length;
    const expectedStatus = identified === 0 ? "unavailable" : identified === notes?.length ? "complete" : "partial";
    if (acquisition.discovery_version !== "1" || !notes || !validNotes
      || acquisition.note_identity_status !== expectedStatus
      || acquisition.identified_note_count !== identified
      || acquisition.unresolved_note_count !== notes.length - (identified ?? 0)
      || !(acquisition.identity_error_code === null || typeof acquisition.identity_error_code === "string")) {
      let endpoint = "当前研究 API";
      try { const url = new URL(this.benchmarkBaseUrl); endpoint = `${url.origin}${url.pathname}`; } catch { /* Do not disclose raw configuration. */ }
      return { code: "BENCHMARK_API_VERSION_MISMATCH", message: `研究 API ${endpoint} 未提供兼容的笔记发现契约（版本 1）。请让维护者更新该服务，并检查 NEXT_PUBLIC_FRAMEFACTORY_BENCHMARK_API_URL 指向已更新的研究 API，然后重试。` };
    }
    if (profile.platform !== "xiaohongshu" || !canonical || canonical.userId !== profile.user_id
      || (expectedUserId && profile.user_id !== expectedUserId)) {
      return { code: "BENCHMARK_NOTE_IDENTITY_CONFLICT", message: "研究 API 返回的主页不属于当前账号，已停止展示。" };
    }
    return null;
  }

  private mapBenchmarkAccountSnapshot(raw: JsonRecord): BenchmarkAccountSnapshot {
    const profile = record(raw.profile);
    const acquisition = record(raw.acquisition);
    const analysis = record(raw.analysis);
    const metric = (value: unknown) => {
      const item = record(value);
      return {
        display: text(item.display, "—"),
        lowerBound: typeof item.lower_bound === "number" ? item.lower_bound : undefined,
        precision: text(item.precision, "unknown") as "exact" | "rounded" | "lower_bound" | "unknown",
      };
    };
    const note = (value: unknown): BenchmarkNote => {
      const item = record(value);
      return {
        sampleIndex: number(item.sample_index),
        noteId: text(item.note_id) || undefined,
        identityStatus: item.identity_status === "verified" ? "verified" : "missing",
        title: text(item.title),
        format: item.format === "image" ? "image" : item.format === "video" ? "video" : "unknown",
        publishedAt: text(item.published_at) || null,
        likes: metric(item.likes),
        pinned: Boolean(item.pinned),
      };
    };
    return {
      schemaVersion: "1.0.0",
      profile: {
        platform: "xiaohongshu", userId: text(profile.user_id), profileUrl: benchmarkProfile(text(profile.profile_url))?.url ?? "",
        nickname: text(profile.nickname), redId: text(profile.red_id), tags: strings(profile.tags),
        following: metric(profile.following), followers: metric(profile.followers),
        likesAndCollections: metric(profile.likes_and_collections),
      },
      acquisition: {
        capturedAt: text(acquisition.captured_at),
        method: acquisition.method === "authenticated_managed_browser"
          ? "authenticated_managed_browser"
          : "public_profile_ssr",
        fromCache: Boolean(acquisition.from_cache), initialPageHasMore: Boolean(acquisition.initial_page_has_more),
        completeness: "initial_page_sample", limitations: strings(acquisition.limitations),
        discoveryVersion: "1",
        noteIdentityStatus: acquisition.note_identity_status as BenchmarkAccountSnapshot["acquisition"]["noteIdentityStatus"],
        identifiedNoteCount: number(acquisition.identified_note_count),
        unresolvedNoteCount: number(acquisition.unresolved_note_count),
        identityErrorCode: text(acquisition.identity_error_code) || null,
      },
      analysis: {
        sampleSize: number(analysis.sample_size), videoCount: number(analysis.video_count),
        imageCount: number(analysis.image_count), videoSharePercent: number(analysis.video_share_percent),
        unknownCount: number(analysis.unknown_count),
        medianLikesLowerBound: typeof analysis.median_likes_lower_bound === "number" ? analysis.median_likes_lower_bound : undefined,
        postsLast30Days: number(analysis.posts_last_30_days),
        medianPublishIntervalDays: typeof analysis.median_publish_interval_days === "number" ? analysis.median_publish_interval_days : undefined,
        themes: (Array.isArray(analysis.themes) ? analysis.themes : []).map((value) => {
          const item = record(value);
          return { theme: text(item.theme), matchingNotes: number(item.matching_notes) };
        }),
        topNotes: (Array.isArray(analysis.top_notes) ? analysis.top_notes : []).map(note),
      },
      notes: (Array.isArray(raw.notes) ? raw.notes : []).map(note),
    };
  }

  private mapLibraryBuildJob(raw: JsonRecord): LibraryBuildJob {
    const spec = record(raw.spec);
    const progress = record(raw.progress);
    const error = record(raw.error);
    return {
      schemaVersion: text(raw.schema_version, "1.0.0"),
      id: text(raw.id),
      workspaceId: text(raw.workspace_id),
      libraryId: text(raw.library_id),
      status: text(raw.status, "queued") as LibraryBuildJob["status"],
      stage: text(raw.stage, "discover") as LibraryBuildJob["stage"],
      spec: {
        topic: text(spec.topic),
        queries: strings(spec.queries),
        sources: strings(spec.sources) as LibraryBuildJob["spec"]["sources"],
        maxAssets: number(spec.max_assets, 6),
        copyrightStatus: text(spec.copyright_status, "public_domain") as LibraryBuildJob["spec"]["copyrightStatus"],
        rightsConfirmed: true,
      },
      progress: {
        assetIds: strings(progress.asset_ids),
        discovered: number(progress.discovered),
        transferred: number(progress.transferred),
        analyzed: number(progress.analyzed),
        indexed: number(progress.indexed),
        failed: number(progress.failed),
      },
      error: Object.keys(error).length ? error as LibraryBuildJob["error"] : undefined,
      revision: number(raw.revision, 1),
      createdBy: text(raw.created_by),
      createdAt: text(raw.created_at),
      startedAt: text(raw.started_at) || undefined,
      completedAt: text(raw.completed_at) || undefined,
      updatedAt: text(raw.updated_at),
    };
  }

  async createLibraryBuildJob(
    request: LibraryBuildJobCreateRequest,
    key: string,
  ): Promise<ApiResult<LibraryBuildJob>> {
    const result = await this.request<JsonRecord>("/v1/library-build-jobs", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        library_id: request.libraryId,
        topic: request.topic,
        queries: request.queries ?? [],
        sources: request.sources,
        max_assets: request.maxAssets,
        copyright_status: request.copyrightStatus,
        rights_confirmed: true,
      }),
    });
    return result.ok ? { ok: true, data: this.mapLibraryBuildJob(result.data) } : result;
  }

  async getLibraryBuildJob(jobId: string): Promise<ApiResult<LibraryBuildJob>> {
    const result = await this.request<JsonRecord>(`/v1/library-build-jobs/${encodeURIComponent(jobId)}`);
    return result.ok ? { ok: true, data: this.mapLibraryBuildJob(result.data) } : result;
  }

  async cancelLibraryBuildJob(jobId: string, revision: number): Promise<ApiResult<LibraryBuildJob>> {
    const result = await this.request<JsonRecord>(`/v1/library-build-jobs/${encodeURIComponent(jobId)}/cancel`, {
      method: "POST",
      headers: { "If-Match": `"${revision}"` },
    });
    return result.ok ? { ok: true, data: this.mapLibraryBuildJob(result.data) } : result;
  }

  async uploadAsset(request: AssetUploadRequest): Promise<ApiResult<void>> {
    try {
      const digest = await sha256Hex(request.file);
      const kind = request.file.type.startsWith("video/") ? "video" : request.file.type.startsWith("image/") ? "image" : "";
      if (!kind) return { ok: false, error: { code: "UNSUPPORTED_MEDIA", message: "只支持图片和视频素材。" } };
      const initiated = await this.request<JsonRecord>("/v1/asset-uploads", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey("asset-upload") },
        body: JSON.stringify({
          library_id: request.libraryId, filename: request.file.name, title: request.title,
          description: request.description, kind, content_type: request.file.type,
          byte_size: request.file.size, sha256: digest,
          copyright_status: request.copyrightStatus, tags: request.tags,
          ...(request.relativePath ? { source: { type: "local_directory", relative_path: request.relativePath } } : {}),
        }),
      });
      if (!initiated.ok) return initiated;
      const upload = record(initiated.data.upload);
      const uploadResponse = await this.fetcher(text(upload.url), {
        method: text(upload.method, "PUT"),
        headers: record(upload.headers) as Record<string, string>,
        body: request.file,
      });
      if (!uploadResponse.ok) return { ok: false, error: { code: "ASSET_UPLOAD_FAILED", message: `对象存储上传失败（${uploadResponse.status}）`, retryable: uploadResponse.status >= 500 } };
      const file = record(initiated.data.file);
      const completed = await this.request<JsonRecord>(`/v1/asset-uploads/${encodeURIComponent(text(initiated.data.id))}/complete`, {
        method: "POST",
        body: JSON.stringify({ object_key: text(file.object_key), sha256: digest, content_type: request.file.type }),
      });
      return completed.ok ? { ok: true, data: undefined } : completed;
    } catch (cause) {
      return { ok: false, error: { code: "ASSET_UPLOAD_FAILED", message: cause instanceof Error ? cause.message : "素材上传失败", retryable: true } };
    }
  }

  async importRemoteAsset(request: RemoteAssetImportRequest): Promise<ApiResult<void>> {
    const result = await this.request<JsonRecord>("/v1/asset-imports", {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey("remote-asset-import") },
      body: JSON.stringify({
        library_id: request.libraryId,
        source_url: request.sourceUrl,
        title: request.title || null,
        description: request.description,
        copyright_status: request.copyrightStatus,
        tags: request.tags,
        rights_confirmed: request.rightsConfirmed,
      }),
    });
    return result.ok ? { ok: true, data: undefined } : result;
  }

  private mapAssetAnalysis(rawValue: unknown): AssetAnalysis | undefined {
    const raw = record(rawValue);
    if (!text(raw.id)) return undefined;
    return {
      id: text(raw.id), version: number(raw.analysis_version, number(raw.version, 1)),
      provider: text(raw.provider), model: text(raw.model), status: text(raw.status, "pending"),
      summary: text(raw.summary), language: text(raw.language) || undefined,
      people: strings(raw.people), organizations: strings(raw.organizations), locations: strings(raw.locations),
      eras: strings(raw.eras), sceneTypes: strings(raw.scene_types), actions: strings(raw.actions),
      moods: strings(raw.moods), visualStyles: strings(raw.visual_styles), keywords: strings(raw.keywords),
      hasEmbeddedText: Boolean(raw.has_embedded_text), hasWatermark: Boolean(raw.has_watermark),
      confidence: typeof raw.confidence === "number" ? raw.confidence : undefined,
      createdAt: text(raw.created_at),
    };
  }

  private mapAsset(raw: JsonRecord): Asset {
    const metadata = record(raw.metadata);
    const files = Array.isArray(raw.files) ? raw.files.map(record) : [];
    const sources = Array.isArray(raw.sources) ? raw.sources.map(record) : [];
    const file = record(raw.file ?? files[0]);
    const source = record(raw.source ?? sources[0] ?? metadata.source);
    const processing = record(raw.processing_job);
    const rawTags = Array.isArray(raw.tags) ? raw.tags : Array.isArray(metadata.tags) ? metadata.tags : [];
    const tags: AssetTag[] = rawTags.map((value) => {
      if (typeof value === "string") return { name: value, source: "manual" };
      const item = record(value);
      return {
        id: text(item.id) || undefined, name: text(item.name, text(item.tag)),
        source: text(item.source, "manual"),
        confidence: typeof item.confidence === "number" ? item.confidence : undefined,
      };
    }).filter((tag) => Boolean(tag.name));
    return {
      id: text(raw.id), revision: number(raw.revision, 1), workspaceId: text(raw.workspace_id), libraryId: text(raw.library_id),
      kind: text(raw.kind, "other") as Asset["kind"], title: text(raw.title), description: text(raw.description),
      status: text(raw.status, "processing") as Asset["status"],
      analysisStatus: text(raw.analysis_status, "pending"),
      copyrightStatus: text(raw.copyright_status, "unknown") as Asset["copyrightStatus"],
      reviewStatus: text(
        raw.review_status,
        text(raw.status) === "ready" ? "approved" : text(raw.status) === "quarantined" ? "rejected" : "pending",
      ) as Asset["reviewStatus"], tags,
      thumbnailUrl: text(raw.thumbnail_url) || undefined,
      posterAvailable: Boolean(text(record(metadata.poster).object_key)),
      file: text(file.id) ? {
        id: text(file.id), originalFilename: text(file.original_filename), mediaType: text(file.media_type),
        byteSize: number(file.byte_size), contentHash: text(file.content_hash), scanStatus: text(file.scan_status, "pending"),
        width: typeof file.width === "number" ? file.width : undefined,
        height: typeof file.height === "number" ? file.height : undefined,
        durationMs: typeof file.duration_ms === "number" ? file.duration_ms : undefined,
      } : undefined,
      source: text(source.id) || text(source.locator) || text(source.canonical_url) || text(source.rights_evidence_locator) || text(source.source_url) ? {
        id: text(source.id) || undefined,
        type: text(source.source_type, text(source.type, "website")),
        locator: text(source.locator, text(source.canonical_url, text(source.rights_evidence_locator, text(source.source_url)))) || undefined,
        provider: text(source.provider, text(source.platform)) || undefined,
        attribution: text(source.attribution, text(source.author)) || undefined,
        license: text(source.license) || undefined, capturedAt: text(source.captured_at) || undefined,
      } : undefined,
      analysis: this.mapAssetAnalysis(raw.latest_analysis ?? raw.analysis),
      processing: text(processing.id) ? {
        jobId: text(processing.id), jobStatus: text(processing.status, "queued"),
        pipelineStatus: text(processing.pipeline_status) || undefined,
        currentStage: text(processing.current_stage) || undefined,
        completedStages: strings(processing.completed_stages),
        attempts: typeof processing.attempts === "number" ? processing.attempts : undefined,
        maxAttempts: typeof processing.max_attempts === "number" ? processing.max_attempts : undefined,
        error: typeof processing.error === "string" ? processing.error : processing.error ? JSON.stringify(processing.error) : text(processing.failure_reason) || undefined,
        updatedAt: text(processing.updated_at) || undefined,
      } : undefined,
      usageHistory: (Array.isArray(raw.usage_history) ? raw.usage_history.map(record) : []).map((item) => ({
        id: text(item.id), runId: text(item.run_id) || undefined, runTopic: text(item.run_topic) || undefined,
        segmentId: text(item.segment_id) || undefined, usedAt: text(item.used_at, text(item.created_at)),
      })),
      reviewHistory: (Array.isArray(raw.review_history) ? raw.review_history.map(record) : []).map((item) => ({
        id: text(item.id), decision: text(item.decision), comment: text(item.comment) || undefined,
        actorName: text(item.actor_name) || undefined, createdAt: text(item.created_at),
      })),
      deletedAt: text(raw.deleted_at) || undefined, createdAt: text(raw.created_at), updatedAt: text(raw.updated_at),
    };
  }

  private mapAssetIngestionJob(raw: JsonRecord): AssetIngestionJob {
    const rawError = raw.error;
    return {
      id: text(raw.id), libraryId: text(raw.library_id), sourceRoot: text(raw.source_root),
      status: text(raw.status, "pending") as AssetIngestionJob["status"],
      discoveredCount: number(raw.discovered_count), importedCount: number(raw.imported_count),
      deduplicatedCount: number(raw.deduplicated_count), taggedCount: number(raw.tagged_count),
      rejectedCount: number(raw.rejected_count),
      error: typeof rawError === "string" ? rawError : rawError ? JSON.stringify(rawError) : undefined,
      startedAt: text(raw.started_at) || undefined, completedAt: text(raw.completed_at) || undefined,
      createdAt: text(raw.created_at), updatedAt: text(raw.updated_at),
    };
  }

  async listAssets(libraryId: string, query: AssetQuery = {}): Promise<ApiResult<AssetPage>> {
    const parameters = new URLSearchParams({ limit: String(query.limit ?? 50) });
    if (query.cursor) parameters.set("cursor", query.cursor);
    if (query.search?.trim()) parameters.set("q", query.search.trim());
    const derivedStatus = query.status ?? (
      query.reviewStatus === "approved" ? "ready" : query.reviewStatus === "rejected" ? "quarantined" : undefined
    );
    if (derivedStatus) parameters.set("status", derivedStatus === "archived" ? "disabled" : derivedStatus);
    if (query.kind) parameters.set("kind", query.kind);
    if (query.copyrightStatus) parameters.set("copyright_status", query.copyrightStatus);
    if (query.tag?.trim()) parameters.set("tag", query.tag.trim());
    const [result, library] = await Promise.all([
      this.request<JsonRecord>(`/v1/asset-libraries/${encodeURIComponent(libraryId)}/assets?${parameters}`),
      this.request<JsonRecord>(`/v1/asset-libraries/${encodeURIComponent(libraryId)}`),
    ]);
    if (!result.ok) return result;
    const page = record(result.data.page);
    const items = Array.isArray(result.data.data) ? result.data.data.map(record) : [];
    return { ok: true, data: {
      data: items.map((item) => this.mapAsset(item)),
      totalCount: library.ok ? number(library.data.asset_count, items.length) : items.length,
      nextCursor: text(page.next_cursor) || undefined,
    } };
  }

  async getAsset(assetId: string): Promise<ApiResult<Asset>> {
    const basePath = `/v1/assets/${encodeURIComponent(assetId)}`;
    const [result, analyses, sources, usage, audits, jobs] = await Promise.all([
      this.request<JsonRecord>(basePath),
      this.request<Page<JsonRecord>>(`${basePath}/analyses?limit=20`),
      this.request<Page<JsonRecord>>(`${basePath}/sources?limit=20`),
      this.request<Page<JsonRecord>>(`${basePath}/usage-records?limit=100`),
      this.request<Page<JsonRecord>>(`${basePath}/audit-events?limit=100`),
      this.request<Page<JsonRecord>>(`${basePath}/analysis-jobs?limit=20`),
    ]);
    if (!result.ok) return result;
    const auditItems = audits.ok ? audits.data.data : [];
    return { ok: true, data: this.mapAsset({
      ...result.data,
      latest_analysis: analyses.ok ? analyses.data.data[0] : undefined,
      sources: sources.ok ? sources.data.data : [],
      usage_history: usage.ok ? usage.data.data : [],
      review_history: auditItems.filter((item) => text(item.action).includes("status")),
      processing_job: jobs.ok ? (
        jobs.data.data.find((item) => ["running", "retry_wait"].includes(text(item.pipeline_status)) || text(item.status) === "running")
        ?? jobs.data.data.find((item) => text(item.status) === "queued")
        ?? jobs.data.data[0]
      ) : undefined,
    }) };
  }

  async updateAsset(assetId: string, revision: number, patch: AssetPatchRequest, key: string): Promise<ApiResult<Asset>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}`, {
      method: "PATCH", headers: { "Idempotency-Key": key, "If-Match": `"${revision}"` },
      body: JSON.stringify({
        ...(patch.title !== undefined ? { title: patch.title } : {}),
        ...(patch.description !== undefined ? { description: patch.description } : {}),
        ...(patch.copyrightStatus !== undefined ? { copyright_status: patch.copyrightStatus } : {}),
        ...(patch.tags !== undefined ? { tags: patch.tags } : {}),
        ...(patch.rightsConfirmed !== undefined ? { rights_confirmed: patch.rightsConfirmed } : {}),
        ...(patch.rightsEvidence !== undefined ? { rights_evidence: patch.rightsEvidence } : {}),
      }),
    });
    return result.ok ? { ok: true, data: this.mapAsset(result.data) } : result;
  }

  async reviewAsset(assetId: string, revision: number, review: AssetReviewRequest, key: string): Promise<ApiResult<Asset>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}/status-transitions`, {
      method: "POST", headers: { "Idempotency-Key": key, "If-Match": `"${revision}"` },
      body: JSON.stringify({
        target_status: review.decision === "approve" ? "ready" : "quarantined",
        reason: review.comment?.trim() || (review.decision === "approve" ? "manual_review_approved" : "manual_review_rejected"),
      }),
    });
    return result.ok ? { ok: true, data: this.mapAsset(result.data) } : result;
  }

  async reanalyzeAsset(assetId: string, revision: number, key: string): Promise<ApiResult<AssetIngestionJob>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}/reanalyze`, {
      method: "POST", headers: { "Idempotency-Key": key, "If-Match": `"${revision}"` },
      body: JSON.stringify({ reason: "manual_reanalysis" }),
    });
    return result.ok ? { ok: true, data: this.mapAssetIngestionJob(result.data) } : result;
  }

  async deleteAsset(assetId: string, revision: number, key: string): Promise<ApiResult<void>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}`, {
      method: "DELETE", headers: { "Idempotency-Key": key, "If-Match": `"${revision}"` },
    });
    return result.ok ? { ok: true, data: undefined } : result;
  }

  async restoreAsset(assetId: string, revision: number, key: string): Promise<ApiResult<Asset>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}/restore`, {
      method: "POST", headers: { "Idempotency-Key": key, "If-Match": `"${revision}"` },
    });
    return result.ok ? { ok: true, data: this.mapAsset(result.data) } : result;
  }

  async bulkUpdateAssets(request: AssetBulkRequest, key: string): Promise<ApiResult<AssetBulkResult>> {
    const body: JsonRecord = {
      items: request.items.map((item) => ({ asset_id: item.assetId, expected_revision: item.revision })),
    };
    let endpoint = "/v1/assets/batch-review";
    if (request.action === "review") {
      body.target_status = request.decision === "approve" ? "ready" : "quarantined";
      body.reason = request.comment?.trim() || `batch_manual_review_${request.decision}`;
    }
    if (request.action === "add_tags") {
      endpoint = "/v1/assets/batch-tags";
      body.add = request.tags;
      body.remove = [];
    }
    if (request.action === "reanalyze") {
      endpoint = "/v1/assets/batch-reanalyze";
      body.reason = "batch_manual_reanalysis";
    }
    if (request.action === "disable") {
      body.target_status = "disabled";
      body.reason = "batch_manual_disable";
    }
    const result = await this.request<JsonRecord>(endpoint, {
      method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(body),
    });
    if (!result.ok) return result;
    if (typeof result.data.succeeded_count !== "number") {
      return { ok: false, error: { code: "API_CONTRACT_ERROR", message: "批量操作响应缺少 succeeded_count" } };
    }
    return { ok: true, data: { acceptedCount: result.data.succeeded_count } };
  }

  async getAssetPreview(assetId: string): Promise<ApiResult<AssetPreview>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}/downloads/preview?expires_in=900`);
    return result.ok ? { ok: true, data: {
      url: text(result.data.url, text(result.data.preview_url)), mediaType: text(result.data.media_type),
      expiresAt: text(result.data.expires_at) || undefined,
      sourceVariant: text(result.data.variant, "preview") === "original" ? "original" : "preview",
      isFallback: Boolean(result.data.fallback),
      byteSize: typeof result.data.byte_size === "number" ? result.data.byte_size : undefined,
    } } : result;
  }

  async getAssetPoster(assetId: string): Promise<ApiResult<AssetPoster>> {
    const result = await this.request<JsonRecord>(`/v1/assets/${encodeURIComponent(assetId)}/downloads/poster?expires_in=900`);
    return result.ok ? { ok: true, data: {
      url: text(result.data.url), mediaType: text(result.data.media_type, "image/jpeg"),
      expiresAt: text(result.data.expires_at) || undefined,
    } } : result;
  }

  async listAssetSegments(assetId: string): Promise<ApiResult<AssetSegment[]>> {
    const items: JsonRecord[] = [];
    let cursor: string | undefined;
    do {
      const result: ApiResult<Page<JsonRecord>> = await this.request<Page<JsonRecord>>(`/v1/assets/${encodeURIComponent(assetId)}/segments?limit=100${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (!result.ok) return result;
      items.push(...result.data.data);
      cursor = result.data.page?.has_more ? result.data.page.next_cursor ?? undefined : undefined;
    } while (cursor);
    return { ok: true, data: items.map((item) => ({
      id: text(item.id), ordinal: number(item.ordinal), startMs: number(item.start_ms), endMs: number(item.end_ms),
      description: text(item.description), people: strings(item.people), locations: strings(item.locations), keywords: strings(item.keywords),
      sceneType: text(item.scene_type) || undefined, action: text(item.action) || undefined, mood: text(item.mood) || undefined,
      shotType: text(item.shot_type) || undefined, confidence: typeof item.confidence === "number" ? item.confidence : undefined,
      representativeFrameUrl: text(item.representative_frame_url) || undefined,
      transcript: text(item.transcript) || undefined,
      boundaryScore: typeof item.boundary_score === "number" ? item.boundary_score : undefined,
      boundaryReasons: strings(item.boundary_reasons),
      cutSafe: Boolean(item.cut_safe),
      semanticComplete: Boolean(item.semantic_complete),
    })) };
  }

  async listAssetIngestionJobs(query: AssetIngestionJobQuery = {}): Promise<ApiResult<AssetIngestionJobPage>> {
    let libraryIds = query.libraryId ? [query.libraryId] : [];
    if (!libraryIds.length) {
      const libraries = await this.page("/v1/asset-libraries");
      if (!libraries.ok) return libraries;
      libraryIds = libraries.data.map((item) => text(item.id)).filter(Boolean);
    }
    const pages = await Promise.all(libraryIds.map((libraryId) => {
      const parameters = new URLSearchParams({ limit: String(query.limit ?? 50) });
      if (query.cursor) parameters.set("cursor", query.cursor);
      return this.request<Page<JsonRecord>>(`/v1/asset-libraries/${encodeURIComponent(libraryId)}/import-jobs?${parameters}`);
    }));
    const failure = pages.find((result) => !result.ok);
    if (failure && !failure.ok) return failure;
    const jobs = pages.flatMap((result) => result.ok ? result.data.data : [])
      .filter((item) => !query.status || text(item.status) === query.status)
      .slice(0, query.limit ?? 50);
    return { ok: true, data: {
      data: jobs.map((item) => this.mapAssetIngestionJob(item)),
      nextCursor: undefined,
    } };
  }

  async getComposerOptions(workspaceId: string): Promise<ApiResult<ComposerOptions>> {
    const [skillsResult, versionsResult, librariesResult, channelsResult] = await Promise.all([
      this.page("/v1/skills"),
      this.page("/v1/skill-versions"),
      this.page("/v1/asset-libraries"),
      this.listChannels({ workspaceId, status: "active" }),
    ]);
    if (!skillsResult.ok) return skillsResult;
    if (!versionsResult.ok) return versionsResult;
    if (!librariesResult.ok) return librariesResult;
    const skills = new Map(skillsResult.data.map((item) => [text(item.id), item]));
    const versions = versionsResult.data.filter((item) => {
      const parent = skills.get(text(item.skill_id));
      return text(item.state) === "published"
        && text(parent?.status) === "active"
        && text(parent?.current_version_id) === text(item.id);
    });
    const pipelineIds = [...new Set(versions.map((item) => text(item.default_pipeline_version_id)).filter(Boolean))];
    return { ok: true, data: {
      channels: channelsResult.ok
        ? channelsResult.data.filter((channel) => channel.status === "active")
        : [],
      channelProblem: channelsResult.ok ? undefined : channelsResult.error,
      assetLibraries: librariesResult.data.map((item) => ({
        id: text(item.id), workspaceId: text(item.workspace_id), name: text(item.name),
        description: text(item.description), assetCount: number(item.asset_count),
        readyAssetCount: number(item.ready_asset_count),
        quarantinedAssetCount: optionalNumber(item.quarantined_asset_count),
        processingAssetCount: optionalNumber(item.processing_asset_count),
        taggedAssetCount: optionalNumber(item.tagged_asset_count),
        copyrightCompleteAssetCount: optionalNumber(item.copyright_complete_asset_count),
      })),
      voiceProfiles: [], renderPresets: [],
      skills: versions.map((item) => { const skill = skills.get(text(item.skill_id)) ?? {}; const system = text(skill.publisher_type) === "system"; return { skillId: text(item.skill_id), skillName: text(skill.name), versionId: text(item.id), version: text(item.version), defaultPipelineVersionId: text(item.default_pipeline_version_id) || undefined, publisher: { id: system ? "system" : text(skill.workspace_id), workspaceId: text(skill.workspace_id), type: system ? "system" : "workspace", displayName: text(skill.publisher_name), verified: system } }; }),
      pipelines: pipelineIds.map((id) => ({ id, workspaceId: "", pipelineId: id, name: "Skill 默认 Pipeline", version: "1.0.0", capabilities: [] })),
    } };
  }

  async createDocumentVideoRun(
    request: DocumentVideoCreateRequest,
    key: string,
  ): Promise<ApiResult<DocumentVideoCreateResult>> {
    if (
      !request.file.name.toLowerCase().endsWith(".pdf")
      || (request.file.type && request.file.type !== "application/pdf")
      || request.file.size <= 0
      || request.file.size > 200 * 1024 * 1024
    ) {
      return {
        ok: false,
        error: {
          code: "DOCUMENT_SOURCE_INVALID",
          message: "仅接受不超过 200 MiB 的 PDF 文件。",
          retryable: false,
        },
      };
    }
    try {
      const digest = await sha256Hex(request.file);
      const initiated = await this.request<JsonRecord>("/v1/document-sources", {
        method: "POST",
        headers: { "Idempotency-Key": `${key}:source` },
        body: JSON.stringify({
          filename: request.file.name,
          content_type: "application/pdf",
          byte_size: request.file.size,
          sha256: digest,
          rights_confirmed: true,
        }),
      });
      if (!initiated.ok) return initiated;
      const upload = record(initiated.data.upload);
      const objectKey = text(initiated.data.object_key);
      if (!text(upload.url) || !objectKey) {
        return {
          ok: false,
          error: {
            code: "DOCUMENT_UPLOAD_SESSION_UNAVAILABLE",
            message: "上传会话不可用或已过期，请重新选择文件并提交。",
            retryable: false,
          },
        };
      }
      const uploaded = await this.fetcher(text(upload.url), {
        method: text(upload.method, "PUT"),
        headers: record(upload.headers) as Record<string, string>,
        body: request.file,
      });
      if (!uploaded.ok) {
        return {
          ok: false,
          error: {
            code: "DOCUMENT_UPLOAD_FAILED",
            message: `PDF 上传失败（${uploaded.status}）。`,
            retryable: uploaded.status >= 500,
          },
        };
      }
      const sourceId = text(initiated.data.id);
      const completed = await this.request<JsonRecord>(
        `/v1/document-sources/${encodeURIComponent(sourceId)}/complete`,
        {
          method: "POST",
          body: JSON.stringify({
            object_key: objectKey,
            sha256: digest,
            content_type: "application/pdf",
          }),
        },
      );
      if (!completed.ok) return completed;
      const run = await this.request<JsonRecord>("/v1/document-video/runs", {
        method: "POST",
        headers: { "Idempotency-Key": `${key}:run` },
        body: JSON.stringify({
          source_id: sourceId,
          topic: request.topic,
          duration_seconds: request.durationSeconds,
          aspect_ratio: request.aspectRatio,
          generated_background_enabled: request.generatedBackgroundEnabled,
        }),
      });
      return run.ok
        ? { ok: true, data: { sourceId, runId: text(run.data.id) } }
        : run;
    } catch (cause) {
      return {
        ok: false,
        error: {
          code: "DOCUMENT_VIDEO_SUBMISSION_FAILED",
          message: cause instanceof Error ? cause.message : "文件讲解任务提交失败。",
          retryable: true,
        },
      };
    }
  }

  private mapFullAiBlockers(value: unknown): FullAiBlocker[] {
    return (Array.isArray(value) ? value : []).map((item) => {
      const blocker = record(item);
      return {
        code: text(blocker.code, "FULL_AI_BLOCKED"),
        message: text(blocker.message, "全 AI 生成当前不可用。"),
        retryable: Boolean(blocker.retryable),
      };
    });
  }

  private fullAiSpecPayload(spec: FullAiSpec): JsonRecord {
    return {
      brief: spec.brief,
      direction: spec.direction,
      aspect_ratio: spec.aspectRatio,
      duration_seconds: spec.durationSeconds,
      variants_per_scene: spec.variantsPerScene,
      continuity: spec.continuity,
      ai_disclosure: spec.aiDisclosure,
    };
  }

  private mapFullAiSpec(raw: JsonRecord): FullAiSpec {
    return {
      brief: text(raw.brief),
      direction: text(raw.direction),
      aspectRatio: text(raw.aspect_ratio),
      durationSeconds: number(raw.duration_seconds),
      variantsPerScene: number(raw.variants_per_scene),
      continuity: Boolean(raw.continuity),
      aiDisclosure: Boolean(raw.ai_disclosure),
    };
  }

  async getFullAiOptions(): Promise<ApiResult<FullAiOptions>> {
    const result = await this.request<JsonRecord>("/v1/full-ai/options");
    if (!result.ok) return result;
    const pipeline = record(result.data.pipeline);
    const provider = record(result.data.provider);
    const limits = record(result.data.limits);
    const providerName = text(provider.name);
    const modelId = text(provider.model_id);
    return { ok: true, data: {
      schemaVersion: text(result.data.schema_version, "1.0.0"),
      mode: "generated_only",
      status: text(result.data.status, "blocked") === "ready" ? "ready" : "blocked",
      pipeline: {
        slug: text(pipeline.slug),
        version: number(pipeline.version),
        visualSourceMode: "generated_only",
      },
      provider: {
        name: providerName || undefined,
        modelId: modelId || undefined,
        status: (["ready", "unconfigured", "incomplete"].includes(text(provider.status))
          ? text(provider.status)
          : "unconfigured") as FullAiOptions["provider"]["status"],
        supportsReconciliation: Boolean(provider.supports_reconciliation),
        submitUnknownPolicy: "manual_only",
        continuityModes: strings(provider.continuity_modes),
      },
      limits: {
        briefMaxLength: number(limits.brief_max_length),
        durationSeconds: (Array.isArray(limits.duration_seconds) ? limits.duration_seconds : []).filter((item): item is number => typeof item === "number"),
        aspectRatios: strings(limits.aspect_ratios),
        directions: strings(limits.directions),
        variantsPerScene: (Array.isArray(limits.variants_per_scene) ? limits.variants_per_scene : []).filter((item): item is number => typeof item === "number"),
        clipSeconds: number(limits.clip_seconds),
      },
      blockers: this.mapFullAiBlockers(result.data.blockers),
    } };
  }

  async estimateFullAiRun(spec: FullAiSpec): Promise<ApiResult<FullAiEstimate>> {
    const result = await this.request<JsonRecord>("/v1/full-ai/estimate", {
      method: "POST",
      body: JSON.stringify(this.fullAiSpecPayload(spec)),
    });
    if (!result.ok) return result;
    const plan = record(result.data.plan);
    const rawQuote = record(result.data.quote);
    const quote = typeof rawQuote.amount_minor === "number" && text(rawQuote.currency)
      ? {
          currency: text(rawQuote.currency) as "CNY" | "USD",
          amountMinor: number(rawQuote.amount_minor),
          expiresAt: text(rawQuote.expires_at),
        }
      : undefined;
    return { ok: true, data: {
      schemaVersion: text(result.data.schema_version, "1.0.0"),
      status: text(result.data.status, "blocked") === "ready" ? "ready" : "blocked",
      requestFingerprint: text(result.data.request_fingerprint),
      plan: {
        sceneCount: number(plan.scene_count),
        clipSeconds: number(plan.clip_seconds),
        candidateCount: number(plan.candidate_count),
        billableSeconds: number(plan.billable_seconds),
      },
      quote,
      blockers: this.mapFullAiBlockers(result.data.blockers),
    } };
  }

  private mapFullAiRun(raw: JsonRecord): FullAiRun {
    const provider = record(raw.provider);
    const quote = record(raw.quote);
    const billing = record(raw.billing);
    const providerName = text(provider.name);
    const modelId = text(provider.model_id);
    return {
      schemaVersion: text(raw.schema_version, "1.0.0"),
      id: text(raw.id),
      projectRunId: text(raw.project_run_id),
      workspaceId: text(raw.workspace_id),
      status: text(raw.status),
      mode: "generated_only",
      provider: {
        name: providerName || undefined,
        modelId: modelId || undefined,
      },
      spec: this.mapFullAiSpec(record(raw.spec)),
      quote: {
        currency: text(quote.currency) as "CNY" | "USD",
        amountMinor: number(quote.amount_minor),
        expiresAt: text(quote.expires_at),
      },
      billing: {
        status: text(billing.status),
        authorizedAmountMinor: number(billing.authorized_amount_minor),
        incurredAmountMinor: number(billing.incurred_amount_minor),
        requiresReconciliation: Boolean(billing.requires_reconciliation),
      },
      createdAt: text(raw.created_at),
      updatedAt: text(raw.updated_at),
    };
  }

  async createFullAiRun(
    request: FullAiRunCreateRequest,
    key: string,
  ): Promise<ApiResult<FullAiRun>> {
    const result = await this.request<JsonRecord>("/v1/full-ai/runs", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        ...this.fullAiSpecPayload(request),
        estimate_fingerprint: request.estimateFingerprint,
        max_cost_minor: request.maxCostMinor,
        currency: request.currency,
      }),
    });
    return result.ok ? { ok: true, data: this.mapFullAiRun(result.data) } : result;
  }

  async getFullAiRun(runId: string): Promise<ApiResult<FullAiRun>> {
    const result = await this.request<JsonRecord>(`/v1/full-ai/runs/${encodeURIComponent(runId)}`);
    return result.ok ? { ok: true, data: this.mapFullAiRun(result.data) } : result;
  }

  private mapWebpageVideoBlockers(value: unknown): WebpageVideoBlocker[] {
    return records(value).map((blocker) => ({
      code: text(blocker.code, "WEBPAGE_VIDEO_BLOCKED"),
      message: text(blocker.message, "网页截图成片服务当前不可用。"),
      retryable: Boolean(blocker.retryable),
    }));
  }

  private mapWebpageVideoOptions(raw: JsonRecord): WebpageVideoOptions {
    const source = nonEmptyRecord(raw.options) ?? raw;
    const limits = nonEmptyRecord(source.limits) ?? source;
    const pipeline = record(source.pipeline);
    const allowedAspectRatios = new Set<WebpageVideoAspectRatio>(["16:9", "9:16", "1:1", "4:3"]);
    const explicitRatios = strings(limits.aspect_ratios ?? source.aspect_ratios);
    const viewportRatios = records(limits.viewports ?? source.viewports).map((viewport) => text(viewport.aspect_ratio));
    const aspectRatios = [...new Set([...explicitRatios, ...viewportRatios])]
      .filter((item): item is WebpageVideoAspectRatio => allowedAspectRatios.has(item as WebpageVideoAspectRatio));
    const rawDurations = limits.duration_seconds ?? source.duration_seconds;
    const durationSeconds = (Array.isArray(rawDurations) ? rawDurations : [])
      .filter((item): item is number => typeof item === "number" && Number.isFinite(item) && item > 0);
    const rawVoices = Array.isArray(source.voice_profiles)
      ? source.voice_profiles
      : Array.isArray(source.voices)
        ? source.voices
        : [];
    const voices = rawVoices.flatMap((item) => {
      if (typeof item === "string" && item) return [{ id: item, name: item }];
      const voice = record(item);
      const id = text(voice.id) || text(voice.voice_profile_id) || text(voice.value);
      if (!id) return [];
      const language = text(voice.language) || text(voice.locale);
      const description = text(voice.description);
      return [{
        id,
        name: text(voice.name) || text(voice.label) || id,
        language: language || undefined,
        description: description || undefined,
      }];
    });
    const subtitles = record(source.subtitles);
    const subtitleModes = strings(source.subtitle_modes ?? limits.subtitle_modes);
    const subtitleSupported = typeof subtitles.supported === "boolean"
      ? subtitles.supported
      : typeof source.subtitles_supported === "boolean"
        ? source.subtitles_supported
        : subtitleModes.length > 0;
    const defaultSubtitles = typeof subtitles.default_enabled === "boolean"
      ? subtitles.default_enabled
      : typeof source.default_subtitles_enabled === "boolean"
        ? source.default_subtitles_enabled
        : subtitleSupported;
    const pipelineSlug = text(pipeline.slug);
    return {
      schemaVersion: text(source.schema_version, "1.0.0"),
      status: text(source.status, "blocked") === "ready" ? "ready" : "blocked",
      pipeline: pipelineSlug ? { slug: pipelineSlug, version: number(pipeline.version, 1) } : undefined,
      limits: {
        urlMaxLength: number(limits.url_max_length ?? limits.target_url_max_length ?? source.target_url_max_length, 2048),
        topicMaxLength: number(limits.topic_max_length ?? limits.brief_max_length, 1600),
        aspectRatios,
        durationSeconds,
        crawlMaxPagesDefault: number(limits.crawl_max_pages_default, 8),
        crawlMaxPagesLimit: number(limits.crawl_max_pages_limit, 12),
        crawlMaxDepthDefault: number(limits.crawl_max_depth_default, 1),
        crawlMaxDepthLimit: number(limits.crawl_max_depth_limit, 2),
      },
      voices,
      subtitles: { supported: subtitleSupported, defaultEnabled: subtitleSupported && defaultSubtitles },
      blockers: this.mapWebpageVideoBlockers(source.blockers),
    };
  }

  async getWebpageVideoOptions(): Promise<ApiResult<WebpageVideoOptions>> {
    const result = await this.request<JsonRecord>("/v1/webpage-video/options");
    return result.ok ? { ok: true, data: this.mapWebpageVideoOptions(result.data) } : result;
  }

  private webpageVideoCreatePayload(request: WebpageVideoRunCreateRequest): JsonRecord {
    return {
      target_url: request.targetUrl,
      capture: {
        mode: "viewport",
        aspect_ratio: request.aspectRatio,
        full_page: false,
      },
      video: {
        topic: request.topic,
        duration_seconds: request.durationSeconds,
        subtitles_enabled: request.subtitlesEnabled,
        voice_profile_id: request.voiceProfileId ?? null,
      },
      rights: {
        public_page_confirmed: request.publicPageConfirmed,
        rights_confirmed: request.rightsConfirmed,
      },
      crawl: {
        max_pages: request.crawl.maxPages,
        max_depth: request.crawl.maxDepth,
        same_origin_only: true,
        include_sitemap: request.crawl.includeSitemap,
      },
    };
  }

  private mapWebpageVideoCapture(raw: JsonRecord): WebpageVideoCapture {
    const source = nonEmptyRecord(raw.capture) ?? raw;
    const screenshot = nonEmptyRecord(source.screenshot) ?? source;
    const artifact = nonEmptyRecord(screenshot.artifact) ?? screenshot;
    const viewport = nonEmptyRecord(source.viewport) ?? nonEmptyRecord(screenshot.viewport);
    const previewUrl = text(screenshot.preview_url)
      || text(screenshot.signed_preview_url)
      || text(screenshot.signed_url)
      || text(screenshot.url)
      || text(artifact.preview_url)
      || text(artifact.download_url);
    const requestedUrl = text(source.requested_url)
      || text(source.target_url)
      || text(raw.requested_url)
      || text(raw.target_url);
    const finalUrl = text(source.final_url) || text(screenshot.final_url) || text(raw.final_url);
    const sha256 = text(screenshot.sha256)
      || text(screenshot.content_hash)
      || text(artifact.sha256)
      || text(artifact.content_hash)
      || text(source.sha256)
      || text(raw.capture_sha256);
    const width = optionalNumber(screenshot.width ?? artifact.width ?? viewport?.width);
    const height = optionalNumber(screenshot.height ?? artifact.height ?? viewport?.height);
    const capturedAt = text(source.captured_at) || text(screenshot.captured_at);
    const expiresAt = text(screenshot.url_expires_at)
      || text(artifact.url_expires_at)
      || text(screenshot.expires_at)
      || text(artifact.expires_at);
    const review = nonEmptyRecord(source.review) ?? nonEmptyRecord(screenshot.review);
    return {
      previewUrl: previewUrl || undefined,
      sha256,
      requestedUrl,
      finalUrl: finalUrl || undefined,
      revision: number(
        screenshot.capture_revision,
        number(source.capture_revision, number(raw.capture_revision, number(screenshot.revision, number(source.revision, number(raw.revision))))),
      ),
      width,
      height,
      capturedAt: capturedAt || undefined,
      expiresAt: expiresAt || undefined,
      review: review ? {
        decision: text(review.decision) as "approve" | "request_changes" | "reject",
        comment: text(review.comment) || undefined,
        issueCodes: strings(review.issue_codes),
        reviewedRevision: number(review.reviewed_revision),
        decidedAt: text(review.decided_at) || undefined,
      } : undefined,
    };
  }

  private mapWebpageVideoMedia(raw: JsonRecord): WebpageVideoMedia | undefined {
    const result = record(raw.result);
    const output = record(raw.output);
    const source = nonEmptyRecord(raw.final_video)
      ?? nonEmptyRecord(result.final_video)
      ?? nonEmptyRecord(output.final_video)
      ?? (text(raw.final_video_url) || text(raw.video_url) || text(raw.output_url) ? raw : undefined);
    if (!source) return undefined;
    const previewUrl = text(source.preview_url) || text(source.signed_preview_url) || text(source.signed_url) || text(source.url)
      || text(source.video_url) || text(source.final_video_url) || text(source.output_url);
    const downloadUrl = text(source.download_url) || text(source.signed_download_url) || previewUrl;
    if (!previewUrl && !downloadUrl) return undefined;
    const mediaType = text(source.media_type) || text(source.content_type);
    const filename = text(source.filename);
    return {
      previewUrl: previewUrl || undefined,
      downloadUrl: downloadUrl || undefined,
      captionsUrl: (text(source.captions_url) || text(source.subtitle_url) || undefined),
      mediaType: mediaType || undefined,
      filename: filename || undefined,
      sha256: text(source.sha256) || text(source.content_hash) || undefined,
      byteSize: optionalNumber(source.byte_size),
    };
  }

  private mapWebpageVideoPilotFeedback(raw: JsonRecord): WebpageVideoPilotFeedback {
    return {
      id: text(raw.id),
      webpageVideoRunId: text(raw.webpage_video_run_id),
      customerSegment: text(raw.customer_segment),
      baselineMinutes: number(raw.baseline_minutes),
      assistedMinutes: number(raw.assisted_minutes),
      savedMinutes: number(raw.saved_minutes),
      timeReductionPercent: number(raw.time_reduction_percent),
      revisionCount: number(raw.revision_count),
      outcome: text(raw.outcome, "evaluating") as WebpageVideoPilotFeedback["outcome"],
      satisfactionScore: optionalNumber(raw.satisfaction_score),
      willingnessToPayHkd: optionalNumber(raw.willingness_to_pay_hkd),
      notes: text(raw.notes) || undefined,
      revision: number(raw.revision),
      createdAt: text(raw.created_at),
      updatedAt: text(raw.updated_at),
    };
  }

  private mapWebpageVideoPilotSummary(raw: JsonRecord): WebpageVideoPilotSummary {
    return {
      schemaVersion: text(raw.schema_version, "1.0.0"),
      generatedAt: text(raw.generated_at),
      totalRecords: number(raw.total_records),
      includedRecords: number(raw.included_records),
      truncated: Boolean(raw.truncated),
      recommendedMinimumPilots: number(raw.recommended_minimum_pilots, 3),
      pilotTargetMet: Boolean(raw.pilot_target_met),
      adoptedCount: number(raw.adopted_count),
      evaluatingCount: number(raw.evaluating_count),
      rejectedCount: number(raw.rejected_count),
      baselineMinutesTotal: number(raw.baseline_minutes_total),
      assistedMinutesTotal: number(raw.assisted_minutes_total),
      savedMinutesTotal: number(raw.saved_minutes_total),
      timeReductionPercent: optionalNumber(raw.time_reduction_percent),
      averageSatisfactionScore: optionalNumber(raw.average_satisfaction_score),
      satisfactionResponseCount: number(raw.satisfaction_response_count),
      averageWillingnessToPayHkd: optionalNumber(raw.average_willingness_to_pay_hkd),
      willingnessToPayResponseCount: number(raw.willingness_to_pay_response_count),
      segments: records(raw.segments).map((segment) => {
        return {
          customerSegment: text(segment.customer_segment),
          pilotCount: number(segment.pilot_count),
          adoptedCount: number(segment.adopted_count),
          savedMinutes: number(segment.saved_minutes),
          averageTimeReductionPercent: number(segment.average_time_reduction_percent),
        };
      }),
      items: records(raw.items).map((pilot) => {
        return {
          webpageVideoRunId: text(pilot.webpage_video_run_id),
          customerSegment: text(pilot.customer_segment),
          baselineMinutes: number(pilot.baseline_minutes),
          assistedMinutes: number(pilot.assisted_minutes),
          savedMinutes: number(pilot.saved_minutes),
          timeReductionPercent: number(pilot.time_reduction_percent),
          revisionCount: number(pilot.revision_count),
          outcome: text(pilot.outcome, "evaluating") as WebpageVideoPilotFeedback["outcome"],
          satisfactionScore: optionalNumber(pilot.satisfaction_score),
          willingnessToPayHkd: optionalNumber(pilot.willingness_to_pay_hkd),
          updatedAt: text(pilot.updated_at),
        };
      }),
    };
  }

  private mapWebpageVideoRun(raw: JsonRecord): WebpageVideoRun {
    const source = nonEmptyRecord(raw.run) ?? raw;
    const request = record(source.request);
    const spec = nonEmptyRecord(source.spec) ?? request;
    const video = nonEmptyRecord(source.video) ?? nonEmptyRecord(spec.video) ?? spec;
    const rawCapture = nonEmptyRecord(source.capture) ?? nonEmptyRecord(source.screenshot);
    const mappedCapture = rawCapture ? this.mapWebpageVideoCapture({ ...source, capture: rawCapture }) : undefined;
    const targetUrl = text(source.target_url)
      || text(source.requested_url)
      || text(request.target_url)
      || text(spec.target_url)
      || mappedCapture?.requestedUrl
      || "";
    const finalUrl = text(source.final_url) || mappedCapture?.finalUrl || "";
    const captureSha256 = text(source.capture_sha256)
      || text(source.screenshot_sha256)
      || mappedCapture?.sha256
      || "";
    const failure = nonEmptyRecord(source.failure) ?? nonEmptyRecord(source.error);
    const failureMessage = failure ? text(failure.message) || text(failure.detail) : "";
    const voiceProfileId = text(video.voice_profile_id) || text(video.voice_id);
    const rawStatus = text(source.status) || text(source.state) || "queued";
    const pilotFeedback = nonEmptyRecord(source.pilot_feedback);
    return {
      schemaVersion: text(source.schema_version, "1.0.0"),
      id: text(source.id) || text(source.webpage_video_run_id),
      projectRunId: (text(source.project_run_id) || text(source.underlying_run_id) || undefined),
      workspaceId: (text(source.workspace_id) || undefined),
      status: normalizeWebpageVideoStatus(rawStatus),
      rawStatus,
      revision: mappedCapture?.revision || number(source.capture_revision, number(source.revision)),
      targetUrl,
      finalUrl: finalUrl || undefined,
      captureSha256: captureSha256 || undefined,
      spec: {
        topic: text(video.topic) || text(video.brief) || text(spec.topic) || text(spec.page_purpose),
        aspectRatio: text(video.aspect_ratio) || text(record(spec.capture).aspect_ratio) || text(spec.aspect_ratio),
        durationSeconds: number(video.duration_seconds, number(spec.duration_seconds)),
        subtitlesEnabled: typeof video.subtitles_enabled === "boolean" ? video.subtitles_enabled : Boolean(video.subtitles),
        voiceProfileId: voiceProfileId || undefined,
      },
      capture: mappedCapture,
      siteMode: Boolean(
        source.site_mode
        || text(source.mode) === "site"
        || Object.keys(record(request.crawl)).length
        || Object.keys(record(spec.crawl)).length
        || ["discovering", "discovering_pages", "awaiting_scope_review", "capturing_pages", "analyzing_regions", "planning_storyboard", "awaiting_storyboard_review"].includes(rawStatus)
      ),
      finalVideo: this.mapWebpageVideoMedia(source),
      pilotFeedback: pilotFeedback ? this.mapWebpageVideoPilotFeedback(pilotFeedback) : undefined,
      failure: failureMessage ? {
        code: text(failure?.code) || undefined,
        message: failureMessage,
        retryable: Boolean(failure?.retryable),
      } : undefined,
      createdAt: text(source.created_at) || undefined,
      updatedAt: text(source.updated_at) || undefined,
    };
  }

  async createWebpageVideoRun(
    request: WebpageVideoRunCreateRequest,
    key: string,
  ): Promise<ApiResult<WebpageVideoRun>> {
    const result = await this.request<JsonRecord>("/v1/webpage-video/runs", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify(this.webpageVideoCreatePayload(request)),
    });
    return result.ok ? { ok: true, data: this.mapWebpageVideoRun(result.data) } : result;
  }

  async getWebpageVideoPilotSummary(): Promise<ApiResult<WebpageVideoPilotSummary>> {
    const result = await this.request<JsonRecord>("/v1/webpage-video/pilot-summary");
    return result.ok ? { ok: true, data: this.mapWebpageVideoPilotSummary(result.data) } : result;
  }

  async getWebpageVideoRun(runId: string): Promise<ApiResult<WebpageVideoRun>> {
    const result = await this.request<JsonRecord>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}`);
    return result.ok ? { ok: true, data: this.mapWebpageVideoRun(result.data) } : result;
  }

  async getWebpageVideoCapture(runId: string): Promise<ApiResult<WebpageVideoCapture>> {
    const result = await this.request<JsonRecord>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/capture`);
    return result.ok ? { ok: true, data: this.mapWebpageVideoCapture(result.data) } : result;
  }

  private mapWebpageVideoSitePage(raw: JsonRecord): WebpageVideoSitePage {
    const rawCapture = nonEmptyRecord(raw.capture) ?? nonEmptyRecord(raw.screenshot);
    const rawRegions = Array.isArray(raw.regions) ? raw.regions : Array.isArray(raw.key_regions) ? raw.key_regions : [];
    const failure = nonEmptyRecord(raw.failure) ?? nonEmptyRecord(raw.error);
    const failureMessage = failure ? text(failure.message) || text(failure.detail) : "";
    return {
      id: text(raw.id) || text(raw.page_id),
      url: text(raw.url) || text(raw.requested_url),
      finalUrl: text(raw.final_url) || text(raw.canonical_url) || undefined,
      title: text(raw.title) || undefined,
      pageType: text(raw.page_type) || text(raw.type) || undefined,
      reason: text(raw.reason) || text(raw.selection_reason) || undefined,
      score: optionalNumber(raw.score ?? raw.relevance_score),
      selected: typeof raw.selected === "boolean" ? raw.selected : true,
      status: text(raw.status) || undefined,
      capture: rawCapture ? this.mapWebpageVideoCapture({ capture: { ...rawCapture, requested_url: text(raw.url), final_url: text(raw.canonical_url) || text(raw.url) } }) : undefined,
      regions: rawRegions.map((value, index) => {
        const region = record(value);
        const artifact = nonEmptyRecord(region.artifact) ?? region;
        return {
          id: text(region.id) || text(region.region_id) || `region-${index + 1}`,
          type: text(region.type) || text(region.region_type) || text(region.kind) || "region",
          label: text(region.label) || text(region.title) || text(region.kind) || `关键区域 ${index + 1}`,
          reason: text(region.reason) || text(region.selection_reason) || undefined,
          score: optionalNumber(region.score),
          previewUrl: text(region.preview_url) || text(region.signed_url) || text(artifact.preview_url) || text(artifact.download_url) || undefined,
          sha256: text(region.sha256) || text(artifact.sha256) || undefined,
          width: optionalNumber(region.width ?? artifact.width),
          height: optionalNumber(region.height ?? artifact.height),
        };
      }),
      failure: failureMessage ? { code: text(failure?.code) || undefined, message: failureMessage, retryable: Boolean(failure?.retryable) } : undefined,
    };
  }

  private mapWebpageVideoStoryboardShot(raw: JsonRecord, index: number): WebpageVideoStoryboardShot {
    const artifact = nonEmptyRecord(raw.artifact) ?? raw;
    return {
      id: text(raw.id) || text(raw.shot_id) || `shot-${index + 1}`,
      pageId: text(raw.page_id),
      regionId: text(raw.region_id) || undefined,
      label: text(raw.label) || text(raw.title) || text(raw.narration_cue) || `镜头 ${index + 1}`,
      reason: text(raw.reason) || text(raw.selection_reason) || text(raw.narration_cue) || undefined,
      previewUrl: text(raw.preview_url) || text(raw.signed_url) || text(artifact.preview_url) || text(artifact.download_url) || undefined,
      durationSeconds: optionalNumber(raw.duration_seconds),
      motion: (["static", "zoom_in", "zoom_out", "pan"].includes(text(raw.motion))
        ? text(raw.motion)
        : "zoom_in") as WebpageVideoStoryboardShot["motion"],
      transition: (["cut", "fade_black"].includes(text(raw.transition))
        ? text(raw.transition)
        : index === 0 ? "cut" : "fade_black") as WebpageVideoStoryboardShot["transition"],
      enabled: typeof raw.enabled === "boolean" ? raw.enabled : true,
      order: number(raw.order, number(raw.ordinal, index + 1) - 1),
    };
  }

  private mapWebpageVideoSite(raw: JsonRecord): WebpageVideoSitePlan {
    const source = nonEmptyRecord(raw.site) ?? raw;
    const scope = nonEmptyRecord(source.scope) ?? nonEmptyRecord(source.page_scope) ?? {};
    const scopeContent = nonEmptyRecord(scope.content) ?? scope;
    const storyboard = nonEmptyRecord(source.storyboard);
    const storyboardContent = nonEmptyRecord(storyboard?.content) ?? storyboard;
    const rawPages = Array.isArray(scopeContent.pages) ? scopeContent.pages : Array.isArray(source.pages) ? source.pages : [];
    const rawShots = Array.isArray(storyboardContent?.shots) ? storyboardContent.shots : [];
    const storyboardPages = Array.isArray(storyboardContent?.pages) ? storyboardContent.pages : [];
    const enrichedPages = new Map(rawPages.map((value) => {
      const page = record(value);
      return [text(page.id) || text(page.page_id), page];
    }));
    for (const value of storyboardPages) {
      const page = record(value);
      const id = text(page.id) || text(page.page_id);
      enrichedPages.set(id, { ...enrichedPages.get(id), ...page });
    }
    const scopeHash = text(scope.sha256) || text(scope.hash) || text(source.scope_sha256);
    return {
      schemaVersion: text(source.schema_version, "2.0.0"),
      mode: "site",
      scope: {
        status: text(scope.status) || text(source.scope_status) || "waiting",
        revision: number(scope.revision, number(scope.scope_revision, number(source.scope_revision))),
        sha256: scopeHash,
        pages: [...enrichedPages.values()].map((value) => this.mapWebpageVideoSitePage(value)),
      },
      storyboard: storyboard ? {
        status: text(storyboard.status) || text(source.storyboard_status) || "waiting",
        revision: number(storyboard.revision, number(storyboard.storyboard_revision, number(source.storyboard_revision))),
        sha256: text(storyboard.sha256) || text(storyboard.hash) || text(source.storyboard_sha256),
        shots: rawShots.map((value, index) => this.mapWebpageVideoStoryboardShot(record(value), index)),
      } : undefined,
    };
  }

  async getWebpageVideoSite(runId: string): Promise<ApiResult<WebpageVideoSitePlan>> {
    const result = await this.request<JsonRecord>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/site`);
    return result.ok ? { ok: true, data: this.mapWebpageVideoSite(result.data) } : result;
  }

  async reviewWebpageVideoRun(
    runId: string,
    review: WebpageVideoReviewRequest,
    key: string,
  ): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/capture/review`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        decision: review.decision,
        ...(review.comment ? { comment: review.comment } : {}),
        expected_revision: review.expectedRevision,
        expected_sha256: review.expectedSha256,
      }),
    });
  }

  async reviewWebpageVideoScope(runId: string, review: WebpageVideoScopeReviewRequest, key: string): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/scope/review`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        decision: review.decision,
        ...(review.comment ? { comment: review.comment } : {}),
        expected_revision: review.expectedRevision,
        expected_sha256: review.expectedSha256,
        selected_page_ids: review.selectedPageIds,
      }),
    });
  }

  async reviewWebpageVideoStoryboard(runId: string, review: WebpageVideoStoryboardReviewRequest, key: string): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/storyboard/review`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        decision: review.decision,
        ...(review.comment ? { comment: review.comment } : {}),
        expected_revision: review.expectedRevision,
        expected_sha256: review.expectedSha256,
        shots: review.shots.map((shot) => ({
          id: shot.id,
          enabled: shot.enabled,
          order: shot.order,
          ...(shot.motion ? { motion: shot.motion } : {}),
          ...(shot.transition ? { transition: shot.transition } : {}),
        })),
      }),
    });
  }

  async saveWebpageVideoPilotFeedback(
    runId: string,
    feedback: WebpageVideoPilotFeedbackSaveRequest,
    key: string,
  ): Promise<ApiResult<WebpageVideoPilotFeedback>> {
    const result = await this.request<JsonRecord>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/pilot-feedback`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        customer_segment: feedback.customerSegment,
        baseline_minutes: feedback.baselineMinutes,
        assisted_minutes: feedback.assistedMinutes,
        revision_count: feedback.revisionCount,
        outcome: feedback.outcome,
        satisfaction_score: feedback.satisfactionScore ?? null,
        willingness_to_pay_hkd: feedback.willingnessToPayHkd ?? null,
        notes: feedback.notes || null,
        expected_revision: feedback.expectedRevision,
      }),
    });
    return result.ok ? { ok: true, data: this.mapWebpageVideoPilotFeedback(result.data) } : result;
  }

  async cancelWebpageVideoRun(runId: string, key: string): Promise<ApiResult<void>> {
    return this.request<void>(`/v1/webpage-video/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
    });
  }

  private runPayload(draft: RunDraft): JsonRecord {
    return {
      workspace_id: draft.workspaceId,
      channel_id: draft.channelId ?? null,
      input: {
        topic: draft.topic,
        ...(draft.researchMode ? { research_mode: draft.researchMode } : {}),
        ...(draft.inventoryConcepts?.length ? { inventory_concepts: [...new Set(draft.inventoryConcepts)] } : {}),
        ...(draft.sourceUrls?.length ? { source_urls: [...new Set(draft.sourceUrls)] } : {}),
      },
      composition: {
        skill_version_id: draft.composition.skillVersionId,
        pipeline_version_id: draft.composition.pipelineVersionId,
        asset_library_ids: draft.composition.assetLibraryIds,
        voice_profile_id: draft.composition.voiceProfileId ?? null,
        render_preset_version_id: draft.composition.renderPresetVersionId ?? null,
        capabilities: [],
      },
      video_settings: draft.videoSettings ? {
        language: draft.videoSettings.language,
        aspect_ratio: draft.videoSettings.aspectRatio,
        target_duration_seconds: draft.videoSettings.targetDurationSeconds,
        visibility: draft.videoSettings.visibility,
        auto_quality_check: draft.videoSettings.autoQualityCheck,
        layout: draft.videoSettings.layout,
        media_fit: draft.videoSettings.mediaFit,
        frame_rate: draft.videoSettings.frameRate,
        subtitles: {
          enabled: draft.videoSettings.subtitles.enabled,
          position: draft.videoSettings.subtitles.position,
          size: draft.videoSettings.subtitles.size,
          max_lines: draft.videoSettings.subtitles.maxLines,
        },
        asset_acquisition: {
          enabled: draft.videoSettings.assetAcquisition.enabled,
          sources: draft.videoSettings.assetAcquisition.sources,
          max_assets: draft.videoSettings.assetAcquisition.maxAssets,
          copyright_status: draft.videoSettings.assetAcquisition.copyrightStatus,
          rights_confirmed: draft.videoSettings.assetAcquisition.rightsConfirmed,
        },
        no_asset_draft: {
          enabled: draft.videoSettings.noAssetDraft.enabled,
        },
      } : undefined,
    };
  }

  async estimateRun(draft: RunDraft): Promise<ApiResult<RunEstimate>> {
    const result = await this.request<JsonRecord>("/v1/runs/estimate", {
      method: "POST",
      body: JSON.stringify(this.runPayload(draft)),
    });
    if (!result.ok) return result;
    return { ok: true, data: {
      cost: { amount: number(record(result.data.cost).amount), currency: text(record(result.data.cost).currency, "CNY") as "CNY" | "USD" },
      durationSeconds: number(result.data.duration_seconds),
      capabilityGaps: (Array.isArray(result.data.capability_gaps) ? result.data.capability_gaps : []).map((item) => {
        const gap = record(item);
        return {
          capability: text(gap.capability),
          resource: text(gap.resource, "pipeline") as "skill" | "pipeline" | "voice" | "render" | "assets",
          message: text(gap.message),
        };
      }),
      capabilitiesKnown: Boolean(result.data.capabilities_known),
    } };
  }

  private mapRunStep(raw: JsonRecord): RunStep {
    const key = text(raw.node_key, text(raw.step_key, text(raw.key)));
    const type = text(raw.operation, text(raw.step_type, text(raw.type, key)));
    const labels: Record<string, string> = {
      "document.inspect": "PDF 安全检查",
      "document.extract": "页面证据提取",
      "writing.compose.document": "证据脚本生成",
      "document.storyboard.plan": "分镜与来源映射",
      "document.materialize": "页面截图固化",
      "media.augment": "非事实背景准备",
      "document.timeline.align": "字幕与时间线对齐",
      "render.composite": "文档画面合成",
      "quality.evaluate.document": "文档成片质量检查",
      research: "研究与事实核验",
      writing: "脚本写作",
      write: "脚本写作",
      draft: "脚本写作",
      tts: "配音生成",
      narration: "配音生成",
      voice: "配音生成",
      assets: "素材准备",
      media: "素材准备",
      render: "画面渲染",
      quality: "质量检查",
      qc: "质量检查",
    };
    const review = record(raw.review);
    const error = record(raw.error);
    const storedDecision = text(review.decision);
    const decision = storedDecision === "request_changes" ? "revise" : storedDecision;
    return {
      id: text(raw.id, text(raw.worker_step_id)),
      key,
      type,
      label: text(raw.label, labels[type] ?? labels[key] ?? (key || "未命名步骤")),
      status: text(raw.status, "queued") as RunStep["status"],
      attemptCount: number(raw.attempt, number(raw.attempt_count)),
      maxAttempts: number(raw.maximum_attempts, number(raw.max_attempts, 1)),
      queueName: text(raw.queue_name) || undefined,
      dependencies: strings(raw.dependencies),
      requiredCapabilities: strings(raw.required_capabilities),
      inputSnapshot: record(raw.input_snapshot) as RunStep["inputSnapshot"],
      outputSummary: record(raw.output_summary) as RunStep["outputSummary"],
      artifacts: (Array.isArray(raw.output_artifacts) ? raw.output_artifacts : [])
        .map((item) => this.mapRunArtifact(record(item))),
      revision: number(raw.revision, number(raw.worker_revision)),
      reviewRequired: Boolean(raw.review_required),
      review: Object.keys(review).length ? {
        decision: (decision || undefined) as "approve" | "reject" | "revise" | undefined,
        actorId: text(review.actor_id) || undefined,
        comment: text(review.comment, text(review.reason)) || undefined,
        issueCodes: strings(review.issue_codes),
        reviewedRevision: number(review.reviewed_revision) || undefined,
        requestedAt: text(review.requested_at) || undefined,
        decidedAt: text(review.decided_at) || undefined,
      } : undefined,
      error: Object.keys(error).length ? {
        code: text(error.code, "STEP_FAILED"),
        message: text(error.message, "步骤执行失败"),
        retryable: Boolean(error.retryable),
      } : undefined,
      startedAt: text(raw.started_at) || undefined,
      completedAt: text(raw.finished_at, text(raw.completed_at)) || undefined,
      nextAttemptAt: text(raw.next_attempt_at) || undefined,
      cancellationRequestedAt: text(raw.cancellation_requested_at) || undefined,
      updatedAt: text(raw.updated_at),
    };
  }

  private mapRunArtifact(raw: JsonRecord): RunArtifact {
    const id = text(raw.id);
    return {
      id,
      kind: text(raw.kind, "artifact"),
      filename: text(raw.filename, "artifact"),
      mediaType: text(raw.media_type, "application/octet-stream"),
      byteSize: number(raw.byte_size),
      contentHash: text(raw.content_hash),
      contentUrl: `${this.baseUrl}/v1/artifacts/${encodeURIComponent(id)}/content`,
      stepId: text(raw.step_id) || undefined,
      createdAt: text(raw.created_at),
    };
  }

  private mapRun(raw: JsonRecord): Run {
    const input = record(raw.input);
    const snapshot = record(raw.composition_snapshot);
    const skillRef = record(snapshot.skill_version);
    const pipelineRef = record(snapshot.pipeline_version);
    const renderRef = record(snapshot.render_preset);
    const production = record(snapshot.production_settings);
    const subtitle = record(production.subtitles);
    const acquisition = record(production.asset_acquisition);
    const noAssetDraft = record(production.no_asset_draft);
    const persistedEstimate = record(raw.estimate);
    const persistedCost = record(persistedEstimate.cost ?? raw.cost);
    const hasCost = typeof persistedCost.amount === "number";
    const hasEmbeddedSteps = Array.isArray(raw.steps);
    const webpageVideoRunId = text(input.webpage_video_run_id);
    const fullAiRunId = text(input.full_ai_run_id);
    const projectKind: Run["projectKind"] = text(input.document_source_id)
      ? "document_video"
      : webpageVideoRunId
      ? "webpage_video"
      : fullAiRunId || text(snapshot.visual_source_mode) === "generated_only"
        ? "full_ai"
        : "standard";
    const videoSettings = Object.keys(production).length ? {
      language: text(production.language, "zh-CN"),
      aspectRatio: text(production.aspect_ratio, "16:9") as VideoSettings["aspectRatio"],
      targetDurationSeconds: number(production.target_duration_seconds, 180),
      visibility: text(production.visibility, "private") as VideoSettings["visibility"],
      autoQualityCheck: Boolean(production.auto_quality_check),
      layout: text(production.layout, "full_frame") as VideoSettings["layout"],
      mediaFit: text(production.media_fit, "cover") as VideoSettings["mediaFit"],
      frameRate: number(production.frame_rate, 30) as VideoSettings["frameRate"],
      subtitles: {
        enabled: subtitle.enabled === undefined ? true : Boolean(subtitle.enabled),
        position: text(subtitle.position, "bottom") as VideoSettings["subtitles"]["position"],
        size: text(subtitle.size, "medium") as VideoSettings["subtitles"]["size"],
        maxLines: number(subtitle.max_lines, 2) as VideoSettings["subtitles"]["maxLines"],
      },
      assetAcquisition: {
        enabled: Boolean(acquisition.enabled),
        sources: strings(acquisition.sources).filter(
          (source): source is "youtube" | "bilibili" | "wikimedia" =>
            source === "youtube" || source === "bilibili" || source === "wikimedia",
        ),
        maxAssets: number(acquisition.max_assets, 3),
        copyrightStatus: text(acquisition.copyright_status, "licensed") as VideoSettings["assetAcquisition"]["copyrightStatus"],
        rightsConfirmed: Boolean(acquisition.rights_confirmed),
      },
      noAssetDraft: {
        enabled: Boolean(noAssetDraft.enabled),
      },
    } satisfies VideoSettings : undefined;
    return {
      id: text(raw.id), workspaceId: text(raw.workspace_id), topic: text(input.topic, text(input.prompt, "未命名主题")),
      projectKind,
      controlRunId: webpageVideoRunId || fullAiRunId || undefined,
      channelId: text(raw.channel_id) || undefined,
      status: text(raw.status, "queued") as Run["status"],
      composition: { skillVersionId: text(skillRef.id), assetLibraryIds: strings(snapshot.asset_library_ids), voiceProfileId: text(snapshot.voice_profile_id) || undefined, renderPresetVersionId: text(renderRef.id) || undefined, pipelineVersionId: text(pipelineRef.id) },
      videoSettings,
      estimate: {
        cost: { amount: number(persistedCost.amount), currency: text(persistedCost.currency, "CNY") as "CNY" | "USD" },
        durationSeconds: number(persistedEstimate.duration_seconds),
        capabilityGaps: [],
      },
      costAvailable: hasCost,
      steps: (Array.isArray(raw.steps) ? raw.steps.map(record) : []).map((item) => this.mapRunStep(item)),
      stepsAvailable: hasEmbeddedSteps,
      artifacts: (Array.isArray(raw.artifacts) ? raw.artifacts.map(record) : []).map((item) => this.mapRunArtifact(item)),
      cancellationRequestedAt: text(raw.cancel_requested_at) || undefined,
      createdBy: text(raw.created_by), createdAt: text(raw.created_at), updatedAt: text(raw.updated_at),
    };
  }

  async createRun(draft: RunDraft, key: string): Promise<ApiResult<Run>> {
    const result = await this.request<JsonRecord>("/v1/runs", { method: "POST", headers: { "Idempotency-Key": key }, body: JSON.stringify(this.runPayload(draft)) });
    return result.ok ? { ok: true, data: this.mapRun(result.data) } : result;
  }

  async listRuns(query: RunQuery = {}): Promise<ApiResult<Run[]>> {
    const result = await this.page("/v1/runs");
    if (!result.ok) return result;
    let runs = result.data.map((item) => this.mapRun(item));
    if (query.channelId) runs = runs.filter((item) => item.channelId === query.channelId);
    if (query.status) runs = runs.filter((item) => item.status === query.status);
    if (query.search) { const needle = query.search.toLowerCase(); runs = runs.filter((item) => `${item.topic} ${item.id}`.toLowerCase().includes(needle)); }
    runs.sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
    return { ok: true, data: runs };
  }

  async getRun(runId: string): Promise<ApiResult<Run>> {
    const [runResult, stepsResult, artifactsResult] = await Promise.all([
      this.request<JsonRecord>(`/v1/runs/${encodeURIComponent(runId)}`),
      this.page(`/v1/steps?run_id=${encodeURIComponent(runId)}`),
      this.page(`/v1/artifacts?run_id=${encodeURIComponent(runId)}&limit=100`),
    ]);
    if (!runResult.ok) return runResult;
    if (!stepsResult.ok) return stepsResult;
    if (!artifactsResult.ok) return artifactsResult;
    return { ok: true, data: {
      ...this.mapRun(runResult.data),
      steps: stepsResult.data.map((item) => this.mapRunStep(item)),
      stepsAvailable: true,
      artifacts: artifactsResult.data.map((item) => this.mapRunArtifact(item)),
    } };
  }

  private mapGenerationBatch(raw: JsonRecord): GenerationBatch {
    const snapshot = record(raw.composition_snapshot);
    const skillRef = record(snapshot.skill_version);
    const pipelineRef = record(snapshot.pipeline_version);
    const renderRef = record(snapshot.render_preset);
    const counts = record(raw.status_counts);
    return {
      id: text(raw.id),
      workspaceId: text(raw.workspace_id),
      name: text(raw.name, "未命名批次"),
      status: text(raw.status, "queued") as GenerationBatch["status"],
      totalCount: number(raw.total_count),
      statusCounts: {
        queued: number(counts.queued),
        running: number(counts.running),
        awaitingReview: number(counts.awaiting_review),
        succeeded: number(counts.succeeded),
        failed: number(counts.failed),
        cancelled: number(counts.cancelled),
      },
      composition: {
        skillVersionId: text(skillRef.id),
        assetLibraryIds: strings(snapshot.asset_library_ids),
        voiceProfileId: text(snapshot.voice_profile_id) || undefined,
        renderPresetVersionId: text(renderRef.id) || undefined,
        pipelineVersionId: text(pipelineRef.id),
      },
      createdBy: text(raw.created_by),
      createdAt: text(raw.created_at),
      updatedAt: text(raw.updated_at),
    };
  }

  private mapGenerationBatchItem(raw: JsonRecord): GenerationBatchItem {
    return {
      id: text(raw.id),
      workspaceId: text(raw.workspace_id),
      batchId: text(raw.batch_id),
      runId: text(raw.run_id),
      ordinal: number(raw.ordinal),
      label: text(raw.label),
      input: record(raw.input) as GenerationBatchItem["input"],
      status: text(raw.status, "queued") as GenerationBatchItem["status"],
      cancellationRequestedAt: text(raw.cancellation_requested_at) || undefined,
      createdAt: text(raw.created_at),
      updatedAt: text(raw.updated_at),
    };
  }

  async cancelRun(runId: string, key: string): Promise<ApiResult<Run>> {
    const result = await this.request<JsonRecord>(`/v1/runs/${encodeURIComponent(runId)}/cancel`, {
      method: "POST", headers: { "Idempotency-Key": key },
    });
    return result.ok ? { ok: true, data: this.mapRun(result.data) } : result;
  }

  async retryRunStep(stepId: string, key: string): Promise<ApiResult<RunStep>> {
    const result = await this.request<JsonRecord>(`/v1/steps/${encodeURIComponent(stepId)}/retry`, {
      method: "POST",
      headers: { "Idempotency-Key": key },
    });
    return result.ok ? { ok: true, data: this.mapRunStep(result.data) } : result;
  }

  async reviewRunStep(stepId: string, review: RunStepReviewRequest, key: string): Promise<ApiResult<RunStep>> {
    const result = await this.request<JsonRecord>(`/v1/steps/${encodeURIComponent(stepId)}/review`, {
      method: "POST", headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        decision: review.decision,
        reason: review.comment?.trim() || null,
        issue_codes: review.issueCodes ?? [],
        expected_revision: review.expectedRevision ?? null,
      }),
    });
    return result.ok ? { ok: true, data: this.mapRunStep(result.data) } : result;
  }

  async listGenerationBatches(): Promise<ApiResult<GenerationBatch[]>> {
    const result = await this.page("/v1/generation-batches?limit=100");
    return result.ok
      ? { ok: true, data: result.data.map((item) => this.mapGenerationBatch(item)) }
      : result;
  }

  async getGenerationBatch(batchId: string): Promise<ApiResult<GenerationBatch>> {
    const result = await this.request<JsonRecord>(
      `/v1/generation-batches/${encodeURIComponent(batchId)}`,
    );
    return result.ok ? { ok: true, data: this.mapGenerationBatch(result.data) } : result;
  }

  async listGenerationBatchItems(
    batchId: string,
    query: GenerationBatchItemQuery = {},
  ): Promise<ApiResult<GenerationBatchItemPage>> {
    const parameters = new URLSearchParams({ limit: String(query.limit ?? 50) });
    if (query.status) parameters.set("status", query.status);
    if (query.search?.trim()) parameters.set("search", query.search.trim());
    if (query.cursor) parameters.set("cursor", query.cursor);
    const result = await this.request<JsonRecord>(
      `/v1/generation-batches/${encodeURIComponent(batchId)}/items?${parameters}`,
    );
    if (!result.ok) return result;
    const page = record(result.data.page);
    return {
      ok: true,
      data: {
        data: (Array.isArray(result.data.data) ? result.data.data : [])
          .map((item) => this.mapGenerationBatchItem(record(item))),
        totalCount: number(result.data.total_count),
        nextCursor: text(page.next_cursor) || undefined,
      },
    };
  }

  async createGenerationBatch(
    request: GenerationBatchCreateRequest,
    key: string,
  ): Promise<ApiResult<GenerationBatch>> {
    const result = await this.request<JsonRecord>("/v1/generation-batches", {
      method: "POST",
      headers: { "Idempotency-Key": key },
      body: JSON.stringify({
        workspace_id: request.workspaceId,
        name: request.name,
        channel_id: request.channelId ?? null,
        research_mode: request.researchMode,
        items: request.items.map((item) => ({ topic: item.topic, inputs: item.inputs ?? {} })),
        composition: {
          skill_version_id: request.composition.skillVersionId,
          pipeline_version_id: request.composition.pipelineVersionId,
          asset_library_ids: request.composition.assetLibraryIds,
          voice_profile_id: request.composition.voiceProfileId ?? null,
          render_preset_version_id: request.composition.renderPresetVersionId ?? null,
          capabilities: [],
        },
        video_settings: request.videoSettings ? {
          language: request.videoSettings.language,
          aspect_ratio: request.videoSettings.aspectRatio,
          target_duration_seconds: request.videoSettings.targetDurationSeconds,
          visibility: request.videoSettings.visibility,
          auto_quality_check: request.videoSettings.autoQualityCheck,
          layout: request.videoSettings.layout,
          media_fit: request.videoSettings.mediaFit,
          frame_rate: request.videoSettings.frameRate,
          subtitles: {
            enabled: request.videoSettings.subtitles.enabled,
            position: request.videoSettings.subtitles.position,
            size: request.videoSettings.subtitles.size,
            max_lines: request.videoSettings.subtitles.maxLines,
          },
          asset_acquisition: {
            enabled: false,
            sources: ["wikimedia"],
            max_assets: 1,
            copyright_status: "public_domain",
            rights_confirmed: false,
          },
          no_asset_draft: {
            enabled: request.videoSettings.noAssetDraft.enabled,
          },
        } : undefined,
      }),
    });
    return result.ok ? { ok: true, data: this.mapGenerationBatch(result.data) } : result;
  }

  async cancelGenerationBatch(batchId: string, key: string): Promise<ApiResult<GenerationBatch>> {
    const result = await this.request<JsonRecord>(
      `/v1/generation-batches/${encodeURIComponent(batchId)}/cancel`,
      { method: "POST", headers: { "Idempotency-Key": key } },
    );
    return result.ok ? { ok: true, data: this.mapGenerationBatch(result.data) } : result;
  }

  async retryFailedGenerationBatch(batchId: string, key: string): Promise<ApiResult<GenerationBatch>> {
    const result = await this.request<JsonRecord>(
      `/v1/generation-batches/${encodeURIComponent(batchId)}/retry-failed`,
      { method: "POST", headers: { "Idempotency-Key": key }, body: "{}" },
    );
    return result.ok ? { ok: true, data: this.mapGenerationBatch(result.data) } : result;
  }

  private mapChannel(raw: JsonRecord): Channel {
    const composition = record(raw.default_composition);
    const brand = record(raw.brand_profile ?? raw.brand_config);
    const profile = { ...brand };
    delete profile.primary_color;
    delete profile.accent_color;
    const singleLibraryId = text(raw.default_asset_library_id);
    const rawStatus = text(raw.status, "active");
    const status = (["active", "paused", "archived"].includes(rawStatus) ? rawStatus : "archived") as Channel["status"];
    return {
      id: text(raw.id),
      workspaceId: text(raw.workspace_id),
      slug: text(raw.slug),
      name: text(raw.name),
      description: text(raw.description),
      platform: normalizeChannelPlatform(text(raw.platform)) || undefined,
      handle: text(raw.handle, text(raw.external_ref)) || undefined,
      platformConnectionId: text(raw.platform_connection_id) || undefined,
      status,
      defaultComposition: {
        skillVersionId: text(composition.skill_version_id, text(raw.default_skill_version_id)),
        pipelineVersionId: text(composition.pipeline_version_id, text(raw.default_pipeline_version_id, text(raw.default_pipeline_id))),
        assetLibraryIds: strings(composition.asset_library_ids).length
          ? strings(composition.asset_library_ids)
          : singleLibraryId ? [singleLibraryId] : [],
        voiceProfileId: text(composition.voice_profile_id, text(raw.default_voice_profile_id)) || undefined,
        renderPresetVersionId: text(
          composition.render_preset_version_id,
          text(raw.default_render_preset_version_id, text(raw.default_render_preset_id)),
        ) || undefined,
      },
      brandConfig: {
        profile: profile as JsonObject,
        logoAssetId: text(brand.logo_asset_id) || undefined,
        introAssetId: text(brand.intro_asset_id) || undefined,
        outroAssetId: text(brand.outro_asset_id) || undefined,
        accentColor: text(brand.primary_color, text(brand.accent_color)) || undefined,
        fontFamily: text(brand.font_family) || undefined,
        guidelines: text(brand.guidelines) || undefined,
      },
      revision: number(raw.revision, 1),
      createdBy: text(raw.created_by),
      createdAt: text(raw.created_at),
      updatedAt: text(raw.updated_at),
    };
  }

  private channelPayload(draft: ChannelDraft): JsonRecord {
    const profile = { ...draft.brandConfig.profile };
    delete profile.primary_color;
    delete profile.accent_color;
    return {
      workspace_id: draft.workspaceId,
      slug: draft.slug?.trim() || null,
      name: draft.name.trim(),
      description: draft.description.trim(),
      platform: normalizeChannelPlatform(draft.platform) || null,
      handle: draft.handle?.trim() || null,
      platform_connection_id: draft.platformConnectionId ?? null,
      status: draft.status ?? "active",
      default_composition: {
        skill_version_id: draft.defaultComposition.skillVersionId,
        pipeline_version_id: draft.defaultComposition.pipelineVersionId,
        asset_library_ids: draft.defaultComposition.assetLibraryIds,
        voice_profile_id: draft.defaultComposition.voiceProfileId ?? null,
        render_preset_version_id: draft.defaultComposition.renderPresetVersionId ?? null,
      },
      brand_profile: {
        ...profile,
        logo_asset_id: draft.brandConfig.logoAssetId ?? null,
        intro_asset_id: draft.brandConfig.introAssetId ?? null,
        outro_asset_id: draft.brandConfig.outroAssetId ?? null,
        primary_color: draft.brandConfig.accentColor ?? null,
        font_family: draft.brandConfig.fontFamily ?? null,
        guidelines: draft.brandConfig.guidelines ?? null,
      },
    };
  }

  async listChannels(query: ChannelQuery): Promise<ApiResult<Channel[]>> {
    const parameters = new URLSearchParams();
    if (query.search?.trim()) parameters.set("search", query.search.trim());
    if (query.platform) parameters.set("platform", normalizeChannelPlatform(query.platform));
    if (query.status) parameters.set("status", query.status);
    const queryString = parameters.toString();
    const result = await this.page(`/v1/channels${queryString ? `?${queryString}` : ""}`);
    if (!result.ok) return result;
    const channels = result.data.map((item) => this.mapChannel(item));
    channels.sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt));
    return { ok: true, data: channels };
  }

  async getChannel(channelId: string): Promise<ApiResult<VersionedResource<Channel>>> {
    const result = await this.requestVersioned<JsonRecord>(`/v1/channels/${encodeURIComponent(channelId)}`);
    if (!result.ok) return result;
    const channel = this.mapChannel(result.data.value);
    const etag = result.data.etag || `"${channel.revision}"`;
    return { ok: true, data: { value: channel, etag } };
  }

  async saveChannel(
    draft: ChannelDraft,
    etag?: string,
    key = idempotencyKey(draft.id ? "replace-channel" : "create-channel"),
  ): Promise<ApiResult<VersionedResource<Channel>>> {
    if (draft.id && !etag) {
      return { ok: false, error: { code: "PRECONDITION_REQUIRED", status: 428, message: "缺少频道版本标识，请刷新后重试。" } };
    }
    const path = draft.id ? `/v1/channels/${encodeURIComponent(draft.id)}` : "/v1/channels";
    const result = await this.requestVersioned<JsonRecord>(path, {
      method: draft.id ? "PUT" : "POST",
      headers: {
        "Idempotency-Key": key,
        ...(draft.id && etag ? { "If-Match": etag } : {}),
      },
      body: JSON.stringify(this.channelPayload(draft)),
    });
    if (!result.ok) return result;
    const channel = this.mapChannel(result.data.value);
    const responseEtag = result.data.etag || `"${channel.revision}"`;
    return { ok: true, data: { value: channel, etag: responseEtag } };
  }

  async archiveChannel(channelId: string, etag: string): Promise<ApiResult<void>> {
    if (!etag) {
      return { ok: false, error: { code: "PRECONDITION_REQUIRED", status: 428, message: "缺少频道版本标识，请刷新后重试。" } };
    }
    return this.request<void>(`/v1/channels/${encodeURIComponent(channelId)}`, {
      method: "DELETE",
      headers: { "If-Match": etag },
    });
  }
}

export function createHttpAdapter(options: HttpAdapterOptions = {}): FrameFactoryAdapter {
  return new HttpFrameFactoryAdapter(options);
}
