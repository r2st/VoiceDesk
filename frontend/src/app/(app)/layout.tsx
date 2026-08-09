"use client";

import { useEffect } from "react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import { useSession } from "@/lib/auth-context";
import { Badge, Spinner, type BadgeTone } from "@/components/ui";
import { titleCase } from "@/lib/format";

const NAV = [
  { href: "/dashboard", label: "Overview" },
  { href: "/monitor", label: "Live" },
  { href: "/calls", label: "Calls" },
  { href: "/agents", label: "Agents" },
  { href: "/numbers", label: "Numbers" },
  { href: "/billing", label: "Billing" },
];

const STATUS_TONE: Record<string, BadgeTone> = {
  active: "success",
  trial: "info",
  suspended: "danger",
  cancelled: "danger",
};

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const { user, business, loading, signOut } = useSession();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [user, loading, router]);

  if (loading || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Spinner label="Loading your workspace" />
      </div>
    );
  }

  const suspended =
    business?.status === "suspended" || business?.status === "cancelled";

  return (
    <div className="flex min-h-screen">
      <aside className="hidden w-60 shrink-0 border-r border-ink-200 bg-white lg:block">
        <div className="flex h-16 items-center gap-2.5 border-b border-ink-100 px-5">
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
            V
          </span>
          <span className="text-sm font-semibold text-ink-900">VoiceDesk</span>
        </div>

        <nav className="p-3">
          {NAV.map((item) => {
            const active = pathname === item.href || pathname.startsWith(`${item.href}/`);
            return (
              <Link
                key={item.href}
                href={item.href}
                className={`mb-0.5 block rounded-lg px-3 py-2 text-sm font-medium transition ${
                  active
                    ? "bg-brand-50 text-brand-700"
                    : "text-ink-600 hover:bg-ink-50 hover:text-ink-900"
                }`}
              >
                {item.label}
              </Link>
            );
          })}
        </nav>

        {business ? (
          <div className="mx-3 mt-2 rounded-lg border border-ink-100 bg-ink-50 px-3 py-2.5">
            <p className="truncate text-xs font-medium text-ink-900">
              {business.name}
            </p>
            <div className="mt-1.5 flex items-center gap-1.5">
              <Badge tone={STATUS_TONE[business.status] ?? "neutral"}>
                {titleCase(business.status)}
              </Badge>
              <span className="text-xs text-ink-500">
                {titleCase(business.plan)}
              </span>
            </div>
          </div>
        ) : null}
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-16 items-center justify-between border-b border-ink-200 bg-white px-5">
          <nav className="flex gap-1 lg:hidden">
            {NAV.map((item) => (
              <Link
                key={item.href}
                href={item.href}
                className={`rounded-md px-2 py-1 text-xs font-medium ${
                  pathname.startsWith(item.href)
                    ? "bg-brand-50 text-brand-700"
                    : "text-ink-600"
                }`}
              >
                {item.label}
              </Link>
            ))}
          </nav>
          <div className="hidden lg:block" />

          <div className="flex items-center gap-3">
            <div className="text-right">
              <p className="text-sm font-medium text-ink-900">
                {user.full_name}
              </p>
              <p className="text-xs text-ink-500">{titleCase(user.role)}</p>
            </div>
            <button
              type="button"
              onClick={() => void signOut()}
              className="rounded-lg border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 transition hover:bg-ink-50"
            >
              Sign out
            </button>
          </div>
        </header>

        {suspended ? (
          <div className="border-b border-amber-200 bg-amber-50 px-5 py-3 text-sm text-amber-900">
            <strong className="font-semibold">
              Calling is paused on this account.
            </strong>{" "}
            Your agents cannot place or answer calls until billing is settled.
            Reporting and invoices below remain available.
          </div>
        ) : null}

        <main className="flex-1 px-5 py-6">{children}</main>
      </div>
    </div>
  );
}
