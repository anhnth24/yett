# Codex adversarial review — harden-yett-harness plan implementation

- Reviewer: Codex (gpt-5-codex) via /codex:rescue, session `019f3548-568e-75a2-9cf4-12dca81e7c54`
- Scope: `git diff 2cc89a8..HEAD` (7 hardening phases + post-review fixes)
- Codex ran read-only and could not write this file itself; orchestrator persisted its verbatim output below.

**Summary**
Findings: Critical 0, High 1, Medium 2, Low 2.

## Disposition (orchestrator, commit `cb4dddb`) — full gate after: 400 passed / 4 skipped, mypy/ruff/import-linter clean
- **H1 → FIXED.** `_exec_hits_protected` (`immutable.py`) now fail-closed substring-scans the whole command for protected names — catches `>|` clobber, inline interpreter (`python -c "open(cfg,'w')"`), nested `sh -c`. Tests: `test_immutable_exec_hits_protected_covers_bypass_forms`. (Note: exec containment in production still rests on Docker rootfs read-only + config outside writable mount; this is the defense-in-depth hardline. `sandbox.backend=local` remains dev/test-only.)
- **M1 → FIXED.** `journalctl --vacuum-*/--rotate/--flush/--sync` no longer classify as SSH read-only (`cmdguard._MUTATING_FLAGS`); plain `journalctl -u ...` still allowed. Test: `test_journalctl_mutating_flags_not_readonly`.
- **M2 → FIXED.** `generic_sk_key` alphabet broadened to `[A-Za-z0-9._-]`; `sk-cp-..._...`/`sk-proj-....` now redacted. Test: `test_redact_generic_sk_key_with_underscore_and_dot`.
- **L1 → NO CHANGE (by design).** An explicit hook `action="mutate"` is honored and re-gated even if values are unchanged; the empty-runner phantom-mutate bug was already fixed (`a55859a`). Adding value-equality short-circuit is a micro-opt, not security.
- **L2 → NO CHANGE (documented).** `network: proxy` is an intentional controlled-egress mode; default `none` keeps no-egress. Operator opting into a bridged net named "proxy" is out-of-scope for v0.0.1 (recorded in plan L4).

## High

**H1: Immutable config protection is live, but bypassable through `exec` command forms.**
Refs: `src/yett/app.py:112`, `src/yett/app.py:197`, `src/yett/policy/immutable.py:86`, `src/yett/policy/immutable.py:108`, `config/harness.example.yaml:133`.

Working gate payloads returned `allow ALLOW_exec` in probe:

```text
exec {"cmd": "cat /tmp/payload >|harness.example.yaml"}
exec {"cmd": "python -c \"open('harness.example.yaml','w').write('x')\""}
exec {"cmd": "sh -c \"sed -i s/a/b/ harness.example.yaml\""}
```

Impact: if the protected config is in exec cwd/mount, or `sandbox.backend=local`, config can be modified despite `_ImmutableFirstGate`. The `python` case matters because the example allowlist already permits `python`.

Fix: handle `>|`, inspect nested `sh -c`, deny inline interpreter code touching protected paths, and keep config outside writable exec mounts / read-only at filesystem layer.

## Medium

**M1: `journalctl --vacuum-time=1s` and `journalctl --rotate` are allowed as SSH read-only.**
Refs: `src/yett/security/cmdguard.py:37`, `src/yett/security/cmdguard.py:41`, `src/yett/security/cmdguard.py:187`, `src/yett/security/basic_gate.py:48`.

Probe result: `BasicGate(...).evaluate("ssh_exec", ...)` returned `allow SSH_READONLY`. These commands delete/rotate journal data. Fix by removing `journalctl` from generic read-only bins or allowlisting only safe flags.

**M2: generic `sk-` redaction misses `_` and `.` token alphabets.**
Ref: `src/yett/security/filters.py:21`.

Payloads not redacted:

```text
sk-cp-AAAAAAAA_BBBBBBBBBBBBBBBBBBBBBBBB
sk-proj-AAAAAAAA.BBBBBBBBBBBBBBBBBBBBBBBB
```

Fix: broaden to a provider-safe alphabet such as `[A-Za-z0-9_.-]` with length floor and tests.

## Low

**L1: `HookRunner` still reports `mutate` for identical copied args/results.**
Refs: `src/yett/hooks/runner.py:68`, `src/yett/hooks/runner.py:75`, `src/yett/tools/wiring.py:106`.

Payload: `HookOutcome("mutate", mutated_args=dict(event.args))` returns `action="mutate"` despite no real change, causing unnecessary re-gate/re-approval. No bypass found.

**L2: Docker `network: proxy` remains an unenforced egress mode.**
Refs: `src/yett/config/models.py:52`, `src/yett/sandbox/docker.py:90`, `tests/unit/test_docker_wiring.py:292`.

`network: none` is safe by default, but `network: proxy` is passed directly as `--network proxy`; Docker treats it as a normal network name unless separately constrained.

## Verified Holds
`write_file` to protected config is denied via resolved path; PreToolUse top-level mutations are re-gated and re-approved; PostToolUse mutated results are re-filtered; SQL `/*!...*/`, CTE DML, multi-statement, and `EXPLAIN(SELECT 1)` recursion are denied; SafeHttpFetcher has per-hop allowlist/IP checks and pinned connect; subagent subset is enforced in loop and RPC broker; dead-code deletion left no production references in grep.

## Unresolved
Is `sandbox.backend=local` user-supported outside tests? Should immutable protection cover every symlink/alias spelling of the config path? Should prior resume M3 be fixed or documented as the official at-least-once/stale-cache guarantee?
