# Phase 2 Report: Real-Provider Loop

- Worktree: `D:\FIS\ai-first\yett\.claude\worktrees\agent-acd4273ad3079d565`
- Branch: `worktree-agent-acd4273ad3079d565` (branched from `2cc89a8`, i.e. before Phase 1's
  commit `5c02fbc` landed on the shared branch — see Issues below)
- Status: DONE

## Files Modified

- `src/yett/provider/base.py` (+3) — `Message.tool_calls: list[ToolCall] | None = None`.
- `src/yett/core/context.py` (rewrite, ~70 net new lines) — system message prepended in
  `assemble_context`; `Context.add_assistant_tool_calls`; `tool_call_signature` (stable
  `name:hash(args)`, RT-2); `pending_tool_calls` (derive dangling tool_use from history,
  RT-2/RT-3); `tokens()`/`prune()` now account for and prune tool_call args together with
  their tool_result (RT-12).
- `src/yett/core/checkpoint.py` (+42) — `serialize_messages`/`deserialize_messages` helpers
  (Message/ToolCall <-> JSON-safe dict); module docstring updated to describe the new
  full-history + signature-idempotency invariant. `CheckpointStore` class API unchanged
  (still generic `dict` state — no schema migration needed).
- `src/yett/core/loop.py` (rewrite, ~111 net changed lines):
  - Resume accepts `status in ("running", "canceled")` (RT-3), not just `"running"`.
  - On resume: rebuilds `ctx.messages` from persisted history, restores `completed_sigs`,
    and — inside the same `try/except Cancelled` as the main loop — finishes any dangling
    tool_calls (`pending_tool_calls`) before asking the model again (never sends a request
    with an unanswered `tool_call`).
  - `ctx.add_assistant_tool_calls(res.tool_calls, res.text)` added right before the tool
    loop (P0-2) — mutually exclusive with the text-only branch, so no double-add on
    `force_text`.
  - New `_execute_tool_call` helper: executes (or, if `consult_cache` and the tool's stable
    signature is already known, replays the cached result instead of re-running) a tool
    call, then checkpoints immediately (per-call, not per-batch) so cancel/crash mid-batch
    doesn't lose progress.
  - `consult_cache` is `True` for the entire run only when the turn started as a resume
    (`is_resume`); a fresh (non-resumed) run never consults the cache. This was a deliberate
    design point — see "Design decision" below.
- `src/yett/provider/openai_compat.py` (+16) — `_to_openai_msg` emits
  `{"role":"assistant","content":..|null,"tool_calls":[...]}` when `Message.tool_calls` is
  set; new `_to_openai_tool_call` serializes `ToolCall.args` to a JSON string per the OpenAI
  wire format. System role already worked via the existing fallback branch — no code change
  needed there.
- `src/yett/provider/fake.py` — no change. Verified `FakeProvider`/`tool_result`/
  `text_result` already round-trip the new `Message.tool_calls` field transparently (it just
  records whatever `list[Message]` it's given); adding an unused change would have violated
  Rule 3 (surgical changes).
- `tests/integration/test_real_provider_contract.py` — created (see Issues: this file
  didn't exist in this worktree because Phase 1 wasn't merged into this branch point).
  Content restored from Phase 1's commit `5c02fbc` (verified byte-identical via `git show`),
  with the `xfail(strict=True)` marker removed and its docstring's final paragraph updated
  to say the test now passes normally.
- `tests/unit/test_core_loop.py` (+6/-2) — one assertion updated: checkpoint schema key
  renamed `completed_tool_calls` (ids) -> `completed_sigs` (stable signature -> result); uses
  new `tool_call_signature` helper. No other change; all 8 pre-existing tests (anti-loop,
  max-iterations, cancel-before-tool, resume-does-not-rerun, context-deterministic, etc.)
  still pass unmodified in behavior.
- `tests/unit/test_resume_checkpoint.py` — new file, 4 tests:
  1. `test_checkpoint_persists_full_message_history_not_just_ids` — asserts the persisted
     `state["history"]` contains the real assistant `tool_calls` (name+args) and the real
     tool_result content (not just an id).
  2. `test_resume_after_cancel_does_not_rerun_tool` — RT-3: cancels a turn right after a
     tool runs (tool's own `run()` calls `cancel.cancel()`), asserts checkpoint status is
     `"canceled"`; resumes with a fresh loop/provider/tool and asserts the tool does not
     run again (idempotent by signature across the canceled->resumed boundary).
  3. `test_context_overflow_prunes_tool_call_args_and_does_not_livelock` — RT-12: seeds a
     context with one large tool_use/tool_result pair, uses a stub provider that raises
     `ProviderError(CONTEXT_OVERFLOW)` until the serialized message size drops under a
     threshold; asserts the turn finishes in exactly 2 provider calls (proving prune()
     converges in one pass instead of looping to `max_iterations`) and that the pruned
     tool_call's args were replaced with `{"_pruned": True}`.
  4. `test_estimate_tokens_counts_tool_call_args` — unit check that `Context.tokens()`
     actually adds `estimate_tokens(json.dumps(args))` for tool_calls.

## Tasks Completed

- [x] P0-3: system prompt prepended as `Message(role="system", ...)` in `assemble_context`.
- [x] P0-2: `Message.tool_calls`; `Context.add_assistant_tool_calls`; loop writes the
      assistant tool_use turn before tool results; `_to_openai_msg` emits
      `{"role":"assistant","tool_calls":[...]}`; verified no double-add on `force_text`.
- [x] RT-2: checkpoint persists full message history (assistant tool_use + tool_result
      content); resume rebuilds `Context` from it; idempotency keyed by
      `tool_name + hash(args)`, never by provider id.
- [x] RT-3: resume works after `canceled` status; regression test proves the completed tool
      is not re-run.
- [x] RT-12: `estimate_tokens`/`tokens()` count serialized tool_call args; `prune()` now
      shrinks the tool_call's args alongside its tool_result (same pair); regression test
      proves no ContextOverflow livelock (exactly 2 provider calls, not 5/max_iterations).
- [x] Removed `xfail(strict=True)` from `test_real_provider_contract.py` — now passes as a
      normal (non-xfail) test.

## Tests Status

- Contract test: `pytest tests/integration/test_real_provider_contract.py` -> 1 passed.
- `tests/unit/test_core_loop.py` -> 8 passed (all pre-existing tests, including
  anti-loop/max-iterations/cancel, still green with unchanged behavior).
- `tests/unit/test_resume_checkpoint.py` -> 4 passed (new).
- Full suite: `python -m pytest -q` -> 229 passed, 1 failed — the 1 failure
  (`tests/unit/test_setup_wizard.py::test_file_secret_store`, a POSIX `chmod 0600`
  assertion that Windows/NTFS cannot satisfy) is pre-existing and unrelated: verified by
  `git stash` + re-run at the branch point (`2cc89a8`) — fails identically with zero of my
  changes applied. The file is explicitly outside Phase 2's ownership (`test_setup_wizard.py`
  is on the "do not touch" list).
- `python -m mypy src` -> Success: no issues found in 104 source files (full sweep, not
  just the changed files — confirms the `Message` contract change didn't break any caller).
- `python -m ruff check src tests` -> All checks passed.
- `lint-imports` (import-linter) -> pre-existing, unrelated failure
  (`'charmap' codec can't decode byte 0x8d...`), confirmed present at the branch point too
  via the same stash test. Windows console-codec issue reading a source file, not caused by
  my changes; not part of Phase 2's explicit quality gate in the assignment (pytest + mypy).

## Design Decisions Worth Flagging

1. `consult_cache` is scoped to "this whole run is a resume", not "this specific tool_call
   was seen before within a fresh run." Initially I considered always consulting the
   signature cache for idempotency. That breaks
   `test_antiloop_repeated_tool_forces_answer` (existing test): it deliberately has the
   model call the same tool with identical (empty) args repeatedly, expecting the tool to
   genuinely run 3 times before anti-loop forces a text answer. If a fresh run's own
   in-progress `completed_sigs` were consulted, the 2nd+ identical call would silently be
   served from cache and never really execute — anti-loop's repeat counter would never
   reach `force_text_after_repeats`, breaking that acceptance criterion ("test_core_loop.py
   cu van xanh"). Resolution: `is_resume` is computed once from the checkpoint status at the
   top of `run_turn`; `consult_cache=is_resume` is passed to every `_execute_tool_call` for
   the entire turn. A brand-new turn (`is_resume=False`) always executes for real (matches
   today's behavior exactly); a resumed turn (`is_resume=True`) treats any signature already
   known-completed (from before or during this resumed run) as authoritative and never
   re-runs it. This satisfies RT-2's stated goal (no duplicate side effects across resume)
   without regressing anti-loop.
2. Checkpointing moved from once-per-batch to once-per-tool-call (inside
   `_execute_tool_call`). The original code saved the checkpoint once after the entire
   `for tc in res.tool_calls` loop finished. If cancel/crash happened mid-batch (tool 1 done,
   tool 2 not yet), the previous checkpoint (from the prior batch) would be stale and, on
   resume, tool 1 would either be lost or (worse) the rebuilt context would have a dangling
   assistant `tool_calls` message. Saving after every single tool call — combined with
   `pending_tool_calls()` deriving exactly which tool_calls in the trailing assistant message
   still lack a result — makes resume correct at any interruption point after the assistant
   tool_use message was recorded.
3. `CheckpointStore`'s SQLite schema/class API was left untouched — it already stores an
   opaque `state: dict` blob, so no migration was needed; only the shape of what `loop.py`
   puts into that dict changed (`history`/`completed_sigs` instead of
   `completed_tool_calls`). The two new module-level functions (`serialize_messages`/
   `deserialize_messages`) live in `checkpoint.py` since that's the file whose job is
   "how do I turn agent state into persisted bytes."

## Issues Encountered

- This worktree's branch point (`2cc89a8`) predates Phase 1's commit (`5c02fbc`, "test:
  expose real-provider and docker-sandbox gaps via strict-xfail, add CI windows leg").
  `tests/integration/test_real_provider_contract.py` therefore did not exist here. I
  recovered its exact byte content via
  `git show 5c02fbc:tests/integration/test_real_provider_contract.py`
  (the commit object is present in this repo's local object database even though it isn't an
  ancestor of my branch), verified every import it uses already resolves in this worktree,
  then removed the `xfail` marker per my task instructions. I did not pull in Phase 1's
  other changes (`ci.yml`, `README.md`, `test_docker_sandbox.py`, `test_setup_wizard.py`
  windows-perm additions) — those are outside my file ownership and are Song-0/Phase-1's
  responsibility to land when the orchestrator merges waves. Consequently this worktree's
  full-suite baseline is "224 passed" (no docker-skip, no win32-xfail) rather than the
  "224 passed, 1 skipped, 2 xfailed" described in my task brief — that discrepancy is fully
  accounted for by the missing Phase-1 merge, not by anything in Phase 2's diff (229 = 224 +
  1 contract test + 4 new resume-checkpoint tests, exactly).
- Pre-existing, out-of-scope failures noted above (Windows `chmod 0600` test,
  `lint-imports` charmap codec) — flagged, not fixed, since both predate my changes and
  touch files/tooling outside Phase 2's ownership.

## Next Steps

- Orchestrator should merge Phase 2 -> Phase 3 -> Phase 4 -> Phase 5 per the plan's stated
  wave order; once Phase 1's commit is actually merged into the shared branch, re-run the
  full suite once to confirm the docker-skip/win32-xfail counts line up as expected.
- Phase 6 (`blockedBy: [2, 4]`) can proceed once this merges — it shares `core/loop.py` with
  this phase; the new `_execute_tool_call`/`is_resume` structure should be reviewed by
  whoever implements Phase 6's `allowed_tools`/subagent-subset changes to loop.py.

Status: DONE
Summary: P0-2/P0-3/RT-2/RT-3/RT-12 all implemented and covered by new tests; full suite green except one pre-existing, out-of-scope Windows permission test; mypy strict clean across all 104 src files.
Concerns/Blockers: None blocking. Note only: this worktree branched before Phase 1's commit landed on the shared branch, so the contract test file had to be recovered from that commit rather than being present already (see Issues).
