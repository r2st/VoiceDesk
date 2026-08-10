/**
 * Presentational primitives.
 *
 * These are shared by every screen, so the assertions target the contract each
 * one exposes rather than its classes: what a screen reader announces, which
 * optional slots collapse when unset, and which callbacks actually fire.
 * Tailwind class strings are only asserted where the class *is* the meaning —
 * the sign of a delta, say, which is the difference between "up 12%" reading
 * as good news or bad.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorNotice,
  Spinner,
  StatCard,
} from "./ui";

describe("Card", () => {
  it("renders its children", () => {
    render(
      <Card>
        <p>Inner content</p>
      </Card>,
    );
    expect(screen.getByText("Inner content")).toBeInTheDocument();
  });

  it("appends a caller's className to its own", () => {
    const { container } = render(<Card className="px-5">content</Card>);
    const card = container.firstElementChild;
    expect(card).toHaveClass("px-5");
    expect(card).toHaveClass("rounded-xl");
  });
});

describe("CardHeader", () => {
  it("renders the title as a heading so screens have an outline", () => {
    render(<CardHeader title="Recent calls" />);
    expect(
      screen.getByRole("heading", { name: "Recent calls" }),
    ).toBeInTheDocument();
  });

  it("omits the subtitle element when no subtitle is given", () => {
    render(<CardHeader title="Recent calls" />);
    expect(screen.queryByText(/last 30 days/i)).not.toBeInTheDocument();
  });

  it("renders the subtitle when given", () => {
    render(<CardHeader title="Recent calls" subtitle="Last 30 days" />);
    expect(screen.getByText("Last 30 days")).toBeInTheDocument();
  });

  it("renders an action slot", () => {
    render(
      <CardHeader title="Recent calls" action={<button>Export</button>} />,
    );
    expect(screen.getByRole("button", { name: "Export" })).toBeInTheDocument();
  });
});

describe("StatCard", () => {
  it("shows the label and value", () => {
    render(<StatCard label="Calls" value="1,204" />);
    expect(screen.getByText("Calls")).toBeInTheDocument();
    expect(screen.getByText("1,204")).toBeInTheDocument();
  });

  it("renders a positive delta in the success tone", () => {
    render(
      <StatCard
        label="Calls"
        value="1,204"
        delta={{ value: "+12%", positive: true }}
      />,
    );
    expect(screen.getByText("+12%")).toHaveClass("text-emerald-700");
  });

  it("renders a negative delta in the danger tone", () => {
    render(
      <StatCard
        label="Calls"
        value="900"
        delta={{ value: "-8%", positive: false }}
      />,
    );
    expect(screen.getByText("-8%")).toHaveClass("text-rose-700");
  });

  it("renders no delta chip when the delta is null", () => {
    render(<StatCard label="Calls" value="1,204" delta={null} hint="vs last month" />);
    expect(screen.getByText("vs last month")).toBeInTheDocument();
    expect(screen.queryByText(/%$/)).not.toBeInTheDocument();
  });

  it("renders a hint alongside a delta", () => {
    render(
      <StatCard
        label="Calls"
        value="1,204"
        hint="vs last month"
        delta={{ value: "+12%", positive: true }}
      />,
    );
    expect(screen.getByText("vs last month")).toBeInTheDocument();
    expect(screen.getByText("+12%")).toBeInTheDocument();
  });
});

describe("Badge", () => {
  it("defaults to the neutral tone", () => {
    render(<Badge>Draft</Badge>);
    expect(screen.getByText("Draft")).toHaveClass("bg-ink-100");
  });

  it.each([
    ["success", "bg-emerald-50"],
    ["warning", "bg-amber-50"],
    ["danger", "bg-rose-50"],
    ["info", "bg-brand-50"],
    ["neutral", "bg-ink-100"],
  ] as const)("renders the %s tone", (tone, expectedClass) => {
    render(<Badge tone={tone}>Status</Badge>);
    expect(screen.getByText("Status")).toHaveClass(expectedClass);
  });
});

describe("Spinner", () => {
  it("announces itself politely so loading is not silent", () => {
    render(<Spinner />);
    const status = screen.getByRole("status");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveTextContent("Loading");
  });

  it("accepts a caller's label", () => {
    render(<Spinner label="Fetching calls" />);
    expect(screen.getByRole("status")).toHaveTextContent("Fetching calls");
  });
});

describe("ErrorNotice", () => {
  it("shows the message", () => {
    render(<ErrorNotice message="Could not reach the API." />);
    expect(screen.getByText("Could not reach the API.")).toBeInTheDocument();
  });

  it("offers no retry affordance when no handler is given", () => {
    render(<ErrorNotice message="Could not reach the API." />);
    expect(
      screen.queryByRole("button", { name: /try again/i }),
    ).not.toBeInTheDocument();
  });

  it("invokes onRetry when the retry button is pressed", async () => {
    const onRetry = vi.fn();
    render(<ErrorNotice message="Could not reach the API." onRetry={onRetry} />);

    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(onRetry).toHaveBeenCalledOnce();
  });
});

describe("EmptyState", () => {
  it("shows the title and description", () => {
    render(
      <EmptyState
        title="No calls yet"
        description="Calls appear here once your number receives one."
      />,
    );
    expect(screen.getByText("No calls yet")).toBeInTheDocument();
    expect(
      screen.getByText("Calls appear here once your number receives one."),
    ).toBeInTheDocument();
  });
});
