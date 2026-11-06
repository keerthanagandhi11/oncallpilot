# OnCallPilot

> When a Stonebranch UAC job fails, OnCallPilot alerts the application support team in Microsoft Teams,
> finds who is on call in ServiceNow, phones the primary, then the secondary, then the manager on a
> schedule you set, and emails the team DL. Anyone can stop it with one click. An AI model explains the
> failure, and every step is audited.

It replaces the manual SysOps routine: watch the UAC dashboard, take a screenshot, post it in Teams,
look up the rota in ServiceNow, call the primary 3 times 5 to 10 minutes apart, then the secondary,
then the manager, then email the DL.

**Live demo:** **https://oncallpilot.onrender.com** (demo mode with mock Teams, phone, email and ServiceNow; time
runs 60× faster; the first visit after 15 idle minutes takes about a minute to wake up)

**Design and reasoning:** [docs/BLUEPRINT.md](docs/BLUEPRINT.md) · **Connecting real systems:** [docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)

![flow](https://img.shields.io/badge/UAC_failure-→_AI_triage_→_Teams_→_call_ladder_→_DL_email-d9480f)

## Features

| | |
|---|---|
| **Detects failures** | UAC webhook (instant), UAC REST poller (backup), manual alert, or **AI reading of a UAC screenshot**. |
| **AI triage** | An LLM reads the error and log and writes a summary, probable cause, category, severity and first steps. Secrets are masked first. Without a key, keyword rules do it. |
| **Teams alert** | Adaptive Card with job details, triage, log tail, escalation plan and an **Acknowledge** button. |
| **On-call lookup** | ServiceNow On-Call Scheduling API (primary, secondary) and the group manager. |
| **Call ladder** | Per-team policy: attempts and interval per level. **Change the interval and attempts on each alert.** |
| **ServiceNow incident** | Links the incident ServiceNow raised for the failure, then transfers it to the app team's queue with a "notified the on-call" work note listing every Teams post, call and email. |
| **DL email** | At the start, before calling the manager (default), or only if nobody responds. |
| **Stop on acknowledgement** | Teams button, email link, answering the call, or the dashboard. A ringing call is hung up. |
| **Storm control** | Further failures for a team that's already escalating are grouped: posted to Teams, no extra calls, and their ServiceNow incidents are transferred with the first. If the first is fixed by a rerun, the grouped failures escalate on their own. |
| **Auto-resolve** | A UAC "Success" event for the task (a rerun) resolves the incident and stops the calls. |
| **Durable** | Escalation state is in the database, so a restart carries on on time. Postgres row locks allow several replicas. |
| **Audit and ops** | Full timeline per incident, ServiceNow work notes, JSON logs, Prometheus `/metrics`, `/api/health`, `/api/ready`. |
| **Demo mode** | Mock UAC, ServiceNow, Teams, phone and email. `TIME_SCALE=60` runs a whole ladder in about a minute. |

## Quick start (demo, no accounts needed)

```bash
cd oncallpilot
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
make run            # http://localhost:8000
```

Open the dashboard and click **Simulate failure**. Watch the Teams alert, triage, call attempts
(red dots), the DL email and the final state. Click **📞 Simulate pick-up** while a call rings,
or **Acknowledge**, to stop the escalation.

Optional: put a free Groq key in `LLM_API_KEY` for LLM triage and screenshot reading.

With Docker (app + Postgres): `docker compose up --build`.

## Deploy a free public demo (Render)

1. Push this repo to GitHub.
2. In Render: **New → Blueprint**, pick the repo, then **Apply**. [render.yaml](render.yaml) sets everything:
   demo mode, time ×60, and random `SECRET_KEY` and `UAC_WEBHOOK_SECRET` values.
3. Optional: add a free Groq key as `LLM_API_KEY` for LLM triage and screenshot reading.

The app reads its public address from Render, so the acknowledge links work with no extra setting. On the
free plan it sleeps after 15 idle minutes, and incidents are cleared on each deploy or restart.

## How it works

```
UAC ──webhook──► /api/webhooks/uac ─► incident (NEW) ─► worker:
                                                        1 AI triage + fetch UAC output
                                                        2 ServiceNow: who is on call
                                                        3 Teams card with Acknowledge button
                                                        4 wait initial_wait → call primary ×N (interval)
                                                        5 → secondary ×N → email DL → manager
                                                        6 nobody? EXHAUSTED: Teams + email, SysOps follows up
                                                        7 ServiceNow incident → app team's queue + work note
             acknowledge (Teams / email / call / dashboard) ─► stop, hang up, post "acknowledged"
```

The LLM never decides who to call or when. The ladder is a fixed state machine
([app/engine/escalation.py](app/engine/escalation.py)), so an LLM outage can't stop an escalation.

## Configure teams and policies

[config/teams.yaml](config/teams.yaml): which tasks belong to which team (glob patterns or UAC business
service), its ServiceNow group, DL, Teams webhook and escalation ladder:

```yaml
policy:
  initial_wait_minutes: 5        # time to reply in Teams before the first call
  email_dl: before_manager       # at_start | before_manager | on_exhausted
  levels:
    - { role: primary,   attempts: 3, interval_minutes: 7 }
    - { role: secondary, attempts: 3, interval_minutes: 7 }
    - { role: manager,   attempts: 1, interval_minutes: 5 }
```

Per alert, the operator (or the API `override` field) can change `interval_minutes` (0.5 to 120),
`initial_wait_minutes`, `primary_attempts`, `secondary_attempts` and `email_dl`.

## API

Interactive docs at `/docs`. Main endpoints:

| Method | Path | |
|---|---|---|
| POST | `/api/webhooks/uac` | UAC task status webhook (`X-Webhook-Token`). Failed → escalate, Success → resolve. |
| POST | `/api/incidents` | Raise an alert by hand (with optional `override`). |
| POST | `/api/incidents/extract` | Upload a UAC screenshot; the vision model returns the fields. |
| GET | `/api/incidents?status=active` | List incidents. |
| GET | `/api/incidents/{id}` | Detail: ladder, attempts, triage, Teams/email log, timeline. |
| POST | `/api/incidents/{id}/ack` · `/resolve` · `/cancel` | Operator actions. |
| GET/POST | `/ack/{signed-token}` | Acknowledge page from Teams/email (GET shows, POST acknowledges). |
| GET | `/metrics` · `/api/health` · `/api/ready` | Operations. |

All `/api/*` routes except health and webhooks need `Authorization: Bearer $API_TOKEN` when it is set.

## Project layout

```
app/
  main.py               FastAPI app factory, lifespan, worker start/stop
  config.py             settings from env; refuses unsafe prod config
  policy.py             team directory + escalation policy (YAML, ${ENV} values)
  models.py, db.py      incidents, call attempts, notifications, events (SQLAlchemy async)
  engine/escalation.py  the state machine
  engine/worker.py      ticker + UAC poller
  engine/messages.py    Teams cards, emails, call script
  ai/                   LLM client, triage (rules fallback), screenshot reader
  integrations/         uac, servicenow, microsoft (Teams webhook, Graph calls, Graph mail), smtp, mock
  api/routes.py         REST, webhooks, ack pages
  static/               dashboard (HTML + vanilla JS)
config/                 teams.yaml, oncall_demo.yaml
tests/                  70 tests: state machine with a fake clock, API, security, adapters
```

## Testing

```bash
make test     # pytest
make lint     # ruff
make e2e      # live run of all 7 scenarios against running servers (see scripts/e2e_scenarios.py)
```

`tests/test_scenarios.py` and `scripts/e2e_scenarios.py` cover the same seven scenarios: acknowledged in
Teams, primary answers the 2nd call, secondary takes it, nobody answers, failure storm, rerun fixes it, and
Teams is down. Each checks both sides: what SysOps sees, and what app support gets in Teams, on the phone, by
email and in ServiceNow.

The engine tests jump a fake clock through whole escalations: full ladder, acks at each stage,
answered calls, ring timeouts, failed calls, missing rota roles, storms, reruns, Teams outages and a
restart midway.

## Production checklist

- `APP_ENV=prod`, a random `SECRET_KEY` (32+ chars), `API_TOKEN`, `TIME_SCALE=1`. The app won't start without them.
- Postgres `DATABASE_URL`, HTTPS in front, `PUBLIC_BASE_URL` set to the public URL (used in ack links).
- `UAC_WEBHOOK_SECRET` set, and the UAC webhook configured ([docs/INTEGRATIONS.md](docs/INTEGRATIONS.md)).
- Real providers switched on one at a time: `CHAT_PROVIDER`, `ONCALL_PROVIDER`, `EMAIL_PROVIDER`, `VOICE_PROVIDER`.
- Scrape `/metrics`. Alert on `oncallpilot_exhausted_total` and `oncallpilot_worker_errors_total`.

## Status and limits

- Tested here: the state machine, API, security and dashboard in demo mode, plus each real adapter
  against mocked HTTP responses shaped like the vendor APIs.
- **Not yet tested against live UAC, ServiceNow, Teams or Graph.** Endpoint paths and field names
  follow the vendors' documented APIs, but check them against your versions before go-live.
- Teams calls need a Teams Phone resource account and Graph `Calls.Initiate.All`. A call that is
  answered counts as acknowledged; "press 1 to confirm" is on the roadmap.

License: MIT
