import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonClient, type DocResults } from "../../api/client";
import { DocsView } from "./DocsView";

afterEach(cleanup);

/* The library's answers as pawdelta-web's pro_api.analysis_docs gives them (_listed, _view, topics). */
const listed = (doc_id: string, title: string, kind: string, session: string, extra: object = {}) => ({
  doc_id, version: 1, title, kind, summary: `${title}, in short.`, findings: [], open_questions: [], topics: ["checkout"],
  mentions: { files: [], symbols: [], services: [] }, source: { kind: "task", ref: "t", session }, paths: [], related: [],
  supersedes: [], superseded_by: null, stale: false, used_in_briefs: 0, created_at: 1760000000, created_by: "u1",
  scope: { kind: "user", id: "u1" }, shared_from: null, ...extra,
});

const DOCS = [
  listed("d1", "Checkout drop after pricing change", "Investigation", "root1", { version: 3, used_in_briefs: 6, findings: ["a", "b"], open_questions: ["c"], related: ["d2"] }),
  listed("d2", "Stripe vs Adyen fees", "Comparison", "root1"),
  listed("d3", "Refund latency", "Investigation", "root2"),
];

const RESULTS: DocResults = { days: 30, minimum: 5, enough: true, briefed: 6, large: 9, compared: 6, similar: 7, with_brief: 1.2, without_brief: 2.0 };

function client(detail: (id: string) => object, results: DocResults = RESULTS) {
  const fetch: typeof globalThis.fetch = (input) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/v1/, "");
    const session = url.searchParams.get("session");
    const body =
      path === "/analysis-docs"
        ? { scope: { kind: "user", id: "u1" }, docs: DOCS.filter((d) => !session || d.source.session === session) }
        : path === "/analysis-docs/topics"
          ? { topics: [{ topic: "checkout", count: 3, stale: 0, latest: "d1" }] }
          : path === "/analysis-docs/scopes"
            ? { scopes: [{ kind: "user", id: "u1" }] }
            : path === "/analysis-docs/results"
              ? results
              : detail(decodeURIComponent(path.split("/").pop()!));
    return Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
  };
  return new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch });
}

const shown = () => [...screen.getByTestId("docs").querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"));

const view = (d: (typeof DOCS)[number], extra: object = {}) => {
  const { doc_id, ...rest } = d;
  return { ...rest, id: doc_id, files: [{ name: "main.md", md: `# ${d.title}\n\nBody.` }], scopes: [d.scope], read_through: d.scope, ...extra };
};

describe("docs view", () => {
  it("lists one session's docs as cards, narrows them by kind and search, and opens one in the reader", async () => {
    render(<DocsView client={client((id) => view(DOCS.find((d) => d.doc_id === id)!))} repo="/r" session="root1" />);
    await waitFor(() => expect(shown()).toEqual(["d1", "d2"]));
    const first = screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!;
    for (const text of ["Checkout drop after pricing change", "Investigation", "Mine", "v3", "2 findings · 1 open question", "used in 6 briefs"]) expect(first.textContent).toContain(text);
    const rail = screen.getByRole("navigation", { name: "Topics and kinds" });
    expect(within(rail).getByRole("button", { name: /checkout/ }).textContent).toBe("checkout3");
    fireEvent.click(within(rail).getByRole("button", { name: /Comparison/ }));
    expect(shown()).toEqual(["d2"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search docs" }), { target: { value: "pricing" } });
    expect(screen.getByText("No doc matches.")).toBeTruthy();
    fireEvent.click(within(rail).getByRole("button", { name: /Comparison/ }));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!).getByRole("button"));
    const body = await screen.findByTestId("doc-body");
    expect(body.textContent).toContain("Body.");
    // A connected doc is named by its title and opens in turn.
    fireEvent.click(screen.getByRole("button", { name: "Stripe vs Adyen fees" }));
    await waitFor(() => expect(screen.getByTestId("doc-body").textContent).toContain("Stripe vs Adyen fees"));
  });

  it("reads a real doc: raw HTML and images stay text, mentions are listed, and a team it is shared with can be unshared", async () => {
    const team = { kind: "group", id: "team-payments" };
    const md = '# Findings\n\n<script>window.pwned = 1</script>\n<img src="https://evil.example/x.png" onerror="alert(1)">\n\n![chart](https://evil.example/chart.png) and **bold**';
    const detail = view(DOCS[0], { files: [{ name: "main.md", md }], mentions: { files: ["src/pay.py"], symbols: ["charge()"], services: ["stripe"] }, scopes: [DOCS[0].scope, team] });
    const { container } = render(<DocsView client={client(() => detail)} doc="d1" />);
    const body = await screen.findByTestId("doc-body");
    expect(body.textContent).toContain("<script>window.pwned = 1</script>");
    expect(body.textContent).toContain("[image: chart]");
    expect(container.querySelector("script, img, iframe, [onerror]")).toBeNull();
    expect(body.querySelector("strong")?.textContent).toBe("bold");
    expect(screen.getByRole("complementary").textContent).toContain("Mentions: src/pay.py · charge() · stripe");
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    expect(within(screen.getByRole("group", { name: "Share with" })).getByRole("button").textContent).toBe("Stop sharing with Team team-pay");
  });

  it("says there isn't enough data yet below the minimum instead of showing numbers", async () => {
    render(<DocsView client={client(() => ({}), { ...RESULTS, enough: false, compared: 2, similar: 3, with_brief: null, without_brief: null })} />);
    await waitFor(() => expect(screen.getByTestId("results-not-enough").textContent).toContain("Not enough data yet"));
    expect(screen.getByTestId("results").textContent).not.toMatch(/review rounds per task/i);
  });
});
