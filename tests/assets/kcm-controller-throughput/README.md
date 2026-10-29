# kcm-controller-throughput

Daily regression test for one kube-controller-manager controller on a provisioned
control plane (PCP) tier. It currently covers the ReplicaSet controller at tier 4XL.

- `replicaset/`: ClusterLoader2 closed-loop wave load. 50 x 100 ReplicaSets are
  patched 2 -> 3 -> 2 replicas per wave at 1,000 patches/s; each half-wave waits
  until every ReplicaSet is reconciled. Pods land on KWOK fake nodes.
- `common/kcm-sampler.yaml`: pod that scrapes `workqueue_*{name="<controller>"}`
  from the in-cluster KCM metrics API on every API server IP every 15 s.
- `common/kwok/`: vendored KWOK, node template and FlowSchema.
- `evaluate.py`: grades a run (standard library only), writes `verdict.json`.
- `testdata/`: unit tests (`cd testdata && python3 -B -m unittest -v`; `-B` keeps
  `__pycache__` out of the tree).

Tekton: `tasks/setup/eks/kubectl-kwok-install.yaml`,
`tasks/generators/clusterloader/kcm-controller-throughput.yaml`,
`pipelines/eks/awscli-eks-cl2-pcp-kcm-throughput.yaml` (self-provisioning) and
`pipelines/eks/awscli-eks-cl2-pcp-kcm-throughput-attach.yaml` (existing cluster).

## Pins

| What | Pin |
|---|---|
| ClusterLoader2 | kubernetes/perf-tests `bc64526c4ca0bdec39c8149f822848fc27868286` |
| KWOK | `v0.8.0`; `kwok.yaml` and `stage-fast.yaml` copied unmodified from the release assets |
| Kubernetes | >= 1.29 (FlowSchema `flowcontrol.apiserver.k8s.io/v1`) |

To bump KWOK, replace both files from
`https://github.com/kubernetes-sigs/kwok/releases/download/<tag>/{kwok.yaml,stage-fast.yaml}`
and update this table.

## Grading

| Check | Meaning |
|---|---|
| `throughput` | median ReplicaSet changes/s per half-wave (junit) >= `min-rs-per-s` |
| `busy_workers` | delta `workqueue_work_duration_seconds_sum` / delta t on the KCM leader >= `busy-ratio` x `expected-workers` |
| `cl2_exit_0`, `reconcile_all_waves`, `all_waves_ran` | CL2 finished and every half-wave reconciled |
| `leader_found`, `single_leader`, `no_counter_reset`, `queue_fed` | the KCM leader's queue is seen on one API server IP (any other IP has under 5% of its syncs), without a counter reset; queue depth > 0 |
| `overrides_parsed`, `expected_workers_set` | tier sizes read from the overrides file; `expected-workers` > 0 |
| `no_leader_change`, `apiservers_unchanged` | KCM lease and API server IPs identical before/after |
| `cleanup` | 0 test pods left |
| `evaluate_ran` | false; only in the fallback verdict the Task writes when evaluate.py crashes |

Any failed check gives datapoint 1 and a failed TaskRun, after the results upload
is attempted. The 0/1 datapoint goes to CloudWatch namespace `<kubernetes-version>`,
metric `<metric-name>`, dimension `Nodes=<kwok-nodes>` (plus a per-run copy with
`RunId`). A preflight failure, or a 0/1 datapoint that CloudWatch rejects, fails the
TaskRun without a datapoint, so an alarm on this series should treat missing data
as breaching.

Resolution: CL2 polls reconcile state every 5 s, so reconcile times mostly fall on
5 s steps; one step is about 16-18% of a 4XL half-wave. With `min-rs-per-s` at
0.85 x a baseline median, a two-step slowdown (about 25% lower throughput) fails,
a one-step slowdown (about 14-16%) sits at the gate and may pass or fail, and
smaller drops are not visible. `busy_workers` has only a lower bound: it catches
fewer workers than `expected-workers`, not more.
