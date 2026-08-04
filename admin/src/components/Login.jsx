import { useState } from "react";

import { api, setToken } from "../api.js";
import { Icon } from "../icons.jsx";
import { Button } from "./Ui.jsx";

export function Login({ onAuthenticated }) {
  const [token, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setToken(token);
    try {
      await api("/api/session");
      onAuthenticated();
    } catch {
      setToken("");
      setError("That control token was not accepted.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-shell">
      <section className="login-panel">
        <div className="brand-mark brand-mark--large">
          <span className="brand-mark__diamond">◇</span>
          <span>Jewelry Scraper</span>
        </div>
        <h1>Catalog operations</h1>
        <p>Enter the generated Render control token to inspect and run the scraper.</p>
        <form onSubmit={submit}>
          <label className="field">
            <span className="field__label">Control token</span>
            <input
              type="password"
              value={token}
              onChange={(event) => setValue(event.target.value)}
              autoComplete="current-password"
              autoFocus
              required
            />
          </label>
          {error ? <div className="form-error"><Icon name="alert" />{error}</div> : null}
          <Button tone="primary" type="submit" disabled={busy || !token.trim()}>
            {busy ? "Checking…" : "Open admin"}
          </Button>
        </form>
      </section>
    </main>
  );
}
