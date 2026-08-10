"use client";

/**
 * Catches errors thrown by the root layout itself (e.g. `SessionProvider`),
 * which `error.tsx` cannot — that boundary lives *inside* the layout it would
 * need to replace. Next.js requires this file to render its own `<html>` and
 * `<body>` since it stands in for the entire root layout when it fires.
 */

import "./globals.css";

export default function GlobalError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  return (
    <html lang="en">
      <body className="min-h-full antialiased">
        <main className="flex min-h-screen items-center justify-center px-4">
          <div className="w-full max-w-sm rounded-xl border border-ink-200 bg-white p-6 text-center shadow-sm">
            <p className="text-sm font-semibold text-ink-900">
              VoiceDesk hit a snag
            </p>
            <p className="mt-1 text-sm text-ink-500">
              Reloading usually fixes this. If it keeps happening, contact
              support.
            </p>
            <button
              type="button"
              onClick={reset}
              className="mt-4 rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700"
            >
              Reload
            </button>
          </div>
        </main>
      </body>
    </html>
  );
}
