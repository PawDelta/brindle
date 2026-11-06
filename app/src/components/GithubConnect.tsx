/* Connect GitHub (optional): how brindle reaches private repositories over
   HTTPS. Either the GitHub CLI's own sign-in (`gh`; the same access gh has),
   or a fine-grained token pasted here. Used by the first launch's project
   step, the project picker's clone failures and Settings. The token is
   cleared the moment it is sent and never logged. */

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { DaemonError, type DaemonClient, type GithubConnect, type GithubConnection } from "../api/client";
import { openExternal, type OpenUrl } from "../api/open";
import { plain } from "../api/wording";
import { alert, button, card, field, main, quiet } from "./ui";

export const TOKEN_PAGE = "https://github.com/settings/personal-access-tokens/new";

const link = "text-branch underline-offset-2 hover:underline";

/* What each refusal means to the person, in plain words. */
function describe(e: unknown): string {
  if (e instanceof DaemonError && e.code === "github_token_invalid") return "GitHub didn't accept that token. Check that you copied all of it and that it hasn't expired.";
  if (e instanceof DaemonError && e.code === "github_gh_unavailable") return "The GitHub CLI isn't signed in on this Mac. Run gh auth login in Terminal, or paste a token instead.";
  return plain(e instanceof Error ? e.message : String(e));
}

export interface Github {
  /* GET /github/connection as last read or written; null until read (or when the engine has no such route). */
  conn: GithubConnection | null;
  busy: boolean;
  error: string | null;
  /* The new connection, or null when it was refused (`error` says why) or went stale. */
  connect(body: GithubConnect): Promise<GithubConnection | null>;
  disconnect(): Promise<GithubConnection | null>;
  cancel(): void;
}

/* The connection, read once and written by the person. Every write and unmount bumps `seq`, so an
   answer that arrives after something else changed the state (or the screen left) is dropped. */
export function useGithub(client: DaemonClient): Github {
  const [conn, setConn] = useState<GithubConnection | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  /* A write is in flight (so cancel has something to drop). */
  const writing = useRef(false);

  useEffect(() => {
    const at = ++seq.current;
    client.githubConnection().then(
      (c) => seq.current === at && setConn(c),
      () => undefined,
    );
    return () => {
      seq.current++;
    };
  }, [client]);

  const write = useCallback(
    async (send: () => Promise<GithubConnection>) => {
      const at = ++seq.current;
      writing.current = true;
      setBusy(true);
      setError(null);
      try {
        const c = await send();
        if (seq.current !== at) return null;
        setConn(c);
        return c;
      } catch (e) {
        if (seq.current === at) setError(describe(e));
        return null;
      } finally {
        if (seq.current === at) {
          writing.current = false;
          setBusy(false);
        }
      }
    },
    [],
  );
  const connect = useCallback((body: GithubConnect) => write(() => client.connectGithub(body)), [client, write]);
  const disconnect = useCallback(() => write(() => client.disconnectGithub()), [client, write]);
  /* Leaving the card: an answer still on its way is dropped. */
  const cancel = useCallback(() => {
    if (writing.current) seq.current++;
    writing.current = false;
    setBusy(false);
    setError(null);
  }, []);
  return { conn, busy, error, connect, disconnect, cancel };
}

/* Whether brindle can use GitHub now: connected and not asking to be reconnected. */
export const githubReady = (c: GithubConnection | null) => !!c && c.connected && !c.needs_reconnect;

export function GithubConnectCard({ gh, openUrl = openExternal, onConnected, onSkip, skipLabel = "Skip for now" }: {
  gh: Github;
  openUrl?: OpenUrl;
  onConnected?(c: GithubConnection): void;
  /* The way out, always offered, errors included. */
  onSkip(): void;
  skipLabel?: string;
}) {
  const hasGh = gh.conn?.gh_available === true;
  const [pasting, setPasting] = useState(false);
  const [token, setToken] = useState("");
  const showToken = pasting || !hasGh;

  const done = (c: GithubConnection | null) => {
    if (c) onConnected?.(c);
  };
  const useCli = () => void gh.connect({ route: "gh" }).then(done);
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const value = token.trim();
    if (!value) return;
    setToken("");      // gone from the field as it is sent, whatever comes back
    void gh.connect({ route: "token", token: value }).then(done);
  };

  return (
    <section className={`${card} flex flex-col gap-s1 p-s2`} aria-label="Connect GitHub" data-testid="github-connect">
      <p className="font-semibold">Connect GitHub</p>
      <p className="text-sm text-ink-2">Optional. It lets brindle clone and work in your private repositories over HTTPS. You can skip this and do it later in Settings.</p>
      {hasGh && (
        <div className="flex flex-col items-start gap-1">
          <button type="button" className={main} disabled={gh.busy} onClick={useCli}>
            Use my GitHub CLI sign-in
          </button>
          <p className="text-xs text-ink-3">This gives brindle the same access the gh command has on this Mac.</p>
        </div>
      )}
      {!showToken && (
        <div>
          <button type="button" className={button} disabled={gh.busy} onClick={() => setPasting(true)}>
            Paste a token
          </button>
        </div>
      )}
      {showToken && (
        <form className="flex flex-col gap-s1" onSubmit={submit} aria-label="Paste a GitHub token">
          <p className="text-sm text-ink-2">
            <button type="button" className={link} onClick={() => void openUrl(TOKEN_PAGE).catch(() => undefined)}>
              Make a fine-grained token on GitHub
            </button>{" "}
            with these permissions: Contents, Metadata, Pull requests and Workflows. Then paste it here.
          </p>
          <div className="flex flex-wrap gap-s1">
            <input
              className={`${field} min-w-0 flex-1 font-mono`}
              type="password"
              name="github-token"
              aria-label="GitHub token"
              autoComplete="off"
              spellCheck={false}
              value={token}
              disabled={gh.busy}
              onChange={(e) => setToken(e.target.value)}
            />
            <button type="submit" className={hasGh ? button : main} disabled={!token.trim() || gh.busy}>
              Save token
            </button>
          </div>
        </form>
      )}
      {gh.error && (
        <p className={alert} role="alert">
          {gh.error}
        </p>
      )}
      <div>
        <button
          type="button"
          className={quiet}
          onClick={() => {
            gh.cancel();
            onSkip();
          }}
        >
          {skipLabel}
        </button>
      </div>
    </section>
  );
}
