import { useEffect, useRef, useState } from "react";
import type { DaemonClient } from "../../api/client";

type Field = "visuals" | "visuals_enabled";
type Client = Pick<DaemonClient, "userSettings" | "patchUserSettings">;

const COPY: Record<Field, { label: string; text: string }> = {
  visuals_enabled: { label: "Visuals feature", text: "Let agents make visuals and plans. Changes take effect in running sessions at once." },
  visuals: { label: "Visuals", text: "Offer “Make a visual?” for choices that are hard to read. Visuals are made only when you accept or ask." },
};

export function VisualsSetting({ client, field = "visuals" }: { client: Client; field?: Field }) {
  const [mode, setMode] = useState<"on" | "off" | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const seq = useRef(0);
  const { label, text } = COPY[field];
  useEffect(() => {
    const mine = ++seq.current;
    setMode(null);
    setSaving(false);
    setError("");
    client.userSettings().then(s => {
      if (mine === seq.current) setMode(s[field] ?? null);
    }, () => undefined);
    return () => { ++seq.current; };
  }, [client, field]);
  const toggle = async () => {
    if (!mode || saving) return;
    const mine = ++seq.current;
    setSaving(true);
    setError("");
    try {
      const settings = await client.patchUserSettings({ [field]: mode === "on" ? "off" : "on" });
      if (mine === seq.current) setMode(settings[field] ?? null);
    } catch {
      if (mine === seq.current) setError(`Could not save ${label}.`);
    } finally {
      if (mine === seq.current) setSaving(false);
    }
  };
  if (mode === null) return null;
  return <div className="flex items-center justify-between gap-s3 px-s2 py-s2">
    <div><div>{label}</div><p className="text-sm text-ink-3">{text}</p></div>
    <button type="button" role="switch" aria-label={label} aria-checked={mode === "on"} disabled={saving}
      className="rounded-md border border-line px-s2 py-s1 disabled:opacity-50" onClick={() => void toggle()}>{mode === "on" ? "On" : "Off"}</button>
    {error && <span role="alert">{error}</span>}
  </div>;
}
