# Connecting the real systems

Switch providers one at a time and keep the others on `mock` while you test.

## 1. Stonebranch UAC → OnCallPilot (webhook)

1. Set `UAC_WEBHOOK_SECRET` to a random string.
2. In UAC, create a **Webhook** on task instance status changes. Filter it to Failed, Start Failure,
   Cancelled and Success (Success lets reruns auto-resolve).
   - URL: `https://<oncallpilot>/api/webhooks/uac`
   - Header: `X-Webhook-Token: <UAC_WEBHOOK_SECRET>`
   - Body: JSON. These field names are accepted (use the UAC variables your version provides):

   ```json
   {
     "taskName": "${ops_task_name}",
     "sysId": "${ops_task_id}",
     "status": "${ops_status}",
     "exitCode": "${ops_exit_code}",
     "agent": "${ops_agent}",
     "workflowInstanceName": "${ops_workflow_name}",
     "businessServices": "${ops_business_services}",
     "statusDescription": "${ops_status_description}",
     "endTime": "${ops_end_time}"
   }
   ```

   The `${...}` names above are placeholders. Map them to your UAC version's variable names.
   Alternative names: `name`, `taskInstanceId`, `id`, `agentName`, `workflowName`, `errorMessage`, and a nested
   `taskInstance` object. If your UAC already knows the ServiceNow incident, add `"incidentNumber"`.
3. Optional: set `UAC_BASE_URL`, `UAC_USERNAME` and `UAC_PASSWORD` (a read-only user). OnCallPilot then fetches
   STDERR/STDOUT for the triage (`GET /resources/taskinstance/retrieveoutput`). With `UAC_POLL_ENABLED=true`
   it also polls `POST /resources/taskinstance/list` as a backup. Duplicates are ignored.

Test it:

```bash
curl -X POST localhost:8000/api/webhooks/uac -H "X-Webhook-Token: $UAC_WEBHOOK_SECRET" \
  -H "content-type: application/json" \
  -d '{"taskName":"PAY_TEST","sysId":"test-1","status":"Failed","statusDescription":"No such file"}'
```

## 2. ServiceNow (on-call and incidents)

`ONCALL_PROVIDER=servicenow` and `INCIDENT_PROVIDER=servicenow`, plus `SERVICENOW_INSTANCE`,
`SERVICENOW_USERNAME` and `SERVICENOW_PASSWORD`.

The integration user needs read access to `sys_user_group`, `sys_user` and the On-Call Scheduling API
(`/api/now/on_call_rota/whoisoncall`), plus read and write on `incident` (assignment group, work notes).

### The failure's incident: link it, then transfer it

ServiceNow usually raises an incident for each UAC failure in the SysOps queue
(`SERVICENOW_INCIDENT_MODE=find`). OnCallPilot links that incident, and once the on-call has been
notified it moves the incident to the app team's queue with a work note. That is the step SysOps does by
hand today.

1. **Link.** OnCallPilot uses the incident number if the UAC webhook sends one (`incidentNumber`).
   Otherwise it searches for active incidents:
   - first `correlation_id = <UAC task instance id>`;
   - then the newest whose short description contains the task name, opened in the last
     `SERVICENOW_MATCH_HOURS`, in `SERVICENOW_QUEUE_GROUP` if set.

   If ServiceNow hasn't raised the incident yet, OnCallPilot searches again at transfer time.
2. **Transfer.** With `SERVICENOW_TRANSFER_WHEN=acknowledged` (default), this happens when someone
   acknowledges, or when nobody answers. OnCallPilot sets `assignment_group` to the team's `assignment_group`
   (or `servicenow_group`), clears `assigned_to`, and adds a work note like this:

   ```
   [OnCallPilot] Notified the on-call for UAC job failure PAY_EOD_SETTLEMENT_LOAD.
   Failed: 06 Nov 2026 02:10 UTC | workflow WF_PAY_END_OF_DAY | agent unxagt-pay-01 | exit code 8
   Error: Exit code 8: input file not found
   Summary: … Probable cause: …
   Notifications (UTC):
   - 02:10 Teams alert posted to Payments Application Support
   - 02:15 Called primary Arjun Mehta (+********0101): no answer
   - 02:23 Called primary Arjun Mehta (+********0101): answered
   Outcome: Acknowledged by Arjun Mehta via call at 02:23 UTC.
   Transferred to Payments App Support for investigation.
   OnCallPilot ID: 3f9c1a2b7d40
   ```

   With `SERVICENOW_TRANSFER_WHEN=notified`, the transfer happens as soon as the Teams alert is posted, and
   the outcome is added later as a second work note.
3. If no incident can be found, the timeline says so and asks SysOps to transfer it by hand.

`SERVICENOW_INCIDENT_MODE=create` makes OnCallPilot raise the incident itself, for teams where ServiceNow
doesn't.

- Primary and secondary are the first two people in the `whoisoncall` order.
- Manager is the group's `manager` field.
- The phone number is `mobile_phone`, or `phone` if that's empty.

`servicenow_group` in `teams.yaml` must match the group name in ServiceNow exactly.

## 3. Microsoft Teams channel alerts

`CHAT_PROVIDER=teams_webhook`. For each team's channel:

1. In Teams, open the channel, then **Workflows**, then **Post to a channel when a webhook request is received**.
2. Copy the URL into the matching env var (`PAYMENTS_TEAMS_WEBHOOK`, …) that `teams.yaml` refers to.

The Acknowledge button opens `PUBLIC_BASE_URL/ack/<signed token>`, so it must be reachable from the
users' browsers (company network or VPN is fine). The link only shows a confirm page. Acknowledging needs a
button press, so Teams' link previews can't acknowledge by accident.

## 4. Teams phone calls (Microsoft Graph)

`VOICE_PROVIDER=teams_graph`. You need:

1. An Entra ID app registration with the **Calls.Initiate.All** application permission (admin consent).
   Set `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID` and `GRAPH_CLIENT_SECRET`.
2. The app registered as a Teams calling bot, and a **Teams Phone resource account** with a phone number
   (Teams Phone licence plus Calling Plan or Direct Routing). Put its object id in `TEAMS_CALL_SOURCE_ID`.
3. `PUBLIC_BASE_URL` reachable **from the internet over HTTPS**: Graph posts call states to
   `/api/webhooks/teams-calls?token=…`. OnCallPilot adds the token itself.

Call states: `established` means answered, and `terminated` without being answered means no answer
(SIP 486 means busy). If a callback is lost, OnCallPilot polls the call. After `RING_TIMEOUT_SECONDS`
it hangs up and counts the call as unanswered.

If your tenant can't do Graph PSTN calls, use another `VoiceProvider` (for example Twilio). It needs only
`place_call`, `get_status` and `hang_up`.

## 5. Email to the DL

- `EMAIL_PROVIDER=smtp` with `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME` and `SMTP_PASSWORD` (STARTTLS by default).
- or `EMAIL_PROVIDER=graph` (Mail.Send application permission). Limit it to the `MAIL_FROM` mailbox
  with an Exchange application access policy.

## 6. LLM

Any OpenAI-compatible endpoint: `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, and `VISION_MODEL` for
screenshots. Logs are masked (passwords, tokens, keys, `user:pass@` URLs) before they're sent. If your
company doesn't allow job logs to go to an external LLM, point `LLM_BASE_URL` at an internal model
(for example Ollama or Azure OpenAI), or leave the key empty to use the keyword rules.
