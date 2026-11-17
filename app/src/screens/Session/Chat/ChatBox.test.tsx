/** Expected failures for the three ChatBox owners. Remove it.fails as each
 * behaviour lands; these exercise user events and resulting state, not mocks.
 * send: composer; queue: option A; paste|drop: attachments and large text.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ChatMessageDTO, ChatResultDTO, QueuedChatMessageDTO } from "../../../api/schema";
import { ChatBox } from "./ChatBox";

afterEach(cleanup);

class ChatServer {
  delivered: ChatMessageDTO[] = [];
  queue: QueuedChatMessageDTO[] = [];
  busy = false;
  fail = false;
  private nextId = 0;

  async chatSend(_agent: string, message: ChatMessageDTO): Promise<ChatResultDTO> {
    if (this.fail) throw new Error("unavailable");
    if (this.busy) return this.chatEnqueue(_agent, message);
    this.delivered.push(message);
    return { status: "delivered" };
  }
  async chatEnqueue(_agent: string, message: ChatMessageDTO): Promise<ChatResultDTO> {
    const id = String(++this.nextId);
    this.queue.push({ id, text: message.text, attachments: message.attachments?.map((a) => a.name) ?? [] });
    return { status: "queued", message_id: id };
  }
  async chatRemove(_agent: string, id: string) {
    this.queue = this.queue.filter((item) => item.id !== id);
    return { removed: id };
  }
  async chatSendNow(_agent: string, id: string): Promise<ChatResultDTO> {
    const item = this.queue.find((item) => item.id === id)!;
    this.delivered.push({ text: item.text });
    await this.chatRemove(_agent, id);
    return { status: "delivered", message_id: id };
  }
}

function mount(server = new ChatServer()) {
  const props = () => ({ client: server, agentId: "a", busy: server.busy, queue: [...server.queue], onChanged: refresh });
  const view = render(<ChatBox {...props()} />);
  function refresh() { view.rerender(<ChatBox {...props()} />); }
  return { server, view };
}
const editor = () => screen.getByRole("textbox") as HTMLTextAreaElement;
const type = (text: string) => fireEvent.change(editor(), { target: { value: text } });
const send = () => fireEvent.click(screen.getByRole("button", { name: /^send$/i }));
const paste = (text: string) => fireEvent.paste(editor(), { clipboardData: { getData: () => text, files: [] } });

describe("ChatBox send owner", () => {
  it.fails("send submits a multiline draft once and clears it after success", async () => {
    const { server } = mount();
    type("First\nSecond");
    fireEvent.keyDown(editor(), { key: "Enter", shiftKey: true });
    expect(server.delivered).toEqual([]);
    fireEvent.keyDown(editor(), { key: "Enter", isComposing: true });
    expect(server.delivered).toEqual([]);
    fireEvent.keyDown(editor(), { key: "Enter" });
    await waitFor(() => expect(server.delivered.map((m) => m.text)).toEqual(["First\nSecond"]));
    expect(editor().value).toBe("");
  });

  it.fails("send failure preserves the draft and exposes an error state", async () => {
    const server = new ChatServer();
    server.fail = true;
    mount(server);
    type("Keep my draft");
    send();
    await screen.findByRole("alert");
    expect(editor().value).toBe("Keep my draft");
    expect(server.delivered).toEqual([]);
  });

  it.fails("send keeps long typed text editable with a bounded scrolling composer", () => {
    mount();
    const text = Array.from({ length: 60 }, (_, i) => `typed line ${i}`).join("\n");
    type(text);
    expect(editor().value).toBe(text);
    expect(screen.queryByTestId("paste-chip")).toBeNull();
    expect(getComputedStyle(screen.getByTestId("chat-composer")).maxHeight).toBe("40vh");
    expect(getComputedStyle(editor()).overflowY).toBe("auto");
  });
});

describe("ChatBox queue owner", () => {
  it.fails("queue busy Send holds the full draft and shows a compact chip inside the box", async () => {
    const server = new ChatServer();
    server.busy = true;
    mount(server);
    const text = "Next task\n" + "more context\n".repeat(24);
    type(text);
    send();
    await waitFor(() => expect(server.queue.map((m) => m.text)).toEqual([text]));
    expect(server.delivered).toEqual([]);
    const chip = within(screen.getByTestId("chat-composer")).getByTestId("queue-chip");
    expect(chip.getAttribute("data-lines")).toBe("25");
    expect(getComputedStyle(within(chip).getByTestId("queue-preview")).whiteSpace).toBe("nowrap");
    expect(editor().value).toBe("");
  });

  it.fails("queue remove deletes only its chip without sending it", async () => {
    const server = new ChatServer();
    await server.chatEnqueue("a", { text: "Remove" });
    await server.chatEnqueue("a", { text: "Keep" });
    mount(server);
    const chip = screen.getAllByTestId("queue-chip")[0];
    fireEvent.click(within(chip).getByRole("button", { name: /remove/i }));
    await waitFor(() => expect(server.queue.map((m) => m.text)).toEqual(["Keep"]));
    expect(server.delivered).toEqual([]);
    expect(screen.getAllByTestId("queue-chip")).toHaveLength(1);
  });

  it.fails("queue Send now sends the selected chip and preserves the others", async () => {
    const server = new ChatServer();
    await server.chatEnqueue("a", { text: "Keep waiting" });
    await server.chatEnqueue("a", { text: "Urgent" });
    mount(server);
    fireEvent.click(within(screen.getAllByTestId("queue-chip")[1]).getByRole("button", { name: /send now/i }));
    await waitFor(() => expect(server.delivered.map((m) => m.text)).toEqual(["Urgent"]));
    expect(server.queue.map((m) => m.text)).toEqual(["Keep waiting"]);
  });
});

describe("ChatBox paste/drop owner", () => {
  it.fails.each([
    ["line threshold", "line\n".repeat(31)],
    ["UTF-8 byte threshold", "✓".repeat(1400)],
  ])("paste above the %s previews and sends the entire content", async (_case, text) => {
    const { server } = mount();
    type("Please inspect");
    paste(text);
    const chip = screen.getByTestId("paste-chip");
    expect(editor().value).toBe("Please inspect");
    fireEvent.click(within(chip).getByRole("button", { name: /preview/i }));
    const preview = screen.getByRole("dialog");
    expect(preview.textContent).toContain(text.split("\n")[0]);
    expect(within(preview).queryByRole("textbox")).toBeNull();
    fireEvent.keyDown(preview, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    send();
    await waitFor(() => expect(server.delivered.map((m) => m.text)).toEqual([`Please inspect\n${text}`]));
  });

  it.fails("paste at both limits stays inline at the current selection", () => {
    mount();
    type("Before after");
    editor().setSelectionRange(7, 7);
    const text = "x".repeat(4067) + "\n".repeat(29); // 4096 bytes, 30 lines.
    paste(text);
    expect(editor().value).toBe(`Before ${text}after`);
    expect(screen.queryByTestId("paste-chip")).toBeNull();
  });

  it.fails("paste chip removal preserves the typed draft", () => {
    mount();
    type("Keep this");
    paste("x".repeat(4097));
    fireEvent.click(within(screen.getByTestId("paste-chip")).getByRole("button", { name: /remove/i }));
    expect(screen.queryByTestId("paste-chip")).toBeNull();
    expect(editor().value).toBe("Keep this");
  });

  it.fails("drop screenshot bytes without a path waits for Send", async () => {
    const server = new ChatServer();
    const file = new File([new Uint8Array([137, 80, 78, 71])], "Screenshot.png", { type: "image/png" });
    render(<ChatBox client={server} agentId="a" busy={false} queue={[]} droppedFiles={[file]} />);
    await screen.findByTestId("attachment-chip");
    expect(server.delivered).toEqual([]);
    send();
    await waitFor(() => expect(server.delivered).toHaveLength(1));
    expect(server.delivered[0].attachments).toEqual([{ name: "Screenshot.png", data_base64: "iVBORw==" }]);
  });
});
