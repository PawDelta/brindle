/* A button whose action takes a while (start a session, merge, Allow, save).
   For the first 300ms nothing changes, so a quick answer never flickers;
   after that the button keeps its label and size, dims to 55% and a line
   sweeps under it, outside the button, in the role's colour (styles.css
   .ld; a still line under prefers-reduced-motion), and it says aria-busy.
   While the action is out a second click does nothing.

   The action is busy while the promise onClick returns is pending, or while
   `busy` is true (an action a hook runs, or a form's submit). A screen may
   still disable the button while it runs: busy, it doesn't wear the
   disabled look (styles.css .ld[data-active]), only the wait's. */
import { useEffect, useRef, useState, type ButtonHTMLAttributes, type MouseEvent } from "react";

export const SLOW_MS = 300;

type Tone = "primary" | "secondary" | "quiet" | "destructive";

export interface LoadingButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "onClick"> {
  onClick?(e: MouseEvent<HTMLButtonElement>): unknown;
  /* Busy from outside: a hook's job, or the form this button submits. */
  busy?: boolean;
  /* The sweep's colour; read from the button's class (ui.ts) when not given. */
  tone?: Tone;
  /* Layout classes for the wrapper (self-start, ml-auto): it is the flex item now. */
  wrapClassName?: string;
}

function toneOf(className: string | undefined): Tone {
  const c = className ?? "";
  if (c.includes("btn-primary")) return "primary";
  if (c.includes("btn-destructive")) return "destructive";
  if (c.includes("btn-quiet")) return "quiet";
  return "secondary";
}

export function LoadingButton({ onClick, busy = false, tone, wrapClassName, className, disabled, type = "button", children, ...rest }: LoadingButtonProps) {
  const [pending, setPending] = useState(false);
  const inflight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const active = pending || busy;
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!active) {
      setSlow(false);
      return;
    }
    const t = setTimeout(() => setSlow(true), SLOW_MS);
    return () => clearTimeout(t);
  }, [active]);

  const click = (e: MouseEvent<HTMLButtonElement>) => {
    if (inflight.current || busy) {
      e.preventDefault();
      return;
    }
    const out = onClick?.(e);
    if (!(out instanceof Promise)) return;
    inflight.current = true;
    setPending(true);
    const done = () => {
      inflight.current = false;
      if (mounted.current) setPending(false);
    };
    out.then(done, done);
  };

  return (
    <span className={`ld${slow ? " slow" : ""}${wrapClassName ? ` ${wrapClassName}` : ""}`} data-tone={tone ?? toneOf(className)} data-active={active || undefined}>
      <button {...rest} type={type} className={className} disabled={disabled} aria-busy={slow || undefined} onClick={click}>
        {children}
      </button>
      <span className="ld-sweep" aria-hidden="true" />
    </span>
  );
}
