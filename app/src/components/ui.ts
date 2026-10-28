/* The app's shared looks, one class string each, so every screen's buttons,
   fields, cards and headings match (styles.css carries the tokens). A screen
   adds its own layout classes beside these; it doesn't restate them. */

export const button =
  "inline-flex items-center gap-1 rounded-md border border-line-strong bg-surface px-s2 py-1 text-sm font-medium text-ink hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-surface";
/* The primary action in the site's accent (--fawn, pass green). */
export const primary =
  "inline-flex items-center gap-1 rounded-md border border-transparent bg-fawn px-s2 py-1 text-sm font-medium text-on-fawn hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:opacity-50";
export const danger =
  "inline-flex items-center gap-1 rounded-md border border-transparent bg-sap px-s2 py-1 text-sm font-medium text-on-sap hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";
/* A small, quiet action in a row (Open, Dismiss, Cancel). */
export const quiet = "rounded-md px-s1 py-0.5 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink disabled:opacity-50";
export const field =
  "rounded-md border border-line bg-surface px-s1 py-1 text-sm text-ink placeholder:text-ink-3 focus:border-branch focus:outline-none";
export const card = "rounded-lg border border-line bg-surface";
/* A notice strip inside a screen: an error, or a warning. */
export const alert = "rounded-md bg-sap-soft px-s2 py-s1 text-sm text-sap";
export const notice = "rounded-md bg-branch-soft px-s2 py-s1 text-sm text-ink";
/* A screen's title, its one-line lede, and a section heading. */
export const title = "text-xl font-semibold tracking-tight";
export const lede = "text-sm text-ink-2";
export const h2 = "text-base font-semibold";
export const label = "text-xs font-medium uppercase tracking-wider text-ink-3";
/* Code and identifiers in running text. */
export const mono = "font-mono text-[13px]";
/* A small status pill: its tone in the text, on a hairline (inset, so it
   takes no room), never a fill. A chip is the same shape for a pill that
   draws its own border (a choice that can be picked). */
export const chip = "inline-flex items-center rounded-sm px-2 py-px text-xs font-medium";
export const pill = `${chip} inset-ring inset-ring-line-strong`;
