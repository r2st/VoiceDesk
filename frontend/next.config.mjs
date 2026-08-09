import { fileURLToPath } from "node:url";
import { dirname } from "node:path";

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // Pin the workspace root. Without it Turbopack walks up past the repository
  // and picks up an unrelated lockfile from the home directory.
  turbopack: {
    root: dirname(fileURLToPath(import.meta.url)),
  },
  // The dashboard talks to the API directly from the browser with a bearer
  // token, so there is no server-side proxy to configure here.
};

export default nextConfig;
