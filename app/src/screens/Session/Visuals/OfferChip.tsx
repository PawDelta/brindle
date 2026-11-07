import { useEffect, useRef, useState } from "react";
import type { DaemonClient, VisualOffer } from "../../../api/client";
import { useAppState } from "../../../store/useStore";

type Client = Pick<DaemonClient, "visualOffer" | "resolveVisualOffer">;
const QUIET = 2000;

function detail(o: VisualOffer): string {
  const n = o.counts;
  switch (o.kind) {
    case "options": return `${n.options} options · ${n.attributes} attributes`;
    case "comparison": return `${n.rows} rows × ${n.columns} columns`;
    case "flow": return `${n.steps} steps · ${n.branches} branches`;
    case "diff": return `${n.lines} lines changed`;
    default: return "Layout choice";
  }
}

export function OfferChip({ client, root }: { client: Client; root: string }) {
  const [state, setState] = useState<{ root: string; offer: VisualOffer | null } | null>(null);
  const [quiet, setQuiet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const latest = useRef(root);
  latest.current = root;
  const changed = useAppState(s => s.recent.filter(e => e.kind === "visual.offers_setting" || (e.root_id === root && e.kind.startsWith("visual."))).at(-1)?.id ?? 0);
  useEffect(() => {
    const mine = ++seq.current;
    setBusy(false);
    setError("");
    client.visualOffer(root).then(offer => {
      if (mine === seq.current) setState({ root, offer });
    }, () => { if (mine === seq.current) setState(null); });
    return () => { ++seq.current; };
  }, [client, root, changed]);
  const offer = state?.root === root ? state.offer : null;
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const delay = () => {
      setQuiet(false);
      clearTimeout(timer);
      timer = setTimeout(() => setQuiet(true), QUIET);
    };
    const typed = (event: Event) => {
      const target = event.target;
      if (!(target instanceof HTMLElement) || (!target.matches("input, textarea") && !target.isContentEditable)) return;
      if (event instanceof KeyboardEvent && (event.key.length > 1 && !["Backspace", "Delete", "Enter"].includes(event.key))) return;
      delay();
    };
    delay();
    // Capture observes terminal keystrokes too, without interrupting their delivery.
    window.addEventListener("keydown", typed, true);
    window.addEventListener("input", typed, true);
    return () => {
      clearTimeout(timer);
      window.removeEventListener("keydown", typed, true);
      window.removeEventListener("input", typed, true);
    };
  }, [root, offer?.id]);
  useEffect(() => {
    if (!offer) return;
    const expiry = setTimeout(() => setState(s => s?.offer?.id === offer.id ? null : s), Math.max(0, offer.expires_at * 1000 - Date.now()));
    return () => clearTimeout(expiry);
  }, [offer]);
  const resolve = async (accept: boolean) => {
    if (!offer || busy) return;
    const mine = ++seq.current;
    setBusy(true);
    setError("");
    try {
      await client.resolveVisualOffer(root, offer.id, accept);
      if (latest.current === root && mine === seq.current) setState(null);
    } catch {
      if (latest.current === root && mine === seq.current) setError("Could not update this offer.");
    } finally {
      if (latest.current === root && mine === seq.current) setBusy(false);
    }
  };
  if (!offer || !quiet) return null;
  return <div className="inline-flex items-center gap-s1 rounded-full border border-dashed border-line px-s2 text-sm">
    <button type="button" disabled={busy} onClick={() => void resolve(true)}>Make a visual?</button>
    <span className="text-ink-3">{detail(offer)}</span>
    <button type="button" aria-label="Not now" disabled={busy} onClick={() => void resolve(false)}>×</button>
    <span className="sr-only" role="status">brindle can show this choice as a visual</span>
    {error && <span role="alert">{error}</span>}
  </div>;
}
