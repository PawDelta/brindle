import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AnalysisDoc, AnalysisDocDetail, DocResults } from "../../api/client";
import { DocsView, type DocsClient } from "./DocsView";

afterEach(cleanup);

const DOCS: AnalysisDoc[] = [
  { id: "d1", title: "Checkout drop after pricing change", kind: "Investigation", scope: { kind: "group", id: "g1", name: "Payments team" }, summary: "The fee shown at the last step.", version: 3, created_at: 1760000000, used_in_briefs: 6, findings: ["a", "b"], open_questions: ["c"] },
  { id: "d2", title: "Stripe vs Adyen fees", kind: "Comparison", scope: { kind: "user" }, summary: "Adyen is cheaper above $2M.", version: 1, used_in_briefs: 1 },
];

const RESULTS: DocResults = { days: 30, minimum: 5, enough: true, briefed: 6, large: 9, compared: 6, similar: 7, with_brief: 1.2, without_brief: 2.0 };

function fake(over: Partial<DocsClient> = {}): DocsClient {
  return {
    docs: vi.fn(() => Promise.resolve({ docs: DOCS })),
    docTopics: vi.fn(() => Promise.resolve({ topics: [{ name: "Checkout and pricing", count: 1 }] })),
    doc: vi.fn(() => Promise.reject(new Error("no doc"))),
    docResults: vi.fn(() => Promise.resolve(RESULTS)),
    shareDoc: vi.fn(() => Promise.resolve({})),
    unshareDoc: vi.fn(() => Promise.resolve({})),
    deleteDoc: vi.fn(() => Promise.resolve({})),
    exportDoc: vi.fn(() => Promise.resolve({ filename: "x.md" })),
    ...over,
  };
}

describe("docs view", () => {
  it("lists the session's docs as cards and narrows them by kind and search", async () => {
    const client = fake();
    render(<DocsView client={client} repo="/r" session="root1" />);
    await waitFor(() => expect(screen.getByTestId("docs").querySelector('li[data-doc="d1"]')).toBeTruthy());
    expect(client.docs).toHaveBeenCalledWith({ repo: "/r", scope: undefined, topic: undefined, session: "root1" });
    const first = screen.getByTestId("docs").querySelector('li[data-doc="d1"]')!;
    for (const text of ["Checkout drop after pricing change", "Investigation", "Payments team", "The fee shown at the last step.", "v3", "2 findings · 1 open question", "used in 6 briefs"]) expect(first.textContent).toContain(text);
    fireEvent.click(within(screen.getByRole("navigation", { name: "Topics and kinds" })).getByRole("button", { name: /Comparison/ }));
    expect([...screen.getByTestId("docs").querySelectorAll("li[data-doc]")].map((li) => li.getAttribute("data-doc"))).toEqual(["d2"]);
    fireEvent.change(screen.getByRole("searchbox", { name: "Search docs" }), { target: { value: "pricing" } });
    expect(screen.getByText("No doc matches.")).toBeTruthy();
  });

  it("shows raw HTML and remote images in a doc as text, never as markup", async () => {
    const detail: AnalysisDocDetail = {
      ...DOCS[0],
      files: [{ name: "doc.md", md: '# Findings\n\n<script>window.pwned = 1</script>\n<img src="https://evil.example/x.png" onerror="alert(1)">\n\n![chart](https://evil.example/chart.png) and **bold**' }],
    };
    const { container } = render(<DocsView client={fake({ doc: vi.fn(() => Promise.resolve(detail)) })} doc="d1" />);
    const body = await screen.findByTestId("doc-body");
    expect(body.textContent).toContain("<script>window.pwned = 1</script>");
    expect(body.textContent).toContain("[image: chart]");
    expect(container.querySelector("script, img, iframe, [onerror]")).toBeNull();
    expect(body.querySelector("strong")?.textContent).toBe("bold");
  });

  it("says there isn't enough data yet below the minimum instead of showing numbers", async () => {
    render(<DocsView client={fake({ docResults: vi.fn(() => Promise.resolve({ ...RESULTS, enough: false, compared: 2, similar: 3, with_brief: null, without_brief: null })) })} />);
    await waitFor(() => expect(screen.getByTestId("results-not-enough").textContent).toContain("Not enough data yet"));
    expect(screen.getByTestId("results").textContent).not.toMatch(/review rounds per task/i);
  });
});
