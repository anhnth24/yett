"""App assembly (spec P0-P1 §7 tích hợp). Ghép mọi thành phần thành một harness chạy được.

Tách khỏi cli.py để test được. clock/provider injectable → test offline deterministic.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

from yett.config.models import HarnessCfg
from yett.core.checkpoint import CheckpointStore
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig, TurnResult
from yett.memory.workspace import WorkspaceMemory
from yett.obs.cost import compute_cost
from yett.obs.spanstore import SpanStore
from yett.provider.base import ChatResult, Provider
from yett.provider.failover import FailoverRouter
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.tools.builtin.exec import ExecTool
from yett.tools.builtin.files import ReadFileTool, WriteFileTool
from yett.tools.builtin.web_fetch import WebFetchTool
from yett.tools.projects import ProjectScope
from yett.tools.registry import Registry
from yett.sandbox.base import Sandbox
from yett.sandbox.local import LocalSandbox


def build_app(
    config_path: str | Path, secrets, *, state_dir: Path, pricing_path: str | Path | None = None
) -> "App":
    """Dựng App từ config file thật (dùng cho `yett chat`). Provider build từ factory."""
    from yett.config.loader import load_config, load_pricing
    from yett.provider.factory import build_provider

    cfg = load_config(config_path)
    provider = build_provider(cfg.provider, secrets)
    fallback = build_provider(cfg.fallback_provider, secrets) if cfg.fallback_provider else None
    pricing = load_pricing(pricing_path) if pricing_path else {}
    sandbox = LocalSandbox() if cfg.sandbox.backend == "local" else None  # docker dựng riêng khi cần
    return App(
        cfg, provider, state_dir=state_dir, pricing=pricing, sandbox=sandbox, fallback=fallback
    )


class App:
    def __init__(
        self,
        cfg: HarnessCfg,
        provider: Provider,
        *,
        state_dir: Path,
        pricing: dict | None = None,
        sandbox: Sandbox | None = None,
        fallback: Provider | None = None,
        fetcher=None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        state_dir.mkdir(parents=True, exist_ok=True)
        self._pricing = pricing or {}
        self._clock = clock

        self.spanstore = SpanStore(state_dir / "traces.db", redactor=redact_attrs)
        self.checkpoints = CheckpointStore(state_dir / "scheduler.db")
        self.scope = ProjectScope(cfg.workspace_root, {n: p.path for n, p in cfg.projects.items()})
        self.workspace = WorkspaceMemory(cfg.workspace_root)

        sb = sandbox or LocalSandbox()
        self.registry = Registry()
        self.registry.register(ExecTool(sb, timeout=cfg.sandbox.timeout_sec))
        self.registry.register(ReadFileTool(self.scope))
        self.registry.register(WriteFileTool(self.scope))
        if fetcher is not None:
            self.registry.register(WebFetchTool(cfg.egress.allowlist, fetcher))

        self.gate = BasicGate(cfg.security)
        router = FailoverRouter(provider, fallback, max_retries=cfg.provider.max_retries)

        def cost_fn(res: ChatResult) -> float:
            return compute_cost(provider.name(), res.raw_model, res.usage, self._pricing)

        self.loop = AgentLoop(
            router, self.gate, self.registry, self.spanstore, self.checkpoints,
            cfg=LoopConfig(
                max_iterations=cfg.budget.max_loop_iterations,
                context_token_budget=cfg.budget.context_token_budget,
            ),
            clock=clock,
            cost_fn=cost_fn,
        )

    async def chat(self, message: str, *, session_key: str = "main", turn_id: str | None = None) -> TurnResult:
        system = self.workspace.build_system_prompt(
            "Bạn là yett, trợ lý fail-closed.", token_budget=self.cfg.budget.context_token_budget // 2
        )
        ctx = assemble_context(system, message)
        tid = turn_id or f"turn-{int(self._clock()*1000)}"
        return await self.loop.run_turn(ctx, session_key=session_key, turn_id=tid)

    def close(self) -> None:
        self.spanstore.close()
        self.checkpoints.close()
