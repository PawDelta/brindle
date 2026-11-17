import { productName } from "./agents";

const MARK: Record<string, string> = { claude: "bg-sap text-on-sap", codex: "bg-ink text-surface", opencode: "bg-ink-3 text-surface", antigravity: "bg-branch text-on-branch", native: "bg-pass text-surface" };

/* A provider's small letter mark. */
export function Mark({ provider }: { provider: string }) {
  if (!provider) return null;
  return (
    <span className={`inline-flex size-4 shrink-0 items-center justify-center rounded-sm text-[10px] font-bold ${MARK[provider] ?? "bg-ink-3 text-surface"}`} aria-hidden>
      {productName(provider)[0]}
    </span>
  );
}
