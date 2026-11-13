/* Document-wide awareness and the author's version controls. Section editors own their carets. */
import { useEffect, useRef, useState } from "react";
import { EphemeralStore, LoroDoc } from "loro-crdt";
import { getUserEphemeralKey, type EphemeralState, type UserState } from "loro-codemirror";
import type { LiveChannel, LiveDocState } from "../../../api/client";
import { quiet } from "../../../components/ui";
import type { LiveClient } from "./DocEditor";
import { Avatar } from "./PlanCards";
import { PRESENCE_TTL, peerClass } from "./docAwareness";

export function DocPresence({ client, visual, isAuthor, name, onState }: {
  client: LiveClient; visual: string; isAuthor: boolean; name: string;
  onState?(state: LiveDocState | null): void;
}) {
  const channel = useRef<LiveChannel | null>(null);
  const [state, setState] = useState<LiveDocState | null>(null);
  const [connected, setConnected] = useState(false);
  const [peers, setPeers] = useState<UserState[]>([]);
  useEffect(() => {
    const stores = new Map<string, EphemeralStore<EphemeralState>>();
    const identity = new LoroDoc();
    const userKey = getUserEphemeralKey(identity);
    const own = new EphemeralStore<EphemeralState>(PRESENCE_TTL);
    const refresh = () => {
      const people = new Map<string, UserState>();
      for (const store of stores.values()) for (const [key, value] of Object.entries(store.getAllStates())) {
        if (key.endsWith("-cm-user") && value && "name" in value && value.name !== name) people.set(value.name, value as UserState);
      }
      setPeers([...people.values()]);
    };
    setState(null);
    onState?.(null);
    setConnected(false);
    setPeers([]);
    let stopRefresh = () => {};
    const live = client.visualLive(visual, {
      onUpdate() {},
      onState(next) { setState(next); onState?.(next); setConnected(true); },
      onAwareness(section, bytes) {
        let store = stores.get(section);
        if (!store) {
          store = new EphemeralStore<EphemeralState>(PRESENCE_TTL);
          stores.set(section, store);
          store.subscribe(refresh);
        }
        store.apply(bytes);
      },
      onClose() { setConnected(false); onState?.(null); stopRefresh(); },
    });
    const off = own.subscribeLocalUpdates((bytes) => live.sendAwareness("__document__", bytes));
    const announce = () => own.set(userKey, { name, colorClassName: peerClass(name) });
    announce();
    const refreshing = setInterval(announce, PRESENCE_TTL / 3);
    stopRefresh = () => clearInterval(refreshing);
    channel.current = live;
    return () => {
      channel.current = null;
      stopRefresh();
      own.delete(userKey);
      off();
      own.destroy();
      live.close();
      for (const store of stores.values()) store.destroy();
    };
  }, [client, visual, name, onState]);
  return (
    <section aria-label="Live document" className="grid gap-2 rounded-md border border-line p-2.5 font-sans text-xs" data-state={state?.lock ?? "editing"}>
      <ul aria-label="Others in this document" className="m-0 flex list-none flex-wrap gap-3 p-0">
        {peers.map((peer) => <li key={peer.name} className="inline-flex items-center gap-1.5" data-peer={peer.name}>
          <Avatar who={peer.name} small />
          <span aria-label="Caret colour" data-colour={peer.colorClassName} className="h-3 w-0.5" style={{ backgroundColor: `var(--group-${Number(peer.colorClassName?.replace("loro-peer-", "")) + 2})` }} />
          {peer.name}
        </li>)}
      </ul>
      {state?.lock === "author_offline" && <p role="status" className="m-0 text-sap">{state.author} went offline while editing · comments and suggestions only</p>}
      {state?.lock === "sign_off" ? <div className="flex items-center gap-2">
        <span>Locked for sign-off</span>
        {isAuthor && <button type="button" className={quiet} disabled={!connected} onClick={() => channel.current?.reopen()}>Reopen</button>}
      </div> : isAuthor ? <button type="button" className={`${quiet} justify-self-start`} disabled={!connected || !!state?.lock} onClick={() => channel.current?.freeze()}>Freeze</button>
        : <p role="status" data-state="author-required" className="m-0 text-ink-3">Ask the author to freeze</p>}
      {state?.version && <div data-testid="doc-version" data-version={state.version.id}>
        <span>Version {state.version.id} · Credited writers</span>
        <ul aria-label="Credited writers" className="m-0 list-none p-0">{state.version.writers.map((writer) => <li key={writer}>{writer}</li>)}</ul>
      </div>}
    </section>
  );
}
