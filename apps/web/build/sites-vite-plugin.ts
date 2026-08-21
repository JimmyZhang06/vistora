import { access, cp, mkdir, rm } from "node:fs/promises";
import { resolve } from "node:path";
import type { Plugin } from "vite";

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

// Packages Sites metadata and migrations after Vite finishes compiling.
export function sites(): Plugin {
  let root = process.cwd();
  let packaging: Promise<void> | undefined;

  return {
    name: "sites",
    apply: "build",
    configResolved(config) {
      root = config.root;
    },
    async closeBundle() {
      // Vinext runs multiple Vite environments whose closeBundle hooks may
      // overlap. Package the shared Sites metadata once so one environment
      // cannot remove `dist/.openai` while another is copying into it.
      packaging ??= (async () => {
        const outputDirectory = resolve(root, "dist", ".openai");
        const hostingConfig = resolve(root, ".openai", "hosting.json");
        const drizzleSource = resolve(root, "drizzle");

        await rm(outputDirectory, { recursive: true, force: true });
        await mkdir(outputDirectory, { recursive: true });

        if (await exists(hostingConfig)) {
          await cp(hostingConfig, resolve(outputDirectory, "hosting.json"));
        }
        if (await exists(drizzleSource)) {
          await cp(drizzleSource, resolve(outputDirectory, "drizzle"), {
            recursive: true,
          });
        }
      })();

      await packaging;
    },
  };
}
