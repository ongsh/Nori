# Small kernel boundary: synthetic validation only

The ordinary controller now verifies that an active or recovered run's fixed task and approved version match the current approved plan. A receipt carrying a correct approval string cannot replace the task's checks with weaker checks. Recovery refuses mismatched orphan records rather than importing them as trusted input.

An opt-in `SyntheticBoundaryController` in `nori/boundary.py` adds a small fixed-policy validation boundary over the same `WorkflowController` and `Store`. It does not create a second state database or planning platform.

- The trusted controller package and control state stay outside the candidate workspace.
- The caller supplies trusted policy and controller digests. Reopening requires the same pins; missing or changed policy is not silently adopted.
- A fixed policy snapshot lives in the control directory, outside the candidate workspace.
- Candidate checks must match the fixed policy. This mode accepts only exact `MockAdapter` and built-in text/JSON checks, not candidate-provided shell scripts, real executors, manual verdicts, or policy revisions.
- Integrity is checked before state transitions and around verification/recovery. Evidence includes the policy and controller digests alongside the normal run/file evidence.
- The installed zipapp is fingerprinted as one archive; source use fingerprints the controller Python files.

## Try the disposable fixture

```sh
nori boundary-demo "$(mktemp -d)"
```

The destination must be new or empty. This runs a deliberately incomplete candidate, independent rejection, corrected candidate, final acceptance, policy-tamper rejection and reopening with the original pins. It never calls a model or modifies Nori's own source.

## Limits

This is integrity checking and a synthetic protocol test, not a hard sandbox. Same-OS-account processes can potentially tamper with both policy and pins, or alter files transiently. Directory separation, hashes, and file permissions alone do not prove hostile-process isolation. The caller must obtain pins from a trusted source, not recompute them from untrusted candidate state.

The web prototype does not automatically switch ordinary projects into this mode. Its diagram is illustrative, and real Nori self-iteration is not running. Real execution needs separately verified isolation, executor behavior, approved scope, and an independent acceptance policy. The module intentionally rejects real adapters rather than pretending this requirement is already satisfied.

Natural-language planning, per-task Agent assignment, and semantic drift evaluation remain separate capability gaps. None is made reliable merely by this boundary layer or by the interface prototype.
