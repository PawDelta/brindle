import { act, cleanup, fireEvent, render, renderHook, screen, waitFor, within } from "@testing-library/react";
import { useReaderDoc } from "../../store/libraryReader";
import { afterEach, describe, expect, it } from "vitest";
import { DaemonClient, type DocResults } from "../../api/client";
import { DocsView } from "./DocsView";
import { docQueue } from "../Library/libraryStages";

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
  it("library search", async () => {
    const docs = [
      listed("d1", "Recovery", "Design", "root1", { created_by: "person", updated_at: Date.now() / 1000, used_in_briefs: 3 }),
      listed("d2", "Hosting", "Design", "root2", { created_by: "supervisor" }),
    ];
    const record = {
      revisions: [{ rev: 2, author: "person", source: "edit", changed: ["s1"], at: 1760000000 }],
      threads: [{ id: 1, author: "person", text: "Reconnect credentials first.", replies: [{ text: "Keep the token encrypted." }] }],
      decisions: [{ id: "storage", title: "Journal format", options: [{ id: "a", label: "Transactional database" }] }],
      verdict: { approved: true, picks: { storage: "a" } },
    };
    const details = docs.map((d, i) => view(d, {
      files: [{ name: "design.md", md: i ? "Use hosted machines." : "Store the journal in SQLite." }],
      source: i ? d.source : { kind: "goal", ref: "v_recovery", session: "root1" },
      ...(i ? { ...record, design: { rev: 1, reviewers: [{ sub: "sam", required: true }], sign_offs: [] } } : {}),
    }));
    let finishOld: ((r: Response) => void) | undefined;
    const api = new DaemonClient({ baseUrl: "http://daemon.test", token: "t", fetch: async (input) => {
      const url = new URL(String(input));
      const path = url.pathname.replace(/^\/v1/, "");
      if (path === "/kb/search" && url.searchParams.get("q") === "old") return new Promise<Response>((resolve) => { finishOld = resolve; });
      if (path === "/analysis-docs/results") return new Response("{}", { status: 404 });
      const body = path === "/analysis-docs" ? { docs }
        : path === "/analysis-docs/topics" ? { topics: [] }
        : path === "/analysis-docs/scopes" ? { scopes: SCOPES }
        : path === "/decisions" ? [{ id: "dec", topic: "SQLite", body: "Use SQLite", rationale: "Atomic journal", created_at: 1760000000 }]
        : path === "/kb/search" ? [
          { chunk_id: "c1", external_id: "README.md", url: "file:///docs/a/README.md", title: "SQLite runbook", text: "SQLite recovery procedure", updated_at: 1760000000 },
          { chunk_id: "c1b", external_id: "README.md", url: "file:///docs/a/README.md", title: "SQLite runbook", text: "Another SQLite passage", updated_at: 1760000000 },
          { chunk_id: "c2", external_id: "README.md", url: "file:///docs/b/README.md", title: "SQLite operations", text: "SQLite maintenance", updated_at: 1760000000 },
          { chunk_id: "c3", external_id: "README.md", url: null, title: "SQLite notes", text: "SQLite notes without a URL", updated_at: 1760000000 },
          { chunk_id: "c4", external_id: "README.md", url: null, title: "SQLite checklist", text: "SQLite checklist without a URL", updated_at: 1760000000 },
        ]
        : path === "/kept-visuals" ? [
          { id: "k1", title: "SQLite layout", repo: "/r", from_session_name: "Recovery", kept_at: 1760000000 },
          { id: "k2", title: "SQLite private layout", repo: "/other", from_session_name: "Other", kept_at: 1760000000 },
        ]
        : path === "/plans/v_recovery" ? { ...record, state: "approved", session: "root1", markdown: "Store the journal in SQLite." }
        : details[docs.findIndex((d) => path.endsWith(d.doc_id))];
      return new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
    } });
    render(<DocsView client={api} repo="/r" />);
    await waitFor(() => expect(shown()).toEqual(["d1", "d2"]));
    // The Sources are listed only for a search; a reviewer narrows the designs, and clearing it restores them.
    const hits = () => [...screen.getByTestId("docs").querySelectorAll("[data-library-hit]")].map((li) => li.getAttribute("data-library-hit"));
    expect(hits()).toEqual([]);
    fireEvent.change(await screen.findByRole("combobox", { name: "Reviewer" }), { target: { value: "sam" } });
    expect(shown()).toEqual(["d2"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Reviewer" }), { target: { value: "" } });
    expect(shown()).toEqual(["d1", "d2"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Author" }), { target: { value: "person" } });
    expect(shown()).toEqual(["d1"]);
    fireEvent.change(screen.getByRole("combobox", { name: "Changed" }), { target: { value: "7" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Author" }), { target: { value: "" } });
    expect(shown()).toEqual(["d1"]);
    const box = screen.getByRole("searchbox", { name: "Search the library" });
    fireEvent.change(box, { target: { value: "sqlite" } });
    await waitFor(() => expect(screen.getByTestId("docs").querySelectorAll("[data-library-hit]").length).toBe(0));
    await waitFor(() => expect(shown()).toEqual(["d1"]));
    await waitFor(() => expect(screen.getByTestId("docs").querySelector('[data-doc="d1"]')?.textContent).toContain("Store the journal in SQLite."));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    fireEvent.change(box, { target: { value: "sqlite" } });
    await waitFor(() => expect(screen.getByTestId("docs").querySelectorAll("[data-library-hit]").length).toBe(6));
    expect([...screen.getByTestId("docs").querySelectorAll('[data-library-hit^="imported:"]')].map((li) => li.getAttribute("data-library-hit"))).toEqual(["imported:c1", "imported:c2", "imported:c3", "imported:c4"]);
    expect(screen.getByTestId("docs").querySelector('[data-library-hit="kept:k2"]')).toBeNull();
    // A scope narrows the search to designs (the Sources can't say whose they are); Everyone's brings them back.
    const scope = screen.getByRole("group", { name: "Scope" });
    fireEvent.click(within(scope).getByRole("button", { name: "Mine" }));
    expect(screen.getByTestId("docs").querySelectorAll("[data-library-hit]").length).toBe(0);
    fireEvent.click(within(scope).getByRole("button", { name: "Everyone's" }));
    await waitFor(() => expect(screen.getByTestId("docs").querySelectorAll("[data-library-hit]").length).toBe(6));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('[data-library-hit="imported:c1"]')!).getByRole("button"));
    expect((await screen.findByTestId("doc-body")).textContent).toContain("SQLite recovery procedure");
    fireEvent.click(screen.getByRole("button", { name: "← Library" }));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('[data-library-hit="decision:dec"]')!).getByRole("button"));
    expect((await screen.findByTestId("doc-body")).textContent).toContain("Atomic journal");
    fireEvent.click(screen.getByRole("button", { name: "← Library" }));
    fireEvent.click(screen.getByRole("button", { name: "Clear" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "encrypted" } });
    await waitFor(() => expect(shown()).toEqual(["d1", "d2"]));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "transactional" } });
    await waitFor(() => expect(shown()).toEqual(["d1", "d2"]));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!).getByRole("button"));
    await screen.findByTestId("doc-body");
    const recordElement = screen.getByRole("complementary", { name: "Design record" });
    expect(recordElement.querySelector("[data-revision='2']")).toBeTruthy();
    expect(recordElement.querySelector("[data-decision='storage']")).toBeTruthy();
    expect(within(recordElement).getByRole("region", { name: "Comments" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "← Library" }));
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "old" } });
    await waitFor(() => expect(finishOld).toBeTruthy());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "missing" } });
    await waitFor(() => expect(shown()).toEqual([]));
    await act(async () => { finishOld!(new Response(JSON.stringify([{ chunk_id: "stale", external_id: "stale", title: "Stale", text: "Old result" }]))); });
    expect(screen.queryByText("Stale")).toBeNull();
    cleanup();
    render(<DocsView client={client(() => ({}), RESULTS, [])} repo="/r" />);
    expect(await screen.findByTestId("library-empty")).toBeTruthy();
  });

  it("library search stage and reviewer: stages come from each shared design's review state, Reviewer selects and clears, and the record lists every signer", async () => {
    const designs: Record<string, object> = {
      d1: { rev: 2, ready: false, reviewers: [{ sub: "sam", required: true }, { sub: "dana", required: false }], sign_offs: [{ sub: "sam", rev: 2 }, { sub: "dana", rev: 1 }, { sub: "ops", rev: 1 }, { sub: "ops", rev: 2 }] },
      d2: { rev: 1, ready: true, reviewers: [{ sub: "dana", required: true }], sign_offs: [{ sub: "dana", rev: 1 }] },
      d3: { rev: 1, ready: false, reviewers: [{ sub: "sam", required: true }], sign_offs: [] },
    };
    // d3 is in the person's queue; d1 and d2 have no Inbox card at all. d4 is a personal design.
    const queue = docQueue([{ kind: "review_requested", reason: "r", doc: "d3" }]);
    const api = client((id) => view(DOCS.find((d) => d.doc_id === id)!, designs[id] ? { design: designs[id] } : {}));
    const { rerender } = render(<DocsView client={api} stage="in-review" queue={queue} />);
    // Waiting on the person doesn't take a design out of its stage: d3 is in review too.
    await waitFor(() => expect(shown()).toEqual(["d3", "d1"]));
    rerender(<DocsView client={api} stage="approved" queue={queue} />);
    expect(shown()).toEqual(["d2"]);
    rerender(<DocsView client={api} stage="needs-you" queue={queue} />);
    expect(shown()).toEqual(["d3"]);
    rerender(<DocsView client={api} queue={queue} />);
    expect(shown()).toHaveLength(4);
    const pick = (name: string, value: string) => fireEvent.change(screen.getByRole("combobox", { name }), { target: { value } });
    pick("Reviewer", "sam");
    expect(shown().sort()).toEqual(["d1", "d3"]);
    pick("Reviewer", "");
    expect(shown()).toHaveLength(4);
    fireEvent.click(screen.getByTestId("docs").querySelector("li[data-doc='d1'] button")!);
    const record = await screen.findByRole("complementary", { name: "Design record" });
    const rows = [...record.querySelectorAll("[data-signoff]")].map((r) => `${r.getAttribute("data-signoff")}@${r.getAttribute("data-rev") || "waiting"}:${r.getAttribute("data-signed")}`);
    // Every recorded signature keeps its revision (ops, outside the roster, signed two); dana signed rev 1 only, so she is also waiting.
    expect(rows).toEqual(["sam@2:yes", "dana@1:no", "ops@1:no", "ops@2:yes", "dana@waiting:no"]);
  });

  it("lists one session's docs, narrows them by search, and opens one in the reader", async () => {
    render(<DocsView client={client((id) => view(DOCS.find((d) => d.doc_id === id)!))} repo="/r" session="root1" />);
    await waitFor(() => expect(shown()).toEqual(["d1", "d2", "d4"]));
    // Scopes without documents still remain available.
    await within(screen.getByRole("group", { name: "Scope" })).findByRole("button", { name: "Platform" });
    expect(screen.getByTestId("docs").querySelector('li[data-doc="d4"]')!.textContent).toContain(SCOPES[1].name);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the library" }), { target: { value: "pricing" } });
    await waitFor(() => expect(shown()).toEqual(["d1"]));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d1"]')!).getByRole("button"));
    const body = await screen.findByTestId("doc-body");
    expect(body.textContent).toContain("Body.");
    const readerDoc = () => renderHook(() => useReaderDoc()).result.current;
    expect(readerDoc()).toBe("d1");
    // A connected doc is named by its title and opens in turn.
    fireEvent.click(screen.getByRole("button", { name: "Stripe vs Adyen fees" }));
    await waitFor(() => expect(screen.getByTestId("doc-body").textContent).toContain("Stripe vs Adyen fees"));
    // The Library rail moves whichever doc is open here: the row's, then the connected one, then none.
    expect(readerDoc()).toBe("d2");
    fireEvent.click(screen.getByRole("button", { name: /Library/ }));
    await waitFor(() => expect(readerDoc()).toBeNull());
  });

  it("opens a design in the design reader here in the Library, and Back comes to the list as it was", async () => {
    const docs = [...DOCS, listed("d5", "Cloud workers", "design", "root1")];
    const designReader = (id: string, title: string | undefined, back: () => void) => (
      <div data-testid="design-reader" data-doc={id}>{title}<button type="button" onClick={back}>Back to list</button></div>
    );
    render(<DocsView client={client((id) => view(docs.find((d) => d.doc_id === id)!), RESULTS, docs)} repo="/r" designReader={designReader} />);
    await waitFor(() => expect(shown()).toContain("d5"));
    fireEvent.change(screen.getByRole("searchbox", { name: "Search the library" }), { target: { value: "cloud" } });
    await waitFor(() => expect(shown()).toEqual(["d5"]));
    fireEvent.click(within(screen.getByTestId("docs").querySelector<HTMLElement>('li[data-doc="d5"]')!).getByRole("button"));
    expect(screen.getByTestId("design-reader").dataset.doc).toBe("d5");
    fireEvent.click(screen.getByRole("button", { name: "Back to list" }));
    expect((screen.getByRole("searchbox", { name: "Search the library" }) as HTMLInputElement).value).toBe("cloud");
    expect(shown()).toEqual(["d5"]);
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
    for (const mention of Object.values(detail.mentions).flat()) expect(screen.getByRole("complementary").textContent).toContain(mention);
    fireEvent.click(screen.getByRole("button", { name: "Share" }));
    // Shared with one team already, never shared with the other, which has no docs at all.
    await waitFor(() =>
      expect(within(screen.getByRole("group", { name: "Share with" })).getAllByRole("button").map((b) => b.textContent)).toEqual(["Stop sharing with Payments team", "Share with Platform"]),
    );
  });
});
