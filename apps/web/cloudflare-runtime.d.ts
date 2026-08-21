interface Fetcher {
  fetch(input: Request | string | URL, init?: RequestInit): Promise<Response>;
}

interface D1Database {
  readonly __framefactoryD1Binding?: never;
}

declare module "cloudflare:workers" {
  export const env: {
    DB?: D1Database;
    [binding: string]: unknown;
  };
}
