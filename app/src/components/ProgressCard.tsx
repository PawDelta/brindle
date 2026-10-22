/* The calm progress card (design/connect/'s install card): what runs, a slim
   bar, the last lines the command prints (a maker's installer, git's
   clone), and when it stops, a tinted strip with what it said and Retry.
   Underneath, whatever the screen adds (From / Into / License). */

import type { ReactNode } from "react";
import { danger, mono, quiet } from "./ui";

export interface ProgressCardProps {
  /* What runs: a mark and a name, as the screen draws them. */
  title: ReactNode;
  status: "starting" | "running" | "done" | "failed";
  lines: string[];
  /* The failure's headline ("Install failed.") and what the command said. */
  failure?: { headline: string; message: string };
  onRetry?(): void;
  /* A second choice beside Retry ("Skip for now"). */
  other?: { label: string; onClick(): void };
  label: string;
  testId?: string;
  children?: ReactNode;
}

/* How many of the command's last lines show. */
const SHOWN = 6;

export function ProgressCard({ title, status, lines, failure, onRetry, other, label, testId, children }: ProgressCardProps) {
  const failed = status === "failed";
  const done = status === "done";
  return (
    <section className="flex flex-col gap-s2 rounded-lg border border-line bg-surface p-s2" aria-label={label} data-testid={testId} data-state={status}>
      <div className="flex items-center gap-s1">
        {title}
        <span className="flex-1" />
        <span className={`${mono} text-ink-2`}>{failed ? "stopped" : done ? "done" : "running"}</span>
      </div>
      {failed || done ? (
        <div className={`h-1.5 rounded-full ${failed ? "bg-fail" : "bg-pass"}`} aria-hidden />
      ) : (
        <div className="onb-bar h-1.5 overflow-hidden rounded-full bg-line" role="progressbar" aria-label={label}>
          <i />
        </div>
      )}
      {lines.length > 0 && (
        <ol className={`${mono} flex flex-col gap-0.5 rounded-md bg-canvas-2 px-s2 py-s1 text-ink-2`} aria-live="polite" data-testid={testId ? `${testId}-lines` : undefined}>
          {lines.slice(-SHOWN).map((line, i) => (
            <li key={`${lines.length}-${i}`} className="break-all">
              {line}
            </li>
          ))}
        </ol>
      )}
      {failed && failure && (
        <div className="flex flex-wrap items-center gap-s2 rounded-md bg-fail-soft px-s2 py-s1 text-sm" role="alert" data-testid={testId ? `${testId}-failed` : undefined}>
          <p className="min-w-0 flex-1 whitespace-pre-line">
            <b className="text-fail">{failure.headline}</b> {failure.message}
          </p>
          {onRetry && (
            <button type="button" className={danger} onClick={onRetry}>
              Retry
            </button>
          )}
          {other && (
            <button type="button" className={quiet} onClick={other.onClick}>
              {other.label}
            </button>
          )}
        </div>
      )}
      {children}
    </section>
  );
}
