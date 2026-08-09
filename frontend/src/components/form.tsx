"use client";

/**
 * Form primitives.
 *
 * The dashboard is mostly read-only, so these live apart from `ui.tsx` and are
 * only pulled in by the screens that actually write. Every field renders a real
 * `<label htmlFor>` — the settings screen is the one place a keyboard or screen
 * reader user has to fill things in.
 */

import type { ReactNode } from "react";

const CONTROL =
  "w-full rounded-lg border border-ink-200 px-3 py-2 text-sm outline-none transition focus:border-brand-500 focus:ring-2 focus:ring-brand-100 disabled:cursor-not-allowed disabled:bg-ink-50 disabled:text-ink-500";

export function TextField({
  id,
  label,
  value,
  onChange,
  hint,
  type = "text",
  placeholder,
  required = false,
  disabled = false,
  autoComplete,
}: {
  id: string;
  label: string;
  value: string;
  onChange: (value: string) => void;
  hint?: string;
  type?: "text" | "email" | "tel" | "password";
  placeholder?: string;
  required?: boolean;
  disabled?: boolean;
  autoComplete?: string;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-ink-700">
        {label}
        {required ? null : (
          <span className="ml-1 font-normal text-ink-400">(optional)</span>
        )}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        required={required}
        disabled={disabled}
        autoComplete={autoComplete}
        onChange={(event) => onChange(event.target.value)}
        className={`mt-1.5 ${CONTROL}`}
      />
      {hint ? <p className="mt-1 text-xs text-ink-400">{hint}</p> : null}
    </div>
  );
}

export function SelectField<T extends string>({
  id,
  label,
  value,
  options,
  onChange,
  hint,
  disabled = false,
}: {
  id: string;
  label: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (value: T) => void;
  hint?: string;
  disabled?: boolean;
}) {
  return (
    <div>
      <label htmlFor={id} className="block text-sm font-medium text-ink-700">
        {label}
      </label>
      <select
        id={id}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value as T)}
        className={`mt-1.5 ${CONTROL}`}
      >
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
      {hint ? <p className="mt-1 text-xs text-ink-400">{hint}</p> : null}
    </div>
  );
}

const BUTTON_VARIANTS = {
  primary:
    "bg-brand-600 text-white hover:bg-brand-700 disabled:hover:bg-brand-600",
  secondary:
    "border border-ink-200 bg-white text-ink-700 hover:bg-ink-50 disabled:hover:bg-white",
  danger:
    "border border-rose-200 bg-white text-rose-700 hover:bg-rose-50 disabled:hover:bg-white",
} as const;

export function Button({
  children,
  onClick,
  type = "button",
  variant = "primary",
  disabled = false,
  size = "md",
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  variant?: keyof typeof BUTTON_VARIANTS;
  disabled?: boolean;
  size?: "sm" | "md";
}) {
  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      className={`rounded-lg font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${
        size === "sm" ? "px-2.5 py-1 text-xs" : "px-4 py-2 text-sm"
      } ${BUTTON_VARIANTS[variant]}`}
    >
      {children}
    </button>
  );
}

/** Confirmation for a write that succeeded, cleared by the next attempt. */
export function SuccessNotice({ message }: { message: string }) {
  return (
    <p
      role="status"
      className="rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800"
    >
      {message}
    </p>
  );
}

export function FormError({ message }: { message: string }) {
  return (
    <p
      role="alert"
      className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800"
    >
      {message}
    </p>
  );
}
