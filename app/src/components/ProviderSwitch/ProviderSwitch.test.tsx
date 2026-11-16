import { cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProviderSwitchCard, type ProviderSwitchOffer } from "./ProviderSwitch";

afterEach(cleanup);

const offer: ProviderSwitchOffer = {
  offerId: "o1",
  rootId: "root",
  kind: "switch",
  fromProvider: "claude",
  toProvider: "codex",
  limitedUntil: 1_800_003_600,
};

describe("ProviderSwitchCard", () => {
  it("answers Switch once however often it is pressed while the switch is out", () => {
    const onAnswer = vi.fn(() => new Promise<void>(() => {}));
    const { container } = render(<ProviderSwitchCard offer={offer} onAnswer={onAnswer} />);
    const card = container.querySelector('[data-kind="switch"]');
    expect(card).not.toBeNull();
    expect(card!.getAttribute("data-to")).toBe("codex");

    const button = container.querySelector<HTMLButtonElement>('[data-action="switch"]')!;
    fireEvent.click(button);
    fireEvent.click(button);
    expect(onAnswer).toHaveBeenCalledTimes(1);
    expect(onAnswer).toHaveBeenCalledWith(true);
  });
});
