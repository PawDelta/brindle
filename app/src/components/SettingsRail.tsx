import { Fragment } from "react";
import { SETTINGS_TABS, type RailPage, type SettingsTab } from "../routes";
import { useAppState } from "../store/useStore";

const ICONS: Record<SettingsTab, string> = {
  account: "M12 4a4 4 0 1 0 0 8 4 4 0 0 0 0-8M5 21v-2a7 7 0 0 1 14 0v2",
  usage: "M4 19h16M7 16V10M12 16V6M17 16v-4",
  general: "M12 3v3M12 18v3M3 12h3M18 12h3M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6",
  providers: "M4 5h16v5H4zM4 14h16v5H4z",
  permissions: "M12 3l7 3v5c0 5-3.5 8-7 10-3.5-2-7-5-7-10V6z",
  format: "M6 3h9l4 4v14H6zM9 10h7M9 14h7M9 18h4",
  storage: "M5 6c0-3 14-3 14 0s-14 3-14 0v12c0 3 14 3 14 0V6M5 12c0 3 14 3 14 0",
  project: "M4 7a2 2 0 0 1 2-2h4l2 2h6a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z",
  about: "M12 4a8 8 0 1 0 0 16 8 8 0 0 0 0-16M12 11v5M12 8h.01",
};

function Icon({ id }: { id: SettingsTab }) {
  return (
    <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      <path d={ICONS[id]} />
    </svg>
  );
}

export function AccountAvatar() {
  const account = useAppState((s) => s.account);
  const name = account?.signed_in ? account.name?.trim() : "";
  const email = account?.signed_in ? account.email?.trim() : "";
  const initials = name
    ? name.split(/\s+/).slice(0, 2).map((part) => Array.from(part)[0]).join("").toUpperCase()
    : email?.split("@")[0]?.slice(0, 1).toUpperCase();
  return (
    <span className={`flex size-7 shrink-0 items-center justify-center rounded-full border font-mono text-[10px] ${account?.signed_in ? "border-pass/35 bg-pass-soft text-pass" : "border-dashed border-line-strong text-ink-3"}`}>
      {initials || <Icon id="account" />}
    </span>
  );
}

const GROUPS: Partial<Record<SettingsTab, string>> = { account: "You", general: "brindle", project: "This project" };

const AGENTS_ICON = "M12 4a3 3 0 1 0 0 6 3 3 0 0 0 0-6M5 20v-1a5 5 0 0 1 5-5h4a5 5 0 0 1 5 5v1M4 9h2M18 9h2";

/* `tab`: the open Settings tab, null while Agents (`page`) is open. Agents is a page of its
   own, listed under "brindle" after General. */
export function SettingsRail({ tab, page = null, onTab, onPage, onBack }: { tab: SettingsTab | null; page?: RailPage | null; onTab(tab: SettingsTab): void; onPage?(page: RailPage): void; onBack(): void }) {
  const account = useAppState((s) => s.account);
  return (
    <div className="flex min-h-0 flex-1 flex-col gap-1 overflow-auto">
      <button type="button" onClick={onBack} className="rounded px-2 py-1 text-left text-[12.5px] text-ink-2 hover:bg-surface-2 hover:text-ink">
        ← Sessions
      </button>
      <div className="mb-1 flex items-center gap-2 border-b border-line px-2 pb-3 pt-1.5">
        <AccountAvatar />
        <span className="truncate text-[12.5px] font-medium">{account?.signed_in ? account.name?.trim() || account.email || "Signed in" : "Sign in"}</span>
      </div>
      <div role="tablist" aria-label="Settings" aria-orientation="vertical" className="flex flex-col gap-px">
        {SETTINGS_TABS.map((item, index) => (
          <Fragment key={item.id}>
            {GROUPS[item.id] && <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-ink-3">{GROUPS[item.id]}</div>}
            {item.id === "about" && <div role="separator" className="mx-1 mb-1 mt-2 border-t border-line" />}
            <button
              type="button"
              id={`settings-tab-${item.id}`}
              role="tab"
              aria-selected={tab === item.id}
              aria-controls={`settings-panel-${item.id}`}
              tabIndex={tab === item.id || (tab === null && index === 0) ? 0 : -1}
              className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-surface-2 hover:text-ink ${tab === item.id ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)] [&_svg]:text-pass" : "text-ink-2"}`}
              onClick={() => onTab(item.id)}
              onKeyDown={(e) => {
                if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
                e.preventDefault();
                const next = SETTINGS_TABS[(index + (e.key === "ArrowDown" ? 1 : -1) + SETTINGS_TABS.length) % SETTINGS_TABS.length].id;
                onTab(next);
                document.getElementById(`settings-tab-${next}`)?.focus();
              }}
            >
              <Icon id={item.id} />
              {item.label}
            </button>
            {item.id === "general" && (
              <button
                type="button"
                aria-current={page === "agents" ? "page" : undefined}
                className={`flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-[12.5px] hover:bg-surface-2 hover:text-ink ${page === "agents" ? "bg-surface-2 font-medium text-ink shadow-[inset_2px_0_0_var(--pass)] [&_svg]:text-pass" : "text-ink-2"}`}
                onClick={() => onPage?.("agents")}
              >
                <svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
                  <path d={AGENTS_ICON} />
                </svg>
                Agents
              </button>
            )}
          </Fragment>
        ))}
      </div>
    </div>
  );
}
