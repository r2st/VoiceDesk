/**
 * Form primitives.
 *
 * The settings screen is the one place a keyboard or screen-reader user has to
 * type, so the label/control association is treated as the contract here:
 * every field is looked up by its visible label via `getByLabelText`, which
 * fails if `htmlFor` and `id` ever drift apart.
 *
 * The other load-bearing behaviour is that `onChange` hands back the *value*
 * rather than the event — every caller is written against that shape.
 */

import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  Button,
  FormError,
  SelectField,
  SuccessNotice,
  TextField,
  TextareaField,
} from "./form";

describe("TextField", () => {
  it("associates the label with the input", () => {
    render(
      <TextField id="name" label="Full name" value="" onChange={() => {}} />,
    );
    expect(screen.getByLabelText(/full name/i)).toBeInTheDocument();
  });

  it("reports the typed value, not the event", async () => {
    const onChange = vi.fn();
    render(<TextField id="name" label="Full name" value="" onChange={onChange} />);

    await userEvent.type(screen.getByLabelText(/full name/i), "A");
    expect(onChange).toHaveBeenCalledWith("A");
  });

  it("marks fields as optional unless required", () => {
    render(<TextField id="gstin" label="GSTIN" value="" onChange={() => {}} />);
    expect(screen.getByText("(optional)")).toBeInTheDocument();
  });

  it("omits the optional marker on a required field", () => {
    render(
      <TextField id="name" label="Full name" value="" onChange={() => {}} required />,
    );
    expect(screen.queryByText("(optional)")).not.toBeInTheDocument();
    expect(screen.getByLabelText(/full name/i)).toBeRequired();
  });

  it("defaults to a text input", () => {
    render(<TextField id="name" label="Full name" value="" onChange={() => {}} />);
    expect(screen.getByLabelText(/full name/i)).toHaveAttribute("type", "text");
  });

  it.each(["email", "tel", "password", "number"] as const)(
    "renders a %s input when asked",
    (type) => {
      render(
        <TextField id="field" label="Field" value="" onChange={() => {}} type={type} />,
      );
      // A password input has no queryable role, so it is found by label.
      expect(screen.getByLabelText(/^Field/)).toHaveAttribute("type", type);
    },
  );

  it("renders a hint when given", () => {
    render(
      <TextField
        id="phone"
        label="Phone"
        value=""
        onChange={() => {}}
        hint="Include the country code."
      />,
    );
    expect(screen.getByText("Include the country code.")).toBeInTheDocument();
  });

  it("does not accept input while disabled", async () => {
    const onChange = vi.fn();
    render(
      <TextField id="name" label="Full name" value="" onChange={onChange} disabled />,
    );

    const input = screen.getByLabelText(/full name/i);
    expect(input).toBeDisabled();
    await userEvent.type(input, "hello");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("passes through placeholder, autoComplete and numeric bounds", () => {
    render(
      <TextField
        id="count"
        label="Count"
        value=""
        onChange={() => {}}
        type="number"
        placeholder="0"
        autoComplete="off"
        min={1}
        max={9}
      />,
    );
    const input = screen.getByLabelText(/^Count/);
    expect(input).toHaveAttribute("placeholder", "0");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(input).toHaveAttribute("min", "1");
    expect(input).toHaveAttribute("max", "9");
  });
});

describe("TextareaField", () => {
  it("associates the label with the textarea", () => {
    render(
      <TextareaField id="notes" label="Notes" value="" onChange={() => {}} />,
    );
    expect(screen.getByLabelText(/notes/i)).toBeInTheDocument();
  });

  it("reports the typed value", async () => {
    const onChange = vi.fn();
    render(<TextareaField id="notes" label="Notes" value="" onChange={onChange} />);

    await userEvent.type(screen.getByLabelText(/notes/i), "x");
    expect(onChange).toHaveBeenCalledWith("x");
  });

  it("defaults to three rows and honours an override", () => {
    const { rerender } = render(
      <TextareaField id="notes" label="Notes" value="" onChange={() => {}} />,
    );
    expect(screen.getByLabelText(/notes/i)).toHaveAttribute("rows", "3");

    rerender(
      <TextareaField id="notes" label="Notes" value="" onChange={() => {}} rows={8} />,
    );
    expect(screen.getByLabelText(/notes/i)).toHaveAttribute("rows", "8");
  });

  it("marks the field optional unless required", () => {
    render(<TextareaField id="notes" label="Notes" value="" onChange={() => {}} />);
    expect(screen.getByText("(optional)")).toBeInTheDocument();
  });

  it("renders a hint and can be disabled", () => {
    render(
      <TextareaField
        id="notes"
        label="Notes"
        value=""
        onChange={() => {}}
        hint="Visible to your team only."
        disabled
      />,
    );
    expect(screen.getByText("Visible to your team only.")).toBeInTheDocument();
    expect(screen.getByLabelText(/notes/i)).toBeDisabled();
  });
});

describe("SelectField", () => {
  const OPTIONS = [
    { value: "owner", label: "Owner" },
    { value: "viewer", label: "Viewer" },
  ] as const;

  it("renders every option and reflects the current value", () => {
    render(
      <SelectField
        id="role"
        label="Role"
        value="viewer"
        options={OPTIONS}
        onChange={() => {}}
      />,
    );
    expect(screen.getByLabelText(/^Role/)).toHaveValue("viewer");
    expect(screen.getAllByRole("option")).toHaveLength(2);
  });

  it("reports the selected value", async () => {
    const onChange = vi.fn();
    render(
      <SelectField
        id="role"
        label="Role"
        value="viewer"
        options={OPTIONS}
        onChange={onChange}
      />,
    );

    await userEvent.selectOptions(screen.getByLabelText(/^Role/), "owner");
    expect(onChange).toHaveBeenCalledWith("owner");
  });

  it("renders a hint and can be disabled", () => {
    render(
      <SelectField
        id="role"
        label="Role"
        value="owner"
        options={OPTIONS}
        onChange={() => {}}
        hint="Owners can manage billing."
        disabled
      />,
    );
    expect(screen.getByText("Owners can manage billing.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Role/)).toBeDisabled();
  });
});

describe("Button", () => {
  it("defaults to a non-submitting button so it cannot post a form by accident", () => {
    render(<Button>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute(
      "type",
      "button",
    );
  });

  it("can be a submit button", () => {
    render(<Button type="submit">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveAttribute(
      "type",
      "submit",
    );
  });

  it("invokes onClick", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Save</Button>);

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("does not invoke onClick while disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button onClick={onClick} disabled>
        Save
      </Button>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).not.toHaveBeenCalled();
  });

  it.each([
    ["primary", "bg-brand-600"],
    ["secondary", "border-ink-200"],
    ["danger", "text-rose-700"],
  ] as const)("renders the %s variant", (variant, expectedClass) => {
    render(<Button variant={variant}>Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveClass(
      expectedClass,
    );
  });

  it("renders a small size when asked", () => {
    render(<Button size="sm">Save</Button>);
    expect(screen.getByRole("button", { name: "Save" })).toHaveClass("text-xs");
  });
});

describe("notices", () => {
  it("announces a success politely via role=status", () => {
    render(<SuccessNotice message="Saved." />);
    expect(screen.getByRole("status")).toHaveTextContent("Saved.");
  });

  it("announces an error assertively via role=alert", () => {
    render(<FormError message="That did not work." />);
    expect(screen.getByRole("alert")).toHaveTextContent("That did not work.");
  });
});
