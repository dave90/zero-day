import { useEffect, useState } from "react";
import { CheckCircle2, CirclePause, Activity } from "lucide-react";
import type { SessionState } from "../api/types";
import { runtimeStatus } from "../api/runtime-status";

export function RuntimeProgress({
  state,
  lastEventAt,
  connected,
}: {
  state: SessionState;
  lastEventAt?: number;
  connected: boolean;
}) {
  const status = runtimeStatus(state);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 15000);
    return () => clearInterval(timer);
  }, []);
  const elapsed = lastEventAt
    ? Math.max(0, Math.floor(now / 1000 - lastEventAt))
    : 0;
  const age =
    elapsed < 10
      ? "just now"
      : elapsed < 60
        ? `${elapsed}s ago`
        : `${Math.floor(elapsed / 60)}m ago`;
  return (
    <section
      className={`runtime-progress ${status.running ? "running" : "settled"}`}
      aria-label="Assessment progress"
    >
      <div className="row">
        {status.running ? (
          <span className="working-dot" />
        ) : state.lifecycle === "idle" ? (
          <CheckCircle2 size={16} />
        ) : (
          <CirclePause size={16} />
        )}
        <strong role="status">{status.label}</strong>
      </div>
      <p>{status.detail}</p>
      {status.running && (
        <div className="runtime-update">
          <Activity size={12} />
          {!connected
            ? "Live updates disconnected · reconnecting"
            : lastEventAt
              ? `Last activity ${age}`
              : "Waiting for live updates"}
        </div>
      )}
      {status.running && connected && lastEventAt && elapsed >= 120 && (
        <p className="runtime-quiet">
          No new activity has been reported recently. The runtime has not
          reported completion.
        </p>
      )}
    </section>
  );
}
