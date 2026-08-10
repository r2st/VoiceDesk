import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, request, tokens } from "./api";
import type { TokenPair } from "./types";

function tokenPair(access: string, refresh: string): TokenPair {
  return {
    access_token: access,
    refresh_token: refresh,
    token_type: "bearer",
    expires_in: 900,
    expires_at: new Date(Date.now() + 900_000).toISOString(),
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authHeader(init: RequestInit | undefined): string | undefined {
  return (init?.headers as Record<string, string> | undefined)?.Authorization;
}

describe("api token refresh", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("dedupes concurrent refreshes when multiple requests 401 at once", async () => {
    tokens.save(tokenPair("stale-access", "refresh-1"));

    let refreshCalls = 0;
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);

      if (url.includes("/auth/refresh")) {
        refreshCalls += 1;
        return jsonResponse({ access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      if (url.includes("/auth/me")) {
        if (authHeader(init) === "Bearer stale-access") return jsonResponse({}, 401);
        return jsonResponse({ id: "u1" });
      }
      if (url.includes("/auth/business")) {
        if (authHeader(init) === "Bearer stale-access") return jsonResponse({}, 401);
        return jsonResponse({ id: "b1" });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    // Mirrors auth-context's `load()`, which fires both in parallel — the
    // scenario that used to race two refresh calls against a single-use
    // refresh token and trip the backend's reuse-detection logout.
    const [me, biz] = await Promise.all([request("/auth/me"), request("/auth/business")]);

    expect(me).toEqual({ id: "u1" });
    expect(biz).toEqual({ id: "b1" });
    expect(refreshCalls).toBe(1);
    expect(tokens.access()).toBe("fresh-access");
    expect(tokens.refresh()).toBe("refresh-2");
  });

  it("refreshes once and retries on a single 401", async () => {
    tokens.save(tokenPair("stale-access", "refresh-1"));

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/auth/refresh")) {
        return jsonResponse({ access_token: "fresh-access", refresh_token: "refresh-2" });
      }
      if (url.includes("/leads")) {
        if (authHeader(init) === "Bearer stale-access") return jsonResponse({}, 401);
        return jsonResponse({ items: [] });
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await request("/leads");
    expect(result).toEqual({ items: [] });
    expect(tokens.access()).toBe("fresh-access");
  });

  it("clears tokens and surfaces the original error when refresh fails", async () => {
    tokens.save(tokenPair("stale-access", "dead-refresh"));

    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/auth/refresh")) {
        return jsonResponse({ error: { code: "invalid_token", message: "expired" } }, 401);
      }
      if (url.includes("/leads")) {
        return jsonResponse({ error: { code: "unauthorized", message: "no token" } }, 401);
      }
      throw new Error(`unexpected fetch: ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(request("/leads")).rejects.toBeInstanceOf(ApiError);
    expect(tokens.access()).toBeNull();
    expect(tokens.refresh()).toBeNull();
  });

  it("does not attempt a refresh when there is no refresh token", async () => {
    tokens.clear();

    const fetchMock = vi.fn(async () => jsonResponse({ error: { message: "no token" } }, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(request("/leads")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
