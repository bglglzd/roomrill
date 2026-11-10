"""Owned bounded HTTP worker. Secrets arrive only over stdin, never in argv/logs."""

import json
import sys
import urllib.error
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    try:
        config = json.loads(sys.stdin.buffer.read(32769))
        payload = json.dumps(config["body"], allow_nan=False).encode()
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if config.get("api_key"):
            headers["Authorization"] = "Bearer " + config["api_key"]
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(
            config["endpoint"], data=payload, headers=headers, method="POST"
        )
        with opener.open(request, timeout=30) as response:
            if (
                response.status != 200
                or response.headers.get("Content-Encoding", "identity") != "identity"
                or "json" not in response.headers.get("Content-Type", "").lower()
            ):
                raise ValueError("unsupported response")
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError("response too large")
        sys.stdout.buffer.write(raw)
        return 0
    except Exception:
        # HTTPError may contain provider data or secret-bearing URLs.
        sys.stderr.write("Model HTTP request failed or exceeded the supported profile\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
