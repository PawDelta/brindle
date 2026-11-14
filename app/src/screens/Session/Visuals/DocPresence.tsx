/* Document-wide awareness and the author's version controls. Section editors own their carets.
   Who else is in the doc shows as faces where this sits (the reader's toolbar); the strip under
   the toolbar (DocFreeze.tsx) says whether the text is live or locked, and holds Freeze and
   Reopen. The reader hands over the element to draw the strip into (`stripHost`); without one it
   is drawn here, after the faces. */
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { EphemeralStore, LoroDoc } from "loro-crdt";
import { getUserEphemeralKey, type EphemeralState, type UserState } from "loro-codemirror";
import { LIVE_RELOAD, type LiveChannel, type LiveDocState, type LiveRefusal } from "../../../api/client";
import type { LiveClient } from "./DocEditor";
import { LiveStrip, LockStrip, OfflineStrip } from "./DocFreeze";
import { Avatar } from "./PlanCards";
import { PRESENCE_TTL, peerClass } from "./docAwareness";

export type LiveCapability = "unknown" | "live" | "unsupported";

export function DocPresence({ client, visual, isAuthor, name, onState, onCapability, mode = "editing", stripHost, reviewers = [], onHistory }: {
  client: LiveClient; visual: string; isAuthor: boolean; name: string;
  onState?(state: LiveDocState | null): void;
  onCapability?(capability: LiveCapability): void;
  /* The reader's mode: the live strip is for writing (and for the author, who freezes). */
  mode?: "viewing" | "suggesting" | "editing";
  stripHost?: HTMLElement | null;
  /* Who signs a frozen version, by name: Freeze says it asks them. */
  reviewers?: string[];
  /* Show what the frozen version changed (the reader's History tab). */
  onHistory?(): void;
}) {
  const channel = useRef<LiveChannel | null>(null);
  const [state, setState] = useState<LiveDocState | null>(null);
  const [connected, setConnected] = useState(false);
  const [peers, setPeers] = useState<UserState[]>([]);
  const [unsupported, setUnsupported] = useState(false);
  // The author's last Freeze or Reopen that didn't happen, and why (cleared by the next one).
  const [refused, setRefused] = useState<LiveRefusal | null>(null);
  // Bumped when the daemon replaced the text (LIVE_RELOAD): the channel opens again.
  const [opened, setOpened] = useState(0);
  const known = useRef({ client, visual, live: false });
  useEffect(() => {
    if (known.current.client !== client || known.current.visual !== visual) known.current = { client, visual, live: false };
    const confirm = () => { known.current.live = true; onCapability?.("live"); };
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
    onCapability?.(known.current.live ? "live" : "unknown");
    setUnsupported(false);
    setRefused(null);
    setConnected(false);
    setPeers([]);
    let stopRefresh = () => {};
    const live = client.visualLive(visual, {
      onUpdate() { confirm(); },
      onState(next) { confirm(); setState(next); onState?.(next); setConnected(true); },
      onRefused(why) { setRefused(why); },
      onAwareness(section, bytes) {
        let store = stores.get(section);
        if (!store) {
          store = new EphemeralStore<EphemeralState>(PRESENCE_TTL);
          stores.set(section, store);
          store.subscribe(refresh);
        }
        store.apply(bytes);
      },
      onClose(code) {
        setConnected(false);
        onState?.(null);
        stopRefresh();
        if (code === LIVE_RELOAD) {
          setOpened((n) => n + 1);
          return;
        }
        // A missing initial document is definitive. A known live document stays locked on loss.
        if (code === 4404 && !known.current.live) {
          setUnsupported(true);
          onCapability?.("unsupported");
        }
      },
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
  }, [client, visual, name, onState, onCapability, opened]);
  if (unsupported) return null;
  const author = state?.author ?? "";
  const freeze = () => { setRefused(null); channel.current?.freeze(); };
  const reopen = () => { setRefused(null); channel.current?.reopen(); };
  const here = ["You", ...peers.map((p) => p.name)];
  const strip = state?.lock === "sign_off" && state.version
    ? <LockStrip version={state.version} author={author} writers={state.version.writers} isAuthor={isAuthor} connected={connected} refused={refused} onReopen={reopen} onHistory={onHistory} />
    : state?.lock === "sign_off"
      ? <LockStrip version={{ id: 0, writers: [], signOffs: [], snapshot: "" }} author={author} writers={[]} isAuthor={isAuthor} connected={connected} refused={refused} onReopen={reopen} onHistory={onHistory} />
      : state?.lock === "author_offline"
        ? <OfflineStrip author={author} />
        : mode !== "viewing" || isAuthor || refused
          ? <LiveStrip isAuthor={isAuthor} author={author} here={here} reviewers={reviewers} editing={mode !== "viewing"} connected={connected} refused={refused} onFreeze={freeze} />
          : null;
  return (
    <>
      <ul aria-label="Others in this document" className="m-0 flex list-none p-0" title={peers.length ? `${peers.map((p) => p.name).join(", ")} ${peers.length === 1 ? "is" : "are"} in this doc` : undefined}>
        {peers.map((peer, i) => (
          <li key={peer.name} className={`inline-flex rounded-full ${i ? "-ml-[5px] shadow-[0_0_0_1.5px_var(--canvas)]" : ""}`} data-peer={peer.name}>
            <Avatar who={peer.name} />
            <span aria-label="Caret colour" data-colour={peer.colorClassName} hidden />
          </li>
        ))}
      </ul>
      {strip && (stripHost ? createPortal(strip, stripHost) : stripHost === undefined ? strip : null)}
    </>
  );
}
