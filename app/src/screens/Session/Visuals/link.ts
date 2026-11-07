/* brindle://visual/<id> in a session's chat opens that visual in the chat's place. The session
   screen provides the opener; a terminal outside a session (the person's own shell) has none, and
   the link does nothing there. */

import { createContext } from "react";

export const VisualLinkContext = createContext<((id: string) => void) | null>(null);

/* The pane's text links, for xterm's web-links addon: the scheme and an id, nothing after. */
export const VISUAL_LINK = /brindle:\/\/visual\/[A-Za-z0-9_-]{1,64}/;
