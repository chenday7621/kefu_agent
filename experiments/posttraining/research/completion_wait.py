"""Poll the original completion endpoint without resubmitting processing."""

import time

PENDING = "Native processing has not completed; no snapshot saved"


def post_native(client, url, path, body, on_wait, maximum_wait_seconds=1200):
    deadline = time.monotonic() + maximum_wait_seconds
    while True:
        response = client.post(url + path, json=body or {}, timeout=600)
        if (
            path.endswith("/snapshot")
            and response.status_code == 409
            and response.json().get("error") == PENDING
            and time.monotonic() < deadline
        ):
            on_wait()
            continue
        response.raise_for_status()
        return response.json()
