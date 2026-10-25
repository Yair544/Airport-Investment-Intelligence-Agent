import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { ApiError, isCancellation, SessionEndedError, type ApiClient } from "./api/client";
import { Login } from "./components/Login";
import { Workspace, type ActiveSession } from "./components/Workspace";
import type { AuthResponse } from "./types";

type AuthState =
  | { status: "checking" }
  | { status: "signed_out"; notice: string | null }
  | { status: "signing_out" }
  | { status: "signed_in"; session: ActiveSession };

const EXPIRED_NOTICE = "Your session has expired. Please sign in again.";
const IDENTITY_CHECK_INTERVAL_MS = 10_000;

function Splash({ text }: { text: string }) {
  return (
    <div className="splash" role="status">
      <span className="spinner" aria-hidden="true" />
      {text}
    </div>
  );
}

export function App({ api, demo = false }: { api: ApiClient; demo?: boolean }) {
  const [auth, setAuth] = useState<AuthState>({ status: "checking" });
  const sessionRef = useRef<ActiveSession | null>(null);
  const epochRef = useRef(0);

  /** Stops every request and poll of the current session and drops its state. */
  const endSession = useCallback((next: AuthState) => {
    const current = sessionRef.current;
    sessionRef.current = null;
    current?.controller.abort(new SessionEndedError());
    setAuth(next);
  }, []);

  const startSession = useCallback(
    (response: AuthResponse, notice: string | null = null) => {
      sessionRef.current?.controller.abort(new SessionEndedError());
      const controller = new AbortController();
      const csrf = { token: response.csrf_token };
      const session: ActiveSession = {
        epoch: ++epochRef.current,
        user: response.user,
        controller,
        csrf,
        notice,
        api: api.session({
          signal: controller.signal,
          csrfToken: () => csrf.token,
          onUnauthorized: () => {
            if (sessionRef.current?.controller === controller) endSession({ status: "signed_out", notice: EXPIRED_NOTICE });
          },
        }),
      };
      sessionRef.current = session;
      setAuth({ status: "signed_in", session });
    },
    [api, endSession],
  );

  // Spec section 7: the session is checked with GET /api/auth/me when the app loads.
  useEffect(() => {
    const controller = new AbortController();
    api.me(controller.signal).then(
      (response) => {
        if (controller.signal.aborted) return;
        if (response) startSession(response);
        else setAuth({ status: "signed_out", notice: null });
      },
      (error: unknown) => {
        if (controller.signal.aborted || isCancellation(error)) return;
        setAuth({ status: "signed_out", notice: "Could not reach the server to check your session." });
      },
    );
    return () => controller.abort();
  }, [api, startSession]);

  /**
   * Re-reads the session: after an expiry go to sign-in; if another tab signed in as a different
   * user, start over with that user and none of the previous account's state.
   */
  const verifySession = useCallback(async () => {
    const current = sessionRef.current;
    if (!current) return;
    try {
      const response = await api.me(current.controller.signal);
      if (sessionRef.current !== current) return;
      if (!response) endSession({ status: "signed_out", notice: EXPIRED_NOTICE });
      else if (response.user.user_id !== current.user.user_id) {
        startSession(response, `You are now signed in as ${response.user.username}. The account changed in another window.`);
      } else current.csrf.token = response.csrf_token;
    } catch {
      // Offline or cancelled: keep the current state; the next request reports the problem.
    }
  }, [api, endSession, startSession]);

  useEffect(() => {
    if (auth.status !== "signed_in") return undefined;
    let lastCheck = Date.now();
    const onFocus = () => {
      if (document.visibilityState !== "visible" || Date.now() - lastCheck < IDENTITY_CHECK_INTERVAL_MS) return;
      lastCheck = Date.now();
      void verifySession();
    };
    window.addEventListener("focus", onFocus);
    document.addEventListener("visibilitychange", onFocus);
    return () => {
      window.removeEventListener("focus", onFocus);
      document.removeEventListener("visibilitychange", onFocus);
    };
  }, [auth.status, verifySession]);

  const logout = useCallback(async () => {
    const current = sessionRef.current;
    if (!current) return;
    // Clear the workspace first, then wait for the server so a quick re-login cannot race it.
    endSession({ status: "signing_out" });
    let notice: string | null = null;
    try {
      try {
        await api.logout(current.csrf.token);
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 403)) throw error;
        const fresh = await api.me(); // stale CSRF token: fetch the current one and try once more
        if (fresh) await api.logout(fresh.csrf_token);
      }
    } catch {
      notice = "You were signed out here, but the server could not be reached to end the session.";
    }
    setAuth((state) => (state.status === "signing_out" ? { status: "signed_out", notice } : state));
  }, [api, endSession]);

  let content: ReactNode;
  if (auth.status === "checking") content = <Splash text="Checking your session…" />;
  else if (auth.status === "signing_out") content = <Splash text="Signing out…" />;
  else if (auth.status === "signed_out") content = <Login api={api} notice={auth.notice} onSignedIn={startSession} />;
  else {
    content = (
      <Workspace
        key={`${auth.session.user.user_id}:${auth.session.epoch}`}
        session={auth.session}
        onLogout={() => void logout()}
        onSessionCheck={() => void verifySession()}
      />
    );
  }

  return (
    <>
      {demo && (
        <div className="demo-banner" role="note">
          Demo mode: responses come from a local mock with sample data, not from the backend.
        </div>
      )}
      {content}
    </>
  );
}
