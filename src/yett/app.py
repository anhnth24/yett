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
from yett.skills.loader import SkillLoader
from yett.tools.remote.hostprofile import HostRegistry


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
        cfg, provider, state_dir=state_dir, pricing=pricing, sandbox=sandbox,
        fallback=fallback, secrets=secrets,
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
        secrets=None,
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

        # --- Nhóm 2 wired vào App ---
        self._secrets = secrets
        self.skill_loader: SkillLoader | None = None
        if cfg.skills_enabled:
            self._wire_skills(state_dir)
        if cfg.databases:
            self._wire_db()
        if cfg.search is not None and secrets is not None:
            self._wire_search()

        # Remote ops (SSH/VPN/log): host registry cần trước khi tạo Gate (Gate phân lớp theo host).
        self.host_registry: HostRegistry | None = None
        if cfg.remote.hosts:
            self._wire_remote()

        self.gate = BasicGate(cfg.security, hosts=self.host_registry)
        # Hooks: rỗng mặc định (điểm cắm sẵn; nạp hook từ config sau).
        from yett.hooks.runner import HookRunner
        self.hooks = HookRunner([])

        router = FailoverRouter(provider, fallback, max_retries=cfg.provider.max_retries)

        def cost_fn(res: ChatResult) -> float:
            return compute_cost(provider.name(), res.raw_model, res.usage, self._pricing)

        self._loop_cfg = LoopConfig(
            max_iterations=cfg.budget.max_loop_iterations,
            context_token_budget=cfg.budget.context_token_budget,
        )
        self.loop = AgentLoop(
            router, self.gate, self.registry, self.spanstore, self.checkpoints,
            cfg=self._loop_cfg, clock=clock, cost_fn=cost_fn, hooks=self.hooks,
        )
        # Subagent: đăng ký delegate sau khi có loop (runner dùng chính loop này).
        if cfg.subagents_enabled:
            self._wire_subagents()

    def _wire_skills(self, state_dir: Path) -> None:
        from yett.skills.loader import SkillLoader
        from yett.skills.tool import LoadSkillTool

        tier_dirs = {
            "bundled": Path("skills"),
            "managed": state_dir / "skills",
            "workspace": Path(self.cfg.workspace_root) / "skills",
        }
        self.skill_loader = SkillLoader(tier_dirs)
        self.registry.register(LoadSkillTool(self.skill_loader))

    def _wire_db(self) -> None:
        from yett.tools.db.db_query import DbProfile, DbQueryTool
        from yett.tools.db.executor import RealDbExecutor

        profiles = {
            name: DbProfile(driver=p.driver, dsn_secret=p.dsn_secret, readonly=p.readonly)
            for name, p in self.cfg.databases.items()
        }
        self.registry.register(DbQueryTool(profiles, RealDbExecutor(), self._secrets))

    def _wire_search(self) -> None:
        # web_search cần search backend cụ thể (nối sau); đăng ký khi có.
        return None

    def _wire_remote(self) -> None:
        from yett.tools.remote.hostprofile import HostProfile
        from yett.tools.remote.ssh_backend import AsyncSSHBackend
        from yett.tools.remote.ssh_exec import LogReadTool, SshExecTool

        hosts = {}
        for name, h in self.cfg.remote.hosts.items():
            addr = f"{h.user}@{h.address}" if h.user else h.address
            hosts[name] = HostProfile(
                address=addr, auth=h.auth, vpn_required=h.vpn_required, tier=h.tier,
                log_paths=h.log_paths, deploy_script=h.deploy_script,
            )
        self.host_registry = HostRegistry(hosts)
        backend = AsyncSSHBackend()
        # VPN CLI runner (openvpn/openfortivpn) chưa nối → vpn=None; SSH vẫn chạy nếu không cần VPN.
        self.registry.register(SshExecTool(self.host_registry, backend, self._secrets, vpn=None))
        self.registry.register(LogReadTool(self.host_registry, backend, self._secrets, vpn=None))

    def _wire_subagents(self) -> None:
        from yett.subagent.delegate import DelegateCtx, DelegateTool
        from yett.subagent.definition import SubagentDef

        agents_dir = Path(self.cfg.workspace_root) / "agents"

        async def runner(sub: SubagentDef, child: DelegateCtx, task: str) -> str:
            ctx = assemble_context(sub.system_prompt, task)
            res = await self.loop.run_turn(
                ctx, session_key=child.session_key, turn_id=f"sub-{int(self._clock()*1000)}",
                session_ctx=child, allowed_tools=set(sub.toolset),
            )
            return res.text

        self.registry.register(DelegateTool(agents_dir, runner))

    async def chat(self, message: str, *, session_key: str = "main", turn_id: str | None = None) -> TurnResult:
        base = "Bạn là yett, trợ lý fail-closed."
        if self.skill_loader is not None:
            menu = self.skill_loader.menu()
            if menu:
                lines = "\n".join(f"- {s['name']}: {s['description']}" for s in menu)
                base += f"\n\nSkill khả dụng (gọi load_skill để lấy hướng dẫn):\n{lines}"
        system = self.workspace.build_system_prompt(
            base, token_budget=self.cfg.budget.context_token_budget // 2
        )
        ctx = assemble_context(system, message)
        tid = turn_id or f"turn-{int(self._clock()*1000)}"
        # session_ctx mang allowed_tools = tất cả (cho phép delegate ở cấp cha)
        parent = _MainCtx(session_key, set(self.registry.names()))
        return await self.loop.run_turn(ctx, session_key=session_key, turn_id=tid, session_ctx=parent)

    def set_approver(self, approver) -> None:
        """Gắn approver (vd web ApprovalCenter.request) — turn sẽ hỏi duyệt khi Gate cần."""
        self.loop._approver = approver

    def close(self) -> None:
        self.spanstore.close()
        self.checkpoints.close()


class _MainCtx:
    """session_ctx cấp cha: mang allowed_tools (để delegate kiểm toolset con ⊆ cha)."""

    def __init__(self, session_key: str, allowed_tools: set[str]) -> None:
        self.session_key = session_key
        self.allowed_tools = allowed_tools
        self.is_subagent = False
        self.parent_span_id: str | None = None
