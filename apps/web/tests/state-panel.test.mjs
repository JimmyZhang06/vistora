import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("long state labels use a pill and network failures stay user-facing", async () => {
  const [panel, composer, styles] = await Promise.all([
    readFile(new URL("../components/page-heading.tsx", import.meta.url), "utf8"),
    readFile(new URL("../components/create-composer.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
  ]);

  assert.match(panel, /code\.length > 3 \? "pill" : "circle"/);
  assert.match(panel, /data-shape=\{codeShape\}/);
  assert.match(styles, /\.state-code\[data-shape="pill"\]/);
  assert.match(composer, /无法连接到控制服务。你的输入尚未提交，也不会丢失。/);
  assert.doesNotMatch(composer, /description=\{`\$\{loadError\}/);
});
