"use client";

import { useState, type FormEvent } from "react";

import { ApiError, api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { dateTime, titleCase } from "@/lib/format";
import {
  Button,
  FormError,
  SelectField,
  SuccessNotice,
  TextField,
} from "@/components/form";
import { Badge, Card, CardHeader, ErrorNotice, Spinner } from "@/components/ui";
import type { User, UserRole } from "@/lib/types";

const ROLES: ReadonlyArray<{ value: UserRole; label: string }> = [
  { value: "owner", label: "Owner — full access, including billing" },
  { value: "admin", label: "Admin — configure agents, numbers and team" },
  { value: "supervisor", label: "Supervisor — monitor and take over calls" },
  { value: "viewer", label: "Viewer — read-only" },
];

const ROLE_LABEL: Record<UserRole, string> = {
  owner: "Owner",
  admin: "Admin",
  supervisor: "Supervisor",
  viewer: "Viewer",
};

export function TeamSection({
  currentUserId,
  manageable,
}: {
  currentUserId: string;
  manageable: boolean;
}) {
  const team = useApi(() => api.team.list(), []);
  const [inviting, setInviting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [invited, setInvited] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  /** Wraps a team mutation so every row shares one busy and error channel. */
  async function run(userId: string, action: () => Promise<unknown>) {
    setBusyId(userId);
    setActionError(null);
    try {
      await action();
      team.reload();
    } catch (err: unknown) {
      setActionError(
        err instanceof ApiError ? err.message : "Could not update this member.",
      );
    } finally {
      setBusyId(null);
      setConfirmingId(null);
    }
  }

  const members = team.data ?? [];

  return (
    <Card>
      <CardHeader
        title="Team"
        subtitle={`${members.length} member(s) can sign in to this workspace.`}
        action={
          manageable ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => {
                setInvited(null);
                setInviting((open) => !open);
              }}
            >
              {inviting ? "Cancel" : "Invite member"}
            </Button>
          ) : null
        }
      />

      {inviting ? (
        <InviteForm
          onDone={(email) => {
            // The notice lives out here, not in the form: a successful invite
            // closes the form, and anything it rendered would close with it.
            setInvited(email);
            setInviting(false);
            team.reload();
          }}
        />
      ) : null}

      {invited ? (
        <div className="px-5 pt-4">
          <SuccessNotice message={`Invited ${invited}.`} />
        </div>
      ) : null}

      {team.error ? (
        <div className="px-5 py-4">
          <ErrorNotice message={team.error} onRetry={team.reload} />
        </div>
      ) : team.loading && !team.data ? (
        <div className="px-5 py-6">
          <Spinner label="Loading team" />
        </div>
      ) : (
        <>
          {actionError ? (
            <div className="px-5 pt-4">
              <FormError message={actionError} />
            </div>
          ) : null}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[40rem] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2 font-medium">Member</th>
                  <th className="px-3 py-2 font-medium">Role</th>
                  <th className="px-3 py-2 font-medium">Last sign-in</th>
                  <th className="px-5 py-2 text-right font-medium">Actions</th>
                </tr>
              </thead>
              <tbody>
                {members.map((member) => (
                  <MemberRow
                    key={member.id}
                    member={member}
                    isSelf={member.id === currentUserId}
                    manageable={manageable}
                    busy={busyId === member.id}
                    confirmingRemoval={confirmingId === member.id}
                    onChangeRole={(role) =>
                      run(member.id, () =>
                        api.team.update(member.id, { role }),
                      )
                    }
                    onToggleActive={() =>
                      run(member.id, () =>
                        api.team.update(member.id, {
                          is_active: !member.is_active,
                        }),
                      )
                    }
                    onAskRemove={() => {
                      setActionError(null);
                      setConfirmingId(member.id);
                    }}
                    onCancelRemove={() => setConfirmingId(null)}
                    onRemove={() =>
                      run(member.id, () => api.team.remove(member.id))
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </Card>
  );
}

function MemberRow({
  member,
  isSelf,
  manageable,
  busy,
  confirmingRemoval,
  onChangeRole,
  onToggleActive,
  onAskRemove,
  onCancelRemove,
  onRemove,
}: {
  member: User;
  isSelf: boolean;
  manageable: boolean;
  busy: boolean;
  confirmingRemoval: boolean;
  onChangeRole: (role: UserRole) => void;
  onToggleActive: () => void;
  onAskRemove: () => void;
  onCancelRemove: () => void;
  onRemove: () => void;
}) {
  // Editing your own row is how an admin locks themselves out of their own
  // workspace, so it is off limits from here.
  const editable = manageable && !isSelf;

  return (
    <tr className="border-b border-ink-50 last:border-0">
      <td className="px-5 py-3">
        <div className="flex items-center gap-2">
          <span className="font-medium text-ink-900">{member.full_name}</span>
          {isSelf ? <Badge>You</Badge> : null}
          {member.is_active ? null : <Badge tone="warning">Deactivated</Badge>}
        </div>
        <p className="text-xs text-ink-500">{member.email}</p>
      </td>
      <td className="px-3 py-3">
        {editable ? (
          <select
            aria-label={`Role for ${member.full_name}`}
            value={member.role}
            disabled={busy}
            onChange={(event) => onChangeRole(event.target.value as UserRole)}
            className="rounded-lg border border-ink-200 px-2 py-1 text-sm outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-100 disabled:opacity-60"
          >
            {ROLES.map((role) => (
              <option key={role.value} value={role.value}>
                {ROLE_LABEL[role.value]}
              </option>
            ))}
          </select>
        ) : (
          <span className="text-ink-700">{titleCase(member.role)}</span>
        )}
      </td>
      <td className="px-3 py-3 text-ink-500">
        {member.last_login_at ? dateTime(member.last_login_at) : "Never"}
      </td>
      <td className="px-5 py-3">
        <div className="flex justify-end gap-2">
          {editable ? (
            confirmingRemoval ? (
              <>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={busy}
                  onClick={onRemove}
                >
                  {busy ? "Removing…" : "Confirm removal"}
                </Button>
                <Button variant="secondary" size="sm" onClick={onCancelRemove}>
                  Keep
                </Button>
              </>
            ) : (
              <>
                <Button
                  variant="secondary"
                  size="sm"
                  disabled={busy}
                  onClick={onToggleActive}
                >
                  {member.is_active ? "Deactivate" : "Reactivate"}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={busy}
                  onClick={onAskRemove}
                >
                  Remove
                </Button>
              </>
            )
          ) : (
            <span className="text-xs text-ink-400">
              {isSelf ? "Manage your own account below" : "—"}
            </span>
          )}
        </div>
      </td>
    </tr>
  );
}

function InviteForm({ onDone }: { onDone: (email: string) => void }) {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [phone, setPhone] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<UserRole>("viewer");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      const user = await api.team.invite({
        email: email.trim(),
        full_name: fullName.trim(),
        password,
        role,
        phone: phone.trim() || null,
      });
      onDone(user.email);
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not invite this person.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <form
      onSubmit={onSubmit}
      className="space-y-4 border-b border-ink-100 bg-ink-50 px-5 py-5"
    >
      <div className="grid gap-4 sm:grid-cols-2">
        <TextField
          id="invite-name"
          label="Full name"
          value={fullName}
          onChange={setFullName}
          required
        />
        <TextField
          id="invite-email"
          label="Work email"
          type="email"
          value={email}
          onChange={setEmail}
          required
        />
        <TextField
          id="invite-phone"
          label="Phone"
          type="tel"
          value={phone}
          onChange={setPhone}
        />
        <TextField
          id="invite-password"
          label="Temporary password"
          type="password"
          value={password}
          onChange={setPassword}
          required
          autoComplete="new-password"
          hint="At least 10 characters. Share it out of band; they can change it after signing in."
        />
      </div>
      <SelectField
        id="invite-role"
        label="Role"
        value={role}
        options={ROLES}
        onChange={setRole}
      />

      {error ? <FormError message={error} /> : null}

      <Button type="submit" disabled={saving}>
        {saving ? "Inviting…" : "Send invite"}
      </Button>
    </form>
  );
}
