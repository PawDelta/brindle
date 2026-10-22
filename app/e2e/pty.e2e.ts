/* The pane view as the app ships it, in WebKit (WKWebView's engine): the
   session screen's provider surface over a real tmux client on a pty
   (PtyTerminal, through src-tauri/examples/pty_ws.rs: the same PtyView Rust
   as the shell's pty_view_* commands), attached to a live Claude Code on a
   private tmux server (e2e/pty/rig.ts: never a person's panes, no API).

   Claude Code draws full screen with the mouse its own: the wheel reaches it
   as the SGR reports xterm.js makes and tmux passes on, a click on its
   "Jump to bottom" as a click, keys as keys. Nothing of the app's own sits
   between: these are xterm.js's and tmux's doing, as under any terminal.

   Screenshots land in the test's output folder (e2e/out/...). */

import { expect, test, type Page } from "@playwright/test";
import { claudeBin, LINES, nativeClient, startRig, tmuxBin, type Rig } from "./pty/rig";

let rig: Rig;

// A live Claude Code and a real browser: a cold first page, or a busy machine, takes longer than the default 30 s.
test.describe.configure({ mode: "serial", timeout: 120_000 });

test.beforeAll(async ({ browserName }, info) => {
  test.skip(browserName !== "webkit", "the app's webview is WebKit's (WKWebView)");
  test.skip(!tmuxBin() || !claudeBin(), "needs tmux and Claude Code on PATH");
  info.setTimeout(300_000);
  rig = await startRig(`http://127.0.0.1:${process.env.E2E_PORT}`);
});

test.afterAll(() => rig?.stop());

/* The text of each of the terminal's rows (the DOM renderer's, which draws some spaces as no-break ones). */
const drawn = async (page: Page) => (await page.locator('[data-testid="terminal"] .xterm-rows > div').allTextContents()).map((r) => r.replace(/ /g, " "));

/* Each row as wide as the terminal: a row narrowed keeps what was past its end in xterm.js's buffer, which the DOM
   renderer writes out past the screen's edge, unseen. */
const rows = async (page: Page) => {
  const cols = viewClient()?.cols ?? Infinity;
  return (await drawn(page)).map((r) => r.slice(0, cols));
};

/* The numbers of the "history line N" rows on screen. */
async function visible(page: Page): Promise<number[]> {
  return (await rows(page)).flatMap((row) => [...row.matchAll(/history line (\d+)/g)].map((m) => Number(m[1])));
}

const top = async (page: Page) => Math.min(...(await visible(page)));
const bottom = async (page: Page) => Math.max(...(await visible(page)));

/* The view's own tmux client: its session, and its size. */
function viewClient(): { session: string; cols: number; rows: number } | null {
  const line = rig
    .tmux("list-clients", "-F", "#{session_name} #{client_width} #{client_height}")
    .split("\n")
    .find((l) => l.startsWith("brindle-view-"));
  if (!line) return null;
  const [session, cols, rows] = line.split(" ");
  return { session, cols: Number(cols), rows: Number(rows) };
}

const windowSize = () => rig.tmux("display-message", "-p", "-t", rig.target, "#{window_width}x#{window_height}");

/* The widest run of Claude Code's rule (─) on screen: it draws its input box's rules the width of the window. */
const ruleWidth = async (page: Page) => Math.max(0, ...(await rows(page)).map((r) => Math.max(0, ...[...r.matchAll(/─+/g)].map((m) => m[0].length))));

async function open(page: Page): Promise<void> {
  await page.goto(`/shots/preview.html?scene=pty&renderer=dom&${rig.query}`);
  await page.waitForSelector("html[data-ready]");
  const terminal = page.getByTestId("terminal");
  await expect(terminal).toHaveAttribute("data-pty", "1");
  await expect(terminal).toHaveAttribute("data-view", "live");
  // The client attached and drew Claude Code: the conversation's foot, at the view's size.
  await expect.poll(() => visible(page), { timeout: 10_000 }).toContain(LINES);
  await expect.poll(() => viewClient() !== null).toBe(true);
}

async function shot(page: Page, name: string): Promise<void> {
  await page.getByTestId("terminal").screenshot({ path: test.info().outputPath(`${name}.png`) });
}

/* Back to the foot of the conversation, wherever a test left it. */
async function toBottom(page: Page): Promise<void> {
  const box = (await page.getByTestId("terminal").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 3);
  for (let i = 0; i < 40 && (await bottom(page)) < LINES; i++) await page.mouse.wheel(0, 400);
  await expect.poll(() => bottom(page)).toBe(LINES);
}

test("pty: the wheel and the trackpad scroll a live Claude Code's history", async ({ page }) => {
  await open(page);
  await shot(page, "1-open");
  const before = await top(page);
  const box = (await page.getByTestId("terminal").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 3);

  // A mouse wheel: one notch up shows earlier history.
  await page.mouse.wheel(0, -100);
  await expect.poll(() => top(page), { message: "a wheel notch up shows earlier history" }).toBeLessThan(before);
  const notched = await top(page);
  await shot(page, "2-wheel");

  // A trackpad: a swipe of small pixel deltas, an event a frame.
  for (const dy of [-4, -8, -14, -20, -24, -24, -20, -16, -12, -9, -6, -4, -3, -2, -1]) {
    await page.mouse.wheel(0, dy);
    await page.waitForTimeout(16);
  }
  await expect.poll(() => top(page), { message: "a trackpad swipe up shows earlier history" }).toBeLessThan(notched);
  await shot(page, "3-trackpad");
  // And back down: a swipe the other way.
  const swiped = await top(page);
  for (const dy of [4, 8, 14, 20, 24, 24, 20, 16, 12, 9, 6, 4]) {
    await page.mouse.wheel(0, dy);
    await page.waitForTimeout(16);
  }
  await expect.poll(() => top(page), { message: "a trackpad swipe down shows later history" }).toBeGreaterThan(swiped);
  await toBottom(page);
});

test("pty: a click on Claude Code's “Jump to bottom” reaches the bottom", async ({ page }) => {
  await open(page);
  const box = (await page.getByTestId("terminal").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 3);
  for (let i = 0; i < 6; i++) await page.mouse.wheel(0, -100);
  await expect.poll(async () => (await rows(page)).some((r) => r.includes("Jump to bottom")), { message: "scrolled up, Claude Code offers to jump to the bottom" }).toBe(true);
  expect(await bottom(page)).toBeLessThan(LINES);
  await shot(page, "1-scrolled-up");

  // Where Claude Code drew it: its row and column, in the view's cells.
  const text = await rows(page);
  const row = text.findIndex((r) => r.includes("Jump to bottom"));
  const col = text[row].indexOf("Jump to bottom") + 2;
  const screen = (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
  const client = viewClient()!;
  const cell = { w: screen.width / client.cols, h: screen.height / client.rows };
  await page.mouse.click(screen.x + (col + 0.5) * cell.w, screen.y + (row + 0.5) * cell.h);

  await expect.poll(() => bottom(page), { message: "the click took Claude Code to the bottom" }).toBe(LINES);
  await expect.poll(async () => (await rows(page)).some((r) => r.includes("Jump to bottom"))).toBe(false);
  await shot(page, "2-jumped");
});

/* ⌘C as in the app: through its clipboard (the shell's, `window.clipboard` here), never the webview's, which WKWebView
   has only through an Edit menu that disables Copy with no text selected in the page. ⌘V takes the webview's own paste
   where one comes with text, else reads the app's clipboard. */
test("pty: ⌘V pastes the app's clipboard into Claude Code as a bracketed paste, and ⌘C copies an Option-drag selection onto it", async ({ page }) => {
  const input: string[] = [];
  page.on("websocket", (socket) => socket.on("framesent", ({ payload }) => {
    const frame = JSON.parse(String(payload));
    if (frame.type === "input") input.push(frame.data);
  }));
  await open(page);
  type Clipboard = Window & { clipboard: string; copied: string[]; webview: string[] };
  // What the webview's own copy would do: none of it, the key taken for the app's clipboard.
  await page.evaluate(() => {
    const w = window as unknown as Clipboard;
    w.webview = [];
    document.addEventListener("copy", () => w.webview.push("copy"), true);
  });
  const text = "pasted from the clipboard ✓";
  await page.evaluate((text) => void ((window as unknown as Clipboard).clipboard = text), text);
  await page.locator('[data-testid="terminal"] textarea').focus();
  input.length = 0;
  await page.keyboard.press("Meta+v");
  await expect.poll(() => input.join(""), { message: "pasted, bracketed" }).toBe(`\x1b[200~${text}\x1b[201~`);
  await expect.poll(() => rig.screen(), { message: "in Claude Code's input box" }).toContain(`❯ ${text}`);

  // Option-drag over a line of the conversation (xterm.js's own selection: Claude Code has the mouse), then ⌘C.
  const before = await rows(page);
  const row = before.findIndex((r) => r.includes(`history line ${LINES}`));
  const col = before[row].indexOf("history line");
  const selected = `history line ${LINES}`;
  const screen = (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
  const cell = { w: screen.width / viewClient()!.cols, h: screen.height / viewClient()!.rows };
  await page.keyboard.down("Alt");
  await page.mouse.move(screen.x + (col + 0.1) * cell.w, screen.y + (row + 0.5) * cell.h);
  await page.mouse.down();
  await page.mouse.move(screen.x + (col + selected.length + 0.1) * cell.w, screen.y + (row + 0.5) * cell.h, { steps: 8 });
  await page.mouse.up();
  await page.keyboard.up("Alt");
  // The pointer's moves are Claude Code's (it asked for them); from here on, only the keys.
  const sent = input.length;
  await page.keyboard.press("Meta+c");
  await expect.poll(() => page.evaluate(() => (window as unknown as Clipboard).copied), { message: "the selection, on the app's clipboard" }).toEqual([selected]);
  expect(await page.evaluate(() => (window as unknown as Clipboard).clipboard)).toBe(selected);
  // ⌘V again: the copy pasted.
  await page.keyboard.press("Meta+v");
  await expect.poll(() => input.slice(sent).join("")).toBe(`\x1b[200~${selected}\x1b[201~`);
  await page.waitForTimeout(200);
  // Nothing went to the pane but the pastes: no Enter, no interrupt, no key of ⌘C's or ⌘V's; and no copy through the webview's clipboard.
  expect(input.slice(sent)).toEqual([`\x1b[200~${selected}\x1b[201~`]);
  expect(input.filter((d) => !d.startsWith("\x1b[<"))).toEqual([`\x1b[200~${text}\x1b[201~`, `\x1b[200~${selected}\x1b[201~`]);
  expect(await page.evaluate(() => (window as unknown as Clipboard).webview)).toEqual([]);
  await clearInput(page);
});

/* The issue's gesture (#217): a plain drag over Claude Code's text (its own selection: it has the mouse), then ⌘C. Claude
   Code copies the selection on release, through tmux (OSC 52) onto the app's clipboard; ⌘C, with nothing selected in
   xterm.js, has tmux copy its selection (none here) and sends nothing to Claude Code. */
test("pty: a plain drag over Claude Code's text, then ⌘C, copies the highlighted text onto the app's clipboard", async ({ page }) => {
  const input: string[] = [];
  page.on("websocket", (socket) => socket.on("framesent", ({ payload }) => {
    const frame = JSON.parse(String(payload));
    if (frame.type === "input") input.push(frame.data);
  }));
  await open(page);
  const text = await rows(page);
  const row = text.findIndex((r) => r.includes(`history line ${LINES}`));
  const col = text[row].indexOf("history line");
  const selected = `history line ${LINES}`;
  const screen = (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
  const cell = { w: screen.width / viewClient()!.cols, h: screen.height / viewClient()!.rows };
  await page.mouse.move(screen.x + (col + 0.5) * cell.w, screen.y + (row + 0.5) * cell.h);
  await page.mouse.down();
  await page.mouse.move(screen.x + (col + selected.length - 0.5) * cell.w, screen.y + (row + 0.5) * cell.h, { steps: 8 });
  await page.mouse.up();
  const sent = input.length;
  await page.keyboard.press("Meta+c");
  await expect.poll(() => page.evaluate(() => (window as unknown as { copied: string[] }).copied), { message: "the drag's text, on the app's clipboard" }).toContain(selected);
  // ⌘C's only input: tmux's copy key (bound in the view's table, never Claude Code's).
  await page.waitForTimeout(200);
  expect(input.slice(sent)).toEqual(["\x1b[24;8~"]);
  expect(rig.screen()).not.toContain("24;8~");
});

test("pty: typing lands in Claude Code's input box", async ({ page }) => {
  await open(page);
  await page.locator('[data-testid="terminal"] textarea').focus();
  const typed = "hello from the pty view";
  await page.keyboard.type(typed, { delay: 15 });
  await expect
    .poll(async () => (await rows(page)).some((r) => r.includes(`❯ ${typed}`)), { message: "drawn in the input box" })
    .toBe(true)
    .catch(async (e) => {
      throw new Error(`${e}\nthe view:\n${(await rows(page)).join("\n")}\nthe pane:\n${rig.screen()}`);
    });
  expect(rig.screen()).toContain(`❯ ${typed}`);
  await shot(page, "typed");
  // Taken out again, never submitted.
  await clearInput(page);
});

/* Claude Code's input box emptied with Backspace, one key a frame or so: a
   burst of them reads to it as a paste, which deletes nothing. Pressed
   again for whatever is left, until its own screen shows the box empty. */
async function clearInput(page: Page, screen = () => rig.screen()): Promise<void> {
  // The input box: the last ❯ on screen (one higher up is a message already sent).
  const left = () => [...screen().matchAll(/❯ (\S.*)$/gm)].at(-1)?.[1].trimEnd().length ?? 0;
  for (let round = 0; round < 5 && left() > 0; round++) {
    for (let i = left(); i > 0; i--) {
      await page.keyboard.press("Backspace");
      await page.waitForTimeout(20);
    }
    await expect.poll(left, { timeout: 1000 }).toBe(0).catch(() => {});
  }
  expect(left(), `the input box left: ${screen()}`).toBe(0);
}

test("pty: shared with a native tmux client of another size, the pane behaves as between two tmux clients", async ({ page }) => {
  await open(page);
  const view = viewClient()!;
  const viewSize = `${view.cols}x${view.rows}`;
  await expect.poll(windowSize, { message: "the view alone: the window is the view's size" }).toBe(viewSize);
  await expect.poll(() => ruleWidth(page)).toBe(view.cols);
  await page.locator('[data-testid="terminal"] textarea').focus();

  // A person's terminal, 100x30, on the agent's own session (its status line on: 29 rows for the window).
  const native = await nativeClient(rig, 100, 30);
  try {
    await expect.poll(windowSize, { message: "the terminal attached last has the window" }).toBe("100x29");
    // Claude Code redraws at the terminal's size; the view shows that, with tmux's fill around it.
    await expect.poll(() => ruleWidth(page)).toBe(100);
    await shot(page, "1-terminal-has-it");
    const clients = rig.tmux("list-clients", "-F", "#{session_name} #{client_width}x#{client_height}");
    expect(clients).toContain(`${view.session} ${viewSize}`);
    expect(clients).toContain("agents 100x30");
    // Each client keeps its own session's status line: none in the view, tmux's in the terminal.
    expect(rig.tmux("show-options", "-v", "-t", `=${view.session}:`, "status")).toBe("off");
    expect(rig.tmux("show-options", "-gv", "status")).toBe("on");

    // Typing in a client gives it the window.
    await page.keyboard.type("v");
    await expect.poll(windowSize, { message: "typed in the view" }).toBe(viewSize);
    await expect.poll(() => ruleWidth(page)).toBe(view.cols);
    await shot(page, "2-view-has-it");
    native.type("t");
    await expect.poll(windowSize, { message: "typed in the terminal" }).toBe("100x29");
    await page.keyboard.type("v");
    await expect.poll(windowSize, { message: "typed in the view again" }).toBe(viewSize);
    native.type("t");
    await expect.poll(windowSize).toBe("100x29");
    // Both clients' keys reached Claude Code's one input box, in order.
    await expect.poll(() => rig.screen()).toContain("❯ vtvt");
  } finally {
    await native.close();
  }
  // The terminal goes: the window is the view's again.
  await expect.poll(windowSize, { message: "the terminal detached" }).toBe(viewSize);
  await expect.poll(() => ruleWidth(page)).toBe(view.cols);
  await shot(page, "3-terminal-gone");
  await clearInput(page);
});

test("pty: on a plain pane the wheel scrolls tmux's history, and a double-click copies a word onto the app's clipboard (OSC 52)", async ({ page }) => {
  await page.goto(`/shots/preview.html?scene=pty&renderer=dom&${rig.queryFor("agents:plain")}`);
  await page.waitForSelector("html[data-ready]");
  await expect(page.getByTestId("terminal")).toHaveAttribute("data-view", "live");
  await expect.poll(async () => (await rows(page)).some((r) => r.includes("copymeplease"))).toBe(true);
  const plainMode = () => rig.tmux("display-message", "-p", "-t", "agents:plain", "#{pane_in_mode}");

  // Double-click the word: tmux's stock binding selects it in copy mode and copies it, which comes to the app as OSC 52.
  const text = await rows(page);
  const row = text.findIndex((r) => r.includes("copymeplease"));
  const screen = (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
  const client = viewClient()!;
  const cell = { w: screen.width / client.cols, h: screen.height / client.rows };
  await page.mouse.dblclick(screen.x + (text[row].indexOf("copymeplease") + 3.5) * cell.w, screen.y + (row + 0.5) * cell.h);
  await expect.poll(() => page.evaluate(() => (window as unknown as { copied: string[] }).copied), { message: "the word copied, through the app's clipboard" }).toEqual(["copymeplease"]);
  await expect.poll(plainMode).toBe("0");

  // The wheel over a pane that asks for no mouse: tmux's copy mode, scrolled back.
  const box = (await page.getByTestId("terminal").boundingBox())!;
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  // The earliest of `seq`'s numbers on screen (rows that are a number alone).
  const earliest = async () => Math.min(...(await rows(page)).map((r) => r.trim()).filter((r) => /^\d+$/.test(r)).map(Number));
  const before = await earliest();
  for (let i = 0; i < 3; i++) await page.mouse.wheel(0, -100);
  await expect.poll(plainMode, { message: "the wheel put the pane in copy mode" }).toBe("1");
  await expect.poll(earliest, { message: "and scrolled tmux's history back" }).toBeLessThan(before);
  await shot(page, "plain-scrolled");
  rig.tmux("send-keys", "-t", "agents:plain", "-X", "cancel");
});

/* The smooth-scroll budget, as the control-mode terminal was held to (its
   e2e/smooth.e2e.ts, gone with it): from a redraw's last byte in to its
   paint, for nineteen redraws in twenty, the native terminal's wait for its
   redraw (6 ms at most) and two frames, one for a busy page; and never a
   tenth of a second for any (a redraw drawing many glyphs new to WebGL's
   atlas takes three frames). A redraw is tmux's: the client brackets each
   in a synchronized update (DEC 2026), its end the redraw's last byte. */
const PAINT_BUDGET_MS = 40;
const PAINT_MAX_MS = 100;

interface Probe {
  wheels: { t: number }[];
  sends: { t: number; text: string }[];
  recvs: { t: number }[];
  ends: number[];
  renders: { t: number }[];
}
type ProbedWin = Window & {
  probe: Probe;
  swipe(el: Element, deltas: number[], perFrame: number): Promise<void>;
  swipeDeltas(peak: number, decay: number, up: boolean): number[];
};

/* A trackpad flick (the finger's push, then momentum, two wheel events a
   frame: a 120 Hz trackpad's) over the terminal, until the client has
   sent nothing for half a second; then each redraw's wait for its paint. */
async function flick(page: Page, up: boolean) {
  const now = () => page.evaluate(() => performance.timeOrigin + performance.now());
  const from = await now();
  await page.evaluate((up) => {
    const w = window as unknown as ProbedWin;
    return w.swipe(document.querySelector('[data-testid="terminal"] .xterm-screen')!, w.swipeDeltas(40, 0.975, up), 2);
  }, up);
  await expect
    .poll(() => page.evaluate(() => performance.timeOrigin + performance.now() - ((window as unknown as ProbedWin).probe.recvs.at(-1)?.t ?? 0)), { intervals: [250], timeout: 20_000 })
    .toBeGreaterThan(500);
  // A last paint's frame.
  await page.waitForTimeout(100);
  const probe = await page.evaluate(() => {
    const p = (window as unknown as ProbedWin).probe;
    // When each wheel report went up the bridge's socket.
    const reports = p.sends.filter((s) => s.text.includes("[<")).map((s) => s.t);
    return { wheels: p.wheels.map((w) => w.t), ends: p.ends, renders: p.renders.map((r) => r.t), reports, recvs: p.recvs.map((r) => r.t) };
  });
  const ends = probe.ends.filter((t) => t >= from);
  const renders = probe.renders.filter((t) => t >= from);
  const paintOf = (t: number) => renders.find((r) => r >= t) ?? Infinity;
  // Each redraw's wait for a paint: the first render at or after its last byte.
  const waits = ends.map((t) => paintOf(t) - t);
  const sorted = [...waits].sort((a, b) => a - b);
  const finalPaint = ends.length ? paintOf(ends[ends.length - 1]) : Infinity;
  return {
    wheelEvents: probe.wheels.filter((t) => t >= from).length,
    reports: probe.reports.filter((t) => t >= from).length,
    redraws: ends.length,
    renders: renders.length,
    paintWaitP95: sorted[Math.floor(sorted.length * 0.95)] ?? Infinity,
    paintWaitMax: Math.max(0, ...waits),
    paintWaitMean: waits.reduce((a, b) => a + b, 0) / Math.max(1, waits.length),
    // Not held to a budget (a live Claude Code's own pace is in it): from the last wheel event, and the last report, to the last redraw painted.
    settle: finalPaint - Math.max(...probe.wheels.filter((t) => t >= from)),
    reportSettle: finalPaint - Math.max(...probe.reports.filter((t) => t >= from)),
    paintWaits: waits.map((w, i) => [Math.round(ends[i] - from), Math.round(w)]),
    reportTimes: probe.reports.filter((t) => t >= from).map((t) => Math.round(t - from)),
    recvTimes: probe.recvs.filter((t) => t >= from).map((t) => Math.round(t - from)),
    wheelSpan: [Math.round(Math.min(...probe.wheels.filter((t) => t >= from)) - from), Math.round(Math.max(...probe.wheels.filter((t) => t >= from)) - from)],
  };
}

test("pty smooth: a trackpad swipe over a live Claude Code paints every redraw within a frame or two of its last byte", async ({ page }) => {
  // WebGL, as in WKWebView (the DOM renderer is the slow fallback), with the terminal and the bridge's socket timed.
  await page.goto(`/shots/preview.html?scene=pty&probe=1&${rig.query}`);
  await page.waitForSelector("html[data-ready]");
  const terminal = page.getByTestId("terminal");
  await expect(terminal).toHaveAttribute("data-view", "live");
  await expect(terminal).toHaveAttribute("data-renderer", "webgl");
  await expect.poll(() => viewClient() !== null).toBe(true);
  await expect.poll(() => rig.screen()).toContain(`history line ${LINES}`);
  // The client drew Claude Code (its first bytes in, painted), the mouse turned on with them.
  await expect.poll(() => page.evaluate(() => Math.min((window as unknown as ProbedWin).probe.recvs.length, (window as unknown as ProbedWin).probe.renders.length)), { timeout: 15_000, message: "the view's client drew" }).toBeGreaterThan(0);
  // A flick first, not timed: a Claude Code just started answers its first wheel reports slowly (cold).
  await flick(page, true);
  await backToFoot(page);

  // Flicks up and down until enough redraws are in to judge by: Claude Code paces its own redraws (it may
  // answer a flick's reports with a handful), which is its business; painting each as it comes is the app's.
  const flicks: Awaited<ReturnType<typeof flick>>[] = [];
  for (let i = 0; i < 6 && flicks.reduce((n, f) => n + f.redraws, 0) < 40; i++) flicks.push(await flick(page, i % 2 === 0));
  await terminal.screenshot({ path: test.info().outputPath("webkit-swiped.png") });
  const waits = flicks.flatMap((f) => f.paintWaits.map(([, w]) => w)).sort((a, b) => a - b);
  const stats = {
    flicks: flicks.length,
    wheelEvents: flicks.reduce((n, f) => n + f.wheelEvents, 0),
    reports: flicks.reduce((n, f) => n + f.reports, 0),
    redraws: waits.length,
    paintWaitP95: waits[Math.floor(waits.length * 0.95)] ?? Infinity,
    paintWaitMax: Math.max(0, ...waits),
  };
  await test.info().attach("smooth", { body: JSON.stringify({ stats, flicks }, null, 1), contentType: "application/json" });
  console.log("pty smooth", JSON.stringify(stats), JSON.stringify(flicks.map(({ paintWaits: _, reportTimes: __, recvTimes: ___, ...f }) => f)));

  expect(stats.wheelEvents).toBeGreaterThan(100);
  expect(stats.reports, "the wheel reached Claude Code as mouse reports").toBeGreaterThan(20);
  expect(stats.redraws, "Claude Code's redraws came back through tmux").toBeGreaterThan(20);
  expect(stats.paintWaitP95, "nineteen redraws in twenty are painted within two frames of their last byte").toBeLessThanOrEqual(PAINT_BUDGET_MS);
  expect(stats.paintWaitMax, "no redraw waits a tenth of a second to be painted").toBeLessThanOrEqual(PAINT_MAX_MS);

  // Back to the foot of the conversation, for the tests after.
  await backToFoot(page);
});

/* The typing budget: from a key going up the bridge's socket to its echo
   painted in Claude Code's input box, never a stall a person sees (the
   display held, then what was typed drawn all at once). Claude Code's own
   pace is in it (it redraws its box for each key), so not a frame or two. */
const TYPING_MAX_MS = 150;
/* How the client's bytes come down: a redraw a message (pty_view.rs
   `forward`), at most one message in twenty leaving a synchronized update
   open for a later one to end, and never more than `MESSAGES_MAX` in a tenth
   of a second. tmux writes a Claude Code keystroke's redraw (the whole pane)
   a kilobyte a pty read, and while Claude Code works, bursts of a few bytes
   a read, hundreds in a tenth of a second: in the app each message is a
   script its main thread runs, the thread every key typed goes through. */
const SPLIT_MAX = 0.05;
const MESSAGES_MAX = 30;

/* Words and spaces, typed a key at a time at a person's pace: nothing Claude
   Code takes for a command (/), a file (@), a shell (!) or help (?). */
const TYPING = "the quick brown fox jumps over the lazy dog and keeps on running";

type TypingWin = ProbedWin & {
  prompts: { t: number; text: string }[];
  writes: { t: number; open: boolean; text: string }[];
};

interface Typed {
  /* Each key's wait for its echo painted, and for the message that brought it in. */
  samples: { key: number; latency: number; read: number | null }[];
  /* When each of the client's messages came, and whether it left a synchronized update open. */
  writes: { t: number; open: boolean }[];
}

/* `rounds` times, TYPING typed into the view's Claude Code (its pane's text: `screen`) a key at a time at a person's
   pace, and rubbed out. */
async function typeAtPace(page: Page, screen: () => string, rounds: number): Promise<Typed> {
  await expect.poll(() => page.evaluate(() => (window as unknown as ProbedWin).probe.renders.length), { timeout: 15_000, message: "the view's client drew" }).toBeGreaterThan(0);
  // Each paint, with what it painted of Claude Code's input box (the last row its prompt's ❯ starts: one higher up
  // is a message already sent); and each of the client's messages as xterm.js parsed it, with what the box held then.
  await page.evaluate(() => {
    const w = window as unknown as TypingWin;
    w.prompts = [];
    w.writes = [];
    const term = w.probe.terms.at(-1)!;
    const prompt = () => {
      const buffer = term.buffer.active;
      for (let y = term.rows - 1; y >= 0; y--) {
        const text = buffer.getLine(buffer.viewportY + y)?.translateToString(true).replace(/ /g, " ") ?? "";
        if (text.includes("❯ ") || text.trimEnd().endsWith("❯")) return text.slice(text.indexOf("❯") + 2);
      }
      return "";
    };
    term.onRender(() => w.prompts.push({ t: performance.timeOrigin + performance.now(), text: prompt() }));
    const write = term.write.bind(term);
    term.write = (data: string | Uint8Array, done?: () => void) => {
      const t = performance.timeOrigin + performance.now();
      write(data, () => {
        w.writes.push({ t, open: term.modes.synchronizedOutputMode, text: prompt() });
        done?.();
      });
    };
  });
  await page.locator('[data-testid="terminal"] textarea').focus();

  const typed: Typed = { samples: [], writes: [] };
  for (let round = 0; round < rounds; round++) {
    const from = await page.evaluate(() => performance.timeOrigin + performance.now());
    // A person's pace: 60 to 180 ms between keys, the same each run.
    for (let i = 0; i < TYPING.length; i++) {
      await page.keyboard.type(TYPING[i]);
      await page.waitForTimeout(60 + ((i * 37 + round * 53) % 121));
    }
    await expect.poll(() => [...screen().matchAll(/❯ (.*)$/gm)].at(-1)?.[1].trimEnd(), { message: "all of it in Claude Code's input box" }).toBe(TYPING);
    // A last paint's frame.
    await page.waitForTimeout(300);
    const { sends, prompts, writes } = await page.evaluate((from) => {
      const w = window as unknown as TypingWin;
      const sends = w.probe.sends
        .filter((s) => s.t >= from)
        .map((s) => ({ t: s.t, frame: JSON.parse(s.text) as { type: string; data?: string } }))
        .filter((s) => s.frame.type === "input" && !s.frame.data!.startsWith("\x1b"))
        .map((s) => s.t);
      return { sends, prompts: w.prompts.filter((p) => p.t >= from), writes: w.writes.filter((p) => p.t >= from) };
    }, from);
    expect(sends, "each key went up the socket on its own").toHaveLength(TYPING.length);
    // Each key's wait: from its send to the first paint showing what was typed up to it; and to the message that
    // brought its echo in (the bridge's, tmux's and Claude Code's part).
    for (let i = 0; i < TYPING.length; i++) {
      const want = TYPING.slice(0, i + 1);
      const painted = prompts.find((p) => p.t >= sends[i] && p.text.startsWith(want));
      const read = writes.find((p) => p.t >= sends[i] && p.text.startsWith(want));
      typed.samples.push({ key: round * TYPING.length + i, latency: painted ? painted.t - sends[i] : Infinity, read: read ? Math.round(read.t - sends[i]) : null });
    }
    typed.writes.push(...writes.map(({ t, open }) => ({ t, open })));
    await clearInput(page, screen);
  }
  return typed;
}

/* What `typeAtPace` saw: the keys' waits, and how the client's messages came. */
function typingStats(typed: Typed) {
  const sorted = typed.samples.map((s) => s.latency).sort((a, b) => a - b);
  const times = typed.writes.map((w) => w.t).sort((a, b) => a - b);
  // The most messages in any tenth of a second.
  let busiest = 0;
  for (let i = 0, j = 0; i < times.length; i++) {
    while (times[i] - times[j] >= 100) j++;
    busiest = Math.max(busiest, i - j + 1);
  }
  return {
    keys: sorted.length,
    p50: Math.round(sorted[Math.floor(sorted.length / 2)]),
    p95: Math.round(sorted[Math.floor(sorted.length * 0.95)]),
    max: Math.round(sorted.at(-1)!),
    messages: times.length,
    split: Math.round((typed.writes.filter((w) => w.open).length / Math.max(1, times.length)) * 1000) / 1000,
    busiest,
    over: typed.samples.filter((s) => s.latency > TYPING_MAX_MS).map((s) => ({ ...s, latency: Math.round(s.latency) })),
  };
}

function expectTyping(what: string, stats: ReturnType<typeof typingStats>): void {
  const said = `${what}: ${JSON.stringify(stats)}`;
  expect(stats.max, `no key waits more than ${TYPING_MAX_MS} ms to be painted (${said})`).toBeLessThanOrEqual(TYPING_MAX_MS);
  expect(stats.split, `a redraw comes down in one message, at most one in twenty left open for the next (${said})`).toBeLessThanOrEqual(SPLIT_MAX);
  expect(stats.busiest, `never more than ${MESSAGES_MAX} messages in a tenth of a second (${said})`).toBeLessThanOrEqual(MESSAGES_MAX);
}

test("pty typing: each key typed into a live Claude Code, idle or at work, is painted well within a stall, its redraw one message", async ({ page }) => {
  test.setTimeout(240_000);
  // WebGL, as in WKWebView, with the terminal and the bridge's socket timed.
  await page.goto(`/shots/preview.html?scene=pty&probe=1&${rig.query}`);
  await page.waitForSelector("html[data-ready]");
  const terminal = page.getByTestId("terminal");
  await expect(terminal).toHaveAttribute("data-view", "live");
  await expect(terminal).toHaveAttribute("data-renderer", "webgl");
  await expect.poll(() => viewClient() !== null).toBe(true);
  await expect.poll(() => rig.screen()).toContain(`history line ${LINES}`);
  const idle = typingStats(await typeAtPace(page, () => rig.screen(), 2));

  // A Claude Code at work: asked something, its answer streaming in (the stand-in API's), typed into meanwhile.
  const work = "agents:work";
  const query = await rig.working("work");
  rig.tmux("select-window", "-t", work);
  let busy: ReturnType<typeof typingStats>;
  try {
    await page.goto(`/shots/preview.html?scene=pty&probe=1&${query}`);
    await page.waitForSelector("html[data-ready]");
    await expect(terminal).toHaveAttribute("data-view", "live");
    const screen = () => rig.tmux("capture-pane", "-p", "-t", work).replace(/ /g, " ");
    await page.locator('[data-testid="terminal"] textarea').focus();
    await page.keyboard.type("go", { delay: 100 });
    await page.keyboard.press("Enter");
    await expect.poll(screen, { message: "Claude Code at work, its answer streaming in", timeout: 30_000 }).toMatch(/word2\d/);
    busy = typingStats(await typeAtPace(page, screen, 2));
    expect(screen(), "still at work: the answer streamed all along").toMatch(/word\d+/);
  } finally {
    await page.goto("about:blank");
    rig.tmux("select-window", "-t", rig.target);
    rig.tmux("kill-window", "-t", work);
  }
  await test.info().attach("typing", { body: JSON.stringify({ idle, busy }, null, 1), contentType: "application/json" });
  console.log("pty typing", JSON.stringify({ idle, busy }));
  expectTyping("Claude Code idle", idle);
  expectTyping("Claude Code at work", busy);
});

/* Flicks down until Claude Code shows the foot of the conversation. */
async function backToFoot(page: Page): Promise<void> {
  for (let i = 0; i < 6 && !rig.screen().includes(`history line ${LINES}`); i++) await flick(page, false);
  await expect.poll(() => rig.screen()).toContain(`history line ${LINES}`);
}

test("pty: shared with a native terminal, the view focused or clicked has the window with nothing typed, and typing in the terminal takes it back", async ({ page }) => {
  // The plain pane (`cat`, echoing what it is sent) in the person's terminal too.
  rig.tmux("select-window", "-t", "agents:plain");
  const plainSize = () => rig.tmux("display-message", "-p", "-t", "agents:plain", "#{window_width}x#{window_height}");
  const pane = () => rig.tmux("capture-pane", "-p", "-t", "agents:plain").trimEnd();
  const native = await nativeClient(rig, 100, 30);
  try {
    await expect.poll(plainSize).toBe("100x29");
    await page.goto(`/shots/preview.html?scene=pty&renderer=dom&${rig.queryFor("agents:plain")}`);
    await page.waitForSelector("html[data-ready]");
    const terminal = page.getByTestId("terminal");
    await expect(terminal).toHaveAttribute("data-view", "live");
    // This page's client alone (the last test's has gone).
    await expect.poll(() => rig.tmux("list-clients", "-F", "#{session_name}").split("\n").filter((s) => s.startsWith("brindle-view-")).length).toBe(1);
    const view = viewClient()!;
    const viewSize = `${view.cols}x${view.rows}`;
    // Attached last, the view has the window; typing in the terminal gives it back.
    native.type("t");
    await expect.poll(plainSize, { message: "typed in the terminal" }).toBe("100x29");
    const lastLine = () => pane().split("\n").at(-1);
    await expect.poll(lastLine).toBe("t");

    // The view takes focus: the window is the view's, though nothing was typed in it.
    const textarea = page.locator('[data-testid="terminal"] textarea');
    await textarea.blur();
    await textarea.focus();
    await expect.poll(plainSize, { message: "the view focused" }).toBe(viewSize);
    native.type("u");
    await expect.poll(plainSize, { message: "typed in the terminal again" }).toBe("100x29");

    // Clicked, focused already (no focus event): the view's again.
    await terminal.click();
    await expect.poll(plainSize, { message: "the view clicked" }).toBe(viewSize);
    await shot(page, "focus-view-has-it");
    native.type("v");
    await expect.poll(plainSize, { message: "and the terminal's once typed in" }).toBe("100x29");

    // The pane (which echoes all it is sent) got only what the terminal typed: the view's focus and click typed nothing.
    await page.waitForTimeout(300);
    expect(lastLine()).toBe("tuv");
  } finally {
    await native.close();
    rig.tmux("select-window", "-t", "agents:a1");
  }
});

/* brindle's CLI sidebar beside Claude Code, as `brindle` started from a terminal splits it: a
   pane 30 wide marked @brindle_sidebar (at the left, `-b`, or the right), held at 30 by a
   window-resized hook. The rig's own window, as brindle sets its own. */
function splitSidebar(at: "left" | "right", target = rig.target): string {
  const pane = rig.tmux("split-window", "-d", "-h", ...(at === "left" ? ["-b"] : []), "-l", "30", "-P", "-F", "#{pane_id}", "-t", target, "sh -c 'echo BRINDLE-SIDEBAR; cat'");
  rig.tmux("set-option", "-p", "-t", pane, "@brindle_sidebar", "e6af0dc0");
  rig.tmux("set-hook", "-w", "-t", target, "window-resized", `resize-pane -t ${pane} -x 30`);
  return pane;
}

/* Whether what is on screen at (x, y) is xterm.js's (not the box around it, or the mask over it). */
const onXterm = (page: Page, x: number, y: number) => page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest(".xterm"), { x, y });

test("pty: brindle's CLI sidebar in the window is cropped off: Claude Code's pane fills the view, and clicks land on its cells", async ({ page }) => {
  const agentPane = rig.tmux("display-message", "-p", "-t", rig.target, "#{pane_id}");
  const agentWidth = () => Number(rig.tmux("display-message", "-p", "-t", agentPane, "#{pane_width}"));
  let sidebar = splitSidebar("left");
  try {
    await open(page);
    const terminal = page.getByTestId("terminal");
    await expect(terminal).toHaveAttribute("data-crop", "31,31");
    const box = (await page.locator('[data-testid="terminal"] > div').first().boundingBox())!;
    const cell = async () => {
      const screen = (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
      return { screen, w: screen.width / viewClient()!.cols, h: screen.height / viewClient()!.rows };
    };
    const { w } = await cell();
    const shown = Math.floor(box.width / w);
    // The client is the view's width and the sidebar's 31 columns; Claude Code's pane is the view's width.
    await expect.poll(() => viewClient()!.cols, { message: "the client: the view and the sidebar's columns" }).toBe(shown + 31);
    await expect.poll(agentWidth, { message: "Claude Code's pane is the view's width" }).toBe(shown);
    await expect.poll(() => ruleWidth(page), { message: "its input box's rules the view's width" }).toBe(shown);
    // The screen shifted left by the sidebar and its border: Claude Code's first column at the box's left edge.
    expect(Math.abs((await cell()).screen.x + 31 * w - box.x)).toBeLessThan(1);
    // The sidebar's columns are drawn, but out of the box: on screen at the box's left edge is Claude Code's pane.
    expect((await rows(page)).some((r) => r.startsWith("BRINDLE-SIDEBAR"))).toBe(true);
    const xtermAt = (x: number, y: number) => onXterm(page, x, y);
    expect(await xtermAt(box.x - 2, box.y + 10), "the cropped columns are clipped: nothing of xterm's left of the box").toBe(false);
    expect(await xtermAt(box.x + 2, box.y + 10)).toBe(true);
    await shot(page, "1-sidebar-cropped");

    // A click where Claude Code drew "Jump to bottom", reckoned from the box (where the person sees it).
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 3);
    for (let i = 0; i < 6; i++) await page.mouse.wheel(0, -100);
    await expect.poll(async () => (await rows(page)).some((r) => r.includes("Jump to bottom")), { message: "the wheel scrolled Claude Code up" }).toBe(true);
    const text = await rows(page);
    const row = text.findIndex((r) => r.includes("Jump to bottom"));
    const col = text[row].indexOf("Jump to bottom") + 2 - 31;
    const { h } = await cell();
    await page.mouse.click(box.x + (col + 0.5) * w, (await cell()).screen.y + (row + 0.5) * h);
    await expect.poll(() => bottom(page), { message: "the click took Claude Code to the bottom" }).toBe(LINES);

    // Option-drag over three of Claude Code's rows (xterm.js's own selection), then ⌘C: only what is shown is copied,
    // never the sidebar's columns or its border, which xterm.js's whole rows would take.
    const before = await rows(page);
    const first = before.findIndex((r) => r.includes(`history line ${LINES - 4}`));
    const at = async (col: number, row: number) => ({ x: box.x + (col + 0.5) * w, y: (await cell()).screen.y + (row + 0.5) * h });
    const from = await at(4, first);
    const to = await at(10, first + 2);
    await page.keyboard.down("Alt");
    await page.mouse.move(from.x, from.y);
    await page.mouse.down();
    await page.mouse.move(to.x, to.y, { steps: 8 });
    await page.mouse.up();
    await page.keyboard.up("Alt");
    const copied = await page.evaluate(() => {
      const data = new DataTransfer();
      document.querySelector('[data-testid="terminal"] textarea')!.dispatchEvent(new ClipboardEvent("copy", { clipboardData: data, bubbles: true, cancelable: true }));
      return data.getData("text/plain");
    });
    const seen = (r: number) => before[r].slice(31, 31 + shown);
    const lines = copied.split("\n");
    expect(lines, `copied: ${JSON.stringify(copied)}`).toHaveLength(3);
    expect(copied).not.toMatch(/│|BRINDLE/);
    expect(seen(first).trimEnd().endsWith(lines[0])).toBe(true);
    expect(lines[0]).toContain(`line ${LINES - 4}`);
    expect(lines[1]).toBe(seen(first + 1).trimEnd());
    expect(seen(first + 2).startsWith(lines[2])).toBe(true);
    expect(lines[2].length).toBeGreaterThan(4);

    // The sidebar closed: nothing cropped, the client the view's width.
    rig.tmux("kill-pane", "-t", sidebar);
    await expect(terminal).toHaveAttribute("data-crop", "0,0");
    await expect.poll(() => viewClient()!.cols).toBe(shown);
    await expect.poll(() => ruleWidth(page)).toBe(shown);
    // One at the right: cropped there, the screen not shifted.
    sidebar = splitSidebar("right");
    await expect(terminal).toHaveAttribute("data-crop", "31,0");
    await expect.poll(() => viewClient()!.cols).toBe(shown + 31);
    await expect.poll(agentWidth).toBe(shown);
    await expect.poll(() => ruleWidth(page)).toBe(shown);
    expect(Math.abs((await cell()).screen.x - box.x)).toBeLessThan(1);
    await shot(page, "2-sidebar-right-cropped");

    // A box that leaves more than half a cell over its columns (the window made wider a pixel at a time): the next
    // column, the sidebar's border, is drawn there by xterm.js, and is neither seen nor hit.
    const viewport = page.viewportSize()!;
    const host = page.locator('[data-testid="terminal"] > div').first();
    const over = async () => {
      const b = (await host.boundingBox())!;
      return b.width - Math.floor(b.width / w) * w;
    };
    for (let px = 1; px <= Math.ceil(w) && (await over()) < 0.6 * w; px++) await page.setViewportSize({ width: viewport.width + px, height: viewport.height });
    expect(await over(), "more than half a cell over").toBeGreaterThan(0.6 * w);
    const wide = (await host.boundingBox())!;
    const fitted = Math.floor(wide.width / w);
    await expect(terminal).toHaveAttribute("data-shown", String(fitted));
    await expect.poll(agentWidth).toBe(fitted);
    const edge = { x: wide.x + wide.width - 1, y: (await cell()).screen.y + 2.5 * h };
    expect(await xtermAt(edge.x, edge.y), "the box's last fraction of a cell: masked").toBe(false);
    expect(await xtermAt(wide.x + (fitted - 0.5) * w, edge.y), "the agent's last column: not").toBe(true);
    const masked = (await page.getByTestId("terminal-mask").boundingBox())!;
    expect(Math.abs(masked.x - (wide.x + fitted * w))).toBeLessThan(1);
    expect(Math.abs(masked.x + masked.width - (wide.x + wide.width))).toBeLessThan(1);
    await page.setViewportSize(viewport);
    await expect(terminal).toHaveAttribute("data-shown", String(shown));
    await expect.poll(agentWidth).toBe(shown);

    // A narrower terminal (100x30) attaches last and has the window: Claude Code's pane is 69 wide and the sidebar
    // at 70-99, inside the view. Only Claude Code's columns are shown; the rest is masked. The view's client keeps its size.
    const native = await nativeClient(rig, 100, 30);
    try {
      await expect.poll(windowSize).toBe("100x29");
      await expect(terminal).toHaveAttribute("data-shown", "69");
      await expect(page.getByTestId("terminal-mask")).toBeVisible();
      expect(viewClient()!.cols).toBe(shown + 31);
      const sidebarRow = (await rows(page)).findIndex((r) => r.includes("BRINDLE-SIDEBAR"));
      expect(sidebarRow).toBeGreaterThanOrEqual(0);
      const sidebarAt = { x: box.x + (75 + 0.5) * w, y: (await cell()).screen.y + (sidebarRow + 0.5) * h };
      expect(await xtermAt(sidebarAt.x, sidebarAt.y), "the sidebar's columns are masked").toBe(false);
      expect(await xtermAt(box.x + 30 * w, sidebarAt.y), "Claude Code's are not").toBe(true);
      await shot(page, "3-sidebar-right-narrower-terminal");
    } finally {
      await native.close();
    }
    // Released: the window is the view's again, all of it Claude Code's.
    await expect.poll(windowSize).toBe(`${shown + 31}x${viewClient()!.rows}`);
    await expect(terminal).toHaveAttribute("data-shown", String(shown));
    await expect.poll(async () => (await page.getByTestId("terminal-mask").boundingBox())!.x).toBeCloseTo(box.x + shown * w, 0);
  } finally {
    rig.tmux("set-hook", "-uw", "-t", rig.target, "window-resized");
    if (rig.tmux("list-panes", "-t", rig.target, "-F", "#{pane_id}").split("\n").includes(sidebar)) rig.tmux("kill-pane", "-t", sidebar);
  }
});

/* A line of numbers, each its own column: "0 2 4 6 8 10 13 ...", `n` long. */
function ruler(n: number): string {
  let s = "";
  while (s.length < n) s += `${s.length} `;
  return s.slice(0, n);
}

test("pty: a wider terminal's window, panned on the view's client, is cropped only of what of brindle's sidebar is on it, and follows the cursor", async ({ page }) => {
  // A plain pane (cat: its cursor where its echo leaves it) beside a sidebar at the left.
  const target = "agents:wide";
  rig.tmux("new-window", "-d", "-t", "agents:", "-n", "wide", "cat");
  const agentPane = rig.tmux("display-message", "-p", "-t", target, "#{pane_id}");
  splitSidebar("left", target);
  let native: Awaited<ReturnType<typeof nativeClient>> | null = null;
  try {
    await page.goto(`/shots/preview.html?scene=pty&renderer=dom&${rig.queryFor(target)}`);
    await page.waitForSelector("html[data-ready]");
    const terminal = page.getByTestId("terminal");
    await expect(terminal).toHaveAttribute("data-view", "live");
    await expect(terminal).toHaveAttribute("data-crop", "31,31");
    const shown = Number(await terminal.getAttribute("data-shown"));
    await expect.poll(() => viewClient()?.cols).toBe(shown + 31);
    const height = viewClient()!.rows;
    const box = (await page.locator('[data-testid="terminal"] > div').first().boundingBox())!;
    const screen = async () => (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
    const w = (await screen()).width / (shown + 31);
    // What the view shows of row `y`: the client's cells from the box's left edge on, as many as it shows.
    const seen = async (y: number) => {
      const from = Math.round((box.x - (await screen()).x) / w);
      return (await rows(page))[y].slice(from, from + shown).trimEnd();
    };
    const line = (y: number) => rig.tmux("capture-pane", "-p", "-t", agentPane, "-S", String(y), "-E", String(y));
    const cursor = () => rig.tmux("display-message", "-p", "-t", agentPane, "#{cursor_x} #{cursor_y}").split(" ").map(Number);
    const panned = () => Number(rig.tmux("list-clients", "-t", `=${viewClient()!.session}`, "-F", "#{window_offset_x}") || 0);

    // A person's terminal 60 columns wider than the view's client, and its height (its status line on), on the
    // window: it attached last and has it. tmux pans the window on the view's client only once its cursor is past it.
    rig.tmux("select-window", "-t", target);
    const wider = shown + 31 + 60;
    native = await nativeClient(rig, wider, height + 1);
    await expect.poll(() => rig.tmux("display-message", "-p", "-t", target, "#{window_width}x#{window_height}")).toBe(`${wider}x${height}`);
    const [, y] = cursor();

    // The cursor at the pane's column `shown + 10` (the window's `shown + 41`), past the client: tmux pans it to the
    // window's end, 60 columns in, past the sidebar and its border. The view shows the agent's pane from the client's
    // first column (its 29th), the cursor among them.
    rig.tmux("send-keys", "-t", agentPane, "-l", ruler(shown + 10));
    await expect.poll(panned, { message: "tmux panned the window on the view's client" }).toBe(60);
    await expect
      .poll(() => seen(y), { message: "the agent's columns from the first on the client" })
      .toBe(line(y).slice(29, 29 + shown).trimEnd())
      .catch(async (e) => {
        throw new Error(`${e}\ncrop ${await terminal.getAttribute("data-crop")}\nthe view: ${await seen(y)}\nthe pane: ${line(y)}`);
      });
    expect(cursor()[0] - 29).toBeLessThan(shown);
    expect(await onXterm(page, box.x + 2, box.y + box.height / 2)).toBe(true);
    await shot(page, "1-panned-past-the-sidebar");

    // At the pane's column `shown + 57`: the client's `shown + 28`, past the view's columns of it. The view goes on as
    // far as keeps the cursor in it: its last column.
    rig.tmux("send-keys", "-t", agentPane, "-l", ruler(shown + 57).slice(shown + 10));
    await expect.poll(() => cursor()[0]).toBe(shown + 57);
    await expect
      .poll(() => seen(y), { message: "the view went on to the cursor" })
      .toBe(line(y).slice(58, 58 + shown).trimEnd())
      .catch(async (e) => {
        throw new Error(`${e}\ncrop ${await terminal.getAttribute("data-crop")}\nthe view: ${await seen(y)}\nthe pane: ${line(y)}`);
      });
    await shot(page, "2-panned-to-the-cursor");

    // Typed in the view: it lands at the cursor, and the view has the window again, the sidebar cropped from its left.
    await page.locator('[data-testid="terminal"] textarea').focus();
    await page.keyboard.type("Z");
    // (The line rewrapped as the window narrowed: its head in the pane's history.)
    await expect.poll(() => rig.tmux("capture-pane", "-p", "-J", "-S", "-", "-t", agentPane)).toContain(`${ruler(shown + 57)}Z`);
    await expect.poll(() => rig.tmux("display-message", "-p", "-t", target, "#{window_width}")).toBe(String(shown + 31));
    await expect(terminal).toHaveAttribute("data-crop", "31,31");
    await expect.poll(panned).toBe(0);
    // A double-click on a number the view shows copies that number: the click lands on the cell under it.
    const row = cursor()[1];
    await expect.poll(async () => (await seen(row)).endsWith("Z")).toBe(true);
    const shownRow = await seen(row);
    const number = [...shownRow.matchAll(/(?<= )\d{3}(?= )/g)][1];
    expect(number, shownRow).toBeDefined();
    const col = number.index! + 1;
    await page.mouse.dblclick(box.x + (col + 0.5) * w, (await screen()).y + (row + 0.5) * ((await screen()).height / height));
    await expect.poll(() => page.evaluate(() => (window as unknown as { copied: string[] }).copied.at(-1))).toBe(number[0]);
  } finally {
    await native?.close();
    rig.tmux("select-window", "-t", rig.target);
    rig.tmux("kill-window", "-t", target);
  }
});

test("pty: in the app's environment (no locale), brindle's CLI sidebar split before the view opens is cropped off, Claude Code's glyphs drawn, and a wider terminal typed in last doesn't keep the window from the app", async ({ page }) => {
  // As `brindle` started from a terminal leaves it: the sidebar at Claude Code's left before the app opens the view.
  // The bridge runs as launchd starts the app, with no LANG (rig.ts): its tmux commands and client too.
  const agentPane = rig.tmux("display-message", "-p", "-t", rig.target, "#{pane_id}");
  const agentWidth = () => Number(rig.tmux("display-message", "-p", "-t", agentPane, "#{pane_width}"));
  const sidebar = splitSidebar("left");
  let native: Awaited<ReturnType<typeof nativeClient>> | null = null;
  try {
    await open(page);
    const terminal = page.getByTestId("terminal");
    const box = (await page.locator('[data-testid="terminal"] > div').first().boundingBox())!;
    const screen = async () => (await page.locator('[data-testid="terminal"] .xterm-screen').boundingBox())!;
    // What the app shows of each row: the client's cells from the box's left edge on, up to the mask (or the box's edge).
    const seen = async () => {
      const w = (await screen()).width / viewClient()!.cols;
      const from = Math.round((box.x - (await screen()).x) / w);
      const mask = page.getByTestId("terminal-mask");
      const to = (await mask.isVisible()) ? (await mask.boundingBox())!.x : box.x + box.width;
      return (await rows(page)).map((r) => r.slice(from, from + Math.floor((to - box.x) / w + 0.01)));
    };
    // The app shows Claude Code's pane, all its columns (it is the view's width, the window the view's: the client's
    // size), and none of the sidebar, neither its text nor its border down the view's left edge; its prompt's ❯
    // drawn as itself, not tmux's `_` for a client it takes for one without UTF-8.
    const whole = async (what: string) => {
      const shown = Number(await terminal.getAttribute("data-shown"));
      await expect
        .poll(async () => {
          const r = await seen();
          const sidebarIn = r.some((row) => row.includes("BRINDLE-SIDEBAR")) || r.every((row) => row.startsWith("│"));
          return { sidebarIn, prompt: r.some((row) => row.startsWith("❯")), agent: agentWidth(), rule: await ruleWidth(page), window: windowSize() };
        }, { message: what })
        .toEqual({ sidebarIn: false, prompt: true, agent: shown, rule: shown, window: `${viewClient()!.cols}x${viewClient()!.rows}` })
        .catch(async (e) => {
          throw new Error(`${e}\ncrop ${await terminal.getAttribute("data-crop")}, the client ${JSON.stringify(viewClient())}\n${(await seen()).join("\n")}`);
        });
      await expect(terminal).toHaveAttribute("data-crop", "31,31");
    };
    await whole("opened on the window with the sidebar, the view alone on it");
    await shot(page, "1-opened");

    // The person's terminal, wider than the view, attaches and types (and rubs it out): it has the window, 248 wide,
    // and the view's client shows it panned, Claude Code's pane wider than the view.
    native = await nativeClient(rig, 248, 43);
    native.type("t\x7f");
    await expect.poll(windowSize, { message: "typed in the terminal" }).toBe("248x42");
    // The app's window comes to the front, its keyboard focus not in the view: the person is at the app, and the
    // view has the window again, nothing typed.
    await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await whole("the app brought to the front after the terminal was typed in");
    expect(rig.screen()).toMatch(/❯ *$/m);
    await shot(page, "2-app-to-the-front");
  } finally {
    await native?.close();
    rig.tmux("set-hook", "-uw", "-t", rig.target, "window-resized");
    rig.tmux("kill-pane", "-t", sidebar);
  }
});

test("pty: closing the view takes its tmux session, and the agent's window stays", async ({ page }) => {
  await open(page);
  const { session } = viewClient()!;
  await page.goto("about:blank");
  await expect.poll(() => rig.tmux("list-sessions", "-F", "#{session_name}").includes(session), { message: "the view session went with its view" }).toBe(false);
  expect(rig.tmux("list-windows", "-t", "=agents", "-F", "#{window_name}")).toBe("a1\nplain");
  expect(rig.screen()).toContain(`history line ${LINES}`);
});
