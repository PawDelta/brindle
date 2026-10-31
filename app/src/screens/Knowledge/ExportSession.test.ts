import { afterEach, describe, expect, it, vi } from "vitest";
import { ExportSession, type ExportDeps } from "./ExportSession";

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

describe("ExportSession", () => {
  it("signs in with PKCE through the system browser and keeps the tokens out of storage", async () => {
    let opened = new URL("https://x.test");
    const requests: { url: string; body?: BodyInit | null }[] = [];
    const deps: ExportDeps = {
      openUrl: async (u) => void (opened = new URL(u)),
      listen: async () => 5151,
      wait: async () => `code=abc&state=${opened.searchParams.get("state")}`,
      cancel: vi.fn(),
      fetch: async (input, init) => {
        requests.push({ url: String(input), body: init?.body });
        const reply = String(input).includes("token") ? { access_token: "ACCESS-SECRET", refresh_token: "REFRESH-SECRET" } : { webViewLink: "https://docs.google.com/d/1" };
        return new Response(JSON.stringify(reply), { status: 200 });
      },
    };
    const session = new ExportSession(deps);
    await session.signIn("google", "client-1");

    expect(opened.searchParams.get("redirect_uri")).toBe("http://127.0.0.1:5151/callback");
    expect(opened.searchParams.get("code_challenge_method")).toBe("S256");
    expect(opened.searchParams.get("scope")).toBe("https://www.googleapis.com/auth/drive.file");
    const exchanged = requests[0].body as URLSearchParams;
    expect(exchanged.get("code")).toBe("abc");
    expect(exchanged.get("code_verifier")).toBeTruthy();
    expect(session.signedIn("google")).toBe(true);
    expect((await session.uploadToGoogle("Doc", new Uint8Array([1]))).url).toBe("https://docs.google.com/d/1");

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    session.signOut();
    expect(session.signedIn("google")).toBe(false);
  });

  it("sends the Google client secret in the token request only when export-config gave one", async () => {
    let opened = new URL("https://x.test");
    const bodies: URLSearchParams[] = [];
    const deps: ExportDeps = {
      openUrl: async (u) => void (opened = new URL(u)),
      listen: async () => 5151,
      wait: async () => `code=abc&state=${opened.searchParams.get("state")}`,
      cancel: vi.fn(),
      fetch: async (_input, init) => {
        bodies.push(init?.body as URLSearchParams);
        return new Response(JSON.stringify({ access_token: "ACCESS" }), { status: 200 });
      },
    };
    const session = new ExportSession(deps);
    await session.signIn("google", "client-1", "SECRET");
    await session.signIn("google", "client-1", null);
    expect(bodies[0].get("client_secret")).toBe("SECRET");
    expect(bodies[1].has("client_secret")).toBe(false);
  });

  it("ignores an answer whose state isn't the one it sent", async () => {
    const deps: ExportDeps = {
      openUrl: async () => undefined,
      listen: async () => 5151,
      wait: async () => "code=abc&state=forged",
      cancel: vi.fn(),
      fetch: vi.fn(),
    };
    const session = new ExportSession(deps);
    await expect(session.signIn("microsoft", "m")).rejects.toThrow();
    expect(deps.fetch).not.toHaveBeenCalled();
    expect(session.signedIn("microsoft")).toBe(false);
  });
});
