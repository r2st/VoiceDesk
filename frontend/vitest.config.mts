import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: { url: "http://localhost:3011" },
    },
    globals: true,
    setupFiles: ["./vitest.setup.ts"],
    coverage: {
      provider: "v8",
      // `all` counts files no test imports. Without it an untested screen is
      // simply absent from the report, so coverage reads high while whole
      // pages go unexercised.
      all: true,
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        // Type-only; erased at compile time, so there is nothing to execute.
        "src/lib/types.ts",
        // Framework glue with no branches of our own: the root layout and the
        // route entry that redirects into the dashboard.
        "src/app/layout.tsx",
        "src/app/page.tsx",
      ],
      reporter: ["text", "html"],
      // A ratchet, not the goal. `all: true` above revealed the real number to
      // be ~19% — the screens under src/app are entirely untested — where the
      // previous report showed 68% by silently counting only files a test had
      // already imported. These floors are set just under the current measured
      // coverage so a regression fails loudly; raise them toward 80 as the
      // page and hook tests land.
      thresholds: {
        statements: 19,
        branches: 13,
        functions: 15,
        lines: 19,
      },
    },
  },
});
