# kcm-controller-throughput

Daily regression test for one kube-controller-manager controller on a provisioned
control plane (PCP) tier. Phase 1 covers the ReplicaSet controller at tier 4XL.

- `replicaset/`: ClusterLoader2 closed-loop wave load. 50 x 100 ReplicaSets are
  patched 2 -> 3 -> 2 replicas per wave at 1,000 patches/s; each half-wave waits
  until every ReplicaSet is reconciled. Pods land on KWOK fake nodes.
- `common/kcm-sampler.yaml`: pod that scrapes `workqueue_*{name="<controller>"}`
  from the in-cluster KCM metrics API on every master IP every 15 s.
- `common/kwok/`: vendored KWOK, node template and FlowSchema.
- `evaluate.py`: grades a run (standard library only), writes `verdict.json`.
- `testdata/`: unit tests (`cd testdata && python3 -m unittest`).

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
| `leader_found`, `no_counter_reset`, `queue_fed` | metrics cover the load window; queue depth > 0 |
| `no_leader_change`, `masters_unchanged` | KCM lease and master IPs identical before/after |
| `cleanup` | 0 test pods left |

Any failed check gives datapoint 1 (CloudWatch) and a failed TaskRun, after the
results are uploaded.
