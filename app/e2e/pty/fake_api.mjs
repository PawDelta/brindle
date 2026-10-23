/* A stand-in for the Messages API, for e2e/pty.e2e.ts: Claude Code at work
   (its spinner, an answer streaming in) with no real API. Every request is
   answered with an answer of numbered words, streamed a word every
   `delay` ms (SSE, when asked for a stream). Listens on a free localhost
   port and prints `fake_api listening on <port>`; goes when stdin closes.

       node fake_api.mjs <words> <delay ms> */

import { createServer } from "node:http";

const words = Number(process.argv[2] ?? 400);
const delay = Number(process.argv[3] ?? 40);
const text = (n) => Array.from({ length: n }, (_, i) => `word${i + 1}${i % 12 === 11 ? "\n\n" : " "}`);

const message = (content, outputTokens) => ({
  id: `msg_e2e${Date.now()}`,
  type: "message",
  role: "assistant",
  model: "claude-haiku-4-5",
  content,
  stop_reason: content.length ? "end_turn" : null,
  stop_sequence: null,
  usage: { input_tokens: 10, output_tokens: outputTokens },
});

const server = createServer((req, res) => {
  let body = "";
  req.on("data", (d) => (body += d));
  req.on("end", () => {
    let asked = {};
    try {
      asked = JSON.parse(body || "{}");
    } catch {
      // not JSON: answered all the same
    }
    if (!req.url.startsWith("/v1/messages") || req.url.startsWith("/v1/messages/count_tokens")) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(req.url.includes("count_tokens") ? { input_tokens: 10 } : {}));
      return;
    }
    if (!asked.stream) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify(message([{ type: "text", text: "ok" }], 1)));
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream", "cache-control": "no-cache", connection: "keep-alive" });
    const event = (type, data) => res.write(`event: ${type}\ndata: ${JSON.stringify({ type, ...data })}\n\n`);
    event("message_start", { message: message([], 1) });
    event("content_block_start", { index: 0, content_block: { type: "text", text: "" } });
    const parts = text(words);
    let i = 0;
    const next = () => {
      if (res.destroyed) return;
      if (i < parts.length) {
        event("content_block_delta", { index: 0, delta: { type: "text_delta", text: parts[i++] } });
        setTimeout(next, delay);
        return;
      }
      event("content_block_stop", { index: 0 });
      event("message_delta", { delta: { stop_reason: "end_turn", stop_sequence: null }, usage: { output_tokens: parts.length } });
      event("message_stop", {});
      res.end();
    };
    next();
  });
});

server.listen(0, "127.0.0.1", () => console.log(`fake_api listening on ${server.address().port}`));
process.stdin.on("end", () => process.exit(0));
process.stdin.resume();
