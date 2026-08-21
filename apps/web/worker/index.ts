/** Cloudflare Worker entry point for the vinext-starter template. */
import { handleImageOptimization, DEFAULT_DEVICE_SIZES, DEFAULT_IMAGE_SIZES } from "vinext/server/image-optimization";
import handler from "vinext/server/app-router-entry";

interface Env {
  ASSETS?: Fetcher;
  DB: D1Database;
  IMAGES: {
    input(stream: ReadableStream): {
      transform(options: Record<string, unknown>): {
        output(options: { format: string; quality: number }): Promise<{ response(): Response }>;
      };
    };
  };
}

interface ExecutionContext {
  waitUntil(promise: Promise<unknown>): void;
  passThroughOnException(): void;
}

// Image security config. SVG sources with .svg extension auto-skip the
// optimization endpoint on the client side (served directly, no proxy).
// To route SVGs through the optimizer (with security headers), set
// dangerouslyAllowSVG: true in next.config.js and uncomment below:
// const imageConfig: ImageConfig = { dangerouslyAllowSVG: true };

const worker = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    const url = new URL(request.url);

    const legacyPage = {
      "/": "/legacy/product/index.html",
      "/product": "/legacy/product/index.html",
      "/product/": "/legacy/product/index.html",
      "/login": "/legacy/login/index.html",
      "/login/": "/legacy/login/index.html",
    }[url.pathname];

    if (legacyPage && (request.method === "GET" || request.method === "HEAD")) {
      const assetUrl = new URL(legacyPage, request.url);
      const assetRequest = new Request(assetUrl, request);
      return env.ASSETS
        ? env.ASSETS.fetch(assetRequest)
        : Response.redirect(assetUrl, 307);
    }

    if (url.pathname === "/_vinext/image") {
      const allowedWidths = [...DEFAULT_DEVICE_SIZES, ...DEFAULT_IMAGE_SIZES];
      return handleImageOptimization(request, {
        fetchAsset: (path) => {
          if (!env.ASSETS) throw new Error("Static asset binding is unavailable");
          return env.ASSETS.fetch(new Request(new URL(path, request.url)));
        },
        transformImage: async (body, { width, format, quality }) => {
          const result = await env.IMAGES.input(body).transform(width > 0 ? { width } : {}).output({ format, quality });
          return result.response();
        },
      }, allowedWidths);
    }

    return handler.fetch(request, env, ctx);
  },
};

export default worker;
