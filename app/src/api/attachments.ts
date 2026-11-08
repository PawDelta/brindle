import { invoke, isTauri } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";

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

// Tauri positions are physical pixels; the terminal's bounds are CSS pixels.
export async function listenForDrops(handle: (drop: FileDrop) => void): Promise<() => void> {
  if (!isTauri()) return () => {};
  return getCurrentWindow().onDragDropEvent(({ payload }) => {
    if (payload.type === "leave") return handle({ type: "leave" });
    handle({ type: payload.type, x: payload.position.x / window.devicePixelRatio, y: payload.position.y / window.devicePixelRatio,
      paths: "paths" in payload ? payload.paths : undefined });
  });
}
