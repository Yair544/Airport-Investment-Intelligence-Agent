import { useState, type FormEvent } from "react";
import { ApiError, NetworkError, type ApiClient } from "../api/client";
import type { AuthResponse } from "../types";
import { Logo } from "./Logo";

type Mode = "sign_in" | "register";

/** Mirrors the server's RegisterRequest so most mistakes never need a round trip. */
const USERNAME_PATTERN = /^[A-Za-z0-9._-]{3,150}$/;
const MIN_PASSWORD_LENGTH = 10;

function authError(error: unknown, mode: Mode): string {
  const action = mode === "sign_in" ? "Sign-in" : "Sign-up";
  if (error instanceof ApiError) {
    if (error.status === 401) return error.fromServer ? error.message : "Incorrect username or password.";
    if (error.status === 409) return "That username is already taken. Choose another one.";
    if (error.status === 429) {
      const wait = error.retryAfterSeconds ? ` Try again in ${error.retryAfterSeconds} s.` : " Try again later.";
      return `Too many ${mode === "sign_in" ? "sign-in" : "sign-up"} attempts.${wait}`;
    }
    if (error.status === 403) return `${action} was blocked by the server's origin check. Open the app from its configured address.`;
    return error.message;
  }
  if (error instanceof NetworkError) return "Could not reach the server. Check that the service is running and try again.";
  return `${action} failed. Please try again.`;
}

function registrationProblem(username: string, password: string, confirm: string): string | null {
  if (!USERNAME_PATTERN.test(username)) {
    return "Use 3–150 characters for the username: letters, digits, dots, dashes or underscores.";
  }
  if (password.length < MIN_PASSWORD_LENGTH) return `Use a password of at least ${MIN_PASSWORD_LENGTH} characters.`;
  if (password !== confirm) return "The passwords do not match.";
  return null;
}

interface LoginProps {
  api: ApiClient;
  notice: string | null;
  onSignedIn: (auth: AuthResponse) => void;
}

export function Login({ api, notice, onSignedIn }: LoginProps) {
  const [mode, setMode] = useState<Mode>("sign_in");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const registering = mode === "register";

  const switchMode = (next: Mode) => {
    if (busy) return;
    setMode(next);
    setPassword("");
    setConfirm("");
    setError(null);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const name = username.trim();
    if (!name || !password) {
      setError("Enter your username and password.");
      return;
    }
    const problem = registering ? registrationProblem(name, password, confirm) : null;
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const auth = registering ? await api.register(name, password) : await api.login(name, password);
      onSignedIn(auth);
    } catch (caught) {
      setError(authError(caught, mode));
      setPassword("");
      setConfirm("");
      setBusy(false);
    }
  };

  return (
    <main className="login">
      <div className="login__card">
        <div className="login__brand">
          <Logo size={36} />
          <h1 className="login__title">Airport Investment Intelligence</h1>
        </div>
        <p className="login__intro">
          {registering
            ? "Create an account to analyze U.S. airport capacity and expansion opportunities."
            : "Sign in to analyze U.S. airport capacity and expansion opportunities."}
        </p>
        {notice && !registering && (
          <p className="alert alert--info" role="status">
            {notice}
          </p>
        )}
        <form className="login__form" onSubmit={(event) => void submit(event)} noValidate>
          <label htmlFor="login-username">Username</label>
          <input
            id="login-username"
            name="username"
            autoComplete="username"
            autoCapitalize="none"
            spellCheck={false}
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
            autoFocus
          />
          <label htmlFor="login-password">Password</label>
          <input
            id="login-password"
            name="password"
            type="password"
            autoComplete={registering ? "new-password" : "current-password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-describedby={registering ? "login-password-hint" : undefined}
            required
          />
          {registering && (
            <>
              <p id="login-password-hint" className="login__hint">
                At least {MIN_PASSWORD_LENGTH} characters.
              </p>
              <label htmlFor="login-confirm">Confirm password</label>
              <input
                id="login-confirm"
                name="confirm-password"
                type="password"
                autoComplete="new-password"
                value={confirm}
                onChange={(event) => setConfirm(event.target.value)}
                required
              />
            </>
          )}
          {error && (
            <p className="alert alert--error" role="alert">
              {error}
            </p>
          )}
          <button type="submit" className="btn btn--primary btn--block" disabled={busy}>
            {registering ? (busy ? "Creating account…" : "Create account") : busy ? "Signing in…" : "Sign in"}
          </button>
        </form>
        <p className="login__switch">
          {registering ? "Already have an account?" : "Don't have an account yet?"}{" "}
          <button
            type="button"
            className="link-button"
            onClick={() => switchMode(registering ? "sign_in" : "register")}
            disabled={busy}
          >
            {registering ? "Sign in" : "Sign up"}
          </button>
        </p>
      </div>
    </main>
  );
}
