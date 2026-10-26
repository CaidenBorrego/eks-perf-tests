"""Unit tests for evaluate.py. Run from this directory: python3 -m unittest -v"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.dirname(HERE)
sys.path.insert(0, ASSETS)
import evaluate  # noqa: E402

BASELINE_JUNIT = os.path.join(HERE, "baseline-4xl-run2", "artifacts", "junit.xml")
OVERRIDES_4XL = os.path.join(ASSETS, "replicaset", "overrides-4xl.yaml")

START = 1_700_000_000
LEADER, STANDBY = "10.0.0.1", "10.0.0.2"


def synthetic_junit(waves=2, scale_s=5.0, reconcile_s=25.0, fail_reconcile=False):
    """CL2-shaped junit: setup + settle, then per half-wave Start/Scale/Reconcile."""
    cases = [("replicaset-worker-tuning overall (config.yaml)", 9999.0, False),
             ("replicaset-worker-tuning: [step: 01] Create ReplicaSets", 50.0, False),
             ("replicaset-worker-tuning: [step: 02] Settle before load [00] - Settle", 60.0, False)]
    step = 3
    for i in range(waves):
        for d in ("up", "down"):
            cases.append((f"replicaset-worker-tuning: [step: {step:02d}] Start wave {i} {d} [00] - Wave{i}{d}", 1.0, False))
            cases.append((f"replicaset-worker-tuning: [step: {step + 1:02d}] Scale {d} wave {i}", scale_s, False))
            cases.append((f"replicaset-worker-tuning: [step: {step + 2:02d}] Reconcile wave {i} {d} [00] - Wave{i}{d}",
                          reconcile_s, fail_reconcile and i == waves - 1 and d == "down"))
            step += 3
    cases.append(("replicaset-worker-tuning: [step: 99] Delete ReplicaSets", 30.0, False))
    body = []
    for name, t, fail in cases:
        inner = '<failure type="Failure">timeout</failure>' if fail else ""
        body.append(f'  <testcase name="{name}" classname="ClusterLoaderV2" time="{t}">{inner}</testcase>')
    return '<?xml version="1.0" encoding="UTF-8"?>\n<testsuite name="ClusterLoaderV2">\n' + "\n".join(body) + "\n</testsuite>\n"


def synthetic_samples(t0, t1, sum_rate=5.0, count_rate=300.0, depth=4900.0, reset_at=None):
    """Leader series every 15 s over [t0, t1]; standby emits only non-matching lines."""
    lines = []
    for k, ts in enumerate(range(int(t0), int(t1) + 1, 15)):
        dt = ts - t0
        sm, ct = 1000.0 + sum_rate * dt, 50000.0 + count_rate * dt
        if reset_at is not None and ts >= reset_at:
            sm, ct = sum_rate * (ts - reset_at), count_rate * (ts - reset_at)
        lines += [f'{ts} {LEADER} workqueue_work_duration_seconds_sum{{name="replicaset"}} {sm}',
                  f'{ts} {LEADER} workqueue_work_duration_seconds_count{{name="replicaset"}} {ct}',
                  f'{ts} {LEADER} workqueue_depth{{name="replicaset"}} {depth}',
                  f'{ts} {LEADER} workqueue_adds_total{{name="replicaset"}} {ct}']
    # A standby KCM has no replicaset queue; the sampler's grep drops everything, but a
    # stray non-matching line must not make it a leader candidate.
    lines.append(f"{int(t0)} {STANDBY} garbage line")
    return "\n".join(lines) + "\n"


class Base(unittest.TestCase):
    def setUp(self):
        self.run = tempfile.mkdtemp(prefix="kcm-eval-")
        os.makedirs(os.path.join(self.run, "artifacts"))
        self.addCleanup(shutil.rmtree, self.run)

    def write(self, name, text):
        with open(os.path.join(self.run, name), "w") as f:
            f.write(text)

    def env(self, **kw):
        e = {"RUN_DIR": self.run, "OVERRIDES": self.overrides, "EXPECTED_WORKERS": "5",
             "MIN_RS_PER_S": "100", "BUSY_RATIO": "0.8", "WARMUP_S": "30"}
        e.update({k: str(v) for k, v in kw.items()})
        return e

    def valid_run(self, junit_xml, **sample_kw):
        """Write a clean run around junit_xml; samples cover the whole load window."""
        with open(os.path.join(self.run, "artifacts", "junit.xml"), "w") as f:
            f.write(junit_xml)
        self.write("start.epoch", str(START))
        self.write("cl2.exit", "0")
        waves, _ = evaluate.waves_from_junit(os.path.join(self.run, "artifacts", "junit.xml"), START)
        t0 = min(w["start"] for w in waves.values()) - 30
        t1 = max(w["end"] for w in waves.values()) + 30
        self.write("samples.txt", synthetic_samples(t0, t1, **sample_kw))
        self.write("lease.before", "ip-10-0-0-1_abc 0")
        self.write("lease.after", "ip-10-0-0-1_abc 0")
        self.write("ips.before", f"{LEADER}\n{STANDBY}")
        self.write("ips.after", f"{LEADER}\n{STANDBY}")
        self.write("leftover.pods", "0")
        return waves


class T1Baseline(Base):
    """Baseline 4XL run (5 workers, 20 waves); its run notes report 161 RS changes/s."""
    overrides = OVERRIDES_4XL

    def test_fixture_present(self):
        self.assertTrue(os.path.exists(BASELINE_JUNIT))

    def test_throughput_matches_notes(self):
        waves, failed = evaluate.waves_from_junit(BASELINE_JUNIT, 0.0)
        self.assertFalse(failed)
        self.assertEqual(len(waves), 40)
        self.assertEqual(evaluate.rs_total([OVERRIDES_4XL]), 5000)
        with open(BASELINE_JUNIT) as f:
            self.valid_run(f.read())
        v = evaluate.evaluate(self.env(WAVES=20))
        self.assertAlmostEqual(v["metrics"]["rs_per_s_median"], 161, delta=161 * 0.05)
        self.assertEqual(v["metrics"]["half_waves"], 40)
        self.assertTrue(v["checks"]["reconcile_all_waves"])
        self.assertTrue(v["checks"]["all_waves_ran"])
        self.assertTrue(v["checks"]["throughput"])
        self.assertTrue(v["passed"], v)

    def test_threshold_above_baseline_fails(self):
        with open(BASELINE_JUNIT) as f:
            self.valid_run(f.read())
        v = evaluate.evaluate(self.env(MIN_RS_PER_S=200))
        self.assertFalse(v["checks"]["throughput"])
        self.assertEqual(v["datapoint"], 1)


class T2Synthetic(Base):
    def setUp(self):
        super().setUp()
        self.overrides = os.path.join(self.run, "ov.yaml")
        self.write("ov.yaml", "CL2_NAMESPACES: 50\nCL2_RS_PER_NAMESPACE: 100\n")

    def test_leader_busy_and_syncs(self):
        self.valid_run(synthetic_junit(waves=5))
        v = evaluate.evaluate(self.env(WAVES=5))
        m = v["metrics"]
        self.assertEqual(m["leader_ip"], LEADER)
        self.assertAlmostEqual(m["busy_workers"], 5.0, places=2)
        self.assertAlmostEqual(m["syncs_per_s"], 300.0, places=1)
        self.assertAlmostEqual(m["rs_per_s_median"], 5000 / 30.0, places=1)
        self.assertTrue(v["passed"], v)
        self.assertEqual(v["datapoint"], 0)

    def test_counter_reset(self):
        waves = self.valid_run(synthetic_junit(waves=5))
        mid = (min(w["start"] for w in waves.values()) + max(w["end"] for w in waves.values())) / 2
        self.write("samples.txt", synthetic_samples(START, START + 1000, reset_at=int(mid)))
        v = evaluate.evaluate(self.env())
        self.assertEqual(v["metrics"]["leader_ip"], LEADER)
        self.assertFalse(v["checks"]["no_counter_reset"])
        self.assertFalse(v["passed"])

    def test_depth_zero(self):
        self.valid_run(synthetic_junit(waves=5), depth=0.0)
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["checks"]["queue_fed"])
        self.assertFalse(v["passed"])

    def test_lease_transition(self):
        self.valid_run(synthetic_junit(waves=5))
        self.write("lease.after", "ip-10-0-0-2_def 1")
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["checks"]["no_leader_change"])
        self.assertFalse(v["passed"])

    def test_lease_holder_restart_same_transitions(self):
        self.valid_run(synthetic_junit(waves=5))
        self.write("lease.after", "ip-10-0-0-1_new 0")
        self.assertFalse(evaluate.evaluate(self.env())["checks"]["no_leader_change"])

    def test_expected_workers_20(self):
        self.valid_run(synthetic_junit(waves=5))
        v = evaluate.evaluate(self.env(EXPECTED_WORKERS=20))
        self.assertAlmostEqual(v["metrics"]["busy_workers"], 5.0, places=2)
        self.assertFalse(v["checks"]["busy_workers"])
        self.assertFalse(v["passed"])

    def test_reconcile_failure(self):
        self.valid_run(synthetic_junit(waves=3, fail_reconcile=True))
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["checks"]["reconcile_all_waves"])

    def test_missing_waves(self):
        self.valid_run(synthetic_junit(waves=3))
        v = evaluate.evaluate(self.env(WAVES=10))
        self.assertFalse(v["checks"]["all_waves_ran"])
        self.assertFalse(v["passed"])

    def test_no_samples(self):
        self.valid_run(synthetic_junit(waves=5))
        self.write("samples.txt", "")
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["checks"]["leader_found"])
        self.assertFalse(v["checks"]["busy_workers"])
        self.assertFalse(v["passed"])

    def test_missing_files_fail_closed(self):
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["passed"])
        for k in ("cl2_exit_0", "reconcile_all_waves", "no_leader_change", "masters_unchanged", "cleanup"):
            self.assertFalse(v["checks"][k], k)

    def test_masters_changed_and_leftovers(self):
        self.valid_run(synthetic_junit(waves=5))
        self.write("ips.after", LEADER)
        self.write("leftover.pods", "3")
        v = evaluate.evaluate(self.env())
        self.assertFalse(v["checks"]["masters_unchanged"])
        self.assertFalse(v["checks"]["cleanup"])

    def test_cl2_nonzero(self):
        self.valid_run(synthetic_junit(waves=5))
        self.write("cl2.exit", "1")
        self.assertFalse(evaluate.evaluate(self.env())["checks"]["cl2_exit_0"])

    def test_short_run_keeps_window(self):
        # Smoke scale: 2 waves of ~6 s each; the 30 s trim must not empty the window.
        self.write("ov.yaml", "CL2_NAMESPACES: 1\nCL2_RS_PER_NAMESPACE: 5\n")
        self.valid_run(synthetic_junit(waves=2, scale_s=1.0, reconcile_s=20.0))
        v = evaluate.evaluate(self.env(MIN_RS_PER_S=0, EXPECTED_WORKERS=0))
        self.assertGreater(v["metrics"]["load_window_s"], 0)
        self.assertTrue(v["checks"]["leader_found"], v)

    def test_cli_writes_verdict(self):
        self.valid_run(synthetic_junit(waves=5))
        env = dict(os.environ, **self.env())
        p = subprocess.run([sys.executable, os.path.join(ASSETS, "evaluate.py")], env=env,
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        with open(os.path.join(self.run, "verdict.json")) as f:
            v = json.load(f)
        self.assertEqual(v["datapoint"], 0)


if __name__ == "__main__":
    unittest.main()
