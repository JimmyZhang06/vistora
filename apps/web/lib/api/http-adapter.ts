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
  JsonObject,
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

type JsonRecord = Record<string, unknown>;

export interface HttpAdapterOptions {
  baseUrl?: string;
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

function idempotencyKey(prefix: string): string {
  const suffix = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}:${suffix}`;
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
    const fallback = spec.assetPolicy.allowExternalAcquisition === previous.allowExternalAcquisition
      ? text(source.fallback, "fail")
      : spec.assetPolicy.allowExternalAcquisition ? "licensed_stock" : "fail";
    output.asset_policy = {
      ...source,
      library_binding: spec.assetPolicy.strategy === "channel_default" ? "channel_default" : spec.assetPolicy.strategy === "explicit_only" ? "none" : "run_composition",
      allowed_kinds: spec.assetPolicy.requiredTags,
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
  private readonly fetcher: typeof globalThis.fetch;
  private readonly pollIntervalMs: number;
  private readonly pollAttempts: number;
  private readonly rawVersions = new Map<string, JsonRecord>();

  constructor(options: HttpAdapterOptions = {}) {
    this.baseUrl = (options.baseUrl ?? process.env.NEXT_PUBLIC_FRAMEFACTORY_API_URL ?? DEFAULT_API_URL).replace(/\/$/, "");
    this.fetcher = options.fetch ?? globalThis.fetch.bind(globalThis);
    this.pollIntervalMs = options.testPollIntervalMs ?? 500;
    this.pollAttempts = options.testPollAttempts ?? 120;
  }

  private async request<T>(path: string, init: RequestInit = {}): Promise<ApiResult<T>> {
    try {
      const response = await this.fetcher(`${this.baseUrl}${path}`, {
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
    const spec = request.kind === "blank" ? request.initialSpec : request.kind === "import" ? request.package.spec : {};
    const version = await this.request<JsonRecord>("/v1/skill-versions", { method: "POST", headers: { "Idempotency-Key": idempotencyKey("create-version") }, body: JSON.stringify({
      skill_id: text(created.data.id), version: "0.1.0", ...defaultCanonicalSpec(spec),
      test_topics: request.kind === "import" ? request.package.testTopics ?? [] : [],
      release_notes: request.kind === "import" ? request.package.releaseNotes ?? "" : "",
    }) });
    if (!version.ok) return version;
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
      source: text(source.id) ? {
        id: text(source.id), type: text(source.source_type, text(source.type)), locator: text(source.locator) || undefined,
        provider: text(source.provider) || undefined, attribution: text(source.attribution) || undefined,
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
    const versions = versionsResult.data.filter((item) => text(item.state) === "published");
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
      skills: versions.map((item) => { const skill = skills.get(text(item.skill_id)) ?? {}; const system = text(skill.publisher_type) === "system"; return { skillId: text(item.skill_id), skillName: text(skill.name), versionId: text(item.id), version: text(item.version), publisher: { id: system ? "system" : text(skill.workspace_id), workspaceId: text(skill.workspace_id), type: system ? "system" : "workspace", displayName: text(skill.publisher_name), verified: system } }; }),
      pipelines: pipelineIds.map((id) => ({ id, workspaceId: "", pipelineId: id, name: "Skill 默认 Pipeline", version: "1.0.0", capabilities: [] })),
    } };
  }

  private runPayload(draft: RunDraft): JsonRecord {
    return {
      workspace_id: draft.workspaceId,
      channel_id: draft.channelId ?? null,
      input: { topic: draft.topic },
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
    const persistedEstimate = record(raw.estimate);
    const persistedCost = record(persistedEstimate.cost ?? raw.cost);
    const hasCost = typeof persistedCost.amount === "number";
    const hasEmbeddedSteps = Array.isArray(raw.steps);
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
    } satisfies VideoSettings : undefined;
    return {
      id: text(raw.id), workspaceId: text(raw.workspace_id), topic: text(input.topic, text(input.prompt, "未命名主题")), channelId: text(raw.channel_id) || undefined,
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
            enabled: request.videoSettings.assetAcquisition.enabled,
            sources: request.videoSettings.assetAcquisition.sources,
            max_assets: request.videoSettings.assetAcquisition.maxAssets,
            copyright_status: request.videoSettings.assetAcquisition.copyrightStatus,
            rights_confirmed: request.videoSettings.assetAcquisition.rightsConfirmed,
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
