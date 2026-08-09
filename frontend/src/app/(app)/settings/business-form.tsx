"use client";

import { useState, type FormEvent } from "react";

import { ApiError, api } from "@/lib/api";
import { useSession } from "@/lib/auth-context";
import { Button, FormError, SuccessNotice, TextField } from "@/components/form";
import { Card, CardHeader } from "@/components/ui";
import type { Business } from "@/lib/types";

/** The subset of the profile this form owns; plan and status belong to billing. */
type Draft = {
  name: string;
  phone: string;
  industry: string;
  gstin: string;
  address: string;
  city: string;
  state: string;
};

function toDraft(business: Business): Draft {
  return {
    name: business.name,
    phone: business.phone,
    industry: business.industry ?? "",
    gstin: business.gstin ?? "",
    address: business.address ?? "",
    city: business.city ?? "",
    state: business.state ?? "",
  };
}

export function BusinessForm({
  business,
  editable,
}: {
  business: Business;
  editable: boolean;
}) {
  const { refresh } = useSession();
  const [draft, setDraft] = useState<Draft>(() => toDraft(business));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  function set<K extends keyof Draft>(key: K) {
    return (value: string) => {
      setDraft((current) => ({ ...current, [key]: value }));
      setSaved(false);
    };
  }

  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(business));

  async function onSubmit(event: FormEvent) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      // Empty optional fields are sent as null so clearing one actually clears
      // it; "" would be stored as an empty string and read back as filled in.
      await api.auth.updateBusiness({
        name: draft.name,
        phone: draft.phone,
        industry: draft.industry || null,
        gstin: draft.gstin || null,
        address: draft.address || null,
        city: draft.city || null,
        state: draft.state || null,
      });
      await refresh();
      setSaved(true);
    } catch (err: unknown) {
      setError(
        err instanceof ApiError
          ? err.message
          : "Could not save the business profile.",
      );
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader
        title="Business profile"
        subtitle={
          editable
            ? "Appears on invoices and on the TRAI consent announcement."
            : "Only an owner or admin can change these details."
        }
      />
      <form onSubmit={onSubmit} className="space-y-4 px-5 py-5">
        <div className="grid gap-4 sm:grid-cols-2">
          <TextField
            id="business-name"
            label="Business name"
            value={draft.name}
            onChange={set("name")}
            required
            disabled={!editable}
          />
          <TextField
            id="business-phone"
            label="Contact number"
            type="tel"
            value={draft.phone}
            onChange={set("phone")}
            required
            disabled={!editable}
            hint="Indian mobile or landline; stored in E.164."
          />
          <TextField
            id="business-industry"
            label="Industry"
            value={draft.industry}
            onChange={set("industry")}
            placeholder="healthcare"
            disabled={!editable}
          />
          <TextField
            id="business-gstin"
            label="GSTIN"
            value={draft.gstin}
            onChange={set("gstin")}
            placeholder="27AAAAA0000A1Z5"
            disabled={!editable}
            hint="Printed on every invoice once set."
          />
          <TextField
            id="business-city"
            label="City"
            value={draft.city}
            onChange={set("city")}
            disabled={!editable}
          />
          <TextField
            id="business-state"
            label="State"
            value={draft.state}
            onChange={set("state")}
            disabled={!editable}
          />
        </div>
        <TextField
          id="business-address"
          label="Address"
          value={draft.address}
          onChange={set("address")}
          disabled={!editable}
        />

        <dl className="grid gap-4 rounded-lg bg-ink-50 px-4 py-3 text-xs sm:grid-cols-3">
          <ReadOnly label="Business ID" value={business.slug} />
          <ReadOnly label="Billing email" value={business.email} />
          <ReadOnly
            label="Plan"
            value={business.plan}
            hint="Change it from Billing."
          />
        </dl>

        {error ? <FormError message={error} /> : null}
        {saved ? <SuccessNotice message="Business profile saved." /> : null}

        {editable ? (
          <div className="flex items-center gap-3">
            <Button type="submit" disabled={saving || !dirty}>
              {saving ? "Saving…" : "Save changes"}
            </Button>
            {dirty && !saving ? (
              <span className="text-xs text-ink-500">Unsaved changes</span>
            ) : null}
          </div>
        ) : null}
      </form>
    </Card>
  );
}

function ReadOnly({
  label,
  value,
  hint,
}: {
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div>
      <dt className="text-ink-400">{label}</dt>
      <dd className="mt-0.5 font-medium text-ink-700">{value}</dd>
      {hint ? <p className="mt-0.5 text-ink-400">{hint}</p> : null}
    </div>
  );
}
