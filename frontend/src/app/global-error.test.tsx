import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import GlobalError from "./global-error";

describe("GlobalError boundary", () => {
  it("shows a recovery message when the root layout itself throws", () => {
    render(<GlobalError error={new Error("layout blew up")} reset={vi.fn()} />);

    expect(screen.getByText("VoiceDesk hit a snag")).toBeInTheDocument();
  });

  it("calls reset when 'Reload' is clicked", () => {
    const reset = vi.fn();
    render(<GlobalError error={new Error("layout blew up")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });
});
