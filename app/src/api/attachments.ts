import { invoke, isTauri } from "@tauri-apps/api/core";

export type Attachment = { path: string } | { name: string; data_base64: string };
/* A drag of files over the window, in CSS pixels. A drop is told as it lands, so whoever it lands on
   settles that it is theirs from what is on screen then; `read` gives the files' bytes after, read
   here in the webview: an HTML5 drop has no path (a screenshot's floating thumbnail has no file at
   all). Whoever reads checks afterwards that it is still showing what the drop was for. */
export type FileDrop =
  | { type: "enter" | "over"; x: number; y: number }
  | { type: "leave" }
  | { type: "drop"; x: number; y: number; read(): Promise<Attachment[]> };

export const clipboardAttachments = (): Promise<Attachment[]> => invoke("pty_view_files");

// Both clipboard files and drops cross this seam before reaching the engine.
// Windows asks the connected WSL distribution, whose drive mounts may differ.
export async function engineAttachment(file: Attachment): Promise<Attachment> {
  return "path" in file && isTauri()
    ? { path: await invoke<string>("pty_view_attachment_path", { path: file.path }) }
    : file;
}

/* A dropped, pasted or picked file's bytes, as the engine takes them. */
export function fileAttachment(file: File): Promise<{ name: string; data_base64: string }> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const url = String(reader.result);
      resolve({ name: file.name, data_base64: url.slice(url.indexOf(",") + 1) });
    };
    reader.onerror = () => reject(reader.error ?? new Error(`Couldn't read ${file.name}`));
    reader.readAsDataURL(file);
  });
}

export const carriesFiles = (data: DataTransfer | null | undefined) => !!data && Array.from(data.types ?? []).includes("Files");

const listeners = new Set<(drop: FileDrop) => void>();
let watching = false;

/* The webview's own (HTML5) file drags, for the whole window: the shell's native drag-drop handler is
   off for the main webview (tauri.conf.json dragDropEnabled), so drops reach the page. Every file drag
   is taken here, so one released anywhere never makes the webview open the file in the app's place.
   A target that takes a drag itself (the session's message box) stops it before it gets here. */
export function watchFileDrops() {
  if (watching || typeof window === "undefined") return;
  watching = true;
  let depth = 0;
  const tell = (drop: FileDrop) => listeners.forEach((handle) => handle(drop));
  window.addEventListener("dragenter", (e) => {
    if (!carriesFiles(e.dataTransfer)) return;
    e.preventDefault();
    depth += 1;
    tell({ type: "enter", x: e.clientX, y: e.clientY });
  });
  window.addEventListener("dragover", (e) => {
    if (!carriesFiles(e.dataTransfer)) return;
    e.preventDefault();
    tell({ type: "over", x: e.clientX, y: e.clientY });
  });
  // Enters and leaves come per element crossed: the drag has left once they even out.
  window.addEventListener("dragleave", (e) => {
    if (!carriesFiles(e.dataTransfer)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) tell({ type: "leave" });
  });
  window.addEventListener("drop", (e) => {
    if (!carriesFiles(e.dataTransfer)) return;
    e.preventDefault();
    depth = 0;
    // The files are the event's only while it is handled; their bytes are read once, when a target asks.
    const dropped = Array.from(e.dataTransfer!.files);
    let bytes: Promise<Attachment[]> | undefined;
    // A folder, or a promised file that never came, can't be read: left out, and a drop of none says so.
    const read = () => (bytes ??= Promise.allSettled(dropped.map(fileAttachment)).then((got) => got.flatMap((r) => (r.status === "fulfilled" ? [r.value] : []))));
    tell({ type: "drop", x: e.clientX, y: e.clientY, read });
  });
}

export async function listenForDrops(handle: (drop: FileDrop) => void): Promise<() => void> {
  watchFileDrops();
  listeners.add(handle);
  return () => void listeners.delete(handle);
}
