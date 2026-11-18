"""Render-attribution diagnostics: replayable adoption decisions.

Every result adoption (accepted or refused) is stored with a
self-contained ``evidence`` snapshot.  This module can re-derive the
verdict from that snapshot *without* the live database, and exposes
human-readable diagnosis of an execution chain so "a newer frame was
overwritten by an old worker" can be traced to a concrete stage:

    claim -> render -> report -> validate -> adopt

The staged fault matrix runs the same checks two ways on purpose:

* ``stale_lease``     — a lost/stalled worker whose attempt is superseded;
* ``out_of_order``    — two nodes finish in reverse order, but the later
                        generation still wins.

Comparing the two distinguishes "reported after the lease was reaped"
(loss/recovery) from "an older result arrived late but its generation was
still live" (ordering / overwrite).  Only the second would indicate a
result-attribution bug.

CLI (assists the panel, it does not replace it)::

    python3 -m renderfarm.diagnostics replay REPORT.json
    python3 -m renderfarm.diagnostics matrix --out report.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from typing import Any, Optional

ACCEPTED = "accepted"
REFUSED = "refused"


def replay_adoption(record: dict) -> dict:
    """Re-derive one adoption verdict from its evidence snapshot alone.

    The input may be a full adoption row (as returned by the store/API) or
    just its ``evidence`` object.  Only fields inside the snapshot are
    consulted, so a report file can be replayed elsewhere.
    """
    ev = record.get("evidence", record)
    attempt = ev.get("attempt") or {}
    task = ev.get("task") or {}
    decision = record.get("decision")
    stage = record.get("stage")
    rules: list[dict] = []

    def rule(name: str, expected: Any, actual: Any, passed: bool) -> bool:
        rules.append({"rule": name, "expected": expected,
                      "actual": actual, "pass": passed})
        return passed

    ok = True
    ok &= rule("attempt observed active", "active", attempt.get("observed_state"),
               attempt.get("observed_state") == "active")
    ok &= rule("reporter owns attempt", attempt.get("worker_id"),
               attempt.get("observed_worker"),
               attempt.get("observed_worker") == attempt.get("worker_id"))
    gen_ok = (task.get("generation") is not None
              and attempt.get("generation") == task.get("generation"))
    ok &= rule("attempt generation matches live lease", task.get("generation"),
               attempt.get("generation"), gen_ok)
    lease_ok = (task.get("status") == "leased"
                and task.get("lease_owner") == attempt.get("worker_id"))
    ok &= rule("task leased to reporting worker", "leased/owner",
               f"{task.get('status')}/{task.get('lease_owner')}", lease_ok)
    if ev.get("validation_ok") is False:
        ok &= rule("payload validated", True, False, False)
    if decision == ACCEPTED:
        ok &= rule("frame revision recorded", True,
                   bool(ev.get("frame_revision")),
                   bool(ev.get("frame_revision")))
        ok &= rule("blob committed", True, bool(record.get("blob_sha")),
                   bool(record.get("blob_sha")))
        ok &= rule("payload validated", True, ev.get("validation_ok"),
                   bool(ev.get("validation_ok")))

    replayed = ACCEPTED if ok else REFUSED
    return {
        "adoption_id": record.get("adoption_id"),
        "task_key": ev.get("task_key"),
        "frame": ev.get("frame"),
        "frame_revision": ev.get("frame_revision"),
        "attempt_id": attempt.get("attempt_id"),
        "worker_id": attempt.get("worker_id"),
        "generation": attempt.get("generation"),
        "recorded_decision": decision,
        "recorded_stage": stage,
        "replayed_decision": replayed,
        "consistent": replayed == decision,
        "rules": rules,
    }


def diagnose_chain(task: dict, attempts: list[dict],
                   adoptions: list[dict]) -> dict:
    """Locate the stage where result attribution diverged, if anywhere."""
    accepted = [a for a in adoptions if a.get("decision") == ACCEPTED]
    refused = [a for a in adoptions if a.get("decision") == REFUSED]
    stages = {}
    for a in refused:
        stages.setdefault(a.get("stage"), 0)
        stages[a.get("stage")] += 1

    anomalies: list[dict] = []
    last_accepted = accepted[-1] if accepted else None
    if task.get("status") == "succeeded" and not last_accepted:
        anomalies.append({
            "kind": "adoption_missing",
            "stage": "adopt",
            "detail": "frame is succeeded but no accepted adoption row exists",
        })
    if task.get("status") == "succeeded" and last_accepted:
        if last_accepted.get("blob_sha") != task.get("blob_sha"):
            anomalies.append({
                "kind": "adoption_blob_mismatch",
                "stage": "adopt",
                "detail": f"adoption blob {last_accepted.get('blob_sha')} != "
                          f"task blob {task.get('blob_sha')}",
            })
        if last_accepted.get("frame_revision") != revision_of(task):
            anomalies.append({
                "kind": "frame_revision_mismatch",
                "stage": "adopt",
                "detail": f"adoption revision "
                          f"{last_accepted.get('frame_revision')} != task "
                          f"revision {revision_of(task)}",
            })
    for a in adoptions:
        replayed = replay_adoption(a)
        if not replayed["consistent"]:
            anomalies.append({
                "kind": "unreplayable_decision",
                "stage": a.get("stage"),
                "detail": f"recorded {a.get('decision')} but replay yields "
                          f"{replayed['replayed_decision']}",
            })
    superseded = [a for a in attempts if a.get("state") == "superseded"]
    adopted_attempts = [a for a in attempts if a.get("adopted")]
    if len(adopted_attempts) > 1:
        anomalies.append({
            "kind": "multiple_adopted_attempts",
            "stage": "adopt",
            "detail": "more than one attempt marked adopted for the same frame",
        })
    phases = sorted({a.get("phase") for a in attempts if a.get("phase")})
    return {
        "task_key": task.get("task_key"),
        "frame": task.get("frame"),
        "frame_revision": revision_of(task),
        "status": task.get("status"),
        "adopted_attempt": last_accepted.get("attempt_id") if last_accepted else None,
        "adopted_worker": last_accepted.get("worker_id") if last_accepted else None,
        "adopted_generation": (last_accepted.get("generation")
                               if last_accepted else None),
        "attempt_count": len(attempts),
        "superseded_attempts": len(superseded),
        "refused_by_stage": stages,
        "phases_seen": phases,
        "adoptions": [{
            "adoption_id": a.get("adoption_id"),
            "decision": a.get("decision"),
            "stage": a.get("stage"),
            "attempt_id": a.get("attempt_id"),
            "worker_id": a.get("worker_id"),
            "generation": a.get("generation"),
            "frame_revision": a.get("frame_revision"),
            "reason": a.get("reason"),
            "blob_sha": a.get("blob_sha"),
            "evidence": a.get("evidence"),
            "replay_consistent": replay_adoption(a)["consistent"],
        } for a in adoptions],
        "anomalies": anomalies,
        "ok": not anomalies,
    }


def revision_of(task: dict) -> Optional[str]:
    """Readable frame revision for a task row (None if identity is absent)."""
    from . import identity
    if not task.get("scene_id"):
        return None
    return identity.frame_revision(
        task["scene_id"], task["version"], int(task["frame"]),
        task["settings_hash"])


# ---------------------------------------------------------------------------
# Staged, in-process fault matrix (used by tests and by the CLI)
# ---------------------------------------------------------------------------
def _scene() -> dict:
    return {
        "scene_id": "diag_scene",
        "total_frames": 1,
        "background": {"top": [8, 8, 12], "bottom": [30, 40, 60]},
        "objects": [{
            "id": "ball", "type": "circle", "radius": 0.1,
            "color": [230, 70, 60],
            "path": {"start": [0.2, 0.5], "end": [0.8, 0.5],
                     "easing": "linear", "loop": False},
        }],
    }


def _valid_png(scene: dict, settings: dict) -> bytes:
    from .renderer import render_frame
    return render_frame(scene, 0, settings)


def run_out_of_order(data_dir: str) -> dict:
    """Two nodes finish in reverse order; the newer generation must win."""
    from .renderer import DEFAULT_SETTINGS
    from .storage import FarmStore

    store = FarmStore(data_dir)
    try:
        scene = _scene()
        settings = dict(DEFAULT_SETTINGS)
        info = store.create_submission("diag-ooo", scene, settings, 0, 0)
        tkey = store.list_tasks("diag-ooo")[0]["task_key"]
        png = _valid_png(scene, settings)

        older = store.claim_task("node-older", 1.0, now=100.0)
        # older's lease expires and the scheduler hands the frame to a newer node
        store.reap_expired(now=102.0)
        newer = store.claim_task("node-newer", 30.0, now=103.0)
        # reverse completion: the newer generation reports first
        newer_out = store.succeed_attempt(newer["attempt_id"], "node-newer", png,
                                          now=104.0,
                                          worker_meta={"finish_order": "first"})
        # then the old node finishes and reports late
        older_out = store.succeed_attempt(older["attempt_id"], "node-older", png,
                                          now=105.0,
                                          worker_meta={"finish_order": "second"})
        task = store.get_task(tkey)
        chain = diagnose_chain(task, store.attempt_trace(tkey),
                               store.adoptions_for_task(tkey))
        return {
            "scenario": "out_of_order",
            "submission_id": info["submission_id"],
            "version": info["version"],
            "settings_hash": info["settings_hash"],
            "newer_outcome": newer_out,
            "older_outcome": older_out,
            "adopted_worker": chain["adopted_worker"],
            "final_status": task["status"],
            "chain": chain,
        }
    finally:
        store.close()


def run_stale_lease(data_dir: str) -> dict:
    """A stalled node's lease is reaped; its late report must be refused."""
    from .renderer import DEFAULT_SETTINGS
    from .storage import FarmStore

    store = FarmStore(data_dir)
    try:
        scene = _scene()
        settings = dict(DEFAULT_SETTINGS)
        info = store.create_submission("diag-stall", scene, settings, 0, 0)
        tkey = store.list_tasks("diag-stall")[0]["task_key"]
        png = _valid_png(scene, settings)

        stalled = store.claim_task("node-stalled", 1.0, now=200.0)
        store.reap_expired(now=202.0)
        successor = store.claim_task("node-successor", 30.0, now=203.0)
        succ_out = store.succeed_attempt(successor["attempt_id"],
                                         "node-successor", png, now=204.0)
        late_out = store.succeed_attempt(stalled["attempt_id"], "node-stalled",
                                         png, now=205.0)
        task = store.get_task(tkey)
        chain = diagnose_chain(task, store.attempt_trace(tkey),
                               store.adoptions_for_task(tkey))
        return {
            "scenario": "stale_lease",
            "submission_id": info["submission_id"],
            "version": info["version"],
            "settings_hash": info["settings_hash"],
            "successor_outcome": succ_out,
            "late_outcome": late_out,
            "adopted_worker": chain["adopted_worker"],
            "final_status": task["status"],
            "chain": chain,
        }
    finally:
        store.close()


def run_matrix(out_path: Optional[str] = None,
               repeat: int = 2) -> dict:
    """Run both scenarios (repeated) and require identical replays."""
    scenarios = []
    replays: list[list[dict]] = []
    for i in range(repeat):
        d1 = tempfile.mkdtemp(prefix=f"diag-ooo-{i}-")
        d2 = tempfile.mkdtemp(prefix=f"diag-stall-{i}-")
        try:
            scenarios.append(run_out_of_order(d1))
            scenarios.append(run_stale_lease(d2))
        finally:
            _cleanup(d1)
            _cleanup(d2)
    # determinism: every repeated run of a scenario must replay identically
    per_scenario: dict[str, list[dict]] = {}
    for s in scenarios:
        view = {
            "adopted_worker": s["adopted_worker"],
            "final_status": s["final_status"],
            "decisions": [
                (a["decision"], a["stage"]) for a in s["chain"]["adoptions"]
            ],
            "normalized": s.get("newer_outcome", s.get("successor_outcome")),
            "refused": s.get("older_outcome", s.get("late_outcome")),
        }
        per_scenario.setdefault(s["scenario"], []).append(view)
    determinism = {}
    for name, runs in per_scenario.items():
        first = json.dumps(runs[0], sort_keys=True)
        determinism[name] = all(
            json.dumps(r, sort_keys=True) == first for r in runs)
    report = {
        "scenarios": scenarios,
        "determinism": determinism,
        "repeat": repeat,
        "all_consistent": all(determinism.values()) and all(
            s["chain"]["ok"] for s in scenarios),
    }
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, sort_keys=True)
    return report


def _cleanup(path: str) -> None:
    import shutil
    shutil.rmtree(path, ignore_errors=True)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render attribution diagnostics (panel is the primary UI)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("replay", help="replay adoption decisions from a report")
    rp.add_argument("report", help="JSON file containing adoption rows")
    mx = sub.add_parser("matrix", help="run the staged out-of-order/stale matrix")
    mx.add_argument("--out", default=None)
    mx.add_argument("--repeat", type=int, default=2)
    args = parser.parse_args(argv)

    if args.cmd == "replay":
        with open(args.report, "rb") as fh:
            data = json.loads(fh.read())
        rows = data.get("adoptions") if isinstance(data, dict) else data
        if rows is None:
            rows = []
        results = [replay_adoption(r) for r in rows]
        bad = [r for r in results if not r["consistent"]]
        print(json.dumps({"replayed": len(results), "inconsistent": bad},
                         ensure_ascii=False, indent=2))
        return 1 if bad else 0

    report = run_matrix(args.out, args.repeat)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["all_consistent"] else 1


if __name__ == "__main__":
    sys.exit(main())
