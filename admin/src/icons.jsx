const paths = {
  overview: <><path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10.5V20h13v-9.5"/><path d="M9.5 20v-6h5v6"/></>,
  products: <><path d="m12 3 8 4.5v9L12 21l-8-4.5v-9Z"/><path d="m4 7.5 8 4.5 8-4.5M12 12v9"/></>,
  runs: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2M3 12h3"/></>,
  settings: <><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/></>,
  exports: <><path d="M12 3v12M7.5 10.5 12 15l4.5-4.5"/><path d="M4 19h16"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h16"/>,
  play: <path d="m8 5 11 7-11 7Z"/>,
  stop: <rect x="6" y="6" width="12" height="12" rx="1"/>,
  download: <><path d="M12 3v12M7.5 10.5 12 15l4.5-4.5"/><path d="M4 19h16"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m16 16 4 4"/></>,
  filter: <path d="M4 5h16l-6.5 7v6l-3 1.5V12Z"/>,
  chevron: <path d="m9 6 6 6-6 6"/>,
  close: <path d="m6 6 12 12M18 6 6 18"/>,
  image: <><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-5-5L5 20"/></>,
  tag: <><path d="M20 13 13 20 4 11V4h7Z"/><circle cx="8.5" cy="8.5" r="1"/></>,
  alert: <><path d="M12 3 2.8 20h18.4Z"/><path d="M12 9v5M12 17.2v.1"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/></>,
  skip: <><path d="m6 6 7 6-7 6Z"/><path d="M18 6v12"/></>,
  store: <><path d="M4 9h16l-1 11H5Z"/><path d="M4 9 6 4h12l2 5"/><path d="M9 13h6"/></>,
  refresh: <><path d="M20 6v5h-5"/><path d="M18.5 15.5A8 8 0 1 1 19.8 9"/></>,
  trash: <><path d="M4 7h16M9 7V4h6v3M6.5 7l1 14h9l1-14"/><path d="M10 11v6M14 11v6"/></>,
};

export function Icon({ name, size = 18, className = "" }) {
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.overview}
    </svg>
  );
}
