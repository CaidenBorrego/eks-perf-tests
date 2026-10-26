#!/usr/bin/env python3
"""Grade one kcm-controller-throughput run.

Env:
  RUN_DIR          dir holding artifacts/junit.xml, start.epoch, cl2.exit, samples.txt,
                   lease.before, lease.after, ips.before, ips.after, leftover.pods
  OVERRIDES        space-separated overrides files (tier file, then run-overrides)
  EXPECTED_WORKERS configured --concurrent-<controller>-syncs on the cluster
  MIN_RS_PER_S     throughput gate (median RS changes/s per half-wave)
  BUSY_RATIO       busy_workers >= BUSY_RATIO * EXPECTED_WORKERS (default 0.8)
  WARMUP_S         seconds trimmed from each end of the load window (default 30;
                   capped at a quarter of the load span so short runs keep a window)
  WAVES            expected number of waves (> 0); exactly 2*WAVES reconciled half-waves
                   must be in junit

Writes RUN_DIR/verdict.json: {"passed", "datapoint" (0 pass, 1 fail), "checks", "metrics"}.
Standard library only.
"""
import json
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET

# Same half-wave matcher as the original manual load runner these assets come from.
WAVE_RE = re.compile(r"(Scale|Reconcile) (?:(up|down) wave (\d+)|wave (\d+) (up|down))")
SAMPLE_RE = re.compile(r'^(\d+) (\S+) (workqueue_\w+)\{name="[^"]+"\} (\S+)$')
OVERRIDE_RE = re.compile(r"\s*(CL2_NAMESPACES|CL2_RS_PER_NAMESPACE):\s*(\d+)")


def read(run, name, default=""):
    p = os.path.join(run, name)
    if not os.path.exists(p):
        return default
    with open(p) as f:
        return f.read().strip()


def parse_overrides(paths):
    """CL2_NAMESPACES / CL2_RS_PER_NAMESPACE found in the overrides files (last wins)."""
    vals = {}
    for p in paths:
        try:
            with open(p) as f:
                lines = f.readlines()
        except OSError:
            continue  # reported by the overrides_parsed check
        for line in lines:
            m = OVERRIDE_RE.match(line)
            if m:
                vals[m.group(1)] = int(m.group(2))
    return vals


def rs_total(paths):
    vals = {"CL2_NAMESPACES": 50, "CL2_RS_PER_NAMESPACE": 100}  # config.yaml defaults
    vals.update(parse_overrides(paths))
    return vals["CL2_NAMESPACES"] * vals["CL2_RS_PER_NAMESPACE"]


def waves_from_junit(path, start):
    """Per half-wave {start, end, scale, reconcile} keyed by (wave, dir); and reconcile failure."""
    root = ET.parse(path).getroot()
    t, waves, failed = start, {}, False
    for tc in root.iter("testcase"):
        name, d = tc.get("name", ""), float(tc.get("time", "0"))
        if "overall" in name:
            continue
        m = WAVE_RE.search(name)
        if m:
            key = (int(m.group(3) or m.group(4)), m.group(2) or m.group(5))
            w = waves.setdefault(key, {"start": t, "scale": 0.0, "reconcile": 0.0, "reconciled": False})
            w["scale" if m.group(1) == "Scale" else "reconcile"] += d
            if m.group(1) == "Reconcile":
                w["reconciled"] = True
            w["end"] = t + d
            if m.group(1) == "Reconcile" and (tc.find("failure") is not None or tc.find("error") is not None):
                failed = True
        t += d
    return waves, failed


def parse_samples(text):
    out = {}
    for line in text.splitlines():
        m = SAMPLE_RE.match(line.strip())
        if m:
            ts, ip, metric, val = int(m.group(1)), m.group(2), m.group(3), float(m.group(4))
            out.setdefault(ip, {}).setdefault(metric, []).append((ts, val))
    for series in out.values():
        for v in series.values():
            v.sort()
    return out


def rate(series):
    if len(series) >= 2 and series[-1][0] > series[0][0]:
        return (series[-1][1] - series[0][1]) / (series[-1][0] - series[0][0])
    return 0.0


def evaluate(env):
    run = env["RUN_DIR"]
    expected = float(env["EXPECTED_WORKERS"])
    min_rs = float(env["MIN_RS_PER_S"])
    busy_ratio = float(env.get("BUSY_RATIO") or "0.8")
    warmup = float(env.get("WARMUP_S") or "30")
    try:
        want_waves = int(env.get("WAVES") or "0")
    except ValueError:
        want_waves = 0
    checks, metrics = {}, {}

    try:
        cl2_exit = int(read(run, "cl2.exit", "99"))
    except ValueError:
        cl2_exit = 99
    checks["cl2_exit_0"] = cl2_exit == 0

    try:
        start = float(read(run, "start.epoch", "0"))
    except ValueError:
        start = 0.0
    junit = os.path.join(run, "artifacts", "junit.xml")
    if os.path.exists(junit):
        waves, reconcile_failed = waves_from_junit(junit, start)
    else:
        waves, reconcile_failed = {}, True
    checks["reconcile_all_waves"] = bool(waves) and not reconcile_failed
    complete = [w for w in waves.values() if w["reconciled"]]
    checks["all_waves_ran"] = want_waves > 0 and len(complete) == 2 * want_waves

    paths = env.get("OVERRIDES", "").split()
    # The denominator must come from the tier file, not silently from config defaults.
    checks["overrides_parsed"] = set(parse_overrides(paths)) == set(("CL2_NAMESPACES", "CL2_RS_PER_NAMESPACE"))
    total = rs_total(paths)
    rates = [total / (w["scale"] + w["reconcile"]) for w in waves.values() if w["scale"] + w["reconcile"] > 0]
    metrics["rs_total"] = total
    metrics["rs_per_s_median"] = round(statistics.median(rates), 1) if rates else 0.0
    metrics["rs_per_s_min"] = round(min(rates), 1) if rates else 0.0
    metrics["half_waves"] = len(rates)
    checks["throughput"] = bool(rates) and metrics["rs_per_s_median"] >= min_rs

    first = min((w["start"] for w in waves.values()), default=start)
    last = max((w["end"] for w in waves.values()), default=start)
    trim = min(warmup, max(last - first, 0.0) / 4)
    load_start, load_end = first + trim, last - trim
    metrics["load_window_s"] = round(load_end - load_start, 1)

    s = parse_samples(read(run, "samples.txt"))

    def win(ip, metric):
        return [v for v in s.get(ip, {}).get(metric, []) if load_start <= v[0] <= load_end]

    # Leader = the master whose queue did the most syncs in the window. Sum only the
    # positive steps so a counter reset (KCM restart) keeps the leader visible and
    # is then reported by no_counter_reset instead of hiding as "no leader".
    work = {}
    for ip in sorted(s):
        cnt = win(ip, "workqueue_work_duration_seconds_count")
        if len(cnt) >= 2:
            work[ip] = sum(max(b[1] - a[1], 0.0) for a, b in zip(cnt, cnt[1:]))
    ranked = sorted((w, ip) for ip, w in work.items() if w > 0)
    leader = ranked[-1][1] if ranked else None
    metrics["leader_ip"] = leader
    # Only the leader runs the queue; a second busy master means the window spans a
    # leader handoff, so the leader's series does not cover the whole load. Not redundant
    # with no_leader_change: it also catches a handoff and hand-back inside the run.
    checks["single_leader"] = len(ranked) <= 1 or ranked[-2][0] < 0.05 * ranked[-1][0]

    busy = syncs = depth_max = 0.0
    counter_reset = False
    if leader:
        sm = win(leader, "workqueue_work_duration_seconds_sum")
        ct = win(leader, "workqueue_work_duration_seconds_count")
        dp = win(leader, "workqueue_depth")
        for series in (sm, ct):
            counter_reset |= any(b[1] < a[1] for a, b in zip(series, series[1:]))
        busy, syncs = rate(sm), rate(ct)
        depth_max = max((v[1] for v in dp), default=0.0)
    metrics.update(busy_workers=round(busy, 2), syncs_per_s=round(syncs, 1), depth_max=depth_max,
                   expected_workers=expected)

    checks["leader_found"] = leader is not None
    checks["no_counter_reset"] = not counter_reset
    checks["expected_workers_set"] = expected > 0
    checks["busy_workers"] = leader is not None and expected > 0 and busy >= busy_ratio * expected
    checks["queue_fed"] = depth_max > 0
    lb, la = read(run, "lease.before"), read(run, "lease.after")
    checks["no_leader_change"] = lb != "" and lb == la
    ib, ia = read(run, "ips.before"), read(run, "ips.after")
    checks["masters_unchanged"] = ib != "" and ib == ia
    checks["cleanup"] = read(run, "leftover.pods", "unknown") == "0"

    passed = all(checks.values())
    return {"passed": passed, "datapoint": 0 if passed else 1, "checks": checks, "metrics": metrics}


def main():
    verdict = evaluate(os.environ)
    with open(os.path.join(os.environ["RUN_DIR"], "verdict.json"), "w") as f:
        json.dump(verdict, f, indent=2)
    print(json.dumps(verdict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
