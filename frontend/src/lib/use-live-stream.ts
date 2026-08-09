"use client";

/**
 * Subscribes to the monitoring WebSocket (design doc §4.3).
 *
 * The socket carries the whole tenant's events; passing a `callId` asks the
 * server to filter to one call and to open with a transcript snapshot.
 *
 * Two details the API forces on us:
 *
 * - The token goes in the query string. The browser WebSocket constructor has
 *   no way to set an `Authorization` header, and the server verifies the token
 *   against the database exactly as it does for a REST call.
 * - The server closes with 1008 for anything auth-related. That is terminal —
 *   reconnecting with the same rejected token would just spin — so only
 *   non-policy closes are retried, with a backoff.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { API_BASE, tokens } from "./api";
import type { StreamFrame } from "./types";

export type StreamStatus = "connecting" | "open" | "closed" | "unauthorized";

/** Close code the server uses for a rejected or expired token. */
const POLICY_VIOLATION = 1008;

const BASE_RETRY_MS = 1000;
const MAX_RETRY_MS = 15000;

function socketUrl(callId?: string): string | null {
  const token = tokens.access();
  if (!token) return null;

  const url = new URL(`${API_BASE}/monitor/stream`, window.location.origin);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.searchParams.set("token", token);
  if (callId) url.searchParams.set("call_id", callId);
  return url.toString();
}

interface Options {
  /** Narrow the stream to one call and receive an opening snapshot. */
  callId?: string;
  /** Set false to tear the socket down, e.g. while a modal is closed. */
  enabled?: boolean;
}

export function useLiveStream(
  onFrame: (frame: StreamFrame) => void,
  { callId, enabled = true }: Options = {},
): { status: StreamStatus } {
  const [status, setStatus] = useState<StreamStatus>("connecting");

  // Held in a ref so a re-render with a new inline callback does not tear down
  // and rebuild the socket — that would drop events on every parent render.
  const handlerRef = useRef(onFrame);
  handlerRef.current = onFrame;

  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const attemptsRef = useRef(0);

  const connect = useCallback(() => {
    const url = socketUrl(callId);
    if (!url) {
      setStatus("unauthorized");
      return;
    }

    setStatus("connecting");
    const socket = new WebSocket(url);
    socketRef.current = socket;

    socket.onopen = () => {
      attemptsRef.current = 0;
      setStatus("open");
    };

    socket.onmessage = (event) => {
      try {
        handlerRef.current(JSON.parse(event.data as string) as StreamFrame);
      } catch {
        // A frame we cannot parse is one bad message, not a reason to drop
        // the connection and lose the rest of the call.
      }
    };

    socket.onclose = (event) => {
      socketRef.current = null;
      if (event.code === POLICY_VIOLATION) {
        setStatus("unauthorized");
        return;
      }
      setStatus("closed");

      const delay = Math.min(
        MAX_RETRY_MS,
        BASE_RETRY_MS * 2 ** attemptsRef.current,
      );
      attemptsRef.current += 1;
      retryRef.current = setTimeout(connect, delay);
    };
  }, [callId]);

  useEffect(() => {
    if (!enabled) return;
    connect();

    return () => {
      if (retryRef.current) clearTimeout(retryRef.current);
      const socket = socketRef.current;
      socketRef.current = null;
      if (socket) {
        // Drop the handler first: the unmount close would otherwise schedule a
        // reconnect for a component that no longer exists.
        socket.onclose = null;
        socket.close();
      }
    };
  }, [connect, enabled]);

  return { status };
}
