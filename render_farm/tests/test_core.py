"""Fast in-process unit tests (no network, no subprocesses).

Run: python3 -m unittest tests.test_core -v
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from renderfarm import identity
from renderfarm.png import PNGError, decode_png, encode_png
from renderfarm.renderer import DEFAULT_SETTINGS, render_frame
from renderfarm.placeholders import placeholder_png
from renderfarm.storage import FarmStore


def _scene() -> dict:
    return {
        "scene_id": "unit_scene",
        "total_frames": 4,
        "background": {"top": [10, 10, 10], "bottom": [20, 20, 20]},
        "objects": [{
            "id": "ball", "type": "circle", "radius": 0.08,
            "color": [255, 0, 0],
            "path": {"start": [0.1, 0.5], "end": [0.9, 0.5],
                     "easing": "linear", "loop": False},
        }],
    }


class TestPNGCodec(unittest.TestCase):
    def test_roundtrip_rgb(self):
        px = b"".join(
            bytes((x * 7 % 256, y * 13 % 256, 9))
            for y in range(7) for x in range(5)
        )
        blob = encode_png(5, 7, px, 3)
        w, h, ch, raw = decode_png(blob)
        self.assertEqual((w, h, ch), (5, 7, 3))
        self.assertEqual(raw, px)

    def test_truncated_stream_rejected(self):
        blob = encode_png(8, 8, bytes(8 * 8 * 3), 3)
        for cut in (0, 8, len(blob) // 2, len(blob) - 5):
            with self.assertRaises(PNGError):
                decode_png(blob[:cut])

    def test_corrupt_byte_rejected(self):
        blob = bytearray(encode_png(6, 6, bytes(6 * 6 * 3), 3))
        # flip a byte inside IDAT (after the 8-byte signature + IHDR chunk)
        idx = 40
        blob[idx] ^= 0xFF
        with self.assertRaises(PNGError):
            decode_png(bytes(blob))

    def test_bad_signature(self):
        with self.assertRaises(PNGError):
            decode_png(b"not a png at all" * 10)

    def test_dimension_mismatch_buffer(self):
        with self.assertRaises(PNGError):
            encode_png(3, 3, bytes(10), 3)


class TestIdentity(unittest.TestCase):
    def test_scene_version_content_addressed(self):
        a, b = _scene(), _scene()
        self.assertEqual(identity.scene_version(a), identity.scene_version(b))
        b["objects"][0]["color"] = [1, 2, 3]
        self.assertNotEqual(identity.scene_version(a), identity.scene_version(b))

    def test_settings_hash_covers_render_affecting_inputs(self):
        s1 = {"width": 320, "height": 180, "ssaa": 2}
        s2 = dict(s1, ssaa=4)
        self.assertNotEqual(identity.settings_hash(s1), identity.settings_hash(s2))
        # key ordering must not matter
        self.assertEqual(
            identity.settings_hash({"a": 1, "b": 2}),
            identity.settings_hash({"b": 2, "a": 1}),
        )

    def test_task_key_unique_per_identity_tuple(self):
        k1 = identity.task_key("s", "v1", 5, "h1")
        self.assertEqual(k1, identity.task_key("s", "v1", 5, "h1"))
        self.assertNotEqual(k1, identity.task_key("s", "v2", 5, "h1"))
        self.assertNotEqual(k1, identity.task_key("s", "v1", 6, "h1"))
        self.assertNotEqual(k1, identity.task_key("s", "v1", 5, "h2"))


class TestRenderer(unittest.TestCase):
    def test_frames_differ_and_decode(self):
        scene = _scene()
        blobs = [render_frame(scene, f, DEFAULT_SETTINGS) for f in range(4)]
        decoded = [decode_png(b) for b in blobs]
        for w, h, ch, _ in decoded:
            self.assertEqual((w, h, ch), (320, 180, 3))
        self.assertEqual(len({b for b in blobs}), 4)

    def test_ball_actually_moves(self):
        scene = _scene()
        # sample the red pixel centroid-ish: count red pixels' mean x
        def red_mass_x(frame):
            _, _, _, raw = decode_png(render_frame(scene, frame, DEFAULT_SETTINGS))
            xs, total = 0, 0
            for y in range(180):
                for x in range(320):
                    r, g, b = raw[(y * 320 + x) * 3:(y * 320 + x) * 3 + 3]
                    if r > 180 and g < 80 and b < 80:
                        xs += x
                        total += 1
            self.assertGreater(total, 0)
            return xs / total
        self.assertLess(red_mass_x(0), red_mass_x(3))

    def test_frame_out_of_range(self):
        with self.assertRaises(ValueError):
            render_frame(_scene(), 99, DEFAULT_SETTINGS)

    def test_placeholder_decodes(self):
        w, h, _, _ = decode_png(placeholder_png(40, 24, "failed"))
        self.assertEqual((w, h), (40, 24))


class TestStoreLeaseLifecycle(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="farm-unit-")
        self.store = FarmStore(self.dir)
        self.scene = _scene()
        self.store.create_submission("sub-1", self.scene, DEFAULT_SETTINGS, 0, 3)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _valid_png(self) -> bytes:
        return render_frame(self.scene, 0, DEFAULT_SETTINGS)

    def test_claim_heartbeat_succeed_provenance(self):
        task = self.store.claim_task("worker-A", 30)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "leased")
        self.assertEqual(task["attempt_count"], 1)
        self.store.heartbeat(task["attempt_id"], "worker-A", 30)
        outcome = self.store.succeed_attempt(
            task["attempt_id"], "worker-A", self._valid_png())
        self.assertEqual(outcome, "ok")
        done = self.store.get_task(task["task_key"])
        self.assertEqual(done["status"], "succeeded")
        prov = self.store.task_provenance(task["task_key"])
        self.assertEqual(prov["completed_by"], "worker-A")
        self.assertTrue(os.path.exists(self.store.frame_blob_path(task["task_key"])))

    def test_half_image_rejected_and_task_requeued(self):
        task = self.store.claim_task("worker-A", 30)
        png = self._valid_png()
        outcome = self.store.succeed_attempt(
            task["attempt_id"], "worker-A", png[: len(png) // 2])
        self.assertEqual(outcome, "invalid")
        again = self.store.get_task(task["task_key"])
        self.assertEqual(again["status"], "pending")
        self.assertIsNone(again["blob_sha"])
        self.assertIn("invalid image", again["last_error"])
        # a fresh attempt can still complete it
        task2 = self.store.claim_task("worker-B", 30)
        self.assertEqual(task2["task_key"], task["task_key"])
        self.assertEqual(task2["attempt_count"], 2)
        self.assertEqual(
            self.store.succeed_attempt(task2["attempt_id"], "worker-B", png), "ok")

    def test_wrong_dimensions_rejected(self):
        task = self.store.claim_task("worker-A", 30)
        small = encode_png(10, 10, bytes(10 * 10 * 3), 3)
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "worker-A", small),
            "invalid")

    def test_expired_lease_reaped_then_old_attempt_rejected(self):
        task = self.store.claim_task("worker-A", lease_seconds=1, now=100.0)
        old = task["attempt_id"]
        # lease expires; reaper hands it back to the queue
        self.assertEqual(self.store.reap_expired(now=102.0), 1)
        self.assertEqual(self.store.get_task(task["task_key"])["status"], "pending")
        task2 = self.store.claim_task("worker-B", lease_seconds=30, now=103.0)
        self.assertNotEqual(old, task2["attempt_id"])
        # old worker reporting late: rejected, cannot overwrite
        self.assertEqual(
            self.store.succeed_attempt(old, "worker-A", self._valid_png(), now=104.0),
            "stale")
        self.assertEqual(
            self.store.fail_attempt(old, "worker-A", "late", now=104.0), "stale")
        # new attempt wins
        self.assertEqual(
            self.store.succeed_attempt(task2["attempt_id"], "worker-B",
                                       self._valid_png(), now=105.0), "ok")
        self.assertEqual(
            self.store.task_provenance(task["task_key"])["completed_by"], "worker-B")

    def test_duplicate_success_report_is_idempotent_stale(self):
        task = self.store.claim_task("worker-A", 30)
        png = self._valid_png()
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "worker-A", png), "ok")
        # reporting the same attempt again must not change anything
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "worker-A", png), "stale")

    def test_cancel_moves_pending_and_leased(self):
        task = self.store.claim_task("worker-A", 30)
        self.store.cancel_submission("sub-1")
        self.assertEqual(self.store.get_task(task["task_key"])["status"], "cancelled")
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "worker-A",
                                       self._valid_png()), "stale")

    def test_restart_recovery_and_resubmit_identity(self):
        task = self.store.claim_task("worker-A", lease_seconds=1, now=50.0)
        self.store.close()
        store2 = FarmStore(self.dir)  # reopen = scheduler restart
        self.assertEqual(store2.reap_expired(now=60.0), 1)
        self.assertEqual(store2.get_task(task["task_key"])["status"], "pending")
        # identical resubmission reuses identities
        info = store2.create_submission("sub-2", self.scene, DEFAULT_SETTINGS, 0, 3)
        self.assertEqual(sorted(info["reused_frames"]), [0, 1, 2, 3])
        # edited scene yields a brand-new version and tasks
        edited = json.loads(json.dumps(self.scene))
        edited["objects"][0]["color"] = [0, 255, 0]
        info2 = store2.create_submission("sub-3", edited, DEFAULT_SETTINGS, 0, 3)
        self.assertNotEqual(info2["version"], info["version"])
        self.assertEqual(sorted(info2["created_frames"]), [0, 1, 2, 3])
        store2.close()

    def test_expiring_one_lease_does_not_touch_other_active_attempts(self):
        # worker A keeps a healthy long lease while B's lease expires
        a = self.store.claim_task("worker-A", lease_seconds=300, now=0.0)
        b = self.store.claim_task("worker-B", lease_seconds=1, now=0.0)
        self.assertNotEqual(a["task_key"], b["task_key"])
        self.assertEqual(self.store.reap_expired(now=50.0), 1)
        prov_a = self.store.task_provenance(a["task_key"])
        self.assertTrue(any(x["state"] == "active" for x in prov_a["attempts"]))
        # A's healthy attempt can still commit
        png = render_frame(self.scene, a["frame"], DEFAULT_SETTINGS)
        self.assertEqual(
            self.store.succeed_attempt(a["attempt_id"], "worker-A", png, now=51.0),
            "ok")
        self.assertEqual(
            self.store.task_provenance(a["task_key"])["completed_by"], "worker-A")

    def test_failure_requeues_until_max_attempts(self):
        for i in range(10):  # MAX_ATTEMPTS
            task = self.store.claim_task("w", lease_seconds=1, now=float(i))
            self.assertEqual(
                self.store.fail_attempt(task["attempt_id"], "w", "boom",
                                        now=float(i)), "ok")
        self.assertEqual(self.store.list_tasks("sub-1")[0]["status"], "failed")


class TestAdoptionEvidence(unittest.TestCase):
    """Every adoption decision must carry a replayable evidence snapshot."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="farm-diag-")
        self.store = FarmStore(self.dir)
        self.scene = _scene()
        self.store.create_submission("sub-d", self.scene, DEFAULT_SETTINGS, 0, 0)
        self.tkey = self.store.list_tasks("sub-d")[0]["task_key"]
        self.png = render_frame(self.scene, 0, DEFAULT_SETTINGS)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_accepted_adoption_replays_consistently(self):
        task = self.store.claim_task("node-A", 30, now=100.0)
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "node-A", self.png,
                                       now=101.0,
                                       worker_meta={"name": "node-A"}),
            "ok")
        adoptions = self.store.adoptions_for_task(self.tkey)
        self.assertEqual(len(adoptions), 1)
        a = adoptions[0]
        self.assertEqual(a["decision"], "accepted")
        self.assertEqual(a["stage"], "adopt")
        self.assertEqual(a["generation"], 1)
        self.assertTrue(a["frame_revision"].startswith("unit_scene|"))
        replayed = self.store.replay_adoption(a)
        self.assertTrue(replayed["consistent"], replayed)
        self.assertTrue(all(r["pass"] for r in replayed["rules"]))

    def test_refused_report_records_superseded_attempt(self):
        old = self.store.claim_task("node-old", 1.0, now=100.0)
        self.store.reap_expired(now=102.0)
        new = self.store.claim_task("node-new", 30.0, now=103.0)
        # older node finishes last (reverse completion) but is superseded
        self.assertEqual(
            self.store.succeed_attempt(new["attempt_id"], "node-new", self.png,
                                       now=104.0), "ok")
        self.assertEqual(
            self.store.succeed_attempt(old["attempt_id"], "node-old", self.png,
                                       now=105.0), "stale")
        adoptions = self.store.adoptions_for_task(self.tkey)
        refusals = [a for a in adoptions if a["decision"] == "refused"]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["stage"], "report")
        self.assertIn("superseded", refusals[0]["reason"])
        for a in adoptions:
            self.assertTrue(self.store.replay_adoption(a)["consistent"], a)
        # the newer generation stays the adopted result
        from renderfarm.diagnostics import diagnose_chain
        chain = diagnose_chain(self.store.get_task(self.tkey),
                               self.store.attempt_trace(self.tkey), adoptions)
        self.assertTrue(chain["ok"], chain["anomalies"])
        self.assertEqual(chain["adopted_generation"], 2)
        self.assertEqual(chain["adopted_worker"], "node-new")

    def test_stale_lease_reason_differs_from_ordering(self):
        old = self.store.claim_task("stalled", 1.0, now=200.0)
        self.store.reap_expired(now=202.0)
        new = self.store.claim_task("successor", 30.0, now=203.0)
        self.store.succeed_attempt(new["attempt_id"], "successor", self.png,
                                   now=204.0)
        self.store.succeed_attempt(old["attempt_id"], "stalled", self.png,
                                   now=205.0)
        attempts = self.store.attempt_trace(self.tkey)
        self.assertEqual(
            [a["phase"] for a in attempts if a["state"] == "superseded"],
            ["reaped"])
        self.assertEqual(
            [a["phase"] for a in attempts if a["adopted"]], ["adopt"])

    def test_invalid_image_refusal_is_replayable(self):
        task = self.store.claim_task("node-A", 30, now=100.0)
        self.assertEqual(
            self.store.succeed_attempt(task["attempt_id"], "node-A",
                                       self.png[: len(self.png) // 2],
                                       now=101.0), "invalid")
        a = self.store.adoptions_for_task(self.tkey)[-1]
        self.assertEqual(a["decision"], "refused")
        self.assertEqual(a["stage"], "validate")
        self.assertFalse(a["evidence"]["validation_ok"])
        self.assertTrue(self.store.replay_adoption(a)["consistent"])

    def test_legacy_database_is_migrated(self):
        # Simulate a pre-diagnosis database: attempts without the new columns.
        legacy = tempfile.mkdtemp(prefix="farm-legacy-")
        try:
            import sqlite3
            conn = sqlite3.connect(os.path.join(legacy, "farm.db"))
            conn.executescript(
                "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY,"
                " task_key TEXT NOT NULL, worker_id TEXT NOT NULL,"
                " lease_expires REAL NOT NULL, state TEXT NOT NULL,"
                " reported_at REAL, error TEXT, created_at REAL NOT NULL);"
            )
            conn.commit()
            conn.close()
            store = FarmStore(legacy)
            try:
                cols = {
                    r["name"] for r in store._conn.execute(
                        "PRAGMA table_info(attempts)").fetchall()
                }
                self.assertTrue(
                    {"generation", "phase", "render_ms", "worker_meta"} <= cols)
            finally:
                store.close()
        finally:
            shutil.rmtree(legacy, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
