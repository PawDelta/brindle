import { useEffect, useRef, useState } from "react";
import type { DaemonClient } from "../../api/client";

export function VisualsSetting({ client }: { client: Pick<DaemonClient, "userSettings" | "patchUserSettings"> }) {
  const [mode, setMode] = useState<"on" | "off" | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  useEffect(() => {
    const mine = ++seq.current;
    setMode(null);
    setSaving(false);
    setError("");
    client.userSettings().then(s => {
      if (mine === seq.current) setMode(s.visuals ?? null);
    }, () => undefined);
    return () => { ++seq.current; };
  }, [client]);
  const toggle = async () => {
    if (!mode || saving) return;
    const mine = ++seq.current;
    setSaving(true);
    setError("");
    try {
      const settings = await client.patchUserSettings({ visuals: mode === "on" ? "off" : "on" });
      if (mine === seq.current) setMode(settings.visuals ?? null);
    } catch {
      if (mine === seq.current) setError("Could not save Visuals.");
    } finally {
      if (mine === seq.current) setSaving(false);
    }
  };
  if (mode === null) return null;
  return <div className="flex items-center justify-between gap-s3 px-s2 py-s2">
    <div><div>Visuals</div><p className="text-sm text-ink-3">Offer “Make a visual?” for choices that are hard to read. Visuals are made only when you accept or ask.</p></div>
    <button type="button" role="switch" aria-label="Visuals" aria-checked={mode === "on"} disabled={saving}
      className="rounded-md border border-line px-s2 py-s1 disabled:opacity-50" onClick={() => void toggle()}>{mode === "on" ? "On" : "Off"}</button>
    {error && <span role="alert">{error}</span>}
  </div>;
}
