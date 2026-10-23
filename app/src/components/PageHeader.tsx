import type { ReactNode } from "react";
import { lede as ledeClass, title as titleClass } from "./ui";

/* Every screen opens the same way: a title, an optional one-line lede, and
   the screen's actions on the right of the same line. */
export function PageHeader({ title, lede, children }: { title: ReactNode; lede?: ReactNode; children?: ReactNode }) {
  return (
    <header className="flex flex-wrap items-start justify-between gap-x-s3 gap-y-s1">
      <div className="min-w-0">
        <h1 className={titleClass}>{title}</h1>
        {lede && <p className={`mt-0.5 ${ledeClass}`}>{lede}</p>}
      </div>
      {children && <div className="flex flex-wrap items-center gap-s1">{children}</div>}
    </header>
  );
}
