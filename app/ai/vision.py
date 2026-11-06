"""Read a UAC dashboard screenshot with a vision LLM and pull out the failed job's details.

The result only pre-fills the operator's form; the operator checks it before raising the alert.
"""

from __future__ import annotations

import base64

from pydantic import BaseModel, Field

from app.ai.llm import LLMClient

PROMPT = """This is a screenshot from the Stonebranch UAC (Universal Automation Center) console showing a \
failed task. Extract what is visible. Return JSON with keys: "task_name", "workflow_name", "agent", "status", \
"exit_code", "error_message", "failed_at" (as shown), "business_service". Use null for anything not visible. \
Copy text exactly; do not guess. If several failed tasks are visible, use the first one and list the other task \
names in "other_failed_tasks" (array)."""


class ScreenshotExtract(BaseModel):
    task_name: str | None = None
    workflow_name: str | None = None
    agent: str | None = None
    status: str | None = None
    exit_code: str | None = None
    error_message: str | None = None
    failed_at: str | None = None
    business_service: str | None = None
    other_failed_tasks: list[str] = Field(default_factory=list)


async def extract_from_screenshot(llm: LLMClient, image: bytes, mime: str, model: str) -> ScreenshotExtract:
    url = f"data:{mime};base64,{base64.b64encode(image).decode()}"
    data = await llm.json_chat(
        [
            {
                "role": "user",
                "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": url}}],
            }
        ],
        model=model,
        max_tokens=600,
    )
    clean = {k: (str(v) if v is not None and not isinstance(v, list) else v) for k, v in data.items()}
    return ScreenshotExtract.model_validate(clean)
