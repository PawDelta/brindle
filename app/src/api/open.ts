/* Opening a web page from the app: in the system browser, never in the
   webview, so the person's own browser session is used (a brindle Pro
   sign-in, the pricing page). In the app the shell does it (the `open_url`
   command in src-tauri/src/lib.rs); a plain browser tab opens a new tab. */

import { invoke, isTauri } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";

export type OpenUrl = (url: string) => Promise<void>;

export const openExternal: OpenUrl = async (url) => {
  if (isTauri()) {
    await invoke("open_url", { url });
    return;
  }
  window.open(url, "_blank", "noopener");
};

/* The native folder picker (the dialog plugin, src-tauri's `dialog:allow-open`):
   the folder's path, or null when the person cancelled. Only the app has one:
   a plain browser tab gets no picker, and Knowledge takes the path typed instead. */
export type PickFolder = (title: string) => Promise<string | null>;

/* Local Knowledge folders are opened by the shell; their paths never go to Pro. */
export async function openLocalPath(path: string): Promise<void> {
  if (!isTauri()) throw new Error("Open local folders in the desktop app.");
  await invoke("open_local_path", { path });
}

export const pickFolder: PickFolder | undefined = isTauri()
  ? async (title) => {
      const picked = await open({ directory: true, multiple: false, title });
      return typeof picked === "string" ? picked : null;
    }
  : undefined;
