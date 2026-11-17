/** Approved chat-ui5 / chat-ui4 option A contract. Unmounted stub.
 * Send owner: composer state, submit/error lifecycle, client.chatSend.
 * Queue owner: snapshot queue chips inside the box, remove/send-now actions.
 * Attach owner: file picker, paste/drop chips and preview; full byte payloads.
 * Pane owner: hosts this box below the live CLI and forwards session drops.
 * Keep these regions independent; coordinate shared prop/API changes.
 */
import type { DaemonClient } from "../../../api/client";
import type { QueuedChatMessageDTO } from "../../../api/schema";

export interface ChatBoxProps {
  client: Pick<DaemonClient, "chatSend" | "chatEnqueue" | "chatRemove" | "chatSendNow">;
  agentId: string;
  busy: boolean;
  queue: QueuedChatMessageDTO[];
  /** Pane forwards dropped files here, including screenshots without paths.
   * Consume each batch once, then call onFilesConsumed. */
  droppedFiles?: File[];
  onFilesConsumed?(): void;
  /** Refresh snapshot after a successful mutation. */
  onChanged?(): void;
}

// -- send owner ---------------------------------------------------------------
// One-line composer grows to 40vh, then scrolls; controls remain visible.
// Green focus border/glow; paperclip left, Send right; no text below the box.
// Enter submits, Shift+Enter inserts a newline; IME composition never submits.
// Empty text with no chips disables Send. Keep the draft on failure; clear on
// success only if agent and draft still match the request's starting state.

// -- queue owner --------------------------------------------------------------
// Busy Send queues in brindle. Snapshot is authoritative, scoped by agentId.
// Option A: one line per chip inside the box above the draft; ellipsis and
// +N line breaks for long text, Send now and remove on each chip. No overlay.

// -- paste/drop owner ---------------------------------------------------------
// Picker and dropped File bytes become {name, data_base64}, never file paths.
// Screenshot thumbnail/name/size and removable file chips wait for Send.
// Pasted text over 30 lines OR 4096 UTF-8 bytes becomes a removable chip.
// Typed text never becomes a chip. Small pastes insert at the text selection.
// Preview is read-only, numbered, scrollable, closed by Esc or its close button.
// Send expands all paste chips after the typed text, in full, in paste order.
export function ChatBox(_props: ChatBoxProps) {
  return null;
}
