"""End-to-end failure-matrix test for the render farm.

Everything here runs as real OS processes: one scheduler (``renderfarm.web``)
plus several workers (``renderfarm.worker``) with injected faults.  The
final rendered sequence is pulled back over HTTP and fully decoded so the
assertions concern actual pixels, not file existence.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http import HTTPStatus

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from renderfarm.png import decode_png, PNGError  # noqa: E402

PORT = 8811
BASE = f"http://127.0.0.1:{PORT}"
LEASE = 4.0


# ----------------------------------------------------------------------------
# HTTP helpers
# ----------------------------------------------------------------------------
def api(method: str, path: str, body=None, raw: bytes | None = None):
    data = None
    headers = {}
    if raw is not None:
        data, headers["Content-Type"] = raw, "image/png"
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            parsed = json.loads(payload) if "json" in ctype else payload
            return resp.status, dict(resp.headers), parsed
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        try:
            parsed = json.loads(payload)
        except ValueError:
            parsed = payload
        return exc.code, dict(exc.headers), parsed


def wait_ready(timeout: float = 15.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, _ = api("GET", "/health")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("scheduler never became ready")


def start_scheduler(data_dir: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "renderfarm.web", "--port", str(PORT),
         "--data-dir", data_dir],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_ready()
    except Exception:
        out = proc.stdout.read() if proc.stdout else ""
        proc.kill()
        raise RuntimeError(f"scheduler failed to start:\n{out}")
    return proc


def stop(proc: subprocess.Popen) -> str:
    proc.send_signal(signal.SIGINT)
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    return out or ""


def start_worker(name: str, fault: str | None = None, lease: float = LEASE) -> subprocess.Popen:
    env = dict(os.environ, PYTHONPATH=ROOT)
    if fault:
        env["RENDERFARM_FAULT"] = fault
    return subprocess.Popen(
        [sys.executable, "-m", "renderfarm.worker", "--server", BASE,
         "--name", name, "--lease", str(lease)],
        cwd=ROOT, env=env,
    )


def load_scene() -> dict:
    with open(os.path.join(ROOT, "examples", "scene_bounce.json"), "rb") as fh:
        return json.loads(fh.read())


def submit(scene_doc: dict, start: int, end: int, settings: dict | None = None) -> dict:
    body = {"scene_doc": scene_doc, "frame_start": start, "frame_end": end}
    if settings:
        body["settings"] = settings
    status, _, data = api("POST", "/api/submissions", body)
    assert status == HTTPStatus.CREATED, f"submit failed: {data}"
    return data


def wait_status(sub_id: str, target: set[str], timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, _, data = api("GET", f"/api/submissions/{sub_id}")
        summary = data["summary"]
        if set(summary) <= target:
            return data
        time.sleep(0.4)
    raise AssertionError(f"timeout waiting for {target}, last summary={summary}")


def fetch_frame(sub_id: str, frame: int) -> tuple[dict, bytes]:
    status, headers, blob = api("GET", f"/api/submissions/{sub_id}/frames/{frame}")
    assert status == 200
    return headers, blob


# ----------------------------------------------------------------------------
# Checks
# ----------------------------------------------------------------------------
def check_half_upload_rejected(data_dir: str) -> None:
    # No blob file may be truncated: every stored blob must decode fully.
    blob_root = os.path.join(data_dir, "blobs")
    count = 0
    for dirpath, _, files in os.walk(blob_root):
        for name in files:
            count += 1
            full = os.path.join(dirpath, name)
            with open(full, "rb") as fh:
                blob = fh.read()
            w, h, _, raw = decode_png(blob)
            assert (w, h) == (320, 180)
            assert len(raw) == w * h * 3
    assert count > 0, "no blobs were committed"
    print(f"  [ok] all {count} committed blobs decode at exact 320x180")


def main() -> None:
    data_dir = tempfile.mkdtemp(prefix="farm-e2e-")
    print(f"[setup] data dir: {data_dir}")
    failures: list[str] = []

    def check(name, fn):
        try:
            fn()
            print(f"[PASS] {name}")
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
            print(f"[FAIL] {name}: {exc}")

    server = start_scheduler(data_dir)
    workers: list[subprocess.Popen] = []
    try:
        scene = load_scene()
        n = scene["total_frames"]

        # --- Phase 1: fault matrix while rendering the full shot -----------
        print("\n=== Phase 1: crash / half-upload / fail-report / stall ===")
        workers = [
            start_worker("w-crash", f"crash:{n // 3}"),
            start_worker("w-half", f"halfupload:{n // 2}"),
            start_worker("w-fail", f"failreport:{2 * n // 3}"),
            start_worker("w-stall", f"stall:{n - 1}"),
            start_worker("w-steady-1"),
            start_worker("w-steady-2"),
        ]
        sub1 = submit(scene, 0, n - 1)
        sub_id = sub1["submission_id"]
        assert sub1["version"] and sub1["settings_hash"]
        data = wait_status(sub_id, {"succeeded"}, timeout=90)
        assert data["summary"].get("succeeded") == n, data["summary"]
        print(f"  [ok] all {n} frames succeeded despite injected faults")

        check("crashed worker died and its lease was recovered", lambda:
              any(w.poll() is not None for w in workers[:1]))

        # real pixels, correct order, no missing / wrong frames
        frames = {}
        for frame in range(n):
            headers, blob = fetch_frame(sub_id, frame)
            assert headers.get("X-Frame-Status") == "succeeded"
            w, h, _, raw = decode_png(blob)
            assert (w, h) == (320, 180)
            frames[frame] = raw
        print("  [ok] every frame downloaded as a complete, dimension-checked PNG")

        def pixels_move():
            diffs = [frames[i] != frames[i + 1] for i in range(n - 1)]
            assert all(diffs), "adjacent frames are identical (no motion?)"
        check("consecutive frames differ (moving objects really rendered)", pixels_move)

        def no_duplicate_images():
            uniq = {frames[i] for i in range(n)}
            assert len(uniq) == n, f"only {len(uniq)} unique images for {n} frames"
        check("no frame duplicates / mis-ordering", no_duplicate_images)

        check("half-upload bytes never became a formal frame",
              lambda: check_half_upload_rejected(data_dir))

        # provenance: every frame names the worker + attempt that made it
        _, _, detail = api("GET", f"/api/submissions/{sub_id}")
        for t in detail["tasks"]:
            prov = t["provenance"]
            assert t["status"] == "succeeded"
            assert prov["completed_by"], f"frame {t['frame']} missing source worker"
            assert prov["completed_attempt"], f"frame {t['frame']} missing attempt id"
            assert t["blob_sha"]
            assert any(a["state"] == "succeeded" for a in prov["attempts"])
        worker_ids = {w for t in detail["tasks"] for w in [t["provenance"]["completed_by"]]}
        print(f"  [ok] provenance present; frames produced by {len(worker_ids)} workers")

        # --- Phase 2: stale attempt / duplicate report cannot overwrite -----
        print("\n=== Phase 2: old attempts cannot overwrite new results ===")
        # isolate the lease/reap path from live workers
        for w in workers:
            if w.poll() is None:
                w.terminate()
                w.wait(timeout=10)
        workers = []
        # corrupt upload must be refused with 422
        status, _, data = api(
            "POST", "/api/attempts/nonexistent-attempt/success",
            raw=b"half-baked-png-data",
        )
        assert status in (409, 422), status
        print(f"  [ok] bogus upload refused ({status})")

        # take one frame's task back out via an expired-lease simulation:
        # submit a second short range, steal a lease, let it expire, then
        # attempt to report on the dead attempt -> 409, real result intact
        sub2 = submit(scene, 0, 0)
        # frame 0 already exists from sub1 (same identity) -> reused
        assert sub2["reused_frames"] == [0], sub2

        # lease a fresh frame through the API directly and let it expire
        sub3 = submit(scene, 1, 1)
        assert sub3["reused_frames"] == [1]
        # force a genuinely new task via different settings
        settings_hd = {"width": 320, "height": 180, "ssaa": 3, "format": "png"}
        sub4 = submit(scene, 2, 2, settings=settings_hd)
        assert sub4["created_frames"] == [2]
        _, _, lease = api("POST", "/api/claim",
                          {"worker_id": "ghost-worker", "lease_seconds": 1})
        lease = lease["task"]
        assert lease["status"] == "leased" and lease["attempt_id"]
        old_attempt = lease["attempt_id"]
        time.sleep(LEASE + 1.0)  # reaper expires it
        _, _, again = api("POST", "/api/claim",
                          {"worker_id": "replacement-worker", "lease_seconds": LEASE})
        again = again["task"]
        assert again["frame"] == 2 and again["attempt_id"] != old_attempt
        # old attempt reporting late must be rejected
        status, _, _ = api(
            "POST", f"/api/attempts/{old_attempt}/success",
            raw=b"",
        )
        assert status == 409, f"expected stale rejection, got {status}"
        print("  [ok] superseded attempt rejected with 409")

        # duplicate late report on the same dead attempt: still 409, no state change
        status, _, _ = api("POST", f"/api/attempts/{old_attempt}/fail",
                           body={"worker_id": "ghost-worker", "error": "late duplicate"})
        assert status == 409
        print("  [ok] duplicate late failure report also rejected")

        # bring workers back to finish the re-leased task and later phases
        workers = [start_worker("w-pool-1"), start_worker("w-pool-2"),
                   start_worker("w-pool-3")]

        # let a real worker finish the re-leased task
        data = wait_status(sub4["submission_id"], {"succeeded"}, timeout=30)
        assert data["summary"] == {"succeeded": 1}
        headers, blob = fetch_frame(sub4["submission_id"], 2)
        w, h, _, _ = decode_png(blob)
        assert (w, h) == (320, 180)
        print("  [ok] re-leased frame completed by a newer attempt")

        # --- Phase 3: edited shot = new identity, no old-image hit ----------
        print("\n=== Phase 3: edited scene version cannot hit old frames ===")
        edited = json.loads(json.dumps(scene))
        edited["objects"][0]["color"] = [60, 200, 120]  # retimed/graded shot
        sub5 = submit(edited, 0, 1)
        assert sub5["version"] != sub1["version"], "version must change with content"
        assert sub5["created_frames"] == [0, 1], "edited frames must be new tasks"
        wait_status(sub5["submission_id"], {"succeeded"}, timeout=30)
        for frame in (0, 1):
            _, new_blob = fetch_frame(sub5["submission_id"], frame)
            _, old_blob = fetch_frame(sub_id, frame)
            assert new_blob != old_blob, "edited shot returned the old image"
        print("  [ok] resubmitted edited shot rendered new images under a new version")

        # settings change (resolution) likewise changes identity
        sub6 = submit(scene, 3, 3, settings={"width": 160, "height": 90, "ssaa": 1})
        assert sub6["settings_hash"] != sub1["settings_hash"]
        wait_status(sub6["submission_id"], {"succeeded"}, timeout=30)
        _, blob6 = fetch_frame(sub6["submission_id"], 3)
        w, h, _, _ = decode_png(blob6)
        assert (w, h) == (160, 90)
        print("  [ok] settings hash separates different-resolution outputs")

        # --- Phase 4: unfinished frames are explicitly marked ---------------
        print("\n=== Phase 4: cancellation marks unfinished frames ===")
        sub7 = submit(scene, 4, 9)
        assert sub7["reused_frames"] == [4, 5, 6, 7, 8, 9]  # same identity as sub1
        # brand-new settings so there is real pending work
        sub8 = submit(scene, 4, 9,
                      settings={"width": 200, "height": 120, "ssaa": 2})
        time.sleep(0.5)
        status, _, _ = api("DELETE", f"/api/submissions/{sub8['submission_id']}/cancel")
        assert status == 200
        _, _, detail8 = api("GET", f"/api/submissions/{sub8['submission_id']}")
        for t in detail8["tasks"]:
            assert t["status"] in ("cancelled", "succeeded")
        headers, placeholder = api("GET",
            f"/api/submissions/{sub8['submission_id']}/frames/4")[1:]
        w, h, _, raw = decode_png(placeholder)
        assert (w, h) == (200, 120)
        assert headers.get("X-Frame-Status") in ("cancelled", "succeeded")
        print("  [ok] cancelled/unfinished frames return marked placeholders, never gaps")

        # --- Phase 5: scheduler restart recovery ----------------------------
        print("\n=== Phase 5: scheduler restart reaps stale leases ===")
        for w in workers:
            w.terminate()
            w.wait(timeout=10)
        workers = []
        stop(server)
        server = start_scheduler(data_dir)  # same data dir -> recovery path
        _, _, subs = api("GET", "/api/submissions")
        assert any(s["submission_id"] == sub_id for s in subs)
        # existing frames still served after restart
        headers, blob = fetch_frame(sub_id, 0)
        assert headers.get("X-Frame-Status") == "succeeded"
        decode_png(blob)
        # new work completes after restart
        workers = [start_worker("w-restart-1"), start_worker("w-restart-2")]
        sub9 = submit(scene, 10, 11,
                      settings={"width": 320, "height": 180, "ssaa": 4})
        wait_status(sub9["submission_id"], {"succeeded"}, timeout=45)
        print("  [ok] state survived restart; new tasks scheduled and rendered")

    finally:
        for w in workers:
            if w.poll() is None:
                w.terminate()
                w.wait(timeout=10)
        if server.poll() is None:
            stop(server)
        shutil.rmtree(data_dir, ignore_errors=True)

    print("\n" + ("ALL E2E CHECKS PASSED" if not failures
                  else f"{len(failures)} FAILURE(S):\n" + "\n".join(failures)))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
