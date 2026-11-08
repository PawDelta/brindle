/* Who owns Escape right now. A reader that goes back on Escape (a visual, a plan) says so while it is
   open; the profile rail's own Esc-back (App) then leaves it alone, whichever listener runs first. */

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
