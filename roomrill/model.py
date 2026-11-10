"""A real chat endpoint proposes closed drafts, never executable room actions."""

import ipaddress
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .validation import Error, draft, encoded, loads, require


def endpoint_url(endpoint):
    require(
        type(endpoint) is str and len(endpoint) <= 2048,
        "invalid_endpoint",
        "Provide a bounded endpoint URL",
    )
    try:
        parts = urlsplit(endpoint)
        port = parts.port
        require(
            parts.scheme in ("https", "http")
            and parts.hostname
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
            and parts.path.endswith("/chat/completions"),
            "invalid_endpoint",
            "Pin an HTTPS chat/completions endpoint or numeric loopback HTTP",
        )
        if parts.scheme == "http":
            require(
                ipaddress.ip_address(parts.hostname).is_loopback,
                "invalid_endpoint",
                "Plain HTTP is numeric loopback only",
            )
        require(port is None or 1 <= port <= 65535, "invalid_endpoint", "Invalid endpoint port")
    except (ValueError, TypeError) as exc:
        raise Error("invalid_endpoint", "Invalid model endpoint") from exc
    return endpoint


def propose(text, *, endpoint, model, rooms, key_env=None):
    require(
        endpoint and model,
        "model_not_configured",
        "Configure a local chat endpoint and model before requesting a draft",
    )
    endpoint_url(endpoint)
    require(type(model) is str and 1 <= len(model) <= 200, "invalid_input", "Provide a model ID")
    require(
        type(text) is str and 1 <= len(text.strip()) <= 4000,
        "invalid_input",
        "Request must be1..4000characters",
    )
    prompt = (
        "Propose a synthetic room lease as a JSON object only. Never issue actions. "
        "Allowed rooms: "
        + ", ".join(rooms)
        + ". Exactly these keys: room,targets,priority,duration_seconds,restore_policy. "
        "targets may contain light {on:boolean,brightness:integer1..100 if on or0 if off} and/or media (playing,paused,stopped). "
        "priority integer0..50; duration_seconds integer1..86400; restore_policy compare or leave. "
        "Never include owner,role,service calls,device IDs or other keys. If the request lacks a room, duration, "
        'or supported target, return {"error":"needs_clarification"}; unsupported devices also require clarification. '
        "The operator must review every draft. A model response never authorizes a lease."
    )
    body = {
        "model": model,
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        "temperature": 0,
        "max_tokens": 512,
        "response_format": {"type": "json_object"},
    }
    config = {
        "endpoint": endpoint,
        "body": body,
        "api_key": os.environ.get(key_env, "") if key_env else "",
    }
    raw = encoded(config)
    require(len(raw) <= 32768, "invalid_input", "Model request exceeds input bound")
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, "-I", "-B", str(Path(__file__).with_name("model_worker.py"))],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        output, _ = process.communicate(raw, timeout=45)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate()
        raise Error(
            "model_timeout", "Model request deadline elapsed; owned worker was reaped"
        ) from exc
    require(
        process.returncode == 0, "model_unavailable", "Model request failed; no lease was created"
    )
    response = loads(output)
    try:
        content = response["choices"][0]["message"]["content"]
        require(type(content) is str, "invalid_draft", "Model did not return plain JSON content")
        result = loads(content)
        if result == {"error": "needs_clarification"}:
            raise Error(
                "needs_clarification", "Specify a room, supported light/media target and duration"
            )
        draft(result, rooms)
    except (KeyError, IndexError, TypeError) as exc:
        raise Error("invalid_draft", "Malformed model response; no lease was created") from exc
    return {
        "draft": result,
        "model": response.get("model", model),
        "requested_model": model,
        "requires_confirmation": True,
        "elapsed_seconds": time.perf_counter() - started,
        "scope": "Model proposal only; engine state unchanged",
    }
