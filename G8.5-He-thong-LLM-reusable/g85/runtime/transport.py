import asyncio
import json
import logging
import os
import time

import httpx

log = logging.getLogger("backend.chat")


async def complete(client, messages, schema, request_id, stage="plan", max_tokens=2500):
    # In-process applications may supply a scoped transport. No global model
    # environment changes are needed when several runtimes share one process.
    if hasattr(client, 'complete_g85'):
        return await client.complete_g85(messages, schema, stage, max_tokens)
    start = time.monotonic()
    async with asyncio.timeout(150):
        response = await client.post(os.environ["G85_MODEL_URL"].rstrip("/") + "/chat/completions",
            json={"model": os.environ["G85_MODEL_NAME"], "messages": messages,
                  "temperature": 0, "seed": 42, "max_tokens": max_tokens,
                  "response_format": {"type": "json_schema", "json_schema": {
                      "name": "g85_" + stage, "strict": True, "schema": schema}}}, timeout=145)
        response.raise_for_status()
    data = response.json()
    usage = data.get("usage") or {}
    log.info("g85_completion %s", json.dumps({"request_id": str(request_id), "stage": stage,
        "ms": round((time.monotonic() - start) * 1000),
        "input_tokens": usage.get("prompt_tokens"), "output_tokens": usage.get("completion_tokens")}))
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise ValueError("OUTPUT_TRUNCATED")
    return choice["message"]["content"]


TRANSPORT_ERRORS = (httpx.HTTPError, TimeoutError)
