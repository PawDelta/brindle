/* Where a visual's page renders: one interface, and the component behind it.

   The page runs in a separate child webview on the brindle-visual:// scheme (owner amendment 8:
   design/visualizer-build-plan-amendments.md), so a hung page can't freeze the app's own webview.
   The shell's side of that is worker 2's (app/src-tauri/src/visual_scheme.rs). Until it merges, a
   placeholder stands in: swapping is the one line `VisualFrame = …` below.

   The host, not the page, says which visual a message came from (the child webview's label), and
   the reader drops one from any other: a page can't speak for another visual or an older revision. */

import type { ComponentType } from "react";

/* What the frame shows: a session's visual at a revision, or a kept copy (Knowledge › Kept visuals). */
export type VisualSource = { kind: "session"; id: string; rev: number } | { kind: "kept"; id: string };

/* The scheme's URL for a source (WebView2 maps it to http://brindle-visual.localhost/…). */
export function visualSrc(source: VisualSource): string {
  return source.kind === "kept" ? `brindle-visual://kept/${source.id}` : `brindle-visual://${source.id}/r${source.rev}`;
}

export const sameSource = (a: VisualSource, b: VisualSource): boolean =>
  a.kind === b.kind && a.id === b.id && (a.kind === "kept" || (b.kind === "session" && a.rev === b.rev));

/* What a page may post to the host (the wave-1 contract's frame → host). A snapshot is worker 5's (export). */
export type FrameMessage = { type: "choice"; option: string } | { type: "brindle.esc" } | { type: "brindle.snapshot"; html: string };

export function frameMessage(data: unknown): FrameMessage | null {
  if (!data || typeof data !== "object") return null;
  const m = data as { type?: unknown; option?: unknown; html?: unknown };
  if (m.type === "brindle.snapshot" && typeof m.html === "string") return { type: "brindle.snapshot", html: m.html };
  if (m.type === "brindle.esc") return { type: "brindle.esc" };
  if (m.type === "choice" && typeof m.option === "string") return { type: "choice", option: m.option };
  return null;
}

export interface VisualFrameProps {
  source: VisualSource;
  title: string;
  /* An increment asks the shell to post the current rendered outerHTML. */
  snapshotRequest?: number;
  /* A message the page posted, and the visual the host saw it come from. */
  onMessage(data: unknown, from: VisualSource): void;
}

/* Until the child webview is in: the frame's place, saying so. It loads nothing. */
export function PlaceholderFrame({ source, title }: VisualFrameProps) {
  return (
    <div className="flex min-h-0 flex-1 items-center justify-center p-s3 text-center text-sm text-ink-3" data-testid="visual-frame" data-src={visualSrc(source)} aria-label={title}>
      This visual shows here once the sandboxed view is in this build.
    </div>
  );
}

export const VisualFrame: ComponentType<VisualFrameProps> = PlaceholderFrame;
