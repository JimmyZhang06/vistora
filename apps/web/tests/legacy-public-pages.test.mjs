import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

async function loadWorker() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  return (await import(workerUrl.href)).default;
}

function staticAssets() {
  return {
    async fetch(request) {
      const pathname = new URL(request.url).pathname;
      const file = new URL(`../public${pathname}`, import.meta.url);
      try {
        return new Response(await readFile(file), {
          headers: { "content-type": pathname.endsWith(".html") ? "text/html; charset=utf-8" : "application/octet-stream" },
        });
      } catch {
        return new Response("Not found", { status: 404 });
      }
    },
  };
}

async function render(worker, pathname) {
  return worker.fetch(
    new Request(`http://localhost${pathname}`, { headers: { accept: "text/html" } }),
    { ASSETS: staticAssets() },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("keeps the Vistora product introduction at both public entry routes", async () => {
  const worker = await loadWorker();
  for (const pathname of ["/", "/product"]) {
    const response = await render(worker, pathname);
    assert.equal(response.status, 200, pathname);
    const html = await response.text();
    assert.match(html, /<title>Vistora — 从主题到成片<\/title>/);
    assert.doesNotMatch(html, /FrameFactory/);
    assert.match(html, /一个主题，/);
    assert.match(html, /href="\/login\?returnTo=\/create"/);
  }
});

test("keeps the legacy login presentation and delegates credentials to hosted sign-in", async () => {
  const worker = await loadWorker();
  const response = await render(worker, "/login?returnTo=/skills");
  assert.equal(response.status, 200);
  const html = await response.text();
  assert.match(html, /<title>登录 — Vistora<\/title>/);
  assert.doesNotMatch(html, /FrameFactory/);
  assert.match(html, /让想法，继续生长。/);
  assert.match(html, /\/signin-with-chatgpt\?return_to=/);
  assert.doesNotMatch(html, /type="password"/);
});
