import { useEffect, useState } from "react";

import { Icon } from "../icons.jsx";

const navigation = [
  ["overview", "Overview"],
  ["products", "Products"],
  ["runs", "Crawl runs"],
  ["settings", "Settings"],
  ["exports", "Exports"],
];

export function AppShell({ page, onNavigate, onLogout, children }) {
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(false), [page]);

  return (
    <div className="app-shell">
      <aside className={`sidebar ${open ? "sidebar--open" : ""}`}>
        <div className="brand-mark">
          <span className="brand-mark__diamond">◇</span>
          <span>Jewelry<br />Scraper</span>
        </div>
        <nav aria-label="Admin sections">
          {navigation.map(([key, label]) => (
            <button
              key={key}
              className={page === key ? "nav-item nav-item--selected" : "nav-item"}
              onClick={() => onNavigate(key)}
            >
              <Icon name={key} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar__user">
          <span className="avatar">AD</span>
          <span><strong>Admin</strong><small>Authenticated</small></span>
          <button onClick={onLogout} title="Sign out">Sign out</button>
        </div>
      </aside>
      {open ? <button className="sidebar-backdrop" onClick={() => setOpen(false)} aria-label="Close menu" /> : null}
      <div className="workspace">
        <header className="mobile-header">
          <button className="icon-button" onClick={() => setOpen(true)} aria-label="Open menu"><Icon name="menu" /></button>
          <strong>Jewelry Scraper</strong>
        </header>
        {children}
      </div>
    </div>
  );
}
