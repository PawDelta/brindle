/* The first launch's frame, the whole window: the lockup in the corner and
   one column of content under a step's kicker, title and lede. Every
   first-launch screen wears it (Onboarding's steps, FirstRun's engine). */

import type { ReactNode } from "react";
import Logo from "../../components/Logo";

export function Frame({ step, narrow = false, kicker, title, lede, children }: { step: string; narrow?: boolean; kicker: string; title: string; lede?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex h-full flex-col overflow-auto bg-canvas text-ink" role="main" aria-label="Set up brindle" data-testid="onboarding" data-step={step}>
      <div className="px-s3 py-s2">
        <Logo />
      </div>
      <div className={`mx-auto flex w-full flex-col gap-s2 px-s3 pb-s8 pt-s4 ${narrow ? "max-w-[560px]" : "max-w-[728px]"}`}>
        <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-ink-3">{kicker}</span>
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {lede && <p className="text-[15px] text-ink-2">{lede}</p>}
        {children}
      </div>
    </div>
  );
}

export function Spinner() {
  return <span className="size-3.5 shrink-0 animate-spin rounded-full border-2 border-branch border-t-transparent" aria-hidden />;
}
