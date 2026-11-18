/** The session as a drop target (approved chat-ui3): files dragged anywhere over
 * the live CLI or the message box get a green dashed outline and the drop card,
 * and a drop hands the files to the message box, where they wait as chips for
 * Send. These are the webview's own (HTML5) drags, so a screenshot's floating
 * thumbnail, which has no path, lands like any file.
 */
import { useRef, useState, type DragEvent, type ReactNode } from "react";
import { carriesFiles } from "../../../api/attachments";
import { PaperclipIcon } from "../../../components/icons";

export interface ChatDropProps {
  /** Off, the session takes no drags here and they go on to whatever else listens (the terminal's own). */
  enabled: boolean;
  onFiles(files: File[]): void;
  /** Told while files are over the session, for the box's own "Drop to attach…". */
  onDragging?(over: boolean): void;
  className?: string;
  children: ReactNode;
}

type Dragged = "image" | "file" | "files";

/* What is dragged, as far as a drag says before it is dropped: how many, and whether it is an image. */
function dragged(data: DataTransfer): Dragged {
  const items = Array.from(data.items ?? []).filter((item) => item.kind === "file");
  if (items.length > 1) return "files";
  return items[0]?.type.startsWith("image/") ? "image" : "file";
}

const CARD: Record<Dragged, { title: string; what: string }> = {
  image: { title: "Drop the image to attach", what: "It's added to your message as an image." },
  file: { title: "Drop the file to attach", what: "It's added to your message as a file." },
  files: { title: "Drop the files to attach", what: "They're added to your message." },
};

export function ChatDrop({ enabled, onFiles, onDragging, className = "", children }: ChatDropProps) {
  const [over, setOver] = useState<Dragged | null>(null);
  // Enters and leaves come per element crossed: the drag has left once they even out.
  const depth = useRef(0);
  const show = (what: Dragged | null) => {
    setOver(what);
    onDragging?.(what !== null);
  };
  // Taken here, and kept from the window's listeners: the terminal under it would attach the file itself.
  const taken = (e: DragEvent) => {
    if (!enabled || !carriesFiles(e.dataTransfer)) return false;
    e.preventDefault();
    e.stopPropagation();
    return true;
  };
  return (
    <div
      className={`relative ${className}`}
      data-testid="chat-drop"
      data-dropping={over ?? undefined}
      onDragEnter={(e) => {
        if (!taken(e)) return;
        depth.current += 1;
        show(dragged(e.dataTransfer));
      }}
      onDragOver={(e) => {
        if (!taken(e)) return;
        e.dataTransfer.dropEffect = "copy";
      }}
      onDragLeave={(e) => {
        if (!taken(e)) return;
        depth.current = Math.max(0, depth.current - 1);
        if (depth.current === 0) show(null);
      }}
      onDrop={(e) => {
        if (!taken(e)) return;
        depth.current = 0;
        show(null);
        const files = Array.from(e.dataTransfer.files);
        if (files.length) onFiles(files);
      }}
    >
      {children}
      {over && (
        <div className="pointer-events-none absolute -inset-1.5 z-30 flex items-center justify-center rounded-[10px] border-2 border-dashed border-pass bg-[color-mix(in_srgb,var(--canvas)_62%,transparent)]" data-testid="chat-drop-card">
          <div className="flex w-[366px] max-w-[calc(100%-32px)] flex-col items-center gap-2 rounded-[10px] border border-line-strong bg-field px-s3 py-s3 text-center shadow-[0_16px_40px_rgba(0,0,0,0.32)]">
            <span className="mb-1 inline-flex size-9 items-center justify-center rounded-full bg-pass-soft text-pass">
              <PaperclipIcon size={18} />
            </span>
            <p className="text-[15px] font-semibold text-ink">{CARD[over].title}</p>
            <p className="text-sm text-ink-2">{CARD[over].what}</p>
            <p className="text-[13px] text-ink-3">Nothing is sent until you press Send.</p>
          </div>
        </div>
      )}
    </div>
  );
}
