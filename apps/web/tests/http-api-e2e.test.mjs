import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import { HttpFrameFactoryAdapter } from "../lib/api/http-adapter.ts";

const repositoryRoot = fileURLToPath(new URL("../../../", import.meta.url));
const apiSource = path.join(repositoryRoot, "apps", "api", "src");

async function unusedPort() {
  const listener = net.createServer();
  await new Promise((resolve, reject) => {
    listener.once("error", reject);
    listener.listen(0, "127.0.0.1", resolve);
  });
  const address = listener.address();
  assert.ok(address && typeof address === "object");
  await new Promise((resolve, reject) => listener.close((error) => error ? reject(error) : resolve()));
  return address.port;
}

async function waitUntilHealthy(baseUrl, process, output) {
  const deadline = Date.now() + 15_000;
  while (Date.now() < deadline) {
    if (process.exitCode !== null) {
      throw new Error(`uvicorn exited before becoming healthy:\n${output.join("")}`);
    }
    try {
      const response = await fetch(`${baseUrl}/healthz`);
      if (response.ok) return;
    } catch {
      // The socket is expected to refuse connections until uvicorn has bound it.
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`uvicorn did not become healthy:\n${output.join("")}`);
}

async function stopProcess(process) {
  if (process.exitCode !== null) return;
  const exited = new Promise((resolve) => process.once("exit", resolve));
  process.kill();
  const stopped = await Promise.race([
    exited.then(() => true),
    new Promise((resolve) => setTimeout(() => resolve(false), 5_000)),
  ]);
  if (!stopped && process.exitCode === null) process.kill("SIGKILL");
}

test("production Web adapter completes the control loop over real HTTP", { timeout: 30_000 }, async (context) => {
  const port = await unusedPort();
  const baseUrl = `http://127.0.0.1:${port}`;
  const output = [];
  const server = spawn(process.env.PYTHON ?? "python", [
    "-m", "uvicorn", "framefactory_api.main:app",
    "--host", "127.0.0.1", "--port", String(port), "--log-level", "warning",
  ], {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      NO_PROXY: "127.0.0.1,localhost",
      PYTHONPATH: [apiSource, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  server.stdout.on("data", (chunk) => output.push(String(chunk)));
  server.stderr.on("data", (chunk) => output.push(String(chunk)));
  context.after(() => stopProcess(server));
  await waitUntilHealthy(baseUrl, server, output);

  const adapter = new HttpFrameFactoryAdapter({ baseUrl, testPollIntervalMs: 0 });
  const session = await adapter.getSession();
  assert.equal(session.ok, true);
  assert.ok(session.ok && session.data.activeWorkspaceId);
  const workspaceId = session.ok ? session.data.activeWorkspaceId : "";

  const suffix = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  const created = await adapter.createSkill({
    kind: "blank",
    workspaceId,
    identity: {
      name: "Web HTTP E2E Skill",
      slug: `web-http-e2e-${suffix}`,
      description: "Created by the production Web adapter over a real TCP socket.",
      visibility: "private",
    },
    initialSpec: {
      inputSchema: {
        type: "object",
        title: "Run input",
        properties: { topic: { type: "string", title: "Topic" } },
        required: ["topic"],
      },
      writingInstructions: "Build one clear argument and distinguish verified facts from interpretation.",
    },
  });
  assert.equal(created.ok, true);
  const skillId = created.ok ? created.data.id : "";

  const detail = await adapter.getSkill(skillId);
  assert.equal(detail.ok, true);
  const draft = detail.ok ? detail.data.versions.find((version) => version.state === "draft") : undefined;
  assert.ok(draft);

  const saved = await adapter.saveDraft(
    skillId,
    draft.id,
    { testTopics: ["Why a real adapter boundary prevents mock drift"] },
    draft.revision,
  );
  assert.equal(saved.ok, true);

  const validation = await adapter.validateVersion(skillId, draft.id);
  assert.equal(validation.ok, true);
  assert.equal(validation.ok && validation.data.ready, true);

  const published = await adapter.publishVersion(skillId, draft.id, "Validated over real HTTP");
  assert.equal(published.ok, true);
  assert.equal(published.ok && published.data.state, "published");
  const publishedVersionId = published.ok ? published.data.id : "";

  const comparison = await adapter.compareVersions({
    skillId,
    leftVersionId: publishedVersionId,
    rightVersionId: publishedVersionId,
    topic: "How does the control loop stay consistent?",
  });
  assert.equal(comparison.ok, true);

  const composerOptions = await adapter.getComposerOptions(workspaceId);
  assert.equal(composerOptions.ok, true, JSON.stringify(composerOptions));
  const pipelineVersionId = composerOptions.ok ? composerOptions.data.pipelines[0]?.id ?? "" : "";
  assert.ok(pipelineVersionId, "the packaged catalog must expose a runnable PipelineVersion");
  const channelDraft = {
    workspaceId,
    name: `Web HTTP Channel ${suffix}`,
    description: "Created and versioned through the production Web adapter.",
    platform: "youtube",
    handle: `@web-${suffix}`,
    status: "active",
    defaultComposition: {
      skillVersionId: publishedVersionId,
      assetLibraryIds: [],
      pipelineVersionId,
    },
    brandConfig: { profile: { tone: "clear" } },
  };
  const createdChannel = await adapter.saveChannel(channelDraft, undefined, `web-http-channel-${suffix}`);
  assert.equal(createdChannel.ok, true, JSON.stringify(createdChannel));
  const channelId = createdChannel.ok ? createdChannel.data.value.id : "";
  const loadedChannel = await adapter.getChannel(channelId);
  assert.equal(loadedChannel.ok && loadedChannel.data.value.name, channelDraft.name);
  assert.ok(loadedChannel.ok && loadedChannel.data.etag);
  const pausedChannel = await adapter.saveChannel(
    { ...channelDraft, id: channelId, status: "paused" },
    loadedChannel.ok ? loadedChannel.data.etag : "",
    `web-http-channel-pause-${suffix}`,
  );
  assert.equal(pausedChannel.ok && pausedChannel.data.value.status, "paused");
  const resumedChannel = await adapter.saveChannel(
    { ...channelDraft, id: channelId, status: "active" },
    pausedChannel.ok ? pausedChannel.data.etag : "",
    `web-http-channel-resume-${suffix}`,
  );
  assert.equal(resumedChannel.ok && resumedChannel.data.value.status, "active");
  const run = await adapter.createRun({
    workspaceId,
    channelId,
    topic: "A Run created by the production HTTP adapter",
    composition: {
      skillVersionId: publishedVersionId,
      assetLibraryIds: [],
      pipelineVersionId,
    },
  }, `web-http-e2e-run-${suffix}`);
  assert.equal(run.ok, true, JSON.stringify(run));
  assert.equal(run.ok && run.data.status, "queued");
  assert.equal(run.ok && run.data.channelId, channelId);
  const queriedRun = await adapter.getRun(run.ok ? run.data.id : "");
  assert.equal(queriedRun.ok && queriedRun.data.id, run.ok && run.data.id);
  assert.equal(queriedRun.ok && queriedRun.data.status, run.ok && run.data.status);
  assert.equal(queriedRun.ok && queriedRun.data.stepsAvailable, true);

  const archivedChannel = await adapter.archiveChannel(
    channelId,
    resumedChannel.ok ? resumedChannel.data.etag : "",
  );
  assert.equal(archivedChannel.ok, true, JSON.stringify(archivedChannel));
  const loadedArchivedChannel = await adapter.getChannel(channelId);
  assert.equal(loadedArchivedChannel.ok && loadedArchivedChannel.data.value.status, "archived");

  const fork = await adapter.createSkill({
    kind: "fork",
    workspaceId,
    identity: {
      name: "Web HTTP E2E Fork",
      slug: `web-http-e2e-fork-${suffix}`,
      description: "Forked through the same production adapter and API.",
      visibility: "private",
    },
    sourceSkillId: skillId,
    sourceVersionId: publishedVersionId,
  });
  assert.equal(fork.ok, true);
  assert.equal(fork.ok && fork.data.forkedFrom?.skillId, skillId);
});
