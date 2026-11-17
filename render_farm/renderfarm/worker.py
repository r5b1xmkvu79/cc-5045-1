"""Local render worker process.

Each worker is a real operating-system process that talks to the scheduler
over HTTP.  For every lease it performs a genuine render with
``renderer.render_frame``, keeps the lease alive with a heartbeat thread,
then uploads the complete PNG.  Fault injection (env ``RENDERFARM_FAULT``)
drives the failure-matrix tests:

* ``crash:<frame>``        process exits hard before uploading
* ``halfupload:<frame>``   uploads only the first half of the PNG bytes
                           (frame -1 triggers on the first task claimed)
* ``failreport:<frame>``   reports one failure, then resumes normally
* ``stall:<frame>``        freezes heartbeats until the lease expires,
                           then tries to report anyway (late/old attempt)
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Optional

from .renderer import render_frame


class FarmClient:
    def __init__(self, base_url: str, worker_id: str):
        self.base_url = base_url.rstrip("/")
        self.worker_id = worker_id

    def _request(self, method: str, path: str, body: Any = None,
                 raw: bool = False, content_type: str = "application/json") -> Any:
        url = self.base_url + path
        if raw:
            data = body
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
        else:
            data = None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None and content_type:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if raw or "json" not in ctype:
                    return resp.status, payload
                return resp.status, json.loads(payload)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            try:
                return exc.code, json.loads(payload)
            except (ValueError, json.JSONDecodeError):
                return exc.code, {"error": payload.decode("utf-8", "replace")}

    def register(self, name: str) -> None:
        self._request("POST", "/api/workers/register",
                      {"worker_id": self.worker_id, "name": name})

    def claim(self, lease_seconds: float) -> Optional[dict]:
        status, body = self._request(
            "POST", "/api/claim",
            {"worker_id": self.worker_id, "lease_seconds": lease_seconds},
        )
        if status >= 400:
            raise RuntimeError(f"claim failed: {body}")
        return body.get("task")

    def scene_doc(self, scene_id: str, version: str) -> dict:
        status, body = self._request("GET", f"/api/scenes/{scene_id}/{version}")
        if status != 200:
            raise RuntimeError(f"scene fetch failed: {body}")
        return body

    def heartbeat(self, attempt_id: str, extend_seconds: float) -> bool:
        status, _ = self._request(
            "POST", "/api/heartbeat",
            {"worker_id": self.worker_id, "attempt_id": attempt_id,
             "extend_seconds": extend_seconds},
        )
        return status == 200

    def succeed(self, attempt_id: str, png: bytes) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"{self.base_url}/api/attempts/{attempt_id}/success",
            data=png, method="POST",
        )
        req.add_header("Content-Type", "image/png")
        req.add_header("X-Worker-Id", self.worker_id)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                return resp.status, json.loads(payload)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read())
            except (ValueError, json.JSONDecodeError):
                return exc.code, {"error": "unparseable error body"}

    def fail(self, attempt_id: str, error: str) -> tuple[int, dict]:
        return self._request(
            "POST", f"/api/attempts/{attempt_id}/fail",
            {"worker_id": self.worker_id, "error": error},
        )


class _Heartbeater(threading.Thread):
    def __init__(self, client: FarmClient, attempt_id: str, lease_seconds: float):
        super().__init__(daemon=True)
        self.client = client
        self.attempt_id = attempt_id
        self.interval = max(1.0, lease_seconds / 3.0)
        self.stop_event = threading.Event()
        self.lost = False

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                ok = self.client.heartbeat(self.attempt_id, self.interval * 3)
            except OSError:
                ok = False
            if not ok:
                self.lost = True
                self.stop_event.set()


def _parse_fault(spec: Optional[str]) -> Optional[dict]:
    if not spec:
        return None
    kind, _, frame = spec.partition(":")
    return {"kind": kind, "frame": int(frame), "used": False}


def worker_loop(base_url: str, name: str, lease_seconds: float = 10.0,
                worker_id: Optional[str] = None,
                fault_spec: Optional[str] = None,
                stop_after: Optional[int] = None) -> dict:
    """Run the claim/render/report loop. Returns small stats for tests."""
    worker_id = worker_id or f"w-{uuid.uuid4().hex[:10]}"
    client = FarmClient(base_url, worker_id)
    fault = _parse_fault(fault_spec)
    stats = {"worker_id": worker_id, "rendered": 0, "failed": 0,
             "rejected": 0, "crashed_frame": None}

    while True:
        try:
            client.register(name)
            task = client.claim(lease_seconds)
        except OSError as exc:
            time.sleep(1.0)
            if stop_after is not None and time.time() >= stop_after:
                return stats
            continue
        if task is None:
            if stop_after is not None and time.time() >= stop_after:
                return stats
            time.sleep(0.5)
            continue

        attempt_id = task["attempt_id"]
        frame = int(task["frame"])
        settings = task["settings"]
        try:
            scene_doc = client.scene_doc(task["scene_id"], task["version"])
            png = render_frame(scene_doc, frame, settings)
        except Exception as exc:  # render itself blew up: report and continue
            client.fail(attempt_id, f"render error: {exc!r}")
            stats["failed"] += 1
            continue

        # --------------------------------------------------------- fault inject
        frame_matches = fault is not None and (
            fault["frame"] < 0 or frame == fault["frame"])
        if frame_matches and not fault["used"]:
            fault["used"] = True
            if fault["kind"] == "crash":
                stats["crashed_frame"] = frame
                os._exit(2)
            if fault["kind"] == "halfupload":
                status, body = client.succeed(attempt_id, png[: len(png) // 2])
                if status == 422:
                    stats["rejected"] += 1
                continue
            if fault["kind"] == "failreport":
                client.fail(attempt_id, "injected render failure")
                stats["failed"] += 1
                continue
            if fault["kind"] == "stall":
                time.sleep(lease_seconds * 1.6)
                status, _ = client.succeed(attempt_id, png)
                stats["rejected"] += 1 if status == 409 else 0
                continue

        hb = _Heartbeater(client, attempt_id, lease_seconds)
        hb.start()
        try:
            status, body = client.succeed(attempt_id, png)
        finally:
            hb.stop_event.set()
        if status == 200:
            stats["rendered"] += 1
        elif status == 409:
            stats["rejected"] += 1  # stale attempt correctly refused
        elif status == 422:
            stats["rejected"] += 1  # invalid image correctly refused
        else:
            stats["failed"] += 1


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Render farm worker")
    parser.add_argument("--server", default=os.environ.get("FARM_URL", "http://127.0.0.1:8765"))
    parser.add_argument("--name", default=os.uname().nodename if hasattr(os, "uname") else "worker")
    parser.add_argument("--lease", type=float, default=float(os.environ.get("FARM_LEASE", "10")))
    parser.add_argument("--id", default=None)
    args = parser.parse_args()
    print(f"[worker {args.name}] connecting to {args.server}", flush=True)
    worker_loop(args.server, args.name, args.lease, worker_id=args.id,
                fault_spec=os.environ.get("RENDERFARM_FAULT"))


if __name__ == "__main__":
    main()
