import { createContext, useContext } from "react";
import type { DaemonClient } from "./client";

/* The client screens call actions and one-off reads through. */
export const DaemonContext = createContext<DaemonClient | null>(null);

export function useDaemon(): DaemonClient {
  const client = useContext(DaemonContext);
  if (!client) throw new Error("useDaemon needs a <DaemonContext.Provider>");
  return client;
}
