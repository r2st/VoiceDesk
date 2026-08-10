"use client";

import { useState, type FormEvent } from "react";

import { ApiError, api } from "@/lib/api";
import { useApi } from "@/lib/use-api";
import { titleCase } from "@/lib/format";
import {
  Button,
  FormError,
  SelectField,
  TextareaField,
  TextField,
} from "@/components/form";
import { Badge, Card, CardHeader, ErrorNotice, Spinner } from "@/components/ui";
import type { Intent, IntentActionType, VoiceAgent } from "@/lib/types";

const ACTION_TYPES: ReadonlyArray<{ value: IntentActionType; label: string }> = [
  { value: "none", label: "No action — recognised, not acted on" },
  { value: "book_appointment", label: "Book appointment" },
  { value: "check_order_status", label: "Check order status" },
  { value: "collect_payment", label: "Collect payment" },
  { value: "qualify_lead", label: "Qualify lead" },
  { value: "transfer_human", label: "Transfer to a human" },
  { value: "whatsapp_handoff", label: "Hand off to WhatsApp" },
  { value: "webhook", label: "Call a webhook" },
  { value: "end_call", label: "End the call" },
];

const ALL_AGENTS = "";

export function IntentsSection({
  agents,
  canManage,
}: {
  agents: VoiceAgent[];
  canManage: boolean;
}) {
  const intents = useApi(() => api.intents.list(), []);
  const [creating, setCreating] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  const agentNames = new Map(agents.map((agent) => [agent.id, agent.name]));

  async function run(intentId: string, action: () => Promise<unknown>) {
    setBusyId(intentId);
    setActionError(null);
    try {
      await action();
      intents.reload();
    } catch (err: unknown) {
      setActionError(
        err instanceof ApiError ? err.message : "Could not update this intent.",
      );
    } finally {
      setBusyId(null);
      setConfirmingId(null);
    }
  }

  const rows = [...(intents.data ?? [])].sort((a, b) => a.priority - b.priority);

  return (
    <Card className="mt-6">
      <CardHeader
        title="Caller intents"
        subtitle="What your agents listen for, and what they do about it."
        action={
          canManage ? (
            <Button
              variant="secondary"
              size="sm"
              onClick={() => setCreating((open) => !open)}
            >
              {creating ? "Cancel" : "Add intent"}
            </Button>
          ) : null
        }
      />

      {creating ? (
        <CreateForm
          agents={agents}
          onDone={() => {
            setCreating(false);
            intents.reload();
          }}
        />
      ) : null}

      {intents.error ? (
        <div className="px-5 py-4">
          <ErrorNotice message={intents.error} onRetry={intents.reload} />
        </div>
      ) : intents.loading && !intents.data ? (
        <div className="px-5 py-6">
          <Spinner label="Loading intents" />
        </div>
      ) : rows.length === 0 ? (
        <div className="px-5 py-8 text-center">
          <p className="text-sm font-medium text-ink-700">No intents defined</p>
          <p className="mx-auto mt-1 max-w-sm text-sm text-ink-500">
            An intent tells an agent what a caller is asking for, and what to do
            about it. Add one to get started.
          </p>
        </div>
      ) : (
        <>
          {actionError ? (
            <div className="px-5 pt-4">
              <FormError message={actionError} />
            </div>
          ) : null}
          <div className="overflow-x-auto">
            <table className="w-full min-w-[44rem] text-sm">
              <thead>
                <tr className="border-b border-ink-100 text-left text-xs uppercase tracking-wide text-ink-500">
                  <th className="px-5 py-2 font-medium">Intent</th>
                  <th className="px-3 py-2 font-medium">Scope</th>
                  <th className="px-3 py-2 font-medium">Action</th>
                  <th className="px-3 py-2 font-medium">Priority</th>
                  <th className="px-3 py-2 font-medium">Status</th>
                  {canManage ? (
                    <th className="px-5 py-2 text-right font-medium">Actions</th>
                  ) : null}
                </tr>
              </thead>
              <tbody>
                {rows.map((intent) => (
                  <IntentRow
                    key={intent.id}
                    intent={intent}
                    scope={
                      intent.agent_id
                        ? (agentNames.get(intent.agent_id) ?? "Unknown agent")
                        : "All agents"
                    }
                    canManage={canManage}
                    busy={busyId === intent.id}
                    confirmingRemoval={confirmingId === intent.id}
                    onToggleActive={() =>
                      run(intent.id, () =>
                        api.intents.setActive(intent.id, !intent.is_active),
                      )
                    }
                    onAskRemove={() => {
                      setActionError(null);
                      setConfirmingId(intent.id);
                    }}
                    onCancelRemove={() => setConfirmingId(null)}
                    onRemove={() =>
                      run(intent.id, () => api.intents.remove(intent.id))
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

function IntentRow({
  intent,
  scope,
  canManage,
  busy,
  confirmingRemoval,
  onToggleActive,
  onAskRemove,
  onCancelRemove,
  onRemove,
}: {
  intent: Intent;
  scope: string;
  canManage: boolean;
  busy: boolean;
  confirmingRemoval: boolean;
  onToggleActive: () => void;
  onAskRemove: () => void;
  onCancelRemove: () => void;
  onRemove: () => void;
}) {
  return (
    <tr className="border-b border-ink-50 last:border-0">
      <td className="px-5 py-3">
        <span className="font-medium text-ink-900">{intent.name}</span>
        {intent.description ? (
          <p className="mt-0.5 max-w-xs truncate text-xs text-ink-500">
            {intent.description}
          </p>
        ) : null}
      </td>
      <td className="px-3 py-3 text-ink-600">{scope}</td>
      <td className="px-3 py-3 text-ink-600">{titleCase(intent.action_type)}</td>
      <td className="tnum px-3 py-3 text-ink-600">{intent.priority}</td>
      <td className="px-3 py-3">
        <Badge tone={intent.is_active ? "success" : "neutral"}>
          {intent.is_active ? "Active" : "Inactive"}
        </Badge>
      </td>
      {canManage ? (
        <td className="px-5 py-3">
          <div className="flex justify-end gap-2">
            {confirmingRemoval ? (
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
                  {intent.is_active ? "Deactivate" : "Activate"}
                </Button>
                <Button
                  variant="danger"
                  size="sm"
                  disabled={busy}
                  onClick={onAskRemove}
                >
                  Delete
                </Button>
              </>
            )}
          </div>
        </td>
      ) : null}
    </tr>
  );
}

function CreateForm({
  agents,
  onDone,
}: {
  agents: VoiceAgent[];
  onDone: () => void;
}) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [samplePhrases, setSamplePhrases] = useState("");
  const [actionType, setActionType] = useState<IntentActionType>("none");
  const [agentId, setAgentId] = useState(ALL_AGENTS);
  const [priority, setPriority] = useState("100");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    try {
      await api.intents.create({
        name: name.trim(),
        description: description.trim(),
        sample_phrases: samplePhrases
          .split("\n")
          .map((phrase) => phrase.trim())
          .filter(Boolean),
        action_type: actionType,
        agent_id: agentId || null,
        priority: Number(priority) || 100,
      });
      onDone();
    } catch (err: unknown) {
      setError(
        err instanceof ApiError ? err.message : "Could not create this intent.",
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
          id="intent-name"
          label="Name"
          value={name}
          onChange={setName}
          placeholder="check_order_status"
          hint="Lowercase letters, digits and underscores, starting with a letter."
          required
        />
        <SelectField
          id="intent-agent"
          label="Applies to"
          value={agentId}
          options={[
            { value: ALL_AGENTS, label: "All agents (business-wide)" },
            ...agents.map((agent) => ({ value: agent.id, label: agent.name })),
          ]}
          onChange={setAgentId}
        />
      </div>

      <TextareaField
        id="intent-description"
        label="Description"
        value={description}
        onChange={setDescription}
        placeholder="What a caller means when this intent fires."
      />

      <TextareaField
        id="intent-phrases"
        label="Sample phrases"
        value={samplePhrases}
        onChange={setSamplePhrases}
        placeholder={"Where is my order?\nHas my order shipped yet?"}
        hint="One phrase per line. Helps the agent recognise the intent."
      />

      <div className="grid gap-4 sm:grid-cols-2">
        <SelectField
          id="intent-action"
          label="Action"
          value={actionType}
          options={ACTION_TYPES}
          onChange={setActionType}
        />
        <TextField
          id="intent-priority"
          label="Priority"
          type="number"
          value={priority}
          onChange={setPriority}
          min={1}
          max={1000}
          hint="Lower numbers are matched first."
        />
      </div>

      {error ? <FormError message={error} /> : null}

      <Button type="submit" disabled={saving}>
        {saving ? "Adding…" : "Add intent"}
      </Button>
    </form>
  );
}
