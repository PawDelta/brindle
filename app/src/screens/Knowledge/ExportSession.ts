/* Export to Google Docs or SharePoint from inside the app, for the length of
   one session. The person signs in with the provider in the system browser
   (OAuth authorization code with PKCE; the redirect is a loopback address the
   shell listens on, src-tauri/src/oauth.rs). The tokens live in this object's
   fields and nowhere else: not in localStorage, not on disk, not sent to the
   daemon or brindle. Closing the app, or dropping the object, forgets them.
   Then the Word export (a docx the daemon fetched) is uploaded straight to the
   provider: Google Drive (converted to a Google Doc, drive.file scope) or a
   chosen Microsoft site and folder through Microsoft Graph. */

import { invoke, isTauri } from "@tauri-apps/api/core";
import { openExternal } from "../../api/open";

export type ExportProvider = "google" | "microsoft";

/* What the sign-in needs from the shell; the app's own are `shellDeps`. */
export interface ExportDeps {
  openUrl(url: string): Promise<void>;
  /* Starts listening on a free loopback port and says which. */
  listen(): Promise<number>;
  /* The query string of the redirect that arrives on that port. */
  wait(port: number): Promise<string>;
  cancel(port: number): void;
  fetch: typeof globalThis.fetch;
}

export const shellDeps: ExportDeps = {
  openUrl: openExternal,
  listen: () => {
    if (!isTauri()) throw new Error("Signing in to Google or Microsoft needs the brindle desktop app.");
    return invoke<number>("oauth_listen");
  },
  wait: (port) => invoke<string>("oauth_wait", { port }),
  cancel: (port) => void invoke("oauth_cancel", { port }).catch(() => undefined),
  fetch: providerFetch,
};

/* The webview's CSP lets it reach no outside host, so provider calls go through the
   shell (`provider_http`, which only talks to Google's and Microsoft's hosts). */
async function providerFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  if (!isTauri()) throw new Error("Signing in to Google or Microsoft needs the brindle desktop app.");
  const body = init.body;
  const bytes = body == null ? undefined : body instanceof Blob ? new Uint8Array(await body.arrayBuffer()) : new TextEncoder().encode(String(body));
  const reply = await invoke<{ status: number; body: string }>("provider_http", {
    method: init.method ?? "GET",
    url: String(input),
    headers: Object.entries((init.headers ?? {}) as Record<string, string>),
    body: bytes ? Array.from(bytes) : null,
  });
  return new Response(reply.body, { status: reply.status });
}

const PROVIDERS = {
  google: {
    authorize: "https://accounts.google.com/o/oauth2/v2/auth",
    token: "https://oauth2.googleapis.com/token",
    scope: "https://www.googleapis.com/auth/drive.file",
    host: "127.0.0.1",
  },
  microsoft: {
    authorize: "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
    token: "https://login.microsoftonline.com/common/oauth2/v2.0/token",
    scope: "Files.ReadWrite.All Sites.Read.All offline_access",
    host: "localhost",
  },
} as const;

/* How long a sign-in may wait for the person before it is given up. */
export const GIVE_UP_MS = 5 * 60 * 1000;

const DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document";

const b64url = (bytes: Uint8Array) => btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const random = (n = 32) => b64url(crypto.getRandomValues(new Uint8Array(n)));

/* PKCE (RFC 7636): a secret verifier, and its SHA-256 challenge sent up front. */
export async function pkce(): Promise<{ verifier: string; challenge: string }> {
  const verifier = random();
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return { verifier, challenge: b64url(new Uint8Array(digest)) };
}

export interface SharePointSite {
  id: string;
  name: string;
}

export interface Uploaded {
  url: string | null;
}

export class ExportSession {
  /* In memory only. */
  private tokens: Partial<Record<ExportProvider, string>> = {};
  private refresh: Partial<Record<ExportProvider, string>> = {};

  constructor(private readonly deps: ExportDeps = shellDeps) {}

  signedIn(p: ExportProvider): boolean {
    return !!this.tokens[p];
  }

  /* The refresh token where the provider gave one, else the access token: what an admin hands to a team destination. */
  credential(p: ExportProvider): string | undefined {
    return this.refresh[p] ?? this.tokens[p];
  }

  signOut(): void {
    this.tokens = {};
    this.refresh = {};
  }

  /* Opens the provider's sign-in in the system browser and waits for it to come back. */
  async signIn(p: ExportProvider, clientId: string): Promise<void> {
    const cfg = PROVIDERS[p];
    const port = await this.deps.listen();
    const redirect = `http://${cfg.host}:${port}/callback`;
    const state = random(16);
    const { verifier, challenge } = await pkce();
    const url = new URL(cfg.authorize);
    url.search = new URLSearchParams({
      client_id: clientId,
      redirect_uri: redirect,
      response_type: "code",
      scope: cfg.scope,
      state,
      code_challenge: challenge,
      code_challenge_method: "S256",
      ...(p === "google" ? { access_type: "offline", prompt: "consent" } : {}),
    }).toString();
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      await this.deps.openUrl(url.toString());
      const give = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          this.deps.cancel(port);
          reject(new Error("The sign-in took too long. Try again."));
        }, GIVE_UP_MS);
      });
      const query = new URLSearchParams(await Promise.race([this.deps.wait(port), give]));
      if (query.get("state") !== state) throw new Error("The sign-in answer didn't match the request, so it was ignored.");
      const code = query.get("code");
      if (!code) throw new Error(query.get("error_description") ?? query.get("error") ?? "The sign-in was refused.");
      const res = await this.deps.fetch(cfg.token, {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({ client_id: clientId, grant_type: "authorization_code", code, redirect_uri: redirect, code_verifier: verifier }),
      });
      const got = (await res.json()) as { access_token?: string; refresh_token?: string; error_description?: string; error?: string };
      if (!res.ok || !got.access_token) throw new Error(got.error_description ?? got.error ?? `Sign-in failed (${res.status}).`);
      this.tokens[p] = got.access_token;
      if (got.refresh_token) this.refresh[p] = got.refresh_token;
    } finally {
      clearTimeout(timer);
      this.deps.cancel(port);
    }
  }

  private async call(p: ExportProvider, url: string, init: RequestInit): Promise<Response> {
    const token = this.tokens[p];
    if (!token) throw new Error("Sign in first.");
    const res = await this.deps.fetch(url, { ...init, headers: { ...init.headers, Authorization: `Bearer ${token}` } });
    if (res.status === 401) delete this.tokens[p];
    if (!res.ok) throw new Error(`${p === "google" ? "Google" : "Microsoft"} refused it (${res.status}).`);
    return res;
  }

  /* The docx becomes a Google Doc in the person's Drive (drive.file: only files this app makes). */
  async uploadToGoogle(name: string, docx: Uint8Array<ArrayBuffer>): Promise<Uploaded> {
    const edge = `brindle${random(12)}`;
    const head = `--${edge}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n${JSON.stringify({ name, mimeType: "application/vnd.google-apps.document" })}\r\n--${edge}\r\nContent-Type: ${DOCX}\r\n\r\n`;
    const body = new Blob([head, docx, `\r\n--${edge}--`]);
    const res = await this.call("google", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink", {
      method: "POST",
      headers: { "Content-Type": `multipart/related; boundary=${edge}` },
      body,
    });
    const got = (await res.json()) as { webViewLink?: string };
    return { url: got.webViewLink ?? null };
  }

  async findSites(query: string): Promise<SharePointSite[]> {
    const res = await this.call("microsoft", `https://graph.microsoft.com/v1.0/sites?search=${encodeURIComponent(query)}`, {});
    const got = (await res.json()) as { value?: { id: string; displayName?: string; name?: string }[] };
    return (got.value ?? []).map((s) => ({ id: s.id, name: s.displayName ?? s.name ?? s.id }));
  }

  /* The docx into `folder` (a path in the site's default library, "" for its root). */
  async uploadToSharePoint(siteId: string, folder: string, name: string, docx: Uint8Array<ArrayBuffer>): Promise<Uploaded> {
    const path = [...folder.split("/"), name].filter(Boolean).map(encodeURIComponent).join("/");
    const res = await this.call("microsoft", `https://graph.microsoft.com/v1.0/sites/${encodeURIComponent(siteId)}/drive/root:/${path}:/content`, {
      method: "PUT",
      headers: { "Content-Type": DOCX },
      body: new Blob([docx]),
    });
    const got = (await res.json()) as { webUrl?: string };
    return { url: got.webUrl ?? null };
  }
}
