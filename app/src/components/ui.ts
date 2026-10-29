/* The app's shared looks, one class string each, so every screen's buttons,
   fields, cards and headings match (styles.css carries the tokens). A screen
   adds its own layout classes beside these; it doesn't restate them. */

/* A button: a hairline on no fill, the same in every role. */
export const button =
  "inline-flex items-center gap-1 rounded-md border border-line-strong bg-transparent px-s2 py-1 text-sm font-medium text-ink hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-50 disabled:hover:bg-transparent";
/* The one main action on a screen (Allow, Update brindle, Continue), in the
   site's chip voice: mono, pass-green text on a pass hairline and a faint
   tint. Green is kept for it, status dots and ticks; one per screen, every
   other action is a plain button. */
export const mainTone =
  "border-pass/55 bg-pass/4 font-mono text-[13px] font-medium tracking-[-0.01em] text-pass hover:bg-pass-soft disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-pass/4";
export const main = `inline-flex items-center gap-1 rounded-md border px-s2 py-1 ${mainTone}`;
export const danger =
  "inline-flex items-center gap-1 rounded-md border border-transparent bg-sap px-s2 py-1 text-sm font-medium text-on-sap hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50";
/* A small, quiet action in a row (Open, Dismiss, Cancel). */
export const quiet = "rounded-md px-s1 py-0.5 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink disabled:opacity-50";
/* A field is an underline, not a box; focus turns the line branch. */
export const field =
  "rounded-none border-0 border-b border-line-strong bg-transparent px-s1 py-1 text-sm text-ink placeholder:text-ink-3 focus:border-branch focus:outline-none";
/* A section is a divider above and whitespace, never an outlined box; a
   tone class beside it (border-pass, border-branch) colours the divider. */
export const card = "border-t border-line";
/* Something that floats over the page (a menu, a popover, a sticky bar)
   still needs its edge. */
export const panel = "rounded-lg border border-line-strong bg-surface";
/* A notice strip inside a screen: an error, or a warning. */
export const alert = "rounded-md bg-sap-soft px-s2 py-s1 text-sm text-sap";
export const notice = "rounded-md bg-branch-soft px-s2 py-s1 text-sm text-ink";
/* A screen's title, its one-line lede, and a section heading. */
export const title = "text-[22px] font-semibold tracking-[-0.015em]";
export const lede = "text-sm text-ink-2";
export const h2 = "text-[15px] font-semibold";
export const label = "text-xs font-medium uppercase tracking-wider text-ink-3";
/* Code and identifiers in running text. */
export const mono = "font-mono text-[13px]";
/* A small status pill: its tone in the text beside a dot in that tone,
   no box (styles.css draws the dot unless the pill carries its own). A chip
   is the same type for a pill that draws its own border (a choice that can
   be picked). */
export const chip = "inline-flex items-center rounded-sm px-2 py-px text-xs font-medium";
export const pill = "pill inline-flex items-center gap-1.5 text-xs font-medium";
