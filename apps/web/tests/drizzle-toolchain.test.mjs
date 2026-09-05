import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, readdir, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative, resolve, sep } from "node:path";
import { DatabaseSync } from "node:sqlite";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { runInNewContext } from "node:vm";

const require = createRequire(import.meta.url);
const run = promisify(execFile);
const webRoot = fileURLToPath(new URL("../", import.meta.url));
const kitCli = join(require.resolve("drizzle-kit"), "..", "bin.cjs");

// Stable Drizzle Kit still includes this legacy loader. Exercise its actual
// transform APIs while its esbuild is overridden for GHSA-67mh-4wv8-2f99.
test("Drizzle's legacy loader transforms TypeScript with the patched esbuild", async () => {
  const { transform, transformSync } = require("@esbuild-kit/core-utils");
  const source = "type Count = number; export const answer: Count = 42;";
  const esm = await transform(source, join(tmpdir(), "drizzle-toolchain-fixture.mts"));
  const compiled = await import(`data:text/javascript,${encodeURIComponent(esm.code)}`);
  assert.equal(compiled.answer, 42);
  assert.equal(esm.map.version, 3);

  const cjs = transformSync(source, join(tmpdir(), "drizzle-toolchain-fixture.cts"));
  const sandbox = { module: { exports: {} } };
  runInNewContext(cjs.code, sandbox);
  assert.equal(sandbox.module.exports.answer, 42);
  assert.equal(cjs.map.version, 3);
});

test("Drizzle generates usable SQLite migrations and no duplicate migration", { timeout: 60_000 }, async (t) => {
  const tempRoot = resolve(tmpdir());
  const workspace = await mkdtemp(join(tempRoot, "vistora-drizzle-"));
  t.after(async () => {
    const child = relative(tempRoot, resolve(workspace));
    assert.ok(child && !isAbsolute(child) && !child.startsWith(`..${sep}`) && child !== "..");
    await rm(workspace, { recursive: true, force: true });
  });
  const generate = (schema, out) => run(process.execPath, [
    kitCli, "generate", "--dialect", "sqlite", "--schema", schema, "--out", out,
  ], { cwd: webRoot, timeout: 20_000, maxBuffer: 1_048_576 });

  const emptyOut = join(workspace, "empty");
  await generate("./db/schema.ts", emptyOut);
  assert.deepEqual((await readdir(emptyOut)).filter((name) => name.endsWith(".sql")), []);

  const exampleOut = join(workspace, "example");
  await generate("./examples/d1/db/schema.ts", exampleOut);
  const sqlFiles = (await readdir(exampleOut)).filter((name) => name.endsWith(".sql"));
  assert.equal(sqlFiles.length, 1);
  const migration = await readFile(join(exampleOut, sqlFiles[0]), "utf8");
  const db = new DatabaseSync(":memory:");
  t.after(() => db.close());
  db.exec(migration);
  db.prepare("INSERT INTO notes (title) VALUES (?)").run("toolchain compatibility");
  const note = db.prepare("SELECT * FROM notes").get();
  assert.equal(note.id, 1);
  assert.equal(note.title, "toolchain compatibility");
  assert.equal(note.content, "");
  assert.match(note.created_at, /^\d{4}-\d{2}-\d{2} /);

  await generate("./examples/d1/db/schema.ts", exampleOut);
  assert.deepEqual((await readdir(exampleOut)).filter((name) => name.endsWith(".sql")), sqlFiles);
  const journal = JSON.parse(await readFile(join(exampleOut, "meta", "_journal.json"), "utf8"));
  assert.equal(journal.entries.length, 1);
});
