/* Scrolling a plain pane's history (tmux's copy mode, the wheel's on a pane
   whose program asks for no mouse: a shell's, or Claude Code's own
   transcript on the normal screen) in the pane view, in WebKit (WKWebView's
   engine), over the same PtyView Rust as the app (e2e/pty/rig.ts), timed
   as docs/scroll-perf.md has it: wheel → paint, and the paints that move
   the view a second while it scrolls.

   Where the view is in tmux's history comes from tmux itself: in copy mode
   it draws its position (`[N/M]`) at the pane's top right, read here at
   every paint. A paint that changes it is one that moved the view. */

import { expect, test, type Page } from "@playwright/test";
import { claudeBin, startRig, tmuxBin, type Rig } from "./pty/rig";

let rig: Rig;

test.describe.configure({ mode: "serial", timeout: 120_000 });

const TARGET = "agents:hist";
const BUSY_MS = 500;

test.beforeAll(async ({ browserName }, info) => {
  test.skip(browserName !== "webkit", "the app's webview is WebKit's (WKWebView)");
  test.skip(!tmuxBin() || !claudeBin(), "needs tmux and Claude Code on PATH");
  info.setTimeout(300_000);
  rig = await startRig(`http://127.0.0.1:${process.env.E2E_PORT}`);
  // A long, coloured history, as an agent's transcript leaves: 4000 lines in a pane that asks for no mouse.
  rig.tmux("set-option", "-g", "history-limit", "10000");
  const line = String.raw`printf '\033[3%dm%05d\033[0m \033[1mstep\033[0m %s \033[2m(%s)\033[0m\n' $((i % 6 + 1)) $i 'the agent read a file, edited it and ran the tests again' 'src/components/terminal.ts'`;
  rig.tmux("new-window", "-d", "-t", "agents:", "-n", "hist", `sh -c 'i=1; while [ $i -le 4000 ]; do ${line.replace(/'/g, `'"'"'`)}; i=$((i+1)); done; exec cat'`);
  const deadline = Date.now() + 20_000;
  while (!rig.tmux("capture-pane", "-p", "-t", TARGET).includes("04000")) {
    if (Date.now() > deadline) throw new Error("the history pane didn't print");
    await new Promise((r) => setTimeout(r, 100));
  }
});

test.afterAll(() => rig?.stop());

type Win = Window & {
  probe: { wheels: { t: number }[]; sends: { t: number; text: string }[]; recvs: { t: number; bytes: number }[]; ends: number[]; renders: { t: number }[]; terms: import("@xterm/xterm").Terminal[] };
  swipe(el: Element, deltas: number[], perFrame: number): Promise<void>;
  swipeDeltas(peak: number, decay: number, up: boolean): number[];
  where: { t: number; at: string }[];
};

async function open(page: Page): Promise<void> {
  await page.goto(`/shots/preview.html?scene=pty&probe=1&${rig.queryFor(TARGET)}`);
  await page.waitForSelector("html[data-ready]");
  const terminal = page.getByTestId("terminal");
  await expect(terminal).toHaveAttribute("data-view", "live");
  await expect(terminal).toHaveAttribute("data-renderer", "webgl");
  await expect.poll(() => page.evaluate(() => (window as unknown as Win).probe.renders.length), { timeout: 15_000 }).toBeGreaterThan(0);
  // At each paint, where the view is: tmux's copy-mode position, or "live" (not in copy mode).
  await page.evaluate(() => {
    const w = window as unknown as Win;
    w.where = [];
    const term = w.probe.terms.at(-1)!;
    term.onRender(() => {
      const top = term.buffer.active.getLine(0)?.translateToString(true) ?? "";
      w.where.push({ t: performance.timeOrigin + performance.now(), at: top.match(/\[(\d+)\/\d+\]\s*$/)?.[1] ?? "live" });
    });
  });
}

/* Out of copy mode, back at the pane's foot, and the view settled. */
async function reset(page: Page): Promise<void> {
  if (rig.tmux("display-message", "-p", "-t", TARGET, "#{pane_in_mode}") === "1") rig.tmux("send-keys", "-t", TARGET, "-X", "cancel");
  await page.waitForTimeout(400);
}

/* A wheel gesture (`act`), then once the view has been still for half a second, what it did. */
async function measure(page: Page, act: () => Promise<void>) {
  const now = () => page.evaluate(() => performance.timeOrigin + performance.now());
  const from = await now();
  await act();
  // Still: no paint that moved the view for half a second.
  await expect
    .poll(
      () =>
        page.evaluate((from) => {
          const w = (window as unknown as Win).where.filter((p) => p.t >= from);
          let last = from;
          for (let i = 1; i < w.length; i++) if (w[i].at !== w[i - 1].at) last = w[i].t;
          return performance.timeOrigin + performance.now() - Math.max(last, (window as unknown as Win).probe.wheels.at(-1)?.t ?? 0);
        }, from),
      { intervals: [200], timeout: 20_000 },
    )
    .toBeGreaterThan(500);
  const got = await page.evaluate((from) => {
    const w = window as unknown as Win;
    const reports = w.probe.sends
      .filter((s) => s.t >= from)
      .map((s) => ({ t: s.t, n: (s.text.match(/\\u001b\[<6[45];|\x1b\[<6[45];/g) ?? []).length }))
      .filter((s) => s.n > 0);
    return {
      wheels: w.probe.wheels.filter((x) => x.t >= from).map((x) => x.t),
      reports,
      recvs: w.probe.recvs.filter((x) => x.t >= from),
      renders: w.probe.renders.filter((x) => x.t >= from).map((x) => x.t),
      where: w.where.filter((x) => x.t >= from),
      before: w.where.filter((x) => x.t < from).at(-1)?.at ?? "live",
    };
  }, from);
  // The paints that moved the view, and how far each did.
  const moves: { t: number; at: string; lines: number }[] = [];
  let at = got.before;
  for (const p of got.where) {
    if (p.at === at) continue;
    moves.push({ t: p.t, at: p.at, lines: (p.at === "live" ? 0 : Number(p.at)) - (at === "live" ? 0 : Number(at)) });
    at = p.at;
  }
  const pct = (xs: number[], p: number) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.min(xs.length - 1, Math.floor(p * xs.length))] : NaN);
  const r = (x: number) => Math.round(x * 10) / 10;
  // The paint that shows each write's reports: the first at which the view has moved as far as every report so far
  // takes it (tmux's copy mode: 5 lines a report; from the foot, the first only enters copy mode).
  let reported = 0;
  const base = got.before === "live" ? 0 : Number(got.before);
  const shown = got.reports.map((s) => {
    reported += s.n;
    const lines = 5 * (got.before === "live" ? reported - 1 : reported);
    return moves.find((m) => m.t >= s.t && (m.at === "live" ? 0 : Number(m.at)) - base >= lines)?.t ?? NaN;
  });
  // Wheel → paint: from the wheel event that completed a step (the last one before its reports went) to the paint
  // that shows them. Events between steps (a trackpad's fraction of a line) wait for a later one.
  const waits = got.reports.map((s, i) => shown[i] - (got.wheels.filter((t) => t <= s.t).at(-1) ?? s.t)).filter(Number.isFinite);
  // Every wheel event's wait for the next paint that moves the view: a notch that sends nothing waits for the next.
  const eventWaits = got.wheels.map((t) => (moves.find((m) => m.t >= t)?.t ?? NaN) - t).filter(Number.isFinite);
  // Each write's round trip, through tmux and back to a paint.
  const trips = got.reports.map((s, i) => shown[i] - s.t).filter(Number.isFinite);
  const first = got.wheels[0];
  const lastMove = moves.at(-1)?.t ?? first;
  const span = got.wheels.at(-1)! - first;
  // While it scrolls: from the first wheel event to the last paint that moved the view.
  const scrolling = lastMove - first;
  const gaps = moves.slice(1).map((m, i) => m.t - moves[i].t);
  // The gesture's busy part, its first `BUSY_MS` (a trackpad's push and early momentum, many pixels a frame): the
  // paints that moved the view a second, and the longest wait between two.
  const busy = moves.filter((m) => m.t < first + BUSY_MS);
  const busyGaps = [busy[0]?.t - first, ...busy.slice(1).map((m, i) => m.t - busy[i].t)].filter(Number.isFinite);
  return {
    wheelEvents: got.wheels.length,
    wheelSpanMs: Math.round(span),
    reports: got.reports.reduce((n, s) => n + s.n, 0),
    writes: got.reports.length,
    lines: Number(at === "live" ? 0 : at) - Number(got.before === "live" ? 0 : got.before),
    moves: moves.length,
    firstPaintMs: r((moves[0]?.t ?? NaN) - first),
    wheelToPaintP50: r(pct(waits, 0.5)),
    wheelToPaintP95: r(pct(waits, 0.95)),
    eventToPaintP50: r(pct(eventWaits, 0.5)),
    eventToPaintP95: r(pct(eventWaits, 0.95)),
    reportToPaintP50: r(pct(trips, 0.5)),
    reportToPaintP95: r(pct(trips, 0.95)),
    settleMs: r(lastMove - got.wheels.at(-1)!),
    movingFps: r(scrolling > 0 ? ((moves.length - 1) / scrolling) * 1000 : 0),
    gapMaxMs: r(Math.max(0, ...gaps)),
    busyFps: r((busy.length / BUSY_MS) * 1000),
    busyGapMaxMs: r(Math.max(0, ...busyGaps)),
    linesPerMoveMax: Math.max(0, ...moves.map((m) => Math.abs(m.lines))),
    bytesPerReport: Math.round(got.recvs.reduce((n, x) => n + x.bytes, 0) / Math.max(1, got.reports.reduce((n, s) => n + s.n, 0))),
    messages: got.recvs.length,
  };
}

async function notches(page: Page, deltas: number[], apart: number) {
  const box = (await page.getByTestId("terminal").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  for (const dy of deltas) {
    await page.mouse.wheel(0, dy);
    await page.waitForTimeout(apart);
  }
}

const swipe = (page: Page, peak: number, decay: number, perFrame: number) => () =>
  page.evaluate(
    ([peak, decay, perFrame]) => {
      const w = window as unknown as Win;
      return w.swipe(document.querySelector('[data-testid="terminal"] .xterm-screen')!, w.swipeDeltas(peak, decay, true), perFrame);
    },
    [peak, decay, perFrame],
  );

/* What a gesture did, logged for docs/scroll-perf.md. */
const report = (name: string, stats: Awaited<ReturnType<typeof measure>>) => {
  console.log(`scroll ${name}`, JSON.stringify(stats));
  return stats;
};

test("pty scroll: a plain pane's history scrolls under the wheel and the trackpad as under a native terminal", async ({ page }) => {
  await open(page);
  // Not timed: the first copy mode a server enters.
  await measure(page, () => notches(page, [-40, -40, -40], 100));
  await reset(page);

  // A mouse: one notch (macOS's deltaY 1, WebKit's 40 px) at a time, a third of a second apart.
  const slow = report("notches", await measure(page, () => notches(page, Array(10).fill(-40), 300)));
  await reset(page);
  // A mouse wheel spun: macOS accelerates its notches, 16 ms apart.
  const spin = report("spin", await measure(page, () => notches(page, [-40, -80, -120, -200, -280, -360, -400, -400, -360, -280, -200, -120, -80, -40], 16)));
  await reset(page);
  // A trackpad: a swipe at 60 Hz (an event a frame), and a flick at 120 Hz (two a frame).
  const swiped = report("swipe60", await measure(page, swipe(page, 48, 0.94, 1)));
  await reset(page);
  const flicked = report("flick120", await measure(page, swipe(page, 40, 0.975, 2)));
  await reset(page);

  // Every notch moves the view, as a native terminal's does.
  expect(slow.moves, `each notch moved the view: ${JSON.stringify(slow)}`).toBeGreaterThanOrEqual(10);
  expect(slow.eventToPaintP95, `each notch is painted within two frames, or three for a busy page: ${JSON.stringify(slow)}`).toBeLessThanOrEqual(50);
  for (const [name, s] of Object.entries({ slow, spin, swiped, flicked })) {
    expect(s.wheelToPaintP95, `${name}: a wheel event is painted within two frames, or three for a busy page: ${JSON.stringify(s)}`).toBeLessThanOrEqual(50);
  }
  for (const [name, s] of Object.entries({ swiped, flicked })) {
    expect(s.busyFps, `${name}: a trackpad swipe moves the view every frame or two while the finger moves: ${JSON.stringify(s)}`).toBeGreaterThanOrEqual(30);
    expect(s.busyGapMaxMs, `${name}: and never stalls then: ${JSON.stringify(s)}`).toBeLessThanOrEqual(70);
  }
});
