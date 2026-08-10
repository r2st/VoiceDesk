import { act, renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { useApi } from "./use-api";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("useApi", () => {
  it("starts loading and resolves with the fetcher's data", async () => {
    const { promise, resolve } = deferred<{ id: string }>();
    const { result } = renderHook(() => useApi(() => promise));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    await act(async () => resolve({ id: "a1" }));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ id: "a1" });
    expect(result.current.error).toBeNull();
  });

  it("surfaces an ApiError's message", async () => {
    const fetcher = () =>
      Promise.reject(new ApiError(404, "not_found", "Call not found."));
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Call not found.");
    expect(result.current.data).toBeNull();
  });

  it("falls back to a generic message for a non-ApiError failure", async () => {
    const fetcher = () => Promise.reject(new TypeError("network down"));
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBe("Could not reach the VoiceDesk API.");
  });

  it("discards a response from a superseded request", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { result, rerender } = renderHook(({ dep }) => useApi(fetcher, [dep]), {
      initialProps: { dep: 1 },
    });

    rerender({ dep: 2 });
    expect(fetcher).toHaveBeenCalledTimes(2);

    // The newer request settles first, then the stale first request resolves
    // late — it must not overwrite the fresher data.
    await act(async () => second.resolve("fresh"));
    await waitFor(() => expect(result.current.data).toBe("fresh"));

    await act(async () => first.resolve("stale"));
    expect(result.current.data).toBe("fresh");
  });

  it("ignores a stale response even when it turns out to be an error", async () => {
    const first = deferred<string>();
    const second = deferred<string>();
    const fetcher = vi.fn().mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { result, rerender } = renderHook(({ dep }) => useApi(fetcher, [dep]), {
      initialProps: { dep: 1 },
    });
    rerender({ dep: 2 });

    await act(async () => second.resolve("fresh"));
    await waitFor(() => expect(result.current.data).toBe("fresh"));

    await act(async () => first.reject(new Error("late failure")));
    expect(result.current.data).toBe("fresh");
    expect(result.current.error).toBeNull();
  });

  it("refetches on reload without changing deps", async () => {
    const fetcher = vi.fn().mockResolvedValueOnce("v1").mockResolvedValueOnce("v2");
    const { result } = renderHook(() => useApi(fetcher));

    await waitFor(() => expect(result.current.data).toBe("v1"));

    act(() => result.current.reload());

    await waitFor(() => expect(result.current.data).toBe("v2"));
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
