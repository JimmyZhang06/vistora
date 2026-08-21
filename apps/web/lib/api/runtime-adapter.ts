import type { FrameFactoryAdapter } from "./adapter";
import { createHttpAdapter, type HttpAdapterOptions } from "./http-adapter";

/** The browser runtime always uses the real control API. Mocks live in test fixtures only. */
export function createFrameFactoryAdapter(options: HttpAdapterOptions = {}): FrameFactoryAdapter {
  return createHttpAdapter(options);
}
