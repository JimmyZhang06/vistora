import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("projects page keeps loading, error, and loaded controls mutually exclusive", async () => {
  const source = await readFile(new URL("../components/projects-view.tsx", import.meta.url), "utf8");

  assert.match(source, /projects === null && !error/);
  assert.match(source, /projects !== null \? <div className="skill-toolbar projects-toolbar">/);
  assert.match(source, /className="projects-loading"/);
  assert.doesNotMatch(source, /className="loading-grid" aria-label="正在加载项目"/);
  assert.match(source, /title="暂时无法读取项目"/);
  assert.match(source, /onClick=\{retry\}/);
  assert.match(source, /project\.projectKind === "webpage_video"/);
  assert.match(source, /`\/webpage-video\/\$\{project\.controlRunId\}`/);
  assert.match(source, /project\.projectKind === "full_ai" \? "AI 影片"/);
});
