"use client";

import { useSession } from "@/lib/auth-context";
import { Spinner } from "@/components/ui";

import { BusinessForm } from "./business-form";
import { PasswordForm } from "./password-form";
import { TeamSection } from "./team-section";

export default function SettingsPage() {
  const { user, business } = useSession();

  // The layout above already blocks on the session, so a null here only means
  // the profile request failed while `/auth/me` succeeded.
  if (!user || !business) {
    return <Spinner label="Loading settings" />;
  }

  const isAdmin = user.role === "owner" || user.role === "admin";

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      <div>
        <h1 className="text-lg font-semibold text-ink-900">Settings</h1>
        <p className="text-sm text-ink-500">
          Your business details, who can sign in, and your own password.
        </p>
      </div>

      <BusinessForm business={business} editable={isAdmin} />
      <TeamSection currentUserId={user.id} manageable={isAdmin} />
      <PasswordForm />
    </div>
  );
}
