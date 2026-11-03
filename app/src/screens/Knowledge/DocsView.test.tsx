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
  listed("d4", "Payments runbook", "Runbook", "root1", { scope: { kind: "group", id: "team-pay" } }),
];

/* GET /analysis-docs/scopes: every scope the person reads, a team with no docs among them. */
const SCOPES = [
  { kind: "user", id: "u1", name: "Me", can_share: false },
  { kind: "group", id: "team-pay", name: "Payments team", can_share: true },
  { kind: "group", id: "team-new", name: "Platform", can_share: true },
];

const RESULTS: DocResults = { days: 30, minimum: 5, enough: true, briefed: 6, large: 9, compared: 6, similar: 7, with_brief: 1.2, without_brief: 2.0 };

function client(detail: (id: string) => object, results: DocResults = RESULTS, docs: object[] = DOCS) {
  const fetch: typeof globalThis.fetch = (input) => {
    const url = new URL(String(input));
    const path = url.pathname.replace(/^\/v1/, "");
    const session = url.searchParams.get("session");
    const body =
      path === "/analysis-docs"
        ? { scope: { kind: "user", id: "u1" }, docs: (docs as typeof DOCS).filter((d) => !session || d.source.session === session) }
        : path === "/analysis-docs/topics"
          ? { topics: [{ topic: "checkout", count: 3, stale: 0, latest: "d1" }] }
          : path === "/analysis-docs/scopes"
            ? { scopes: SCOPES }
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
  it("lists one session's docs as cards, narrows them by category and search, and opens one in the reader", async () => {
    const docs = DOCS.map((d) => ({ ...d, category: d.doc_id === "d2" ? "Fees" : "Checkout" }));
    render(<DocsView client={client((id) => view(DOCS.find((d) => d.doc_id === id)!), RESULTS, docs)} repo="/r" session="root1" />);
    await waitFor(() => expect(shown()).toEqual(["d1", "d4", "d2"]));
    const first = screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!;
    for (const text of ["Checkout drop after pricing change", "Investigation", "Mine", "v3", "2 findings · 1 open question", "used in 6 briefs"]) expect(first.textContent).toContain(text);
    // A team's doc carries the team's name; a team with no docs still gets its chip.
    await waitFor(() => expect(screen.getByTestId("docs").querySelector('li[data-doc="d4"]')!.textContent).toContain("Payments team"));
    await waitFor(() => expect(within(screen.getByRole("group", { name: "Scope" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["All", "Mine", "Payments team", "Platform"]));
    const rail = screen.getByRole("navigation", { name: "Topics and kinds" });
    expect(within(rail).getByRole("button", { name: /checkout/ }).textContent).toBe("checkout3");
    fireEvent.click(within(rail).getByRole("button", { name: /^Fees/ }));
    expect(shown()).toEqual(["d2"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search docs" }), { target: { value: "pricing" } });
    expect(screen.getByText("No doc matches.")).toBeTruthy();
    fireEvent.click(within(rail).getByRole("button", { name: /^Fees/ }));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!).getByRole("button"));
    const body = await screen.findByTestId("doc-body");
    expect(body.textContent).toContain("Body.");
    // A connected doc is named by its title and opens in turn.
    fireEvent.click(screen.getByRole("button", { name: "Stripe vs Adyen fees" }));
    await waitFor(() => expect(screen.getByTestId("doc-body").textContent).toContain("Stripe vs Adyen fees"));
  });

  it("groups docs under their categories, largest first and uncategorised last, and remembers a collapsed one", async () => {
    const docs = [
      { ...DOCS[0], category: "Security" }, { ...DOCS[1], category: " " }, { ...DOCS[2], category: "Security" }, { ...DOCS[3], category: "Desktop updates" },
    ];
    const at = (repo: string, listed: object[] = docs) => <DocsView client={client(() => ({}), RESULTS, listed)} repo={repo} />;
    const open = () => render(at("/r"));
    open();
    await waitFor(() => expect(screen.getAllByRole("region").map((r) => r.getAttribute("aria-label")).slice(2)).toEqual(["Security", "Desktop updates", "Uncategorised"]));
    expect([...screen.getByRole("region", { name: "Security" }).querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"))).toEqual(["d1", "d3"]);
    fireEvent.click(within(screen.getByRole("region", { name: "Security" })).getByRole("button", { expanded: true }));
    expect(shown()).toEqual(["d4", "d2"]);
    cleanup();
    const { rerender } = open();
    await waitFor(() => expect(shown()).toEqual(["d4", "d2"]));
    // The rail lists the categories with their counts; picking a collapsed one opens and filters to it, and its header still collapses.
    const rail = screen.getByRole("navigation", { name: "Topics and kinds" });
    expect(within(rail).getAllByRole("button").slice(0, 3).map((b) => b.textContent)).toEqual(["Security2", "Desktop updates1", "Uncategorised1"]);
    fireEvent.click(within(rail).getByRole("button", { name: /^Security/ }));
    expect(shown()).toEqual(["d1", "d3"]);
    fireEvent.click(within(screen.getByRole("region", { name: "Security" })).getByRole("button", { expanded: true }));
    expect(shown()).toEqual([]);
    // A repo without that category drops the filter, so its docs stay reachable.
    rerender(at("/other", DOCS));
    await waitFor(() => expect(shown()).toEqual(["d1", "d2", "d3", "d4"]));
    // Collapsed headers are each repo's own.
    rerender(at("/third"));
    await waitFor(() => expect(shown()).toEqual(["d1", "d3", "d4", "d2"]));
    rerender(at("/r"));
    await waitFor(() => expect(shown()).toEqual(["d4", "d2"]));
    localStorage.clear();
  });

  it("reads a real doc: raw HTML and images stay text, mentions are listed, and Share offers every team of the person's", async () => {
    const team = { kind: "group", id: "team-pay" };
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
    // Shared with one team already, never shared with the other, which has no docs at all.
    await waitFor(() =>
      expect(within(screen.getByRole("group", { name: "Share with" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["Stop sharing with Payments team", "Share with Platform"]),
    );
  });
});
