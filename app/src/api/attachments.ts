import { invoke, isTauri } from "@tauri-apps/api/core";
import { getCurrentWebview } from "@tauri-apps/api/webview";

export type Attachment = { path: string } | { name: string; data_base64: string };
export type FileDrop = { type: "enter" | "over" | "drop" | "leave"; x?: number; y?: number; paths?: string[] };

export const clipboardAttachments = (): Promise<Attachment[]> => invoke("pty_view_files");

// Both clipboard files and drops cross this seam before reaching the engine.
// Windows asks the connected WSL distribution, whose drive mounts may differ.
export async function engineAttachment(file: Attachment): Promise<Attachment> {
  return "path" in file && isTauri()
    ? { path: await invoke<string>("pty_view_attachment_path", { path: file.path }) }
    : file;
}

// Wry reports logical points on macOS and GTK; only WebView2 uses physical pixels.
// Hit testing in both the terminal and visual answer bar needs CSS pixels.
// The webview's listener, not the window's: with Tauri's `unstable` feature (child webviews for
// visuals) the main webview is a child too, and a child's drags reach only its own listeners.
export async function listenForDrops(handle: (drop: FileDrop) => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  return getCurrentWebview().onDragDropEvent(({ payload }) => {
    if (payload.type === "leave") return handle({ type: "leave" });
    const scale = /Win/.test(navigator.platform) ? window.devicePixelRatio : 1;
    handle({ type: payload.type, x: payload.position.x / scale, y: payload.position.y / scale,
      paths: "paths" in payload ? payload.paths : undefined });
  });
}
