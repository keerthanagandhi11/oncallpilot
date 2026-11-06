"""Prometheus metrics, served at /metrics."""

from prometheus_client import Counter, Gauge, Histogram

INCIDENTS = Counter("oncallpilot_incidents_total", "Incidents opened", ["team", "source"])
DUPLICATES = Counter("oncallpilot_duplicate_events_total", "Failure events ignored as duplicates", ["source"])
CALLS = Counter("oncallpilot_calls_total", "Calls placed, by outcome", ["team", "role", "outcome"])
ACKS = Counter("oncallpilot_acks_total", "Acknowledgements", ["team", "channel"])
EXHAUSTED = Counter("oncallpilot_exhausted_total", "Escalations where nobody responded", ["team"])
NOTIFICATIONS = Counter("oncallpilot_notifications_total", "Teams posts and emails", ["channel", "status"])
TIME_TO_ACK = Histogram(
    "oncallpilot_time_to_ack_seconds",
    "Seconds from incident open to acknowledgement",
    ["team"],
    buckets=(60, 300, 600, 900, 1200, 1800, 2700, 3600, 7200),
)
TRIAGE = Counter("oncallpilot_triage_total", "Triage results by source", ["source"])
WORKER_ERRORS = Counter("oncallpilot_worker_errors_total", "Errors while processing an incident step")
ACTIVE = Gauge("oncallpilot_active_escalations", "Escalations currently in progress")
