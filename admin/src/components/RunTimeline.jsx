import { Icon } from "../icons.jsx";
import { formatNumber } from "./Ui.jsx";

const MARKS = {
  pending: { icon: "clock", label: "Waiting" },
  active: { icon: "refresh", label: "In progress" },
  done: { icon: "check", label: "Done" },
  skipped: { icon: "skip", label: "Skipped" },
  failed: { icon: "alert", label: "Failed" },
};

function elapsed(phase) {
  if (!phase.started_at) return "";
  const start = new Date(phase.started_at).getTime();
  const end = phase.finished_at ? new Date(phase.finished_at).getTime() : Date.now();
  if (Number.isNaN(start) || Number.isNaN(end)) return "";
  const seconds = Math.max(0, Math.round((end - start) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

/**
 * The ordered list of everything a run does, service steps and crawler steps
 * alike, so the page never just says "running" without saying what of.
 */
export function RunTimeline({ timeline, compact = false }) {
  const phases = timeline?.phases || [];
  if (!phases.length) {
    return (
      <p className="timeline-idle">
        No run has been started yet. The steps of a run appear here as they happen.
      </p>
    );
  }

  return (
    <ol className={compact ? "timeline timeline--compact" : "timeline"}>
      {phases.map((phase, index) => {
        const mark = MARKS[phase.state] || MARKS.pending;
        const showBar =
          phase.state === "active" && phase.total > 0 && phase.processed != null;
        const percent = showBar
          ? Math.min(100, Math.round((phase.processed / phase.total) * 100))
          : 0;
        return (
          <li
            key={`${phase.key}-${index}`}
            className={`timeline-step timeline-step--${phase.state}`}
          >
            <span className="timeline-step__mark" title={mark.label}>
              <Icon name={mark.icon} size={15} />
            </span>
            <div className="timeline-step__body">
              <div className="timeline-step__head">
                <strong>{phase.label}</strong>
                <span className="timeline-step__time">{elapsed(phase)}</span>
              </div>
              {phase.detail ? <small>{phase.detail}</small> : null}
              {showBar ? (
                <div className="timeline-step__progress">
                  <div className="progress-track">
                    <span style={{ width: `${percent}%` }} />
                  </div>
                  <span>
                    {formatNumber(phase.processed)} / {formatNumber(phase.total)}
                  </span>
                </div>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

/** One-line summary of where a run currently is, for headers and cards. */
export function CurrentStep({ status }) {
  const timeline = status?.timeline;
  const running = status?.state === "running" || status?.state === "stopping";
  if (!timeline?.steps) return null;
  const label = timeline.current_label || status?.message || "Idle";
  return (
    <div className={`current-step${running ? " current-step--running" : ""}`}>
      <span className="current-step__counter">
        Step {timeline.step} of {timeline.steps}
      </span>
      <strong>{label}</strong>
      {timeline.current_detail ? <small>{timeline.current_detail}</small> : null}
    </div>
  );
}

/** Result of the storefront availability scan. */
export function StorefrontPanel({ storefront, catalog, onNavigate }) {
  const counts = catalog?.storefront || {};
  const scanned = storefront?.scanned;

  return (
    <section className="panel">
      <div className="panel-header">
        <h2>Storefront availability</h2>
        <span>Which saved products the live site still publishes</span>
      </div>
      <div className="storefront-grid">
        <button
          type="button"
          className="storefront-tile"
          onClick={() => onNavigate?.("products", "", { storefront: "live" })}
        >
          <small>Live on site</small>
          <strong>{formatNumber(counts.live)}</strong>
        </button>
        <button
          type="button"
          className={`storefront-tile${counts.offline ? " storefront-tile--warn" : ""}`}
          onClick={() => onNavigate?.("products", "", { storefront: "offline" })}
        >
          <small>Switched off</small>
          <strong>{formatNumber(counts.offline)}</strong>
        </button>
        <button
          type="button"
          className="storefront-tile"
          onClick={() => onNavigate?.("products", "", { storefront: "unknown" })}
        >
          <small>Not checked yet</small>
          <strong>{formatNumber(counts.unknown)}</strong>
        </button>
      </div>
      <p className="storefront-note">
        {scanned === true ? (
          <>
            Last scan found {formatNumber(storefront.live_products)} products published
            on the storefront and skipped {formatNumber(storefront.skipped_offline)}{" "}
            switched-off {storefront.skipped_offline === 1 ? "product" : "products"}
            {storefront.newly_offline
              ? `, ${formatNumber(storefront.newly_offline)} newly switched off`
              : ""}
            {storefront.back_online
              ? `, ${formatNumber(storefront.back_online)} back online`
              : ""}
            .
          </>
        ) : scanned === false ? (
          <>
            The storefront index could not be read on the last run, so availability is
            unknown and every saved product was tried.
          </>
        ) : (
          <>
            Run <strong>Enrich saved catalog</strong> to check which products are still
            published. Switched-off products are skipped instead of costing a failed
            request each.
          </>
        )}
      </p>
    </section>
  );
}

export function StorefrontBadge({ status }) {
  const tone =
    status === "live" ? "live" : status === "offline" ? "offline" : "unknown";
  const label =
    status === "live" ? "Live" : status === "offline" ? "Off" : "Unchecked";
  return <span className={`storefront-badge storefront-badge--${tone}`}>{label}</span>;
}
