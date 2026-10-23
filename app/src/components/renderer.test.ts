import { describe, expect, it, vi } from "vitest";
import type { Terminal as XTerm } from "@xterm/xterm";

/* A WebGL addon whose context the test loses at will, or that can't get one. */
const addons = vi.hoisted(() => ({ made: [] as { lose(): void; disposed: boolean }[], fails: 0 }));
vi.mock("@xterm/addon-webgl", () => ({
  WebglAddon: class {
    disposed = false;
    private lost: (() => void) | null = null;
    constructor() {
      if (addons.fails > 0) {
        addons.fails--;
        throw new Error("WebGL2 not supported");
      }
      addons.made.push(this);
    }
    onContextLoss(fn: () => void) {
      this.lost = fn;
    }
    lose() {
      this.lost?.();
    }
    dispose() {
      this.disposed = true;
    }
  },
}));

const { loadRenderer } = await import("./PtyTerminal");

describe("loadRenderer", () => {
  it("asks for a lost WebGL context again, and falls back to the DOM renderer only once none can be had", () => {
    addons.made.length = 0;
    const term = { loadAddon: vi.fn() } as unknown as XTerm;
    const changes: string[] = [];
    expect(loadRenderer(term, (r) => changes.push(r), 2)).toBe("webgl");
    // The GPU switched: a fresh context, still WebGL.
    addons.made[0].lose();
    expect(addons.made[0].disposed).toBe(true);
    expect(changes).toEqual(["webgl"]);
    addons.made[1].lose();
    expect(changes).toEqual(["webgl", "webgl"]);
    // Out of retries: the DOM renderer.
    addons.made[2].lose();
    expect(changes).toEqual(["webgl", "webgl", "dom"]);
    expect(term.loadAddon).toHaveBeenCalledTimes(3);
  });

  it("takes the DOM renderer when a lost context can't be had again", () => {
    addons.made.length = 0;
    const term = { loadAddon: vi.fn() } as unknown as XTerm;
    const changes: string[] = [];
    expect(loadRenderer(term, (r) => changes.push(r))).toBe("webgl");
    addons.fails = 1;
    addons.made[0].lose();
    expect(changes).toEqual(["dom"]);
  });
});
