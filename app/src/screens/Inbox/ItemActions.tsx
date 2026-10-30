/* What a sign-in item can be completed with where it is listed, without its
   sheet: a lapsed provider sign-in's "Sign in to <provider>" (the provider
   CLI's own login, browser first; the provider is the one the engine's
   sign-in route names, else the agent's own); brindle's own cloud sign-in
   for a "login" item; and "Go to it", the item's agent in its session.
   Sits in the sidebar's session context (a permission request has its own
   card there, components/SessionContext PermissionCard); the Inbox page
   only links. */

import { useDaemon } from "../../api/context";
import type { InboxItemDTO } from "../../api/schema";
import { ProviderSignIn } from "../../components/ProviderSignIn";
import { button, quiet } from "../../components/ui";
import { useAppState } from "../../store/useStore";
import type { Route } from "../../routes";
import { CloudLoginStatus, useCloudLogin } from "./CloudSignIn";
import { itemKey, loginProvider } from "./items";
import type { InboxActions } from "./useInboxActions";

export interface ItemActionsProps {
  item: InboxItemDTO;
  actions: Pick<InboxActions, "now" | "busy" | "notes">;
  /* Open the item's agent in its session. */
  onGo?(): void;
  onNavigate?(route: Route): void;
  /* The repo the item's session is in: a sign-in checks the provider there. */
  repo?: string | null;
  /* A sign-in ended (either way): the inbox is read again. */
  onDone?(): void;
  /* Smaller controls, for the sidebar. */
  compact?: boolean;
}

/* The provider a credentials item signs in to: the engine's route, its
   detail, else the agent's own CLI (its record in the snapshot). */
export function useLoginProvider(item: InboxItemDTO) {
  const provider = useAppState((s) => s.workspaces.flatMap((ws) => ws.agents ?? []).find((a) => a.id === item.agent_id)?.provider ?? null);
  return loginProvider(item, provider);
}

export function ItemActions({ item, actions, onGo, onNavigate, repo, onDone, compact = false }: ItemActionsProps) {
  const client = useDaemon();
  const provider = useLoginProvider(item);
  const cloud = useCloudLogin();
  const key = itemKey(item);
  const note = actions.notes[key];
  const size = compact ? "px-s1 py-0.5 text-xs" : "";
  return (
    <div className="flex flex-col gap-1" data-testid="item-actions">
      <div className="flex flex-wrap items-center gap-1">
        {item.kind === "doc" && (
          <>
            <button type="button" className={`${button} ${size}`} disabled={item.doc == null || !onNavigate} onClick={() => onNavigate?.({ screen: "knowledge", view: "docs", doc: item.doc!, section: item.section ?? undefined })}>Open</button>
            {onGo && <button type="button" className={`${quiet} ${size}`} onClick={onGo}>Reply</button>}
            <button type="button" className={`${quiet} ${size}`} disabled={actions.busy.has(key)} onClick={() => actions.now(item, "dismiss")}>Dismiss</button>
          </>
        )}
        {item.kind === "credentials" && (
          <ProviderSignIn
            provider={provider}
            small={compact}
            check={async () => {
              const routing = await client.routing(repo ?? "");
              return routing.profiles.some((x) => x.provider === provider && x.installed && x.signed_in);
            }}
            onDone={onDone}
          />
        )}
        {item.kind === "login" && (
          <button type="button" className={`${button} ${size}`} disabled={cloud.busy} onClick={cloud.start}>
            Sign in to brindle
          </button>
        )}
        {onGo && item.kind !== "doc" && (
          <button type="button" className={`${quiet} ${size} ml-auto`} onClick={onGo} title="Open the agent in its session">
            Go to it ↗
          </button>
        )}
      </div>
      {item.kind === "login" && <CloudLoginStatus job={cloud.job} compact={compact} />}
      {note && (
        <p className={`${compact ? "text-xs" : "text-sm"} ${note.failed ? "text-sap" : "text-pass"}`} role={note.failed ? "alert" : "status"}>
          {note.text}
        </p>
      )}
    </div>
  );
}
