# OnCallPilot: Blueprint

How the manual SysOps escalation process becomes an automated, auditable service.

---

## 1. The problem today

When a job fails in **Stonebranch UAC (Universal Automation Center)**, the SysOps operator:

1. Notices the failure on the UAC dashboard.
2. Takes a screenshot and posts it to the application support team in **Microsoft Teams**.
3. Opens **ServiceNow** to find the team's **on-call schedule** and phone numbers.
4. If nobody replies, calls through Teams soft dial:
   - **Primary on-call**: up to 3 calls, 5 to 10 minutes apart.
   - Then the **secondary on-call**.
   - Then the **manager**.
5. Emails the team's **distribution list (DL)** if nobody responds.

### What goes wrong

| Pain | Effect |
|---|---|
| Manual watching | Failures are noticed late, especially at night or when many jobs fail at once. |
| Screenshots | Not searchable, no log text, and the support team has to retype job names. |
| ServiceNow lookups | Slow, and easy to read the wrong rota or an old number. |
| Timing by hand | Intervals drift. Calls get missed when the operator is busy with other failures. |
| No audit trail | Hard to prove who was called, when, and who acknowledged it. |
| One person, many failures | A storm of 20 failures can mean 20 escalations by hand. |

---

## 2. The solution

**OnCallPilot** is a service that runs the escalation for the operator:

```
UAC failure ─► detect ─► AI triage ─► find on-call ─► Teams alert ─► call ladder ─► email DL
                                                         ▲                │
                                                         └── acknowledge ─┘  (stops everything)
```

1. **Detect**: UAC sends a webhook when a task fails. A poller of the UAC REST API is the backup.
   The operator can also raise an alert by hand, or upload a dashboard **screenshot** that an AI vision
   model reads.
2. **AI triage**: an LLM reads the job's error output and writes a summary, probable cause, category,
   severity and suggested next step. Secrets are masked before the text is sent to the LLM.
   Without an LLM key, keyword rules do the same job.
3. **Find on-call**: ServiceNow's on-call API gives the primary and secondary on-call. The group
   record gives the manager. Phone numbers come from the user records.
4. **Teams alert**: an Adaptive Card goes to the team's channel. It has the job details, the AI summary,
   a log excerpt and an **Acknowledge** button. This replaces the screenshot.
5. **Call ladder**: a timed sequence of calls that you set per team, for example primary 3 calls 7 minutes
   apart, then secondary 3 calls, then the manager once. You can change the interval and attempts on
   each alert.
6. **Email the DL** at the point you choose: at the start, before calling the manager (default),
   or when everyone has been tried.
7. **Acknowledge** stops the ladder at once. Ways to acknowledge: the Teams button, answering the call,
   the dashboard, or the API.
8. **Transfer the ServiceNow incident**: OnCallPilot links the incident ServiceNow raised for the failure.
   Once the on-call acknowledges (or nobody answers), it moves the incident from the SysOps queue to the
   app team's queue with a work note: "notified the on-call", plus every Teams post, call and email.
9. **Audit**: every step is saved to a timeline.

### Design principle: AI advises, rules decide

The LLM **never** decides who to call or when. Calling is a fixed state machine driven by the team's
policy. The AI only helps people understand the failure faster. That way, an LLM outage or a wrong answer
can never stop or change an escalation.

---

## 3. Architecture

```
                 ┌──────────────────────────── OnCallPilot (FastAPI) ────────────────────────────┐
 UAC webhook ───►│ /api/webhooks/uac ─┐                                                           │
 UAC REST poll ─►│ poller ────────────┼─► Incident created (status NEW) ──► database             │
 Operator UI ───►│ /api/incidents ────┘                                         ▲                 │
 Screenshot ────►│ /api/incidents/extract (vision LLM)                         │                 │
                 │                                                              │                 │
                 │ Worker (ticks every few seconds) — picks due incidents ──────┘                 │
                 │   NEW       → triage (LLM) → on-call lookup (ServiceNow) → Teams card → wait    │
                 │   NOTIFIED  → place call to current level/attempt (Teams voice)                 │
                 │   CALLING   → check call result → answered = ack / no answer = schedule next    │
                 │   WAITING   → place next call … → move up the ladder → email DL → EXHAUSTED     │
                 │                                                                                 │
                 │ /ack/{signed-token} ◄── Teams card button        /metrics  /api/health          │
                 └─────────────────────────────────────────────────────────────────────────────────┘
        Integrations (each has a real adapter and a mock for demo/testing):
        UAC REST · ServiceNow Table + On-Call API · Teams Workflows webhook · Microsoft Graph calls · SMTP / Graph mail · OpenAI-compatible LLM
```

### Why a durable database-backed worker?

Escalations last up to an hour. If the server restarts mid-ladder, the calls must continue on time.
Each incident stores `next_action_at` in the database. The worker picks up due incidents, does one step,
and saves the next due time. Nothing lives only in memory, so a restart loses nothing. With Postgres,
`SELECT … FOR UPDATE SKIP LOCKED` lets several workers run safely.

### Escalation state machine

```
NEW ──notify──► NOTIFIED ──initial wait──► CALLING ──answered──► ACKNOWLEDGED
                                             │  ▲
                                   no answer │  │ interval
                                             ▼  │
                                           WAITING ──ladder done──► EXHAUSTED
any active state ──ack──► ACKNOWLEDGED     any active state ──job rerun OK──► RESOLVED
any active state ──operator──► CANCELLED   same team already escalating ──► GROUPED (no calls)
```

---

## 4. Data model

| Table | Holds |
|---|---|
| `incidents` | The failure, AI triage, team, policy snapshot, on-call snapshot, status, ladder position, `next_action_at`. |
| `call_attempts` | One row per call: level, role, attempt number, contact, provider call id, outcome, times. |
| `notifications` | Every Teams post and email: target, subject, body, sent or failed, error. |
| `events` | The audit timeline: every step with time and details. |

The policy and on-call list are **saved with the incident** when it opens. A rota change halfway through an
escalation can't confuse it, and the audit shows exactly who was on call at the time.

---

## 5. Team configuration (`config/teams.yaml`)

```yaml
teams:
  - id: payments
    name: Payments Application Support
    servicenow_group: Payments App Support
    email_dl: payments-support@example.com
    teams_webhook_url: ${PAYMENTS_TEAMS_WEBHOOK}
    match:
      task_patterns: ["PAY_*", "SETTLE_*"]
    policy:
      initial_wait_minutes: 5        # time for the team to reply in Teams before the first call
      email_dl: before_manager       # at_start | before_manager | on_exhausted
      levels:
        - { role: primary,   attempts: 3, interval_minutes: 7 }
        - { role: secondary, attempts: 3, interval_minutes: 7 }
        - { role: manager,   attempts: 1, interval_minutes: 5 }
```

---

## 6. Production concerns

| Concern | How it's handled |
|---|---|
| Duplicate alerts | Each incident has a unique key from the UAC task instance id. The same failure never escalates twice. |
| Failure storms | If a team already has an active escalation, new failures are **grouped** under it. They're posted to Teams, but nobody is called again. Their ServiceNow incidents are transferred with the first one's. If the first failure is fixed by a rerun, the oldest grouped failure starts its own escalation. |
| Job fixed by a rerun | A UAC "success" event for the same task resolves the incident and stops the calls. |
| Restarts | State is in the database. The worker resumes on time. |
| Link previews | Teams and Outlook open links to preview them. The ack link shows a confirm page, and only a button press (POST) acknowledges. |
| Forged acks | Ack links are HMAC-signed, tied to one incident, and expire. |
| Webhook security | A shared secret header, compared in constant time. |
| API security | Bearer token for the dashboard and API (`API_TOKEN`). |
| Secrets in logs | Passwords, tokens and keys are masked before the log goes to the LLM, Teams or email. |
| Phone privacy | Phone numbers are masked in the UI, the API and logs. |
| An integration is down | Teams or email failures are recorded and the call ladder carries on. LLM failure falls back to rules. |
| Observability | JSON logs, `/api/health`, `/api/ready`, and Prometheus `/metrics` (calls, acks, time-to-ack). |
| Testing without real systems | Every integration has a mock. **Demo mode** speeds up time so a full ladder runs in about a minute. |

---

## 7. Tech stack

| Layer | Choice | Why |
|---|---|---|
| API and worker | Python 3.11, FastAPI, asyncio | Async I/O for many parallel calls and HTTP integrations. |
| Database | SQLAlchemy 2 (async). SQLite for dev, Postgres for prod | Durable timers, row locking for many workers. |
| LLM | Any OpenAI-compatible API (Groq `openai/gpt-oss-120b` by default) | Free tier, swappable. JSON output checked with Pydantic. |
| Vision | OpenAI-compatible vision model | Reads UAC dashboard screenshots. |
| UI | One HTML page with vanilla JS | No build step. Live board, timeline, ack buttons. |
| Packaging | Docker, docker-compose (app + Postgres), Render blueprint | One command to run. |
| Quality | pytest (fake clock, mock providers, HTTP mock transport), ruff, GitHub Actions | Every branch of the state machine is tested. |

---

## 8. Build plan

1. Config and team policy loading (YAML with `${ENV}` values).
2. Database models and async session.
3. Integration interfaces, mock adapters, then real adapters (UAC, ServiceNow, Teams, Graph voice, email).
4. AI triage (masking, LLM JSON, rule fallback) and screenshot extraction.
5. Escalation engine (state machine) and durable worker.
6. REST API, webhooks, signed ack flow, metrics.
7. Operator dashboard.
8. Tests, Docker, CI, docs.

## 9. Roadmap

- Answer the call with "press 1 to acknowledge" (Graph `subscribeToTone`) instead of "answered = acknowledged".
- A Teams bot for two-way chat (ack and "I'm looking" from inside Teams).
- Similar past incidents and known fixes (RAG over resolved incidents and runbooks).
- Twilio or other PSTN voice as a second provider.
- Shift handover report: what failed overnight and who handled it.
