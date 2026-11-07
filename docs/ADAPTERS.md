# V1 tool adapters

`CodexAdapter(home, workspace)` and `ClaudeCodeAdapter(home, workspace)` implement
`probe`, `start(run, mode='complete')`, `observe`, `request_stop`, `collect`,
`reconcile` and `inventory`. `ClaudeAdapter` is an alias. `ProcessAdapter.start_argv`
is a trusted-local API used for deterministic executable fixtures, not a model
command endpoint. All process launches use argv and never `shell=True`.

## Official command contracts

Verified against official documentation during implementation:

- [Codex non-interactive mode](https://developers.openai.com/codex/noninteractive/):
  `codex exec --json` emits JSONL. Prompts are sent on standard input using `-`.
- [Codex CLI reference](https://developers.openai.com/codex/cli/reference/):
  sandbox `workspace-write`, `--skip-git-repo-check` and `codex login status`.
- [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference):
  `claude -p --output-format stream-json --verbose --permission-mode default`,
  with piped standard input; `claude auth status` and `--version`.

No permission bypass flags, installation, login or credentials are configured.
Existing CLI settings, hooks and integrations still apply. Claude's default
permission mode can deny actions in non-interactive mode; denial is a blocker,
not a reason to skip permissions. Probe checks local CLI/auth status only; it
**does not prove model entitlement, connectivity, sufficient credit or task success**.
Missing executables, unsupported hosts and probe failures are reported explicitly.

## Process lifecycle and recovery

Linux (including Linux under WSL with working `/proc`) is supported. Native
Windows and macOS execution are explicitly unsupported by this supervisor.
Receipts are kept outside the business workspace, under `process-receipts`.
Each run directory is reserved exclusively and the original run, identity, argv,
working directory and UNKNOWN launch intent are fsynced before creating any
process. An existing or damaged run directory never triggers another launch.
Inventory includes original run/task identity so backup recovery can detect
orphan runs. A missing receipt is not evidence of non-execution.

A separate supervisor survives controller exit. It records its Linux boot ID,
PID and process start ticks and launches the CLI in a separate session/process
group. Stop writes a durable identity-bound request; the supervisor sends TERM,
then KILL after the configured grace period, and waits for the child plus all
live group members to exit. EXITED requires reaping the child and observing an
empty live process group. An actual launch error is recorded as exit 127.
Dead/mismatched supervisor identity means UNKNOWN, even if the PID vanished.
No guessed PID killing, automatic run resumption or unknown-run relaunch occurs.

Stdout JSONL, stderr, supervisor diagnostics and receipt/exit code persist.
`collect` is permitted only after confirmed exit; it returns parsed raw events,
invalid-line count and log paths. Tool messages are claims, not acceptance.
The controller must independently verify the approved requirements and scope.
Prompt construction includes `run.task`, optional `run.requirements` and
`run.goal`; the controller should include relevant approved requirements.

## Limits

Process-group supervision is not a hostile-code sandbox. Descendants that
intentionally escape their session, same-account tampering, services launched
outside the group, inaccessible `/proc` entries, and arbitrary filesystem/network
side effects need OS/container isolation. Post-run scope checking does not undo
side effects. Use only trusted tasks in an appropriately isolated workspace.
A supervisor crash deliberately blocks progress and needs operator investigation.
There is no promise of universal process-tree containment or exact cost metering.
CLI-provided usage and results remain available as raw events.

## Tests

`python -m unittest tests.test_tool_adapters -v`

Tests run temporary Python CLI fixtures only, with no real model calls, network,
credentials or business projects. They cover launch intent crash recovery,
repeat-launch suppression, reopened receipts, identity mismatch, missing tools,
nonzero exit, malformed JSON output and TERM-resistant child/grandchild stopping.
