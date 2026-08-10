import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { tokens } from "./api";
import type { StreamFrame } from "./types";
import { useLiveStream } from "./use-live-stream";

/** Header a real backend would use to close a socket carrying a rejected token. */
const POLICY_VIOLATION = 1008;

class MockSocket {
  static instances: MockSocket[] = [];

  url: string;
  closed = false;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockSocket.instances.push(this);
  }

  close(): void {
    this.closed = true;
  }

  open(): void {
    this.onopen?.();
  }

  message(frame: unknown): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  raw(data: string): void {
    this.onmessage?.({ data });
  }

  disconnect(code: number): void {
    this.onclose?.({ code });
  }
}

function latestSocket(): MockSocket {
  const socket = MockSocket.instances.at(-1);
  if (!socket) throw new Error("no socket was constructed");
  return socket;
}

function tokenPair() {
  return {
    access_token: "access-1",
    refresh_token: "refresh-1",
    token_type: "bearer" as const,
    expires_in: 900,
    expires_at: new Date(Date.now() + 900_000).toISOString(),
  };
}

describe("useLiveStream", () => {
  beforeEach(() => {
    window.localStorage.clear();
    MockSocket.instances = [];
    vi.stubGlobal("WebSocket", MockSocket);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("reports unauthorized and never opens a socket without an access token", () => {
    const { result } = renderHook(() => useLiveStream(() => {}));

    expect(result.current.status).toBe("unauthorized");
    expect(MockSocket.instances).toHaveLength(0);
  });

  it("opens a socket carrying the token in the query string", () => {
    tokens.save(tokenPair());
    const { result } = renderHook(() => useLiveStream(() => {}));

    expect(MockSocket.instances).toHaveLength(1);
    expect(latestSocket().url).toContain("token=access-1");

    act(() => latestSocket().open());
    expect(result.current.status).toBe("open");
  });

  it("delivers parsed frames to the handler without re-subscribing on every render", () => {
    tokens.save(tokenPair());
    const onFrame = vi.fn();
    const { rerender } = renderHook(
      ({ handler }) => useLiveStream(handler),
      { initialProps: { handler: onFrame } },
    );

    const frame: StreamFrame = { type: "ping", at: "2026-01-01T00:00:00Z" };
    act(() => latestSocket().message(frame));
    expect(onFrame).toHaveBeenCalledWith(frame);

    // A fresh inline callback on re-render must not tear the socket down.
    rerender({ handler: vi.fn() });
    expect(MockSocket.instances).toHaveLength(1);
    expect(latestSocket().closed).toBe(false);
  });

  it("ignores a frame that is not valid JSON instead of dropping the connection", () => {
    tokens.save(tokenPair());
    const onFrame = vi.fn();
    const { result } = renderHook(() => useLiveStream(onFrame));

    act(() => latestSocket().open());
    expect(() => act(() => latestSocket().raw("not json"))).not.toThrow();
    expect(onFrame).not.toHaveBeenCalled();
    expect(result.current.status).toBe("open");
  });

  it("does not retry after a policy-violation close (rejected or expired token)", () => {
    tokens.save(tokenPair());
    const { result } = renderHook(() => useLiveStream(() => {}));

    act(() => latestSocket().disconnect(POLICY_VIOLATION));
    expect(result.current.status).toBe("unauthorized");

    act(() => vi.advanceTimersByTime(60_000));
    expect(MockSocket.instances).toHaveLength(1);
  });

  it("reconnects with exponential backoff after a non-policy close", () => {
    tokens.save(tokenPair());
    const { result } = renderHook(() => useLiveStream(() => {}));
    expect(MockSocket.instances).toHaveLength(1);

    act(() => latestSocket().disconnect(1006));
    expect(result.current.status).toBe("closed");

    // First retry waits ~1s; nothing happens before then.
    act(() => vi.advanceTimersByTime(999));
    expect(MockSocket.instances).toHaveLength(1);
    act(() => vi.advanceTimersByTime(2));
    expect(MockSocket.instances).toHaveLength(2);

    // Second failure waits longer (~2s) — doubling, not a flat retry.
    act(() => latestSocket().disconnect(1006));
    act(() => vi.advanceTimersByTime(1500));
    expect(MockSocket.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1000));
    expect(MockSocket.instances).toHaveLength(3);
  });

  it("caps the backoff delay instead of growing it forever", () => {
    tokens.save(tokenPair());
    renderHook(() => useLiveStream(() => {}));

    // Fail enough times that an uncapped doubling would be far past the cap.
    for (let i = 0; i < 8; i += 1) {
      act(() => latestSocket().disconnect(1006));
      act(() => vi.advanceTimersByTime(15_000));
    }

    const countBefore = MockSocket.instances.length;
    act(() => latestSocket().disconnect(1006));
    // Just under the documented 15s cap: no reconnect yet.
    act(() => vi.advanceTimersByTime(14_999));
    expect(MockSocket.instances).toHaveLength(countBefore);
    act(() => vi.advanceTimersByTime(1));
    expect(MockSocket.instances).toHaveLength(countBefore + 1);
  });

  it("resets the backoff counter after a successful reconnect", () => {
    tokens.save(tokenPair());
    renderHook(() => useLiveStream(() => {}));

    act(() => latestSocket().disconnect(1006));
    act(() => vi.advanceTimersByTime(1000));
    expect(MockSocket.instances).toHaveLength(2);

    // A clean open before the next failure means the next retry starts over
    // at the base delay rather than continuing to escalate.
    act(() => latestSocket().open());
    act(() => latestSocket().disconnect(1006));
    act(() => vi.advanceTimersByTime(999));
    expect(MockSocket.instances).toHaveLength(2);
    act(() => vi.advanceTimersByTime(2));
    expect(MockSocket.instances).toHaveLength(3);
  });

  it("closes a live socket on unmount", () => {
    tokens.save(tokenPair());
    const { unmount } = renderHook(() => useLiveStream(() => {}));
    const socket = latestSocket();

    act(() => socket.open());
    unmount();
    expect(socket.closed).toBe(true);
  });

  it("cancels a pending retry on unmount so it never reconnects", () => {
    tokens.save(tokenPair());
    const { unmount } = renderHook(() => useLiveStream(() => {}));

    act(() => latestSocket().disconnect(1006));
    unmount();

    act(() => vi.advanceTimersByTime(60_000));
    expect(MockSocket.instances).toHaveLength(1);
  });

  it("tears the socket down when disabled and reconnects when re-enabled", () => {
    tokens.save(tokenPair());
    const { rerender } = renderHook(
      ({ enabled }) => useLiveStream(() => {}, { enabled }),
      { initialProps: { enabled: true } },
    );
    const first = latestSocket();
    expect(first.closed).toBe(false);

    rerender({ enabled: false });
    expect(first.closed).toBe(true);
    expect(MockSocket.instances).toHaveLength(1);

    rerender({ enabled: true });
    expect(MockSocket.instances).toHaveLength(2);
  });

  it("scopes the stream to one call via the call_id query param", () => {
    tokens.save(tokenPair());
    renderHook(() => useLiveStream(() => {}, { callId: "call-42" }));

    expect(latestSocket().url).toContain("call_id=call-42");
  });

  it("stays unauthorized without opening a socket if the token disappears between renders", () => {
    tokens.save(tokenPair());
    const { rerender, result } = renderHook(
      ({ callId }: { callId?: string }) => useLiveStream(() => {}, { callId }),
      { initialProps: { callId: undefined as string | undefined } },
    );
    expect(MockSocket.instances).toHaveLength(1);

    tokens.clear();
    act(() => rerender({ callId: "call-7" }));

    expect(result.current.status).toBe("unauthorized");
    expect(MockSocket.instances).toHaveLength(1);
  });
});
