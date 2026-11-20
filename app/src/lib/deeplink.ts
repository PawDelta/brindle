/* brindle:// links from outside the app: a shared plan's "Open in brindle" on its web page
   (plans.pawdelta.com). The shell (src-tauri/src/deep_links.rs) registers the scheme, keeps each
   link it is opened with and nudges the page; this side checks each link and opens what it names.
   The URL format is docs/deep-links.md.

     brindle://plan/b/<token>                    a Pro account's own plan
     brindle://plan/<org>/<plan>/<token>         a plan an org holds
     brindle://plan/<org>/<team>/<plan>/<token>  one whose link is kept to a team

   As on the web, the token alone opens the plan: the other segments are slugs for people to read
   and are never sent anywhere. The plan is read through the link as the account signed in
   (screens/Session/Visuals/SharedLinkReader), so the link's own checks decide what opens, the same
   as for its web page. A link is only ever parsed: nothing in it is run, fetched or followed, and
   one that doesn't parse opens the calm "couldn't open this link" page and nothing else. */

import { useEffect, useRef } from "react";
import type { Route } from "../routes";

/* A plan a link names. `org`, `team` and `plan`: the link's slugs, for reading only. */
export interface PlanLink {
  kind: "plan";
  token: string;
  org: string | null;
  team: string | null;
  plan: string | null;
}

export type DeepLink = PlanLink;

const PREFIX = "brindle://plan/";
/* The longest link looked at: a plan link is well under it (the shell keeps none longer). */
const MAX_LENGTH = 512;
/* A share link's token: dsl_ and 43 URL-safe characters (256 bits). */
const TOKEN = /^dsl_[A-Za-z0-9_-]{43}$/;
/* A slug: letters, digits, - and _, as the plans host's own paths. No dot, slash, %, ? or #. */
const SLUG = /^[A-Za-z0-9_-]{1,100}$/;

/* The plan a brindle:// link names, or null for anything else (another scheme, another kind of
   link, a query or fragment, an escaped character, a malformed token). */
export function parseDeepLink(raw: unknown): DeepLink | null {
  if (typeof raw !== "string" || raw.length > MAX_LENGTH || !raw.startsWith(PREFIX)) return null;
  // Some systems hand a link over with a slash added.
  const rest = raw.slice(PREFIX.length).replace(/\/$/, "");
  const parts = rest.split("/");
  if (parts.length < 2 || parts.length > 4 || !parts.every((p) => SLUG.test(p))) return null;
  const token = parts[parts.length - 1];
  if (!TOKEN.test(token)) return null;
  if (parts.length === 2) return parts[0] === "b" ? { kind: "plan", token, org: null, team: null, plan: null } : null;
  const [org, ...named] = parts.slice(0, -1);
  const plan = named[named.length - 1];
  return { kind: "plan", token, org, team: named.length === 2 ? named[0] : null, plan };
}

/* Where a link opens: the plan's reader, or (null) the page that says the link couldn't be opened.
   `from`: where Back goes. */
export function deepLinkRoute(raw: unknown, from: Route | null): Route {
  // From one link's page to the next: Back still goes to where the person was before either.
  const back = from?.screen === "plan-link" ? from.from : from ?? undefined;
  return { screen: "plan-link", link: parseDeepLink(raw), from: back };
}

/* The shell's links (api/tauri.ts): `take` empties its queue; `subscribe` hears that links are waiting. */
export interface DeepLinkSource {
  take(): Promise<string[]>;
  subscribe(nudge: () => void): () => void;
}

/* Open every link the shell has, now and as they come: one for a cold start or a sign-in that came
   first waits in the shell's queue until this runs. */
export function useDeepLinks(source: DeepLinkSource | undefined, navigate: (to: Route) => void, current: () => Route | null) {
  const latest = useRef({ navigate, current });
  latest.current = { navigate, current };
  useEffect(() => {
    if (!source) return;
    let live = true;
    const drain = () => {
      source.take().then((links) => {
        if (!live) return;
        for (const raw of links) latest.current.navigate(deepLinkRoute(raw, latest.current.current()));
      }, () => {});
    };
    const stop = source.subscribe(drain);
    drain();
    return () => {
      live = false;
      stop();
    };
  }, [source]);
}
