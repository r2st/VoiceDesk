import Link from "next/link";

export default function NotFound() {
  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-xl border border-ink-200 bg-white p-6 text-center shadow-sm">
        <p className="text-sm font-semibold text-ink-900">Page not found</p>
        <p className="mt-1 text-sm text-ink-500">
          The page you&apos;re looking for doesn&apos;t exist or has moved.
        </p>
        <Link
          href="/dashboard"
          className="mt-4 inline-block rounded-md bg-brand-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-brand-700"
        >
          Go to dashboard
        </Link>
      </div>
    </main>
  );
}
