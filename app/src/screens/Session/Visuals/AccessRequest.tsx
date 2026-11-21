import { useEffect, useRef, useState } from "react";
import type { SharedDesign } from "../../../api/client";
import type { SharedDesignClient } from "./client";
import { button } from "../../../components/ui";

/* The Library queues this for the author; asking never changes a reader's grants. */
export function AccessRequest({ client, doc, repo }: { client: SharedDesignClient; doc: SharedDesign; repo?: string }) {
  const [answer, setAnswer] = useState<{ basis: SharedDesign; request: SharedDesign["access_request"] } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);
  const live = useRef(true);
  const latest = useRef(doc);
  latest.current = doc;
  useEffect(() => { live.current = true; return () => { live.current = false; }; }, []);
  const ask = async (role: "comment" | "edit") => {
    if (pending.current) return;
    pending.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await client.writeSharedDesign(doc.id, "access-requests", { role, repo });
      if (live.current && latest.current === doc) setAnswer({ basis: doc, request: result.access_request });
    } catch (e) {
      if (live.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      pending.current = false;
      if (live.current) setBusy(false);
    }
  };
  // A mutation reply bridges the next read; a newer Library view always replaces it.
  const request = answer?.basis === doc ? answer.request : doc.access_request;
  if (doc.can_edit) return null;
  return <div className="flex flex-wrap items-center gap-2 px-s2 py-1 text-xs">
    {(["comment", "edit"] as const).filter((role) => role !== "comment" || !doc.can_comment).map((role) => {
      const requested = request?.status === "pending" && (request.role === role || request.role === "edit");
      return <button key={role} type="button" className={button} disabled={busy || requested} onClick={() => void ask(role)}>
        {requested ? `${role === "edit" ? "Edit" : "Comment"} access requested` : `Ask to ${role}`}
      </button>;
    })}
    {error && <span role="alert">{error}</span>}
  </div>;
}
