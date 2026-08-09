"use client";

import { useState, type FormEvent } from "react";

import { ApiError, api } from "@/lib/api";
import { useSession } from "@/lib/auth-context";
import { Button, FormError, TextField } from "@/components/form";
import { Card, CardHeader } from "@/components/ui";

export function PasswordForm() {
  const { signOut } = useSession();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changed, setChanged] = useState(false);

  const mismatch = confirm.length > 0 && confirm !== next;

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    if (mismatch) return;
    setSaving(true);
    setError(null);
    try {
      await api.auth.changePassword(current, next);
      setCurrent("");
      setNext("");
      setConfirm("");
      setChanged(true);
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not change the password.",
      );
    } finally {
      setSaving(false);
    }
  }

  // The server revokes every refresh token on a password change, so this
  // session is already half dead. Saying so and offering the door beats letting
  // the next request fail on its own.
  if (changed) {
    return (
      <Card>
        <CardHeader title="Password" />
        <div className="space-y-4 px-5 py-5">
          <p className="text-sm text-ink-700">
            Password changed. Every other device signed in as you has been
            signed out.
          </p>
          <Button onClick={() => void signOut()}>Sign in again</Button>
        </div>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Password"
        subtitle="Changing it signs out every device, including this one."
      />
      <form onSubmit={onSubmit} className="space-y-4 px-5 py-5">
        <TextField
          id="current-password"
          label="Current password"
          type="password"
          value={current}
          onChange={setCurrent}
          required
          autoComplete="current-password"
        />
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            id="new-password"
            label="New password"
            type="password"
            value={next}
            onChange={setNext}
            required
            autoComplete="new-password"
            hint="At least 10 characters, and different from the current one."
          />
          <TextField
            id="confirm-password"
            label="Confirm new password"
            type="password"
            value={confirm}
            onChange={setConfirm}
            required
            autoComplete="new-password"
          />
        </div>

        {mismatch ? <FormError message="The two passwords do not match." /> : null}
        {error ? <FormError message={error} /> : null}

        <Button
          type="submit"
          disabled={saving || mismatch || next.length === 0}
        >
          {saving ? "Changing…" : "Change password"}
        </Button>
      </form>
    </Card>
  );
}
