import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import ErrorBoundary from "./error";

describe("Error boundary", () => {
  it("shows a recovery message instead of a blank page", () => {
    render(<ErrorBoundary error={new Error("render blew up")} reset={vi.fn()} />);

    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Go to dashboard" }),
    ).toHaveAttribute("href", "/dashboard");
  });

  it("calls reset when 'Try again' is clicked", () => {
    const reset = vi.fn();
    render(<ErrorBoundary error={new Error("render blew up")} reset={reset} />);

    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it("logs the error for diagnostics without throwing", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    const error = new Error("boom");
    render(<ErrorBoundary error={error} reset={vi.fn()} />);

    expect(spy).toHaveBeenCalledWith(error);
    spy.mockRestore();
  });
});
