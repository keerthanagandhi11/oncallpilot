"""Realistic sample UAC failures for demos (fake hosts, files and data)."""

from __future__ import annotations

import random
import uuid
from datetime import datetime

from app.integrations.base import FailureEvent

SCENARIOS: dict[str, dict] = {
    "missing_file": {
        "task_name": "PAY_EOD_SETTLEMENT_LOAD",
        "workflow_name": "WF_PAY_END_OF_DAY",
        "agent": "unxagt-pay-01",
        "exit_code": "8",
        "error_message": "Exit code 8: input file not found",
        "log_excerpt": (
            "2026-11-06 02:10:03 INFO  Starting settlement load, run date 20261105\n"
            "2026-11-06 02:10:03 INFO  Looking for /data/inbound/settlement/SETTLE_20261105_*.csv\n"
            "2026-11-06 02:10:04 ERROR No such file or directory: /data/inbound/settlement/SETTLE_20261105_*.csv\n"
            "2026-11-06 02:10:04 ERROR 0 files matched pattern; upstream feed from card processor not received\n"
            "2026-11-06 02:10:04 FATAL Aborting with RC=8"
        ),
    },
    "db_auth": {
        "task_name": "CDP_CUSTOMER_MERGE",
        "workflow_name": "WF_CDP_NIGHTLY",
        "agent": "winagt-cdp-03",
        "exit_code": "12",
        "error_message": "ORA-01017: invalid username/password; logon denied",
        "log_excerpt": (
            "02:31:10 Connecting to CDPPRD as svc_cdp_batch password=Winter2026!\n"
            "02:31:11 ORA-01017: invalid username/password; logon denied\n"
            "02:31:11 Retry 1/3 ... ORA-01017\n02:31:41 Retry 3/3 ... ORA-01017\n02:31:41 Job ended RC=12"
        ),
    },
    "disk_full": {
        "task_name": "SETTLE_ARCHIVE_COMPRESS",
        "workflow_name": "WF_PAY_HOUSEKEEPING",
        "agent": "unxagt-pay-02",
        "exit_code": "1",
        "error_message": "gzip: write error: No space left on device",
        "log_excerpt": (
            "03:05:00 Compressing 4,812 files from /data/archive/2026-11\n"
            "03:17:42 gzip: /data/archive/2026-11/txn_0448.dat.gz: No space left on device\n"
            "03:17:42 df -h /data -> 100% used (0 B free)\n03:17:42 RC=1"
        ),
    },
    "connection": {
        "task_name": "CRM_LEADS_EXTRACT",
        "workflow_name": "WF_CRM_DAILY",
        "agent": "unxagt-crm-01",
        "exit_code": "255",
        "error_message": "Connection refused: crm-api.internal:443",
        "log_excerpt": (
            "04:00:01 GET https://crm-api.internal/v2/leads?since=2026-11-05\n"
            "04:00:31 ERROR java.net.ConnectException: Connection refused (crm-api.internal:443)\n"
            "04:00:31 Exiting with status 255"
        ),
    },
    "unknown_task": {
        "task_name": "HR_PAYSLIP_PRINT",
        "workflow_name": "WF_HR_MONTHLY",
        "agent": "winagt-hr-01",
        "exit_code": "3",
        "error_message": "Process ended with exit code 3",
        "log_excerpt": "05:12:00 Generating payslips\n05:14:22 Unexpected error in module PRT-17\n05:14:22 RC=3",
    },
}


def demo_failure(scenario: str | None, now: datetime) -> FailureEvent:
    key = scenario if scenario in SCENARIOS else random.choice(list(SCENARIOS))
    data = SCENARIOS[key]
    return FailureEvent(
        **data,
        failed_at=now,
        source="demo",
        external_id=f"demo-{uuid.uuid4().hex[:16]}",
        uac_status="FAILED",
    )
