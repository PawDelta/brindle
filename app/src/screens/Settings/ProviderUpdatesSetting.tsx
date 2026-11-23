import { useEffect, useId, useState } from "react";
import type { DaemonClient, ProviderUpdateMode, UserSettings } from "../../api/client";
import { plain } from "../../api/wording";
import { field } from "../../components/ui";

export const PROVIDER_UPDATE_CHOICES = [
  { value: "automatic", name: "Automatic", help: "Install updates when no worker is using the CLI." },
  { value: "ask", name: "Ask", help: "Offer an Update button in the Inbox." },
  { value: "off", name: "Off", help: "Turn off brindle's provider update checks." },
] as const;

export function ProviderUpdatesSetting({ client }: { client: DaemonClient }) {
  // Snapshots are bound to the client that read or saved them.
  const [snapshot, setSnapshot] = useState<{ client: DaemonClient; settings: UserSettings }>();
  const [saving, setSaving] = useState<DaemonClient>();
  const [error, setError] = useState<{ client: DaemonClient; text: string }>();
  const id = useId();
  useEffect(() => {
    let active = true;
    client.userSettings().then(settings => {
      if (active) setSnapshot({ client, settings });
    }, e => {
      if (active) setError({ client, text: plain(String(e)) });
    });
    return () => { active = false; };
  }, [client]);
  const settings = snapshot?.client === client ? snapshot.settings : undefined;
  const mode = settings?.provider_cli_updates;
  const locked = settings?.provider_cli_updates_locked;
  const save = async (mode: ProviderUpdateMode) => {
    setSaving(client);
    setError(undefined);
    try {
      const settings = await client.patchUserSettings({ provider_cli_updates: mode });
      setSnapshot(current => current?.client === client ? { client, settings } : current);
    } catch (e) {
      setError({ client, text: plain(String(e)) });
    } finally {
      setSaving(current => current === client ? undefined : current);
    }
  };
  if (!mode) return null;
  return <div className="flex flex-wrap items-center justify-between gap-s2 border-t border-line px-s2 py-s1">
    <div>
      <label htmlFor={id} className="text-sm font-medium">Provider CLI updates</label>
      <p className="text-xs text-ink-3">{locked ? "Set by your organization." : PROVIDER_UPDATE_CHOICES.find(choice => choice.value === mode)?.help}</p>
    </div>
    <select id={id} className={field} value={mode} disabled={locked || saving === client}
      onChange={e => void save(e.target.value as ProviderUpdateMode)}>
      {PROVIDER_UPDATE_CHOICES.map(choice => <option key={choice.value} value={choice.value}>{choice.name}</option>)}
    </select>
    {error?.client === client && <p role="alert" className="text-sm">{error.text}</p>}
  </div>;
}
