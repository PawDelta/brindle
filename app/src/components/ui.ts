/* The app's shared looks, one class string each, so every screen's buttons,
   fields, cards and headings match (styles.css carries the tokens). A screen
   adds its own layout classes beside these; it doesn't restate them. */

/* Buttons, the Tinted scheme (styles.css .btn): every role shares one base
   in Geist Sans 13px with its hover, pressed, focus and disabled states. A
   slow action wraps it in LoadingButton.tsx. */
/* A secondary action: a hairline on no fill. */
export const button = "btn btn-secondary";
/* The one main action on a screen (Allow, Update brindle, Continue): pass
   text on a 12% pass wash and a 40% pass hairline. Green is kept for it,
   status dots and ticks; one per screen, every other action is a plain
   button. mainTone is the same, for a screen that sets its own padding. */
export const mainTone = "btn btn-primary";
export const main = mainTone;
/* Something that can't be undone (Remove, Rewind): primary's shape in fail. */
export const danger = "btn btn-destructive";
/* A small, quiet action in a row (Open, Dismiss, Cancel): text only. */
export const quiet = "btn btn-quiet";
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
