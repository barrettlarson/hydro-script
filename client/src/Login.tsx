import { useState, type SubmitEvent } from "react";
import { postLogin } from "./api";

/**
 * Shared-credential login form. The backend checks the fields against the
 * household iAquaLink account and answers with a session cookie, so success
 * here just means the next status poll will be authenticated.
 */
export default function Login({ onSuccess }: { onSuccess: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(e: SubmitEvent<HTMLFormElement>) {
    e.preventDefault();
    setPending(true);
    setError(null);
    try {
      await postLogin(email, password);
      onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(false);
    }
  }

  return (
    <form className="login-card" onSubmit={(e) => void submit(e)}>
      <p className="login-hint">Sign in with the iAquaLink account.</p>
      {error && <div className="banner banner-error">{error}</div>}
      <label className="login-field">
        Email
        <input
          type="email"
          value={email}
          autoComplete="email"
          autoCapitalize="none"
          required
          onChange={(e) => setEmail(e.target.value)}
        />
      </label>
      <label className="login-field">
        Password
        <input
          type="password"
          value={password}
          autoComplete="current-password"
          required
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      <button type="submit" className="login-submit" disabled={pending} aria-busy={pending}>
        {pending ? <span className="spinner" aria-label="Signing in…" /> : "Sign in"}
      </button>
    </form>
  );
}
