import { describe, expect, it } from "vitest";
import { EventStream, type StreamMessage, type StreamState } from "./events";
import { FakeDaemon, flush } from "../test/fakeDaemon";

function stream(daemon: FakeDaemon, token = daemon.token, since = 0) {
  const client = daemon.client(token);
  const messages: StreamMessage[] = [];
  const states: StreamState[] = [];
  const s = new EventStream({
    url: (n) => client.eventsUrl(n),
    token,
    since,
    connect: daemon.connect,
    backoff: () => 0,
    onMessage: (m) => messages.push(m),
    onState: (st) => states.push(st),
  });
  s.start();
  return { s, messages, states };
}

const ids = (messages: StreamMessage[]) => messages.map((m) => (m.type === "event" ? m.id : m.type));

describe("EventStream", () => {
  it("sends the token as its first frame, never in the URL, and gets the replay above since", async () => {
    const daemon = new FakeDaemon();
    daemon.publish("agent.status");
    daemon.publish("agent.status");
    daemon.publish("agent.result");
    const { s, messages, states } = stream(daemon, daemon.token, 1);
    await flush();
    const [socket] = daemon.sockets;
    expect(socket.frames).toEqual([daemon.token]);
    expect(socket.url.toString()).not.toContain(daemon.token);
    expect(ids(messages)).toEqual([2, 3]);
    expect(s.since).toBe(3);
    expect(states).toEqual(["connecting", "open"]);
  });

  it("reconnects after a drop, resuming from the last event it saw", async () => {
    const daemon = new FakeDaemon();
    const { s, messages } = stream(daemon);
    await flush();
    daemon.publish("agent.status");
    await flush();
    daemon.drop();
    daemon.publish("agent.result"); // while the app is away
    await flush();
    expect(daemon.sockets.map((sock) => sock.since)).toEqual([0, 1]);
    expect(ids(messages)).toEqual([1, 2]);
    daemon.publish("review.submitted");
    await flush();
    expect(ids(messages)).toEqual([1, 2, 3]);
    expect(s.since).toBe(3);
  });

  it("is told to resnapshot when the events it missed were pruned", async () => {
    const daemon = new FakeDaemon();
    const { messages } = stream(daemon);
    daemon.publish("agent.status");
    await flush();
    daemon.drop();
    daemon.publish("agent.status");
    daemon.publish("agent.status");
    daemon.prune(2);
    await flush();
    expect(ids(messages)).toEqual([1, "resnapshot"]);
  });

  it("stops for good when the daemon refuses its token", async () => {
    const daemon = new FakeDaemon();
    const { states } = stream(daemon, "stale-token");
    await flush();
    expect(states).toEqual(["connecting", "open", "unauthorized"]);
    expect(daemon.sockets).toHaveLength(1);
  });

  it("doesn't reconnect once stopped", async () => {
    const daemon = new FakeDaemon();
    const { s } = stream(daemon);
    await flush();
    s.stop();
    await flush();
    expect(daemon.sockets).toHaveLength(1);
    expect(daemon.sockets[0].closed).toBe(true);
  });
});
