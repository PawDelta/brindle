/* A reader that goes back on Escape (a visual or plan) claims it while open. The profile rail
   and xterm defer to the reader even if their listeners run first. */

import { useEffect } from "react";

let owners = 0;

export const escapeOwned = (): boolean => owners > 0;

export function useEscapeOwner(): void {
  useEffect(() => {
    owners += 1;
    return () => {
      owners -= 1;
    };
  }, []);
}
