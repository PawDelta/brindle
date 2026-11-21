import { useEffect, useRef, useState } from "react";
import type { SharedDesign } from "../../../api/client";
import type { SharedDesignClient } from "./client";
import { button } from "../../../components/ui";

/* The Library queues this for the author; asking never changes a reader's grants. */
export function AccessRequest({ client, doc, repo }: { client: SharedDesignClient; doc: SharedDesign; repo?: string }) {
  const [sent, setSent] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);
  const live = useRef(true);
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const ask = async (role: "comment" | "edit") => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      await client.writeSharedDesign(doc.id, "access-requests", { role, repo });
      if (live.current) setSent((old) => [...old, role]);
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      pending.current = false;
      if (live.current) setBusy(false);
    }
  };
  if (doc.can_edit) return null;
  return <div className="flex flex-wrap items-center gap-2 px-s2 py-1 text-xs">
    {(["comment", "edit"] as const).filter((role) => role !== "comment" || !doc.can_comment).map((role) => {
      const requested = sent.includes(role) || (doc.access_request?.status === "pending" && (doc.access_request.role === role || doc.access_request.role === "edit"));
      return <button key={role} type="button" className={button} disabled={busy || requested} onClick={() => void ask(role)}>
        {requested ? `${role === "edit" ? "Edit" : "Comment"} access requested` : `Ask to ${role}`}
      </button>;
    })}
    {error && <span role="alert">{error}</span>}
  </div>;
}
