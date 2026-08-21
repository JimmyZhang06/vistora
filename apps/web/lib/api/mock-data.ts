import type {
  AssetLibraryOption,
  Channel,
  PipelineOption,
  Publisher,
  RenderPresetOption,
  Run,
  SessionContext,
  Skill,
  SkillSpec,
  SkillVersion,
  VoiceProfileOption,
  Workspace,
} from "./contracts";

export interface MockDatabase {
  session: SessionContext;
  workspaces: Workspace[];
  skills: Skill[];
  versions: SkillVersion[];
  channels: Channel[];
  assetLibraries: AssetLibraryOption[];
  voiceProfiles: VoiceProfileOption[];
  renderPresets: RenderPresetOption[];
  pipelines: PipelineOption[];
  runs: Run[];
}

const CREATED_AT = "2026-08-10T08:00:00.000Z";
const UPDATED_AT = "2026-08-14T03:30:00.000Z";

export const PERSONAL_WORKSPACE_ID = "workspace_personal_demo";
export const TEAM_WORKSPACE_ID = "workspace_team_demo";
export const SYSTEM_WORKSPACE_ID = "workspace_system_seed";
export const CURRENT_USER_ID = "user_demo_editor";

const personalPublisher: Publisher = {
  id: "publisher_personal_demo",
  workspaceId: PERSONAL_WORKSPACE_ID,
  type: "workspace",
  displayName: "个人创作空间",
  verified: false,
};

const teamPublisher: Publisher = {
  id: "publisher_team_demo",
  workspaceId: TEAM_WORKSPACE_ID,
  type: "workspace",
  displayName: "示范工作室",
  verified: true,
};

const systemPublisher: Publisher = {
  id: "publisher_system_seed",
  workspaceId: SYSTEM_WORKSPACE_ID,
  type: "system",
  displayName: "Vistora 官方",
  verified: true,
};

export function createDefaultSkillSpec(
  pipelineVersionId = "pipeline_version_standard_1",
): SkillSpec {
  return {
    inputSchema: {
      type: "object",
      required: ["topic"],
      properties: {
        topic: { type: "string", title: "创作主题" },
        viewpoint: { type: "string", title: "核心观点" },
      },
    },
    researchPolicy: {
      factBoundary: "strict",
      requireCitations: true,
      preferredSources: ["一手资料", "权威公开资料"],
      excludedSources: ["无法核验的转载"],
    },
    writingInstructions:
      "先明确受众与核心问题，再以可核验事实组织论点，最后给出简洁、有行动感的结论。",
    visualPolicy: {
      direction: "克制、清晰、以信息层级推动叙事",
      shotGuidance: ["开场建立主题", "关键论点使用证据画面", "结尾回到核心观点"],
      forbiddenTreatments: ["无来源的夸张数据", "与主题无关的装饰镜头"],
    },
    assetPolicy: {
      strategy: "channel_default",
      requiredTags: [],
      allowExternalAcquisition: false,
    },
    qcRubric: {
      criteria: [
        {
          id: "criterion_factuality",
          label: "事实准确",
          description: "关键事实可追溯，表述不超出证据边界。",
          minimumScore: 0.85,
        },
        {
          id: "criterion_structure",
          label: "结构完整",
          description: "开场、论证与结论之间存在清楚的推进关系。",
          minimumScore: 0.8,
        },
      ],
    },
    outputContract: {
      format: "video_script",
      fields: ["title", "hook", "narration", "shotList"],
      constraints: { language: "zh-CN", maxDurationSeconds: 120 },
    },
    modelRequirements: {
      capabilities: ["research.citations", "script.structured", "render.basic"],
    },
    defaultPipelineVersionId: pipelineVersionId,
  };
}

const baseSpec = createDefaultSkillSpec();
const conciseSpec: SkillSpec = {
  ...createDefaultSkillSpec(),
  writingInstructions:
    "用一句可验证判断开场；正文只保留三个最有解释力的要点；用问题或下一步行动收束。",
  outputContract: {
    format: "short_video_script",
    fields: ["title", "hook", "beats", "closing", "shotList"],
    constraints: { language: "zh-CN", maxDurationSeconds: 75 },
  },
};

const workspaces: Workspace[] = [
  {
    id: PERSONAL_WORKSPACE_ID,
    name: "个人创作空间",
    slug: "personal-studio",
    kind: "personal",
    role: "owner",
  },
  {
    id: TEAM_WORKSPACE_ID,
    name: "示范工作室",
    slug: "sample-studio",
    kind: "team",
    role: "editor",
  },
  {
    id: SYSTEM_WORKSPACE_ID,
    name: "系统种子空间",
    slug: "system-seeds",
    kind: "system",
    role: "viewer",
  },
];

const session: SessionContext = {
  user: {
    id: CURRENT_USER_ID,
    displayName: "林然",
    email: "demo@example.test",
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
  },
  activeWorkspaceId: PERSONAL_WORKSPACE_ID,
  // The product currently exposes one default personal workspace. The array and
  // activeWorkspaceId remain transport-compatible with a future workspace UI.
  workspaces: workspaces.filter((workspace) => workspace.kind === "personal"),
};

const skills: Skill[] = [
  {
    id: "skill_structured_narrative",
    workspaceId: SYSTEM_WORKSPACE_ID,
    name: "结构化叙事法",
    slug: "structured-narrative",
    description: "从事实边界、叙事节奏和画面证据出发组织内容。",
    publisher: systemPublisher,
    visibility: "public_readonly",
    status: "published",
    currentVersionId: "skill_version_structured_narrative_1",
    createdBy: "user_system_seed",
    createdAt: CREATED_AT,
    updatedAt: UPDATED_AT,
    permissions: {
      view: true,
      fork: true,
      edit: false,
      test: true,
      publish: false,
      deleteDraft: false,
      deprecate: false,
    },
    stats: { runCount: 128, versionCount: 1 },
  },
  {
    id: "skill_topic_insight",
    workspaceId: PERSONAL_WORKSPACE_ID,
    name: "主题洞察短片",
    slug: "topic-insight",
    description: "把一个宽泛主题收束成清晰观点与紧凑短片。",
    publisher: personalPublisher,
    visibility: "private",
    status: "draft",
    currentVersionId: "skill_version_topic_insight_1",
    draftVersionId: "skill_version_topic_insight_2_draft",
    forkedFrom: {
      skillId: "skill_structured_narrative",
      versionId: "skill_version_structured_narrative_1",
    },
    createdBy: CURRENT_USER_ID,
    createdAt: CREATED_AT,
    updatedAt: UPDATED_AT,
    permissions: {
      view: true,
      fork: true,
      edit: true,
      test: true,
      publish: true,
      deleteDraft: true,
      deprecate: true,
    },
    stats: { runCount: 7, versionCount: 2 },
  },
  {
    id: "skill_brand_story",
    workspaceId: TEAM_WORKSPACE_ID,
    name: "品牌故事结构",
    slug: "brand-story",
    description: "以受众问题、产品证据和品牌立场构建短篇故事。",
    publisher: teamPublisher,
    visibility: "workspace",
    status: "published",
    currentVersionId: "skill_version_brand_story_1",
    createdBy: "user_team_member",
    createdAt: CREATED_AT,
    updatedAt: UPDATED_AT,
    permissions: {
      view: true,
      fork: true,
      edit: true,
      test: true,
      publish: true,
      deleteDraft: false,
      deprecate: true,
    },
    stats: { runCount: 23, versionCount: 1 },
  },
];

const versions: SkillVersion[] = [
  {
    id: "skill_version_structured_narrative_1",
    skillId: "skill_structured_narrative",
    version: "1.0.0",
    schemaVersion: "1.0",
    state: "published",
    revision: 1,
    spec: baseSpec,
    testTopics: ["解释一个公共议题", "介绍一项新工具", "复盘一次产品决策"],
    contentHash: "sha256:mock-structured-narrative-1",
    releaseNotes: "首个公开版本。",
    createdBy: "user_system_seed",
    createdAt: CREATED_AT,
    publishedAt: "2026-08-11T04:00:00.000Z",
  },
  {
    id: "skill_version_topic_insight_1",
    skillId: "skill_topic_insight",
    version: "1.0.0",
    schemaVersion: "1.0",
    state: "published",
    revision: 1,
    spec: baseSpec,
    testTopics: ["远程协作", "城市公共空间", "个人知识管理"],
    contentHash: "sha256:mock-topic-insight-1",
    releaseNotes: "建立基础叙事与质量规则。",
    createdBy: CURRENT_USER_ID,
    createdAt: CREATED_AT,
    publishedAt: "2026-08-12T04:00:00.000Z",
  },
  {
    id: "skill_version_topic_insight_2_draft",
    skillId: "skill_topic_insight",
    version: "1.1.0-draft",
    schemaVersion: "1.0",
    state: "draft",
    revision: 3,
    spec: conciseSpec,
    testTopics: ["远程协作", "城市公共空间", "个人知识管理"],
    releaseNotes: "缩短开场并加强结尾行动感。",
    createdBy: CURRENT_USER_ID,
    createdAt: "2026-08-13T06:00:00.000Z",
  },
  {
    id: "skill_version_brand_story_1",
    skillId: "skill_brand_story",
    version: "1.0.0",
    schemaVersion: "1.0",
    state: "published",
    revision: 1,
    spec: {
      ...createDefaultSkillSpec(),
      writingInstructions:
        "从受众的真实问题出发，以可验证的产品证据展开，品牌主张只在结尾自然出现。",
    },
    testTopics: ["新品介绍", "团队幕后", "客户案例"],
    contentHash: "sha256:mock-brand-story-1",
    releaseNotes: "工作区首发版本。",
    createdBy: "user_team_member",
    createdAt: CREATED_AT,
    publishedAt: "2026-08-13T02:00:00.000Z",
  },
];

const channels: Channel[] = [
  {
    id: "channel_personal_main",
    workspaceId: PERSONAL_WORKSPACE_ID,
    slug: "daily-publishing",
    name: "日常发布",
    description: "面向通用知识短片的发布频道。",
    platform: "douyin",
    handle: "@daily-stories",
    status: "active",
    defaultComposition: {
      skillVersionId: "skill_version_topic_insight_1",
      assetLibraryIds: ["asset_library_general"],
      voiceProfileId: "voice_profile_calm",
      renderPresetVersionId: "render_preset_version_portrait_1",
      pipelineVersionId: "pipeline_version_standard_1",
    },
    brandConfig: { profile: {} },
    revision: 1,
    createdBy: CURRENT_USER_ID,
    createdAt: CREATED_AT,
    updatedAt: UPDATED_AT,
  },
];

const assetLibraries: AssetLibraryOption[] = [
  {
    id: "asset_library_general",
    workspaceId: PERSONAL_WORKSPACE_ID,
    name: "通用画面库",
    description: "经过来源标记的通用环境、人物与物件画面。",
    assetCount: 42,
  },
];

const voiceProfiles: VoiceProfileOption[] = [
  {
    id: "voice_profile_calm",
    workspaceId: PERSONAL_WORKSPACE_ID,
    name: "清晰中性声线",
    provider: "Mock Voice",
  },
];

const renderPresets: RenderPresetOption[] = [
  {
    id: "render_preset_version_portrait_1",
    workspaceId: PERSONAL_WORKSPACE_ID,
    presetId: "render_preset_portrait",
    name: "信息流竖屏",
    version: "1.0.0",
    aspectRatio: "9:16",
    capabilities: ["render.basic", "render.subtitles"],
  },
];

const pipelines: PipelineOption[] = [
  {
    id: "pipeline_version_standard_1",
    workspaceId: PERSONAL_WORKSPACE_ID,
    pipelineId: "pipeline_standard",
    name: "标准内容生产",
    version: "1.0.0",
    capabilities: ["research.citations", "script.structured", "render.basic"],
  },
];

const runs: Run[] = [
  {
    id: "run_sample_completed",
    workspaceId: PERSONAL_WORKSPACE_ID,
    topic: "如何让复杂信息更容易理解",
    channelId: "channel_personal_main",
    status: "succeeded",
    composition: channels[0].defaultComposition,
    estimate: {
      cost: { amount: 2.6, currency: "CNY" },
      durationSeconds: 96,
      capabilityGaps: [],
    },
    steps: [
      { id: "run_step_research", key: "research", type: "research", label: "研究", status: "succeeded", attemptCount: 1, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: "2026-08-14T01:03:00.000Z" },
      { id: "run_step_script", key: "writing", type: "writing", label: "写作", status: "succeeded", attemptCount: 1, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: "2026-08-14T01:03:00.000Z" },
      { id: "run_step_render", key: "render", type: "render", label: "渲染", status: "succeeded", attemptCount: 1, maxAttempts: 3, dependencies: [], reviewRequired: false, updatedAt: "2026-08-14T01:03:00.000Z" },
    ],
    artifacts: [],
    createdBy: CURRENT_USER_ID,
    createdAt: "2026-08-14T01:00:00.000Z",
    updatedAt: "2026-08-14T01:03:00.000Z",
  },
];

const NORMAL_MOCK_DATA: MockDatabase = {
  session,
  workspaces,
  skills,
  versions,
  channels,
  assetLibraries,
  voiceProfiles,
  renderPresets,
  pipelines,
  runs,
};

export function createNormalMockData(): MockDatabase {
  return structuredClone(NORMAL_MOCK_DATA);
}

export function createEmptyMockData(): MockDatabase {
  const data = createNormalMockData();
  return {
    ...data,
    skills: [],
    versions: [],
    channels: [],
    assetLibraries: [],
    voiceProfiles: [],
    renderPresets: [],
    pipelines: [],
    runs: [],
  };
}
