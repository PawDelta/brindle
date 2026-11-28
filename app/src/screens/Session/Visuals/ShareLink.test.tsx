import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { DaemonError, type DesignShareLink } from "../../../api/client";
import { planLinkTarget, ShareLinkPanel, ShareLinkSection, useShareLink, type ShareLinkTarget } from "./ShareLink";

afterEach(cleanup);

it("copies the selected organization or team URL from the plan menu", async () => {
  const copied: string[] = [];
  Object.assign(navigator, { clipboard: { writeText: async (url: string) => { copied.push(url); } } });
  const target = planLinkTarget({
    docScopes: async () => ({ scopes: [{ kind: "org", id: "o", name: "Acme" }, { kind: "group", id: "g", name: "Core" }] }),
    planShareLink: async () => ({ available: true, scoped: true, link: null }),
    copyPlanShareLink: async (_id, _repo, selection) => ({ available: true, scoped: true, link: {
      url: `https://plans.pawdelta.com/acme/${selection?.scope === "team" && selection.team === "g" ? "core/" : selection?.scope === "org" ? "" : "invalid/"}plan/token`,
      access: "org", created_at: 1,
    } }),
    revokePlanShareLink: async () => ({ available: true, link: null }),
  }, "p1")!;
  await act(async () => render(<Menu target={target} fresh={0} />));
  const picker = screen.getByRole("combobox", { name: "Link audience" });
  fireEvent.change(picker, { target: { value: "team:g" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy link" })));
  expect(copied).toEqual(["https://plans.pawdelta.com/acme/core/plan/token"]);
  fireEvent.change(picker, { target: { value: "org" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy link" })));
  expect(copied[1]).toBe("https://plans.pawdelta.com/acme/plan/token");
});

it("hides copying when the org disables links, including an existing link", async () => {
  const target: ShareLinkTarget = {
    key: "plan:p1", read: async () => ({ available: true, link: null }),
    copy: async () => { throw new Error("must not copy"); },
    revoke: async () => ({ available: true, link: null }),
  };
  const { rerender } = render(<ShareLinkPanel target={target}
    state={{ available: true, link: null, access: "off" }} onState={() => {}} />);
  expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
  let state: DesignShareLink = { available: true,
    link: { url: "https://example.com/link", access: "off", created_at: 1 } };
  const onState = (next: DesignShareLink) => { state = next; };
  rerender(<ShareLinkPanel target={target} state={state} onState={onState} />);
  expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByRole("button", { name: "Revoke link" })).toBeTruthy();
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Revoke link" })));
  rerender(<ShareLinkPanel target={target} state={state} onState={onState} />);
  expect(state.link).toBeNull();
  expect(screen.queryByRole("button", { name: "Copy link" })).toBeNull();
});

it("reads the audiences again without touching a change still out", async () => {
  let scopesDown = true;
  let revoked: (got: DesignShareLink) => void = () => {};
  const target: ShareLinkTarget = {
    key: "plan:p1", read: async () => ({ available: true, scoped: true, link: null }),
    copy: async () => { throw new Error("must not copy"); },
    revoke: () => new Promise<DesignShareLink>((resolve) => { revoked = resolve; }),
    scopes: async () => {
      if (scopesDown) throw new DaemonError("library_offline", "Couldn't reach brindle Pro.", 503);
      return { scopes: [{ kind: "org", id: "o", name: "Acme" }] };
    },
  };
  const state: DesignShareLink = { available: true, scoped: true, link: { url: "https://example.com/link", access: "org", created_at: 1 } };
  await act(async () => render(<ShareLinkPanel target={target} state={state} onState={() => {}} />));
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Revoke link" })));
  const panel = screen.getByTestId("share-link");
  expect(panel.dataset.state).toBe("saving");
  expect(screen.getByTestId("library-network").dataset.state).toBe("offline");
  scopesDown = false;
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
  expect(screen.queryByTestId("library-network")).toBeNull();
  expect(panel.dataset.state).toBe("saving");
  expect((screen.getByRole("button", { name: "Revoke link" }) as HTMLButtonElement).disabled).toBe(true);
  await act(async () => revoked({ available: true, scoped: true, link: null }));
  expect(panel.dataset.state).toBe("ready");
});

function Menu({ target, fresh }: { target: ShareLinkTarget; fresh: number }) {
  const link = useShareLink(target, fresh);
  return <ShareLinkSection target={target} link={link} />;
}

it("can read the plan link again after a busy sign-in", async () => {
  let blocked = true;
  const target: ShareLinkTarget = {
    key: "plan:p1",
    read: async () => {
      if (blocked) throw new DaemonError("busy", "Sign-in refresh in progress", 503);
      return { available: true, link: null };
    },
    copy: async () => ({ available: true, link: null }),
    revoke: async () => ({ available: true, link: null }),
  };
  await act(async () => render(<Menu target={target} fresh={0} />));
  expect(screen.getByRole("alert")).toBeTruthy();
  blocked = false;
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Try again" })));
  expect(screen.queryByRole("alert")).toBeNull();
  expect(screen.getByTestId("share-link")).toBeTruthy();
});

it("adopts a delayed audience refresh while retaining choices across unchanged audiences", async () => {
  const reads: ((got: DesignShareLink) => void)[] = [];
  const copied: string[] = [];
  Object.assign(navigator, { clipboard: { writeText: async (url: string) => { copied.push(url); } } });
  const org: DesignShareLink = { available: true, scoped: true, link: {
    url: "https://plans.pawdelta.com/acme/plan/org-token", access: "org", created_at: 1,
    scope: { kind: "org", org_id: "o", team: null },
  } };
  const team: DesignShareLink = { ...org, link: { ...org.link!,
    url: "https://plans.pawdelta.com/acme/core/plan/team-token",
    scope: { kind: "team", org_id: "o", team: "g" },
  } };
  const target: ShareLinkTarget = {
    key: "plan:p1", read: () => new Promise((resolve) => { reads.push(resolve); }),
    scopes: async () => ({ scopes: [{ kind: "org", id: "o" }, { kind: "group", id: "g" }] }),
    copy: async (selection) => selection?.scope === "team" && selection.team === "g" ? team : org,
    revoke: async () => ({ available: true, link: null }),
  };
  const { rerender } = render(<Menu target={target} fresh={0} />);
  await act(async () => reads[0](org));
  const picker = screen.getByRole("combobox", { name: "Link audience" }) as HTMLSelectElement;
  rerender(<Menu target={target} fresh={1} />);
  fireEvent.change(picker, { target: { value: "team:g" } });
  await act(async () => reads[1]({ ...org }));
  expect(picker.value).toBe("team:g");
  fireEvent.change(picker, { target: { value: "org" } });
  rerender(<Menu target={target} fresh={2} />);
  await act(async () => reads[2](team));
  expect(picker.value).toBe("team:g");
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy link" })));
  expect(copied).toEqual([team.link!.url]);
});

it("a read sent before a copy can't take the copied link back when it answers late", async () => {
  const url = "https://plans.pawdelta.com/b/dsl_x";
  const reads: ((got: DesignShareLink) => void)[] = [];
  Object.assign(navigator, { clipboard: { writeText: async () => {} } });
  const target: ShareLinkTarget = {
    key: "plan:p1",
    read: () => new Promise((resolve) => { reads.push(resolve); }),
    copy: async () => ({ available: true, link: { url, created_at: 1, access: "anyone" } }),
    revoke: async () => ({ available: true, link: null }),
  };
  const { rerender } = render(<Menu target={target} fresh={0} />);
  await act(async () => reads[0]({ available: true, link: null }));
  rerender(<Menu target={target} fresh={1} />);                  // the menu opens: a read goes out, saw no link
  expect(reads).toHaveLength(2);

  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Copy link" })));
  await act(async () => reads[1]({ available: true, link: null }));
  expect((screen.getByRole("textbox") as HTMLInputElement).value).toBe(url);
  expect(screen.getByRole("button", { name: "Revoke link" })).toBeTruthy();
});
