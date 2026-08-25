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
  AssetBulkResult,
  AssetIngestionJob,
  AssetIngestionJobPage,
  AssetPage,
  AssetPoster,
  AssetPreview,
  AssetSegment,
  AssetUploadRequest,
  RemoteAssetImportRequest,
  Channel,
  ChannelDraft,
  ChannelQuery,
  ComparisonRequest,
  ComparisonResult,
  ComparisonSide,
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
  SkillPermissions,
  SkillQuery,
  SkillSpec,
  SkillVersion,
  ValidationCheck,
  ValidationReport,
  VersionedResource,
  Workspace,
} from "./contracts";
import {
  createDefaultSkillSpec,
  createEmptyMockData,
  createNormalMockData,
  type MockDatabase,
} from "./mock-data";

export type MockScenario = "normal" | "empty" | "error";

export interface CreateMockAdapterOptions {
  scenario?: MockScenario;
  latencyMs?: number;
}

class MockFailure extends Error {
  constructor(readonly problem: ApiProblem) {
    super(problem.message);
    this.name = "MockFailure";
  }
}

const editablePermissions: SkillPermissions = {
  view: true,
  fork: true,
  edit: true,
  test: true,
  publish: true,
  deleteDraft: true,
  deprecate: true,
};

function clone<T>(value: T): T {
  return value === undefined ? value : structuredClone(value);
}

function normalize(value: string): string {
  return value.trim().toLocaleLowerCase("zh-CN");
}

function slugify(value: string): string {
  return value
    .trim()
    .toLocaleLowerCase("en-US")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 48);
}

function roundMoney(value: number): number {
  return Math.round(value * 100) / 100;
}

export class InMemoryFrameFactoryAdapter implements FrameFactoryAdapter {
  private readonly scenario: MockScenario;
  private readonly latencyMs: number;
  private readonly data: MockDatabase;
  private readonly idempotentRuns = new Map<string, string>();
  private readonly generationBatches: GenerationBatch[] = [];
  private readonly generationBatchItems: GenerationBatchItem[] = [];
  private readonly idempotentBatches = new Map<string, string>();
  private readonly libraryBuildJobs = new Map<string, LibraryBuildJob>();
  private readonly idempotentLibraryBuildJobs = new Map<string, string>();
  private sequence = 1;
  private profileRevision = 1;
  private preferencesRevision = 1;
  private creationPreferences: Omit<CreationPreferences, "userId" | "workspaceId" | "revision" | "updatedAt"> = {
    defaultLanguage: "zh-CN",
    defaultAspectRatio: "16:9",
    defaultDurationSeconds: 180,
    defaultVisibility: "private",
    autoQualityCheck: true,
  };
  private readonly accountSessions: AccountSession[] = [];
  private readonly apiKeys: AccountApiKey[] = [];

  constructor(options: CreateMockAdapterOptions = {}) {
    this.scenario = options.scenario ?? "normal";
    this.latencyMs = Math.max(0, options.latencyMs ?? 0);
    this.data = this.scenario === "empty" ? createEmptyMockData() : createNormalMockData();
  }

  private nextId(prefix: string): string {
    const value = `${prefix}_mock_${this.sequence}`;
    this.sequence += 1;
    return value;
  }

  private async execute<T>(operation: () => T): Promise<ApiResult<T>> {
    if (this.latencyMs > 0) {
      await new Promise<void>((resolve) => setTimeout(resolve, this.latencyMs));
    }

    if (this.scenario === "error") {
      return {
        ok: false,
        error: {
          code: "mock_unavailable",
          message: "模拟数据服务暂时不可用，请重试。",
          retryable: true,
        },
      };
    }

    try {
      return { ok: true, data: clone(operation()) };
    } catch (error) {
      if (error instanceof MockFailure) {
        return { ok: false, error: error.problem };
      }
      return {
        ok: false,
        error: {
          code: "mock_internal_error",
          message: error instanceof Error ? error.message : "模拟数据操作失败。",
          retryable: false,
        },
      };
    }
  }

  private fail(code: string, message: string, field?: string, status?: number): never {
    throw new MockFailure({ code, message, field, status, retryable: false });
  }

  private findSkill(skillId: string): Skill {
    const skill = this.data.skills.find((item) => item.id === skillId);
    if (!skill) this.fail("skill_not_found", "未找到该 Skill。", "skillId");
    return skill;
  }

  private findVersion(versionId: string): SkillVersion {
    const version = this.data.versions.find((item) => item.id === versionId);
    if (!version) this.fail("skill_version_not_found", "未找到该 Skill 版本。", "versionId");
    return version;
  }

  private findWorkspace(workspaceId: string): Workspace {
    const workspace = this.data.workspaces.find((item) => item.id === workspaceId);
    if (!workspace) this.fail("workspace_not_found", "未找到目标工作区。", "workspaceId");
    return workspace;
  }

  private assertSkillVersion(skillId: string, versionId: string): SkillVersion {
    const version = this.findVersion(versionId);
    if (version.skillId !== skillId) {
      this.fail("version_skill_mismatch", "该版本不属于指定 Skill。", "versionId");
    }
    return version;
  }

  private publisherFor(workspace: Workspace): Skill["publisher"] {
    if (workspace.kind === "system") {
      this.fail("system_workspace_readonly", "系统工作区不接受用户创建内容。", "workspaceId");
    }
    return {
      id: `publisher_${workspace.id}`,
      workspaceId: workspace.id,
      type: "workspace",
      displayName: workspace.name,
      verified: workspace.kind === "team",
    };
  }

  getSession(): Promise<ApiResult<SessionContext>> {
    return this.execute(() => this.data.session);
  }

  getAccountCapabilities(): Promise<ApiResult<AccountCapabilities>> {
    return this.execute(() => ({
      profile: "available", creationPreferences: "available", sessions: "not_recorded",
      apiKeys: "management_only", twoFactorAuthentication: "unavailable",
    }));
  }

  getAccountProfile(): Promise<ApiResult<VersionedResource<AccountProfile>>> {
    return this.execute(() => ({
      value: {
        userId: this.data.session.user.id,
        workspaceId: this.data.session.activeWorkspaceId,
        email: this.data.session.user.email,
        displayName: this.data.session.user.displayName,
        avatarUrl: this.data.session.user.avatarUrl,
        locale: this.data.session.user.locale,
        timezone: this.data.session.user.timezone,
        revision: this.profileRevision,
        updatedAt: new Date().toISOString(),
      },
      etag: `"${this.profileRevision}"`,
    }));
  }

  replaceAccountProfile(profile: AccountProfileUpdate, etag: string): Promise<ApiResult<VersionedResource<AccountProfile>>> {
    return this.execute(() => {
      if (etag.replace(/(?:W\/)?"/g, "") !== String(this.profileRevision)) {
        this.fail("revision_conflict", "个人资料已在其他位置更新，请刷新后重试。", "If-Match");
      }
      this.data.session.user.displayName = profile.displayName.trim();
      this.data.session.user.avatarUrl = profile.avatarUrl;
      this.data.session.user.locale = profile.locale;
      this.data.session.user.timezone = profile.timezone;
      this.profileRevision += 1;
      return {
        value: {
          userId: this.data.session.user.id, workspaceId: this.data.session.activeWorkspaceId,
          email: this.data.session.user.email, displayName: this.data.session.user.displayName,
          avatarUrl: profile.avatarUrl, locale: profile.locale, timezone: profile.timezone,
          revision: this.profileRevision, updatedAt: new Date().toISOString(),
        },
        etag: `"${this.profileRevision}"`,
      };
    });
  }

  getCreationPreferences(): Promise<ApiResult<VersionedResource<CreationPreferences>>> {
    return this.execute(() => ({
      value: {
        userId: this.data.session.user.id, workspaceId: this.data.session.activeWorkspaceId,
        ...this.creationPreferences, revision: this.preferencesRevision, updatedAt: new Date().toISOString(),
      },
      etag: `"${this.preferencesRevision}"`,
    }));
  }

  replaceCreationPreferences(preferences: CreationPreferencesUpdate, etag: string): Promise<ApiResult<VersionedResource<CreationPreferences>>> {
    return this.execute(() => {
      if (etag.replace(/(?:W\/)?"/g, "") !== String(this.preferencesRevision)) {
        this.fail("revision_conflict", "创作偏好已在其他位置更新，请刷新后重试。", "If-Match");
      }
      this.creationPreferences = { ...preferences };
      this.preferencesRevision += 1;
      return {
        value: {
          userId: this.data.session.user.id, workspaceId: this.data.session.activeWorkspaceId,
          ...this.creationPreferences, revision: this.preferencesRevision, updatedAt: new Date().toISOString(),
        },
        etag: `"${this.preferencesRevision}"`,
      };
    });
  }

  listAccountSessions(): Promise<ApiResult<AccountSession[]>> {
    return this.execute(() => {
      if (this.accountSessions.length === 0) {
        const now = new Date();
        this.accountSessions.push({
          id: this.nextId("session"), workspaceId: this.data.session.activeWorkspaceId,
          userId: this.data.session.user.id, userAgent: "Vistora test browser",
          createdAt: now.toISOString(), lastSeenAt: now.toISOString(),
          expiresAt: new Date(now.getTime() + 86_400_000).toISOString(),
        });
      }
      return this.accountSessions;
    });
  }

  revokeAccountSession(sessionId: string): Promise<ApiResult<void>> {
    return this.execute(() => {
      const session = this.accountSessions.find((item) => item.id === sessionId);
      if (!session) this.fail("session_not_found", "未找到该登录会话。");
      session.revokedAt = new Date().toISOString();
    });
  }

  listApiKeys(): Promise<ApiResult<AccountApiKey[]>> {
    return this.execute(() => this.apiKeys);
  }

  createApiKey(request: ApiKeyCreateRequest): Promise<ApiResult<CreatedApiKey>> {
    return this.execute(() => {
      const plaintext = `ffk_test_${this.nextId("secret")}`;
      const key: AccountApiKey = {
        id: this.nextId("api_key"), workspaceId: this.data.session.activeWorkspaceId,
        name: request.name.trim(), keyPrefix: plaintext.slice(0, 12), scopes: [...request.scopes],
        createdAt: new Date().toISOString(), expiresAt: request.expiresAt,
      };
      this.apiKeys.push(key);
      return { key, plaintext };
    });
  }

  revokeApiKey(keyId: string): Promise<ApiResult<void>> {
    return this.execute(() => {
      const key = this.apiKeys.find((item) => item.id === keyId);
      if (!key) this.fail("api_key_not_found", "未找到该 API 密钥。");
      key.revokedAt = new Date().toISOString();
    });
  }

  listSkills(query: SkillQuery = {}): Promise<ApiResult<Skill[]>> {
    return this.execute(() => {
      let items = [...this.data.skills];
      if (query.workspaceId) {
        items = items.filter((skill) => skill.workspaceId === query.workspaceId);
      }
      if (query.scope === "official") {
        items = items.filter((skill) => skill.publisher.type === "system");
      } else if (query.scope === "mine") {
        items = items.filter(
          (skill) =>
            skill.publisher.type === "workspace" &&
            skill.createdBy === this.data.session.user.id,
        );
      } else if (query.scope === "team") {
        const teamIds = new Set(
          this.data.workspaces
            .filter((workspace) => workspace.kind === "team")
            .map((workspace) => workspace.id),
        );
        items = items.filter((skill) => teamIds.has(skill.workspaceId));
      }
      if (query.status) items = items.filter((skill) => skill.status === query.status);
      if (query.visibility) {
        items = items.filter((skill) => skill.visibility === query.visibility);
      }
      if (query.search?.trim()) {
        const search = normalize(query.search);
        items = items.filter((skill) =>
          normalize(`${skill.name} ${skill.description} ${skill.publisher.displayName}`).includes(search),
        );
      }
      return items.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    });
  }

  getSkill(skillId: string): Promise<ApiResult<SkillDetail>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const versions = this.data.versions
        .filter((version) => version.skillId === skillId)
        .sort((left, right) => right.createdAt.localeCompare(left.createdAt));
      return { skill, versions };
    });
  }

  createSkill(request: CreateSkillRequest): Promise<ApiResult<Skill>> {
    return this.execute(() => {
      const workspace = this.findWorkspace(request.workspaceId);
      const publisher = this.publisherFor(workspace);
      const identity = request.kind === "import" ? request.package.identity : request.identity;
      if (!identity.name.trim()) this.fail("name_required", "请输入 Skill 名称。", "name");

      let spec: SkillSpec;
      let testTopics: string[] = [];
      let releaseNotes: string | undefined;
      let forkedFrom: Skill["forkedFrom"];

      if (request.kind === "fork") {
        const source = this.assertSkillVersion(request.sourceSkillId, request.sourceVersionId);
        const sourceSkill = this.findSkill(request.sourceSkillId);
        if (!sourceSkill.permissions.fork) {
          this.fail("fork_forbidden", "当前 Skill 不允许分叉。", "sourceSkillId");
        }
        spec = clone(source.spec);
        testTopics = [...source.testTopics];
        forkedFrom = { skillId: sourceSkill.id, versionId: source.id };
      } else if (request.kind === "import") {
        if (request.package.schemaVersion !== "1.0") {
          this.fail("unsupported_schema", "仅支持 1.0 版声明式 Skill 包。", "schemaVersion");
        }
        spec = clone(request.package.spec);
        testTopics = [...(request.package.testTopics ?? [])];
        releaseNotes = request.package.releaseNotes;
      } else if (request.kind === "distill") {
        if (request.examples.length === 0) {
          this.fail("examples_required", "请至少提供一个示例。", "examples");
        }
        spec = {
          ...createDefaultSkillSpec(),
          writingInstructions: `从 ${request.examples.length} 个示例中提炼共同结构；保留可解释规则，不复制示例原文。`,
        };
        testTopics = request.examples.slice(0, 3).map((example) => example.label);
      } else {
        spec = { ...createDefaultSkillSpec(), ...clone(request.initialSpec ?? {}) };
      }

      const now = new Date().toISOString();
      const skillId = this.nextId("skill");
      const versionId = this.nextId("skill_version");
      const skill: Skill = {
        id: skillId,
        workspaceId: workspace.id,
        name: identity.name.trim(),
        slug: identity.slug?.trim() || slugify(identity.name) || this.nextId("draft"),
        description: identity.description.trim(),
        publisher,
        visibility: identity.visibility ?? "private",
        status: "draft",
        draftVersionId: versionId,
        forkedFrom,
        createdBy: this.data.session.user.id,
        createdAt: now,
        updatedAt: now,
        permissions: { ...editablePermissions },
        stats: { runCount: 0, versionCount: 1 },
      };
      const version: SkillVersion = {
        id: versionId,
        skillId,
        version: "0.1.0-draft",
        schemaVersion: "1.0",
        state: "draft",
        revision: 1,
        spec,
        testTopics,
        releaseNotes,
        createdBy: this.data.session.user.id,
        createdAt: now,
      };
      this.data.skills.unshift(skill);
      this.data.versions.unshift(version);
      return skill;
    });
  }

  createDraft(skillId: string, sourceVersionId: string): Promise<ApiResult<SkillVersion>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      if (!skill.permissions.edit) this.fail("edit_forbidden", "你没有编辑此 Skill 的权限。");
      if (skill.draftVersionId) return this.assertSkillVersion(skillId, skill.draftVersionId);
      const source = this.assertSkillVersion(skillId, sourceVersionId);
      const versions = this.data.versions.filter((item) => item.skillId === skillId);
      const highest = versions.reduce((current, item) => {
        const left = current.split(".").map(Number);
        const right = item.version.replace(/-draft$/, "").split(".").map(Number);
        return (right[0] ?? 0) > (left[0] ?? 0)
          || ((right[0] ?? 0) === (left[0] ?? 0) && (right[1] ?? 0) > (left[1] ?? 0))
          || ((right[0] ?? 0) === (left[0] ?? 0) && (right[1] ?? 0) === (left[1] ?? 0) && (right[2] ?? 0) > (left[2] ?? 0))
          ? item.version.replace(/-draft$/, "") : current;
      }, "0.0.0");
      const [major = 0, minor = 0, patch = 0] = highest.split(".").map(Number);
      const now = new Date().toISOString();
      const version: SkillVersion = {
        ...clone(source),
        id: this.nextId("skill_version"),
        version: `${major}.${minor}.${patch + 1}`,
        state: "draft",
        revision: 1,
        contentHash: undefined,
        publishedAt: undefined,
        releaseNotes: "",
        createdBy: this.data.session.user.id,
        createdAt: now,
      };
      this.data.versions.unshift(version);
      skill.draftVersionId = version.id;
      skill.status = "draft";
      skill.stats.versionCount += 1;
      skill.updatedAt = now;
      return version;
    });
  }

  saveDraft(
    skillId: string,
    versionId: string,
    patch: SkillDraftPatch,
    expectedRevision: number,
  ): Promise<ApiResult<SkillVersion>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const version = this.assertSkillVersion(skillId, versionId);
      if (!skill.permissions.edit) this.fail("edit_forbidden", "你没有编辑此 Skill 的权限。");
      if (version.state === "published" || version.state === "deprecated") {
        this.fail("immutable_version", "已发布版本不可原地修改，请创建新草稿。");
      }
      if (version.revision !== expectedRevision) {
        this.fail("revision_conflict", "草稿已在其他位置更新，请刷新后重试。", "revision");
      }

      version.spec = { ...version.spec, ...clone(patch.spec ?? {}) };
      if (patch.testTopics) version.testTopics = [...patch.testTopics];
      if (patch.releaseNotes !== undefined) version.releaseNotes = patch.releaseNotes;
      version.revision += 1;
      version.state = "draft";
      skill.status = "draft";
      skill.updatedAt = new Date().toISOString();
      return version;
    });
  }

  validateVersion(skillId: string, versionId: string): Promise<ApiResult<ValidationReport>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const version = this.assertSkillVersion(skillId, versionId);
      if (!skill.permissions.edit) this.fail("validate_forbidden", "你没有校验此 Skill 的权限。");
      if (version.state === "published" || version.state === "deprecated") {
        this.fail("immutable_version", "已发布版本无需重新校验。");
      }

      version.state = "validating";
      skill.status = "validating";
      const suspiciousInstruction = /(?:process\.env|<script|(?:[a-z]:\\|\/etc\/))/i.test(
        version.spec.writingInstructions,
      );
      const checks: ValidationCheck[] = [
        {
          id: "schema",
          label: "结构校验",
          passed: Object.keys(version.spec.inputSchema).length > 0,
          severity: "error",
          message: "输入契约必须是非空声明式对象。",
          section: "inputSchema",
        },
        {
          id: "instructions",
          label: "写作指令",
          passed: version.spec.writingInstructions.trim().length >= 20,
          severity: "error",
          message: "写作指令至少需要 20 个字符。",
          section: "writingInstructions",
        },
        {
          id: "dangerous-content",
          label: "危险内容检查",
          passed: !suspiciousInstruction,
          severity: "error",
          message: "指令中不能包含可执行脚本、环境变量或服务端路径。",
          section: "writingInstructions",
        },
        {
          id: "test-topics",
          label: "测试主题",
          passed: version.testTopics.filter(Boolean).length >= 1,
          severity: "error",
          message: "发布前至少需要 1 个测试主题。",
          section: "testTopics",
        },
        {
          id: "output-contract",
          label: "输出契约",
          passed: version.spec.outputContract.fields.length > 0,
          severity: "error",
          message: "输出契约至少需要一个字段。",
          section: "outputContract",
        },
      ];
      const ready = checks.every((check) => check.passed || check.severity !== "error");
      version.state = ready ? "ready" : "rejected";
      version.revision += 1;
      skill.status = ready ? "ready" : "draft";
      return {
        skillId,
        versionId,
        ready,
        revision: version.revision,
        checks,
        estimatedCost: { amount: 1.8, currency: "CNY" },
      };
    });
  }

  publishVersion(
    skillId: string,
    versionId: string,
    releaseNotes: string,
  ): Promise<ApiResult<SkillVersion>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const version = this.assertSkillVersion(skillId, versionId);
      if (!skill.permissions.publish) this.fail("publish_forbidden", "你没有发布此 Skill 的权限。");
      if (version.state !== "ready") {
        this.fail("validation_required", "请先完成校验并修复阻断项。");
      }
      const now = new Date().toISOString();
      version.state = "published";
      version.version = version.version.replace(/-draft$/, "");
      version.releaseNotes = releaseNotes.trim() || version.releaseNotes;
      version.publishedAt = now;
      version.contentHash = `sha256:mock-${skill.id}-${version.revision}`;
      skill.currentVersionId = version.id;
      skill.draftVersionId = undefined;
      skill.status = "published";
      skill.updatedAt = now;
      return version;
    });
  }

  compareVersions(request: ComparisonRequest): Promise<ApiResult<ComparisonResult>> {
    return this.execute(() => {
      this.findSkill(request.skillId);
      if (!request.topic.trim()) this.fail("topic_required", "请输入测试主题。", "topic");
      const leftVersion = this.assertSkillVersion(request.skillId, request.leftVersionId);
      const rightVersion = this.assertSkillVersion(request.skillId, request.rightVersionId);
      const side = (version: SkillVersion, label: string): ComparisonSide => ({
        versionId: version.id,
        version: version.version,
        output: `${request.topic.trim()}：${label}依据当前版本的研究、写作与输出规则生成结构化内容。`,
        cost: {
          amount: roundMoney(0.72 + version.spec.writingInstructions.length * 0.001),
          currency: "CNY",
        },
        durationMs: 820 + version.spec.writingInstructions.length * 4,
        checks: version.spec.qcRubric.criteria.map((criterion) => ({
          id: criterion.id,
          label: criterion.label,
          passed: true,
          score: Math.min(0.96, criterion.minimumScore + 0.08),
          note: "模拟输出满足当前最低分。",
        })),
      });
      return {
        id: this.nextId("comparison"),
        topic: request.topic.trim(),
        left: side(leftVersion, "左侧版本"),
        right: side(rightVersion, "右侧版本"),
        differences:
          leftVersion.spec.writingInstructions === rightVersion.spec.writingInstructions
            ? []
            : [
                {
                  path: "writingInstructions",
                  change: "changed",
                  left: leftVersion.spec.writingInstructions,
                  right: rightVersion.spec.writingInstructions,
                },
              ],
        createdAt: new Date().toISOString(),
      };
    });
  }

  rollbackSkill(skillId: string, versionId: string): Promise<ApiResult<Skill>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const version = this.assertSkillVersion(skillId, versionId);
      if (!skill.permissions.publish) this.fail("rollback_forbidden", "你没有回滚此 Skill 的权限。");
      if (version.state !== "published") {
        this.fail("rollback_target_invalid", "只能回滚到未弃用的已发布版本。", "versionId");
      }
      skill.currentVersionId = version.id;
      skill.status = "published";
      skill.updatedAt = new Date().toISOString();
      return skill;
    });
  }

  deprecateVersion(skillId: string, versionId: string): Promise<ApiResult<SkillVersion>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      const version = this.assertSkillVersion(skillId, versionId);
      if (!skill.permissions.deprecate) this.fail("deprecate_forbidden", "你没有弃用此版本的权限。");
      if (version.state !== "published") {
        this.fail("version_not_published", "只有已发布版本可以弃用。", "versionId");
      }
      version.state = "deprecated";
      if (skill.currentVersionId === version.id) skill.status = "deprecated";
      skill.updatedAt = new Date().toISOString();
      return version;
    });
  }

  deleteDraft(skillId: string): Promise<ApiResult<void>> {
    return this.execute(() => {
      const skill = this.findSkill(skillId);
      if (!skill.permissions.deleteDraft) this.fail("delete_forbidden", "你没有删除此草稿的权限。");
      const draftId = skill.draftVersionId;
      if (!draftId) this.fail("draft_not_found", "当前 Skill 没有可删除草稿。");
      if (this.data.runs.some((run) => run.composition.skillVersionId === draftId)) {
        this.fail("draft_in_use", "该草稿已被项目引用，只能保留或弃用。");
      }
      this.data.versions = this.data.versions.filter((version) => version.id !== draftId);
      if (skill.currentVersionId) {
        skill.draftVersionId = undefined;
        skill.status = "published";
        skill.stats.versionCount = Math.max(1, skill.stats.versionCount - 1);
        skill.updatedAt = new Date().toISOString();
      } else {
        this.data.skills = this.data.skills.filter((item) => item.id !== skill.id);
      }
    });
  }

  getComposerOptions(workspaceId: string): Promise<ApiResult<ComposerOptions>> {
    return this.execute(() => {
      this.findWorkspace(workspaceId);
      const accessibleSkills = this.data.skills.filter(
        (skill) => (skill.workspaceId === workspaceId || skill.publisher.type === "system")
          && skill.status === "published",
      );
      const skills = accessibleSkills.flatMap((skill) => {
        const versionId = skill.currentVersionId ?? skill.draftVersionId;
        if (!versionId) return [];
        const version = this.data.versions.find((item) => item.id === versionId);
        return version
          ? [
              {
                skillId: skill.id,
                skillName: skill.name,
                versionId: version.id,
                version: version.version,
                publisher: skill.publisher,
              },
            ]
          : [];
      });
      return {
        channels: this.data.channels.filter(
          (channel) => channel.workspaceId === workspaceId && channel.status === "active",
        ),
        skills,
        assetLibraries: this.data.assetLibraries.filter((item) => item.workspaceId === workspaceId),
        voiceProfiles: this.data.voiceProfiles.filter((item) => item.workspaceId === workspaceId),
        renderPresets: this.data.renderPresets.filter((item) => item.workspaceId === workspaceId),
        pipelines: this.data.pipelines.filter((item) => item.workspaceId === workspaceId),
      };
    });
  }

  getFullAiOptions(): Promise<ApiResult<FullAiOptions>> {
    return this.execute(() => ({
      schemaVersion: "1.0.0",
      mode: "generated_only",
      status: "blocked",
      pipeline: { slug: "full-ai-production", version: 2, visualSourceMode: "generated_only" },
      provider: {
        status: "unconfigured",
        supportsReconciliation: false,
        submitUnknownPolicy: "manual_only",
        continuityModes: ["prompt_pack", "none"],
      },
      limits: {
        briefMaxLength: 1600,
        durationSeconds: [15, 30, 45, 60],
        crawlMaxPagesDefault: 8,
        crawlMaxPagesLimit: 12,
        crawlMaxDepthDefault: 1,
        crawlMaxDepthLimit: 2,
        aspectRatios: ["9:16", "16:9"],
        directions: ["cinematic", "graphic", "illustrated"],
        variantsPerScene: [1, 2, 3],
        clipSeconds: 5,
      },
      blockers: [{ code: "FULL_AI_PROVIDER_UNAVAILABLE", message: "模拟环境未配置生成 Provider。", retryable: false }],
    }));
  }

  estimateFullAiRun(spec: FullAiSpec): Promise<ApiResult<FullAiEstimate>> {
    return this.execute(() => ({
      schemaVersion: "1.0.0",
      status: "blocked",
      requestFingerprint: "",
      plan: {
        sceneCount: Math.ceil(spec.durationSeconds / 5),
        clipSeconds: 5,
        candidateCount: Math.ceil(spec.durationSeconds / 5) * spec.variantsPerScene,
        billableSeconds: spec.durationSeconds * spec.variantsPerScene,
      },
      blockers: [{ code: "FULL_AI_PROVIDER_UNAVAILABLE", message: "模拟环境未配置生成 Provider。", retryable: false }],
    }));
  }

  createFullAiRun(
    request: FullAiRunCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<FullAiRun>> {
    void request;
    void idempotencyKey;
    return this.execute(() => this.fail("FULL_AI_PROVIDER_UNAVAILABLE", "模拟环境未配置生成 Provider。"));
  }

  getFullAiRun(runId: string): Promise<ApiResult<FullAiRun>> {
    void runId;
    return this.execute(() => this.fail("full_ai_run_not_found", "全 AI 任务不存在。"));
  }

  getWebpageVideoOptions(): Promise<ApiResult<WebpageVideoOptions>> {
    return this.execute(() => ({
      schemaVersion: "1.0.0",
      status: "blocked",
      pipeline: { slug: "webpage-capture-video", version: 1 },
      limits: {
        urlMaxLength: 2048,
        topicMaxLength: 1600,
        aspectRatios: ["16:9", "9:16", "1:1", "4:3"],
        durationSeconds: [15, 30, 45, 60],
      },
      voices: [],
      subtitles: { supported: false, defaultEnabled: false },
      blockers: [{
        code: "WEBPAGE_VIDEO_UNAVAILABLE",
        message: "模拟环境未运行隔离网页截图服务。",
        retryable: false,
      }],
    }));
  }

  createWebpageVideoRun(
    request: WebpageVideoRunCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<WebpageVideoRun>> {
    void request;
    void idempotencyKey;
    return this.execute(() => this.fail("WEBPAGE_VIDEO_UNAVAILABLE", "模拟环境未运行隔离网页截图服务。"));
  }

  getWebpageVideoRun(runId: string): Promise<ApiResult<WebpageVideoRun>> {
    void runId;
    return this.execute(() => this.fail("webpage_video_run_not_found", "网页截图成片任务不存在。"));
  }

  getWebpageVideoCapture(runId: string): Promise<ApiResult<WebpageVideoCapture>> {
    void runId;
    return this.execute(() => this.fail("capture_not_ready", "截图尚未生成。"));
  }

  getWebpageVideoSite(runId: string): Promise<ApiResult<WebpageVideoSitePlan>> {
    void runId;
    return this.execute(() => this.fail("site_plan_not_ready", "多页面解析结果尚未生成。"));
  }

  reviewWebpageVideoRun(
    runId: string,
    review: WebpageVideoReviewRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<void>> {
    void runId;
    void review;
    void idempotencyKey;
    return this.execute(() => this.fail("WEBPAGE_VIDEO_UNAVAILABLE", "模拟环境不支持网页截图审核。"));
  }

  reviewWebpageVideoScope(
    runId: string,
    review: WebpageVideoScopeReviewRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<void>> {
    void runId;
    void review;
    void idempotencyKey;
    return this.execute(() => this.fail("WEBPAGE_VIDEO_UNAVAILABLE", "模拟环境不支持页面范围审核。"));
  }

  reviewWebpageVideoStoryboard(
    runId: string,
    review: WebpageVideoStoryboardReviewRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<void>> {
    void runId;
    void review;
    void idempotencyKey;
    return this.execute(() => this.fail("WEBPAGE_VIDEO_UNAVAILABLE", "模拟环境不支持镜头板审核。"));
  }

  cancelWebpageVideoRun(runId: string, idempotencyKey: string): Promise<ApiResult<void>> {
    void runId;
    void idempotencyKey;
    return this.execute(() => this.fail("WEBPAGE_VIDEO_UNAVAILABLE", "模拟环境不支持取消网页截图成片任务。"));
  }

  private calculateEstimate(draft: RunDraft): RunEstimate {
    if (!draft.topic.trim()) this.fail("topic_required", "请输入创作主题。", "topic");
    this.findWorkspace(draft.workspaceId);
    const gaps: RunEstimate["capabilityGaps"] = [];
    const version = this.data.versions.find(
      (item) => item.id === draft.composition.skillVersionId,
    );
    const pipeline = this.data.pipelines.find(
      (item) => item.id === draft.composition.pipelineVersionId,
    );
    if (!version) {
      gaps.push({ capability: "skill.version", resource: "skill", message: "所选 Skill 版本不可用。" });
    }
    if (!pipeline) {
      gaps.push({ capability: "pipeline.version", resource: "pipeline", message: "所选 Pipeline 版本不可用。" });
    }
    if (version && pipeline) {
      for (const capability of version.spec.modelRequirements.capabilities) {
        if (!pipeline.capabilities.includes(capability)) {
          gaps.push({
            capability,
            resource: "pipeline",
            message: `当前 Pipeline 缺少 ${capability} 能力。`,
          });
        }
      }
    }
    if (
      draft.composition.voiceProfileId &&
      !this.data.voiceProfiles.some((item) => item.id === draft.composition.voiceProfileId)
    ) {
      gaps.push({ capability: "voice.profile", resource: "voice", message: "所选声音配置不可用。" });
    }
    if (
      draft.composition.renderPresetVersionId &&
      !this.data.renderPresets.some((item) => item.id === draft.composition.renderPresetVersionId)
    ) {
      gaps.push({ capability: "render.preset", resource: "render", message: "所选渲染预设不可用。" });
    }
    for (const libraryId of draft.composition.assetLibraryIds) {
      if (!this.data.assetLibraries.some((item) => item.id === libraryId)) {
        gaps.push({ capability: "assets.library", resource: "assets", message: `素材库 ${libraryId} 不可用。` });
      }
    }
    return {
      cost: {
        amount: roundMoney(1.6 + draft.topic.trim().length * 0.025 + draft.composition.assetLibraryIds.length * 0.2),
        currency: "CNY",
      },
      durationSeconds: 70 + draft.composition.assetLibraryIds.length * 12,
      capabilityGaps: gaps,
      capabilitiesKnown: true,
    };
  }

  estimateRun(draft: RunDraft): Promise<ApiResult<RunEstimate>> {
    return this.execute(() => this.calculateEstimate(draft));
  }

  createRun(draft: RunDraft, idempotencyKey: string): Promise<ApiResult<Run>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) {
        this.fail("idempotency_key_required", "创建项目需要幂等键。", "idempotencyKey");
      }
      const existingRunId = this.idempotentRuns.get(idempotencyKey);
      if (existingRunId) {
        const existing = this.data.runs.find((run) => run.id === existingRunId);
        if (existing) return existing;
      }
      const estimate = this.calculateEstimate(draft);
      if (estimate.capabilityGaps.length > 0) {
        this.fail("capability_gap", "当前组合存在能力缺口，无法创建项目。");
      }
      if (draft.channelId) {
        const channel = this.data.channels.find((item) => item.id === draft.channelId);
        if (!channel || channel.workspaceId !== draft.workspaceId) {
          this.fail("channel_not_found", "所选发布频道不可用。", "channelId");
        }
      }
      const now = new Date().toISOString();
      const run: Run = {
        id: this.nextId("run"),
        workspaceId: draft.workspaceId,
        topic: draft.topic.trim(),
        channelId: draft.channelId,
        status: "queued",
        composition: clone(draft.composition),
        videoSettings: draft.videoSettings ? clone(draft.videoSettings) : undefined,
        estimate,
        steps: [
          { id: this.nextId("run_step"), key: "research", type: "research", label: "研究", status: "queued", attemptCount: 0, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: now },
          { id: this.nextId("run_step"), key: "writing", type: "writing", label: "写作", status: "queued", attemptCount: 0, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: now },
          { id: this.nextId("run_step"), key: "render", type: "render", label: "渲染", status: "queued", attemptCount: 0, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: now },
        ],
        artifacts: [],
        createdBy: this.data.session.user.id,
        createdAt: now,
        updatedAt: now,
      };
      this.data.runs.unshift(run);
      this.idempotentRuns.set(idempotencyKey, run.id);
      const version = this.data.versions.find(
        (item) => item.id === draft.composition.skillVersionId,
      );
      const skill = version
        ? this.data.skills.find((item) => item.id === version.skillId)
        : undefined;
      if (skill) skill.stats.runCount += 1;
      return run;
    });
  }

  listRuns(query: RunQuery = {}): Promise<ApiResult<Run[]>> {
    return this.execute(() => {
      let runs = [...this.data.runs];
      if (query.workspaceId) runs = runs.filter((run) => run.workspaceId === query.workspaceId);
      if (query.channelId) runs = runs.filter((run) => run.channelId === query.channelId);
      if (query.status) runs = runs.filter((run) => run.status === query.status);
      if (query.search?.trim()) {
        const search = normalize(query.search);
        runs = runs.filter((run) => normalize(run.topic).includes(search));
      }
      return runs.sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    });
  }

  getRun(runId: string): Promise<ApiResult<Run>> {
    return this.execute(() => {
      const run = this.data.runs.find((item) => item.id === runId);
      if (!run) this.fail("run_not_found", "未找到该项目。", "runId");
      return run;
    });
  }

  createAssetLibrary(request: AssetLibraryCreateRequest): Promise<ApiResult<AssetLibraryOption>> {
    return this.execute(() => {
      const workspaceId = this.data.session.activeWorkspaceId;
      const library: AssetLibraryOption = {
        id: this.nextId("asset_library"),
        workspaceId,
        name: request.name,
        description: request.description,
        assetCount: 0,
        readyAssetCount: 0,
      };
      this.data.assetLibraries.push(library);
      return clone(library);
    });
  }

  createLibraryBuildJob(
    request: LibraryBuildJobCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<LibraryBuildJob>> {
    return this.execute(() => {
      const existingId = this.idempotentLibraryBuildJobs.get(idempotencyKey);
      const existing = existingId ? this.libraryBuildJobs.get(existingId) : undefined;
      if (existing) return existing;
      const library = this.data.assetLibraries.find((item) => item.id === request.libraryId);
      if (!library) this.fail("ASSET_LIBRARY_NOT_FOUND", "素材库不存在", "libraryId");
      const now = new Date().toISOString();
      const job: LibraryBuildJob = {
        schemaVersion: "1.0.0",
        id: this.nextId("library_build_job"),
        workspaceId: this.data.session.activeWorkspaceId,
        libraryId: request.libraryId,
        status: "queued",
        stage: "discover",
        spec: {
          topic: request.topic.trim(),
          queries: [...(request.queries ?? [])],
          sources: [...request.sources],
          maxAssets: request.maxAssets,
          copyrightStatus: request.copyrightStatus,
          rightsConfirmed: true,
        },
        progress: { assetIds: [], discovered: 0, transferred: 0, analyzed: 0, indexed: 0, failed: 0 },
        revision: 1,
        createdBy: this.data.session.user.id,
        createdAt: now,
        updatedAt: now,
      };
      this.libraryBuildJobs.set(job.id, job);
      this.idempotentLibraryBuildJobs.set(idempotencyKey, job.id);
      return job;
    });
  }

  getLibraryBuildJob(jobId: string): Promise<ApiResult<LibraryBuildJob>> {
    return this.execute(() => {
      const job = this.libraryBuildJobs.get(jobId);
      if (!job) this.fail("LIBRARY_BUILD_JOB_NOT_FOUND", "素材建库任务不存在", "jobId");
      return job;
    });
  }

  cancelLibraryBuildJob(jobId: string, revision: number): Promise<ApiResult<LibraryBuildJob>> {
    return this.execute(() => {
      const job = this.libraryBuildJobs.get(jobId);
      if (!job) this.fail("LIBRARY_BUILD_JOB_NOT_FOUND", "素材建库任务不存在", "jobId");
      if (job.revision !== revision) this.fail("REVISION_CONFLICT", "建库任务已更新，请刷新后重试。", "If-Match", 412);
      if (["queued", "running"].includes(job.status)) {
        job.status = "cancelled";
        job.revision += 1;
        job.updatedAt = new Date().toISOString();
        job.completedAt = job.updatedAt;
      }
      return job;
    });
  }

  uploadAsset(request: AssetUploadRequest): Promise<ApiResult<void>> {
    return this.execute(() => {
      const library = this.data.assetLibraries.find((item) => item.id === request.libraryId);
      if (!library) throw new MockFailure({ code: "ASSET_LIBRARY_NOT_FOUND", message: "素材库不存在" });
      library.assetCount += 1;
      library.readyAssetCount = (library.readyAssetCount ?? 0) + 1;
    });
  }

  importRemoteAsset(request: RemoteAssetImportRequest): Promise<ApiResult<void>> {
    return this.execute(() => {
      const library = this.data.assetLibraries.find((item) => item.id === request.libraryId);
      if (!library) throw new MockFailure({ code: "ASSET_LIBRARY_NOT_FOUND", message: "素材库不存在" });
      library.assetCount += 1;
      library.readyAssetCount = (library.readyAssetCount ?? 0) + 1;
    });
  }

  private unsupportedAssets<T>(): Promise<ApiResult<T>> {
    return Promise.resolve({ ok: false, error: { code: "NOT_SUPPORTED", message: "测试夹具不模拟正式素材管理 API" } });
  }

  listAssets(): Promise<ApiResult<AssetPage>> { return this.unsupportedAssets(); }
  getAsset(): Promise<ApiResult<Asset>> { return this.unsupportedAssets(); }
  updateAsset(): Promise<ApiResult<Asset>> { return this.unsupportedAssets(); }
  reviewAsset(): Promise<ApiResult<Asset>> { return this.unsupportedAssets(); }
  reanalyzeAsset(): Promise<ApiResult<AssetIngestionJob>> { return this.unsupportedAssets(); }
  deleteAsset(): Promise<ApiResult<void>> { return this.unsupportedAssets(); }
  restoreAsset(): Promise<ApiResult<Asset>> { return this.unsupportedAssets(); }
  bulkUpdateAssets(): Promise<ApiResult<AssetBulkResult>> { return this.unsupportedAssets(); }
  getAssetPreview(): Promise<ApiResult<AssetPreview>> { return this.unsupportedAssets(); }
  getAssetPoster(): Promise<ApiResult<AssetPoster>> { return this.unsupportedAssets(); }
  listAssetSegments(): Promise<ApiResult<AssetSegment[]>> { return this.unsupportedAssets(); }
  listAssetIngestionJobs(): Promise<ApiResult<AssetIngestionJobPage>> { return this.unsupportedAssets(); }

  cancelRun(runId: string, idempotencyKey: string): Promise<ApiResult<Run>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) this.fail("idempotency_key_required", "取消项目需要幂等键。", "idempotencyKey");
      const run = this.data.runs.find((item) => item.id === runId);
      if (!run) this.fail("run_not_found", "未找到该项目。", "runId");
      if (!["succeeded", "failed", "cancelled"].includes(run.status)) {
        run.cancellationRequestedAt = new Date().toISOString();
      }
      return run;
    });
  }

  retryRunStep(stepId: string, idempotencyKey: string): Promise<ApiResult<RunStep>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) this.fail("idempotency_key_required", "重试步骤需要幂等键。", "idempotencyKey");
      const run = this.data.runs.find((item) => item.steps.some((step) => step.id === stepId));
      const step = run?.steps.find((item) => item.id === stepId);
      if (!step) this.fail("step_not_found", "未找到该步骤。", "stepId");
      if (step.status !== "failed" || step.attemptCount >= 20) {
        this.fail("step_not_retryable", "该步骤当前不可重试。", "stepId");
      }
      const now = new Date().toISOString();
      step.maxAttempts = Math.max(step.maxAttempts, step.attemptCount + 1);
      step.status = "retrying";
      step.error = undefined;
      step.nextAttemptAt = now;
      step.updatedAt = now;
      if (run) {
        run.status = "retrying";
        run.updatedAt = now;
      }
      return step;
    });
  }

  reviewRunStep(stepId: string, review: RunStepReviewRequest, idempotencyKey: string): Promise<ApiResult<RunStep>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) this.fail("idempotency_key_required", "审核步骤需要幂等键。", "idempotencyKey");
      const run = this.data.runs.find((item) => item.steps.some((step) => step.id === stepId));
      const step = run?.steps.find((item) => item.id === stepId);
      if (!step) this.fail("step_not_found", "未找到该步骤。", "stepId");
      if (step.status !== "awaiting_review" || !step.reviewRequired) {
        this.fail("step_not_awaiting_review", "该步骤当前不可审核。", "stepId");
      }
      if (review.expectedRevision !== undefined && review.expectedRevision !== (step.revision ?? 0)) {
        this.fail("step_state_changed", "步骤已更新，请重新读取审核证据。", "expectedRevision");
      }
      const now = new Date().toISOString();
      step.review = {
        decision: review.decision,
        comment: review.comment,
        issueCodes: review.issueCodes ?? [],
        reviewedRevision: step.revision,
        decidedAt: now,
      };
      step.status = review.decision === "approve" ? "succeeded" : review.decision === "revise" ? "retrying" : "failed";
      step.updatedAt = now;
      if (run) { run.status = step.status === "retrying" ? "running" : step.status; run.updatedAt = now; }
      return step;
    });
  }

  listGenerationBatches(): Promise<ApiResult<GenerationBatch[]>> {
    return this.execute(() => [...this.generationBatches]);
  }

  getGenerationBatch(batchId: string): Promise<ApiResult<GenerationBatch>> {
    return this.execute(() => {
      const batch = this.generationBatches.find((item) => item.id === batchId);
      if (!batch) this.fail("generation_batch_not_found", "未找到该生产批次。", "batchId");
      return batch;
    });
  }

  listGenerationBatchItems(
    batchId: string,
    query: GenerationBatchItemQuery = {},
  ): Promise<ApiResult<GenerationBatchItemPage>> {
    return this.execute(() => {
      const batch = this.generationBatches.find((item) => item.id === batchId);
      if (!batch) this.fail("generation_batch_not_found", "未找到该生产批次。", "batchId");
      let items = this.generationBatchItems.filter((item) => item.batchId === batchId);
      if (query.status) items = items.filter((item) => item.status === query.status);
      if (query.search?.trim()) {
        const needle = normalize(query.search);
        items = items.filter((item) => normalize(item.label).includes(needle));
      }
      const offset = Number.parseInt(query.cursor ?? "0", 10) || 0;
      const limit = query.limit ?? 50;
      const data = items.slice(offset, offset + limit);
      return {
        data,
        totalCount: items.length,
        nextCursor: offset + data.length < items.length ? String(offset + data.length) : undefined,
      };
    });
  }

  createGenerationBatch(
    request: GenerationBatchCreateRequest,
    idempotencyKey: string,
  ): Promise<ApiResult<GenerationBatch>> {
    return this.execute(() => {
      const existingId = this.idempotentBatches.get(idempotencyKey);
      const existing = this.generationBatches.find((item) => item.id === existingId);
      if (existing) return existing;
      const now = new Date().toISOString();
      const batchId = this.nextId("batch");
      request.items.forEach((item, ordinal) => {
        const runId = this.nextId("run");
        this.generationBatchItems.push({
          id: this.nextId("batch_item"), workspaceId: request.workspaceId, batchId, runId,
          ordinal, label: item.topic, input: { ...(item.inputs ?? {}), topic: item.topic, research_mode: request.researchMode },
          status: "queued", createdAt: now, updatedAt: now,
        });
      });
      const batch: GenerationBatch = {
        id: batchId, workspaceId: request.workspaceId, name: request.name,
        status: "queued", totalCount: request.items.length,
        statusCounts: { queued: request.items.length, running: 0, awaitingReview: 0, succeeded: 0, failed: 0, cancelled: 0 },
        composition: clone(request.composition), createdBy: this.data.session.user.id,
        createdAt: now, updatedAt: now,
      };
      this.generationBatches.unshift(batch);
      this.idempotentBatches.set(idempotencyKey, batch.id);
      return batch;
    });
  }

  cancelGenerationBatch(batchId: string, idempotencyKey: string): Promise<ApiResult<GenerationBatch>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) this.fail("idempotency_key_required", "取消批次需要幂等键。", "idempotencyKey");
      const batch = this.generationBatches.find((item) => item.id === batchId);
      if (!batch) this.fail("generation_batch_not_found", "未找到该生产批次。", "batchId");
      const now = new Date().toISOString();
      this.generationBatchItems.filter((item) => item.batchId === batchId).forEach((item) => {
        if (!["succeeded", "failed", "cancelled"].includes(item.status)) item.status = "cancelled";
        item.updatedAt = now;
      });
      batch.status = "completed_with_errors";
      batch.statusCounts.cancelled = batch.totalCount - batch.statusCounts.succeeded - batch.statusCounts.failed;
      batch.statusCounts.queued = 0; batch.statusCounts.running = 0; batch.statusCounts.awaitingReview = 0;
      batch.updatedAt = now;
      return batch;
    });
  }

  retryFailedGenerationBatch(batchId: string, idempotencyKey: string): Promise<ApiResult<GenerationBatch>> {
    return this.execute(() => {
      if (!idempotencyKey.trim()) this.fail("idempotency_key_required", "重开失败项需要幂等键。", "idempotencyKey");
      const source = this.generationBatches.find((item) => item.id === batchId);
      if (!source) this.fail("generation_batch_not_found", "未找到该生产批次。", "batchId");
      const failed = this.generationBatchItems.filter((item) => item.batchId === batchId && item.status === "failed");
      if (!failed.length) this.fail("batch_has_no_failed_items", "这个批次没有失败项。", "batchId");
      const now = new Date().toISOString();
      const id = this.nextId("batch");
      const batch: GenerationBatch = {
        ...clone(source), id, name: `${source.name} · retry`, status: "queued",
        totalCount: failed.length,
        statusCounts: { queued: failed.length, running: 0, awaitingReview: 0, succeeded: 0, failed: 0, cancelled: 0 },
        createdAt: now, updatedAt: now,
      };
      this.generationBatches.unshift(batch);
      return batch;
    });
  }

  listChannels(query: ChannelQuery): Promise<ApiResult<Channel[]>> {
    return this.execute(() => {
      this.findWorkspace(query.workspaceId);
      return this.data.channels
        .filter((channel) => channel.workspaceId === query.workspaceId)
        .filter((channel) => !query.status || channel.status === query.status)
        .filter((channel) => !query.platform || normalize(channel.platform ?? "") === normalize(query.platform))
        .filter((channel) => !query.search || `${channel.name} ${channel.description} ${channel.handle ?? ""}`.toLowerCase().includes(query.search.toLowerCase()))
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    });
  }

  getChannel(channelId: string): Promise<ApiResult<VersionedResource<Channel>>> {
    return this.execute(() => {
      const channel = this.data.channels.find((item) => item.id === channelId);
      if (!channel) this.fail("channel_not_found", "未找到该发布频道。", "channelId");
      return { value: channel, etag: `"${channel.revision}"` };
    });
  }

  saveChannel(draft: ChannelDraft, etag?: string): Promise<ApiResult<VersionedResource<Channel>>> {
    return this.execute(() => {
      this.findWorkspace(draft.workspaceId);
      if (!draft.name.trim()) this.fail("channel_name_required", "请输入发布频道名称。", "name");
      const estimate = this.calculateEstimate({
        workspaceId: draft.workspaceId,
        topic: "频道组合校验",
        channelId: draft.id,
        composition: draft.defaultComposition,
      });
      if (estimate.capabilityGaps.length > 0) {
        this.fail("channel_composition_invalid", "默认组合存在能力缺口，无法保存。");
      }
      const now = new Date().toISOString();
      if (draft.id) {
        const channel = this.data.channels.find((item) => item.id === draft.id);
        if (!channel) this.fail("channel_not_found", "未找到该发布频道。", "id");
        if (etag !== `"${channel.revision}"`) this.fail("REVISION_CONFLICT", "频道已在其他位置更新，请刷新后重试。", "If-Match", 412);
        if (channel.workspaceId !== draft.workspaceId) {
          this.fail("channel_workspace_mismatch", "发布频道不属于目标工作区。", "workspaceId");
        }
        Object.assign(channel, {
          slug: draft.slug?.trim() || channel.slug,
          name: draft.name.trim(),
          description: draft.description.trim(),
          platform: draft.platform,
          handle: draft.handle,
          platformConnectionId: draft.platformConnectionId,
          status: draft.status ?? channel.status,
          defaultComposition: clone(draft.defaultComposition),
          brandConfig: clone(draft.brandConfig),
          revision: channel.revision + 1,
          updatedAt: now,
        });
        return { value: channel, etag: `"${channel.revision}"` };
      }
      const channelId = this.nextId("channel");
      const channel: Channel = {
        id: channelId,
        workspaceId: draft.workspaceId,
        slug: draft.slug?.trim() || slugify(draft.name) || `channel-${channelId}`,
        name: draft.name.trim(),
        description: draft.description.trim(),
        platform: draft.platform,
        handle: draft.handle,
        platformConnectionId: draft.platformConnectionId,
        status: draft.status ?? "active",
        defaultComposition: clone(draft.defaultComposition),
        brandConfig: clone(draft.brandConfig),
        revision: 1,
        createdBy: this.data.session.user.id,
        createdAt: now,
        updatedAt: now,
      };
      this.data.channels.unshift(channel);
      return { value: channel, etag: '"1"' };
    });
  }

  archiveChannel(channelId: string, etag: string): Promise<ApiResult<void>> {
    return this.execute(() => {
      const channel = this.data.channels.find((item) => item.id === channelId);
      if (!channel) this.fail("channel_not_found", "未找到该发布频道。", "channelId", 404);
      if (!etag) this.fail("PRECONDITION_REQUIRED", "缺少频道版本标识，请刷新后重试。", "If-Match", 428);
      if (etag !== `"${channel.revision}"`) {
        this.fail("REVISION_CONFLICT", "频道已在其他位置更新，请刷新后重试。", "If-Match", 412);
      }
      channel.status = "archived";
      channel.revision += 1;
      channel.updatedAt = new Date().toISOString();
    });
  }
}

export function createMockAdapter(
  options: CreateMockAdapterOptions = {},
): FrameFactoryAdapter {
  return new InMemoryFrameFactoryAdapter(options);
}
