"use client";

import { useEffect } from "react";

/**
 * Catches render-time exceptions anywhere under the root layout that a page's
 * own data-fetch error handling (`useApi` + `ErrorNotice`) can't — a bug in a
 * component's render path, not a failed API call. Without this, React would
 * unmount the whole tree and the user would see a blank page.
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-xl border border-ink-200 bg-white p-6 text-center shadow-sm">
        <p className="text-sm font-semibold text-ink-900">Something went wrong</p>
        <p className="mt-1 text-sm text-ink-500">
          This page hit an unexpected error. You can try again, or head back to
          the dashboard.
        </p>
        <div className="mt-4 flex justify-center gap-2">
          <button
            type="button"
            onClick={reset}
            className="rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700"
          >
            Try again
          </button>
          <a
            href="/dashboard"
            className="rounded-md border border-ink-200 px-3 py-1.5 text-sm font-medium text-ink-700 hover:bg-ink-50"
          >
            Go to dashboard
          </a>
        </div>
      </div>
    </main>
  );
}
