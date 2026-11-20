import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import type { DesignShareLink } from "../../../api/client";
import { ShareLinkPanel, useShareLink, type ShareLinkTarget } from "./ShareLink";

afterEach(cleanup);

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

function Menu({ target, fresh }: { target: ShareLinkTarget; fresh: number }) {
  const link = useShareLink(target, fresh);
  return link.state?.available ? <ShareLinkPanel target={target} state={link.state} onState={link.setState} /> : null;
}

it("a read sent before a copy can't take the copied link back when it answers late", async () => {
  const url = "https://pawdelta.com/api/brindle/v1/share-links/dsl_x";
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
