import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import NotFound from "./not-found";

describe("NotFound", () => {
  it("links back to the dashboard instead of leaving a dead end", () => {
    render(<NotFound />);

    expect(screen.getByText("Page not found")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Go to dashboard" }),
    ).toHaveAttribute("href", "/dashboard");
  });
});
