import { useEffect, useState } from "react";
import { ScrollText } from "lucide-react";
import { Link } from "react-router-dom";
import { fetchDecisionLogs } from "../../api/client";
import type { DecisionLogEvent } from "../../types";
import { formatHumanDecision } from "../../utils/decisionLogFormat";

function formatTs(ts: string) {
  try {
    return new Date(ts).toLocaleTimeString("en-IN", {
      timeZone: "Asia/Kolkata",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  } catch {
    return ts;
  }
}

export function DecisionLogFeed() {
  const [events, setEvents] = useState<DecisionLogEvent[]>([]);

  useEffect(() => {
    const load = () => {
      fetchDecisionLogs(12)
        .then((res) => setEvents(res.events ?? []))
        .catch(() => setEvents([]));
    };
    load();
    const id = setInterval(load, 8000);
    return () => clearInterval(id);
  }, []);

  const displayEvents = events.slice(0, 8);

  return (
    <section className="cockpit-panel decision-feed-panel">
      <header className="cockpit-panel-head">
        <ScrollText size={14} />
        <h3>AI Decision Log</h3>
        <Link to="/logs" className="decision-feed-link">
          View all
        </Link>
        {events.length ? (
          <span className="logs-live-pill mono muted">{events.length} live</span>
        ) : null}
      </header>
      <ul className="decision-feed-list">
        {displayEvents.length === 0 ? (
          <li className="decision-feed-item sev-info">
            <span className="decision-feed-msg muted">No decision events yet.</span>
          </li>
        ) : (
          displayEvents.map((ev) => {
            const human = formatHumanDecision(ev);
            return (
              <li key={ev.id} className={`decision-feed-item sev-${ev.severity}`}>
                <span className="decision-feed-time mono">{formatTs(ev.ts)}</span>
                <strong className="decision-feed-title">{human.title || ev.message}</strong>
                <span className="decision-feed-msg">{human.summary}</span>
              </li>
            );
          })
        )}
      </ul>
    </section>
  );
}
