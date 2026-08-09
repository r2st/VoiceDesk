"use client";

/**
 * Session state for the dashboard.
 *
 * The provider resolves the session once on mount by calling `/auth/me`. That
 * round trip is what distinguishes "has a token" from "has a valid session" —
 * a token in localStorage may be stale, revoked, or belong to a user who has
 * since been deactivated, and only the server can say.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useRouter } from "next/navigation";

import { api, tokens } from "./api";
import type { Business, User } from "./types";

interface Session {
  user: User | null;
  business: Business | null;
  /** True until the initial `/auth/me` resolves; guards against a login flash. */
  loading: boolean;
  signIn: (email: string, password: string, slug?: string) => Promise<void>;
  signOut: () => Promise<void>;
  refresh: () => Promise<void>;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const router = useRouter();
  const [user, setUser] = useState<User | null>(null);
  const [business, setBusiness] = useState<Business | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    if (!tokens.access()) {
      setUser(null);
      setBusiness(null);
      setLoading(false);
      return;
    }
    try {
      const [me, biz] = await Promise.all([api.auth.me(), api.auth.business()]);
      setUser(me);
      setBusiness(biz);
    } catch {
      // Any failure here means the stored token cannot be used. Clearing it
      // avoids a loop where every page retries a token that will never work.
      tokens.clear();
      setUser(null);
      setBusiness(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const signIn = useCallback(
    async (email: string, password: string, slug?: string) => {
      const pair = await api.auth.login(email, password, slug);
      tokens.save(pair);
      setLoading(true);
      await load();
      router.push("/dashboard");
    },
    [load, router],
  );

  const signOut = useCallback(async () => {
    const refreshToken = tokens.refresh();
    if (refreshToken) {
      // Best effort: revoking server-side is preferable, but a failed logout
      // must still clear the local session rather than strand the user.
      try {
        await api.auth.logout(refreshToken);
      } catch {
        /* ignore */
      }
    }
    tokens.clear();
    setUser(null);
    setBusiness(null);
    router.push("/login");
  }, [router]);

  const value = useMemo<Session>(
    () => ({ user, business, loading, signIn, signOut, refresh: load }),
    [user, business, loading, signIn, signOut, load],
  );

  return (
    <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
  );
}

export function useSession(): Session {
  const context = useContext(SessionContext);
  if (!context) {
    throw new Error("useSession must be used inside a SessionProvider.");
  }
  return context;
}
