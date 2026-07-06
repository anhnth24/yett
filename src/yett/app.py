"""App assembly (spec P0-P1 §7 tích hợp). Ghép mọi thành phần thành một harness chạy được.

Tách khỏi cli.py để test được. clock/provider injectable → test offline deterministic.
"""

from __future__ import annotations

import time
from pathlib import Path, PurePosixPath
from typing import Callable

from yett.config.models import HarnessCfg
from yett.core.checkpoint import CheckpointStore
from yett.core.complexity import ComplexityRouter
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig, TurnResult
from yett.errors import UserFacingError
from yett.memory.session import SessionStore
from yett.memory.workspace import WorkspaceMemory
from yett.obs.cost import compute_cost
from yett.obs.spanstore import SpanStore
from yett.provider.base import ChatResult, Provider
from yett.provider.failover import FailoverRouter
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.tools.builtin.codenav import GrepTool, ListDirTool, SearchTool
from yett.tools.builtin.exec import ExecTool
from yett.tools.builtin.files import ReadFileTool, WriteFileTool
from yett.tools.builtin.http_fetcher import SafeHttpFetcher
from yett.tools.builtin.web_fetch import WebFetchTool
from yett.tools.projects import ProjectScope
from yett.tools.registry import Registry
from yett.sandbox.base import Sandbox
from yett.sandbox.docker import DockerSandbox, probe_docker
from yett.sandbox.local import LocalSandbox
from yett.skills.loader import SkillLoader
from yett.tools.remote.hostprofile import HostRegistry


def build_app(
    config_path: str | Path, secrets, *, state_dir: Path, pricing_path: str | Path | None = None
) -> "App":
    """Dựng App từ config file thật (dùng cho `yett chat`). Provider build từ factory.

    Sandbox KHÔNG dựng ở đây — `App.__init__` là nguồn quyết định duy nhất theo
    `cfg.sandbox.backend`, để tránh 2 nơi tính mount/host→container cwd lệch nhau. Nếu
    backend=docker mà Docker thiếu, `App.__init__` raise `UserFacingError` (fail-closed)."""
    from yett.config.loader import load_config, load_pricing
    from yett.provider.factory import build_provider

    cfg = load_config(config_path)
    provider = build_provider(cfg.provider, secrets)
    fallback = build_provider(cfg.fallback_provider, secrets) if cfg.fallback_provider else None
    pricing = load_pricing(pricing_path) if pricing_path else {}
    return App(
        cfg, provider, state_dir=state_dir, pricing=pricing,
        fallback=fallback, secrets=secrets,
    )


_CONTAINER_ROOT = "/workspace"


def _project_roots(cfg: HarnessCfg) -> dict[Path, str]:
    """Ánh xạ workspace_root + mọi project root (host, tuyệt đối) sang path container cố định
    để mount Docker: `workspace_root` -> `/workspace`; project đăng ký nằm NGOÀI workspace_root
    -> `/workspace/projects/<name>` (project đã lồng sẵn trong workspace_root dùng chung mount
    workspace, không mount trùng)."""
    ws = Path(cfg.workspace_root).resolve()
    roots: dict[Path, str] = {ws: _CONTAINER_ROOT}
    for name, proj in cfg.projects.items():
        root = Path(proj.path).resolve()
        if root == ws or ws in root.parents:
            continue
        roots[root] = f"{_CONTAINER_ROOT}/projects/{name}"
    return roots


def _cwd_mapper(backend: str, roots: dict[Path, str]) -> Callable[[Path], str]:
    """Hàm map một path HOST (đã qua `ProjectScope.resolve_in_scope`) sang path dùng cho
    `-w`/`cwd` của sandbox. LocalSandbox chạy thẳng trên host nên giữ nguyên path; DockerSandbox
    cần path container theo mount đã tính ở `_project_roots` (khớp longest-prefix root trước)."""
    if backend != "docker":
        return lambda host_path: str(host_path)
    ordered = sorted(roots.items(), key=lambda kv: len(kv[0].parts), reverse=True)

    def mapper(host_path: Path) -> str:
        for host_root, container_root in ordered:
            if host_path == host_root or host_root in host_path.parents:
                rel = host_path.relative_to(host_root)
                if not rel.parts:
                    return container_root
                return str(PurePosixPath(container_root, *rel.parts))
        raise UserFacingError(
            f"'{host_path}' đã qua scope-check nhưng không khớp mount Docker nào — từ chối"
        )

    return mapper


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
        self.sessions = SessionStore(state_dir / "sessions.db")
        self.scope = ProjectScope(cfg.workspace_root, {n: p.path for n, p in cfg.projects.items()})
        self.workspace = WorkspaceMemory(cfg.workspace_root)
        self.complexity = ComplexityRouter(cfg.router)

        roots = _project_roots(cfg)
        sb: Sandbox
        if sandbox is not None:
            sb = sandbox
        elif cfg.sandbox.backend == "docker":
            # Fail-closed: KHÔNG bao giờ hạ cấp âm thầm về host khi backend=docker.
            probe_docker()
            sb = DockerSandbox(cfg.sandbox, mounts={str(h): c for h, c in roots.items()})
        else:
            sb = LocalSandbox()
        self.registry = Registry()
        self.registry.register(ExecTool(
            sb, timeout=cfg.sandbox.timeout_sec,
            scope=self.scope, to_sandbox_path=_cwd_mapper(cfg.sandbox.backend, roots),
        ))
        self.registry.register(ReadFileTool(self.scope))
        self.registry.register(WriteFileTool(self.scope))
        self.registry.register(ListDirTool(self.scope))
        self.registry.register(GrepTool(self.scope))
        self.registry.register(SearchTool(self.scope))
        # [RT-7] fetcher=None (không inject, vd test) + có allowlist -> dựng fetcher SSRF-safe
        # thật (SafeHttpFetcher) thay vì để web_fetch không bao giờ được đăng ký trong runtime
        # thật. allowlist rỗng -> không đăng ký tool (sẽ luôn deny, không có ích).
        if fetcher is None and cfg.egress.allowlist:
            fetcher = SafeHttpFetcher(cfg.egress.allowlist)
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
            name: DbProfile(driver=p.driver, dsn_secret=p.dsn_secret)
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
                address=addr, auth=h.auth, port=h.port, vpn_required=h.vpn_required,
                tier=h.tier, log_paths=h.log_paths, deploy_script=h.deploy_script,
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

    def capabilities_summary(self) -> str:
        """Mô tả khả năng THẬT (tool + tài nguyên đã cấu hình) để agent tự giới thiệu đúng.
        Không lộ secret — chỉ tên project/DB/host."""
        tool_desc = {
            "exec": "chạy lệnh trong sandbox", "read_file": "đọc file", "write_file": "ghi file",
            "list_dir": "liệt kê cây thư mục (trong scope)", "grep": "tìm regex trong source (trong scope)",
            "search": "gộp nhiều grep/read/list trong 1 lần (nhanh, ít vòng)",
            "web_fetch": "tải URL (qua allowlist)", "web_search": "tìm kiếm web",
            "db_query": "query DB CHỈ ĐỌC (không sửa/xóa)", "db_config": "quản lý profile DB",
            "ssh_exec": "chạy lệnh trên server qua SSH (deploy phải duyệt; cấm xóa file)",
            "log_read": "đọc log server (read-only)", "vpn": "bật/tắt VPN",
            "load_skill": "nạp hướng dẫn skill", "delegate": "giao việc cho subagent",
        }
        lines = ["Bạn là yett — trợ lý DevOps cá nhân, fail-closed (mặc định từ chối, chặn trước khi chạy).",
                 "", "KHẢ NĂNG (tool đang bật):"]
        for name in self.registry.names():
            lines.append(f"- {name}: {tool_desc.get(name, name)}")
        if self.cfg.projects:
            lines.append(f"\nProject đã đăng ký: {', '.join(self.cfg.projects)}")
        if self.cfg.databases:
            lines.append(f"Database (chỉ đọc): {', '.join(self.cfg.databases)}")
        if self.cfg.remote.hosts:
            lines.append(f"Server SSH: {', '.join(self.cfg.remote.hosts)}")
        if self.skill_loader is not None:
            menu = self.skill_loader.menu()
            if menu:
                lines.append("\nSkill (gọi load_skill để lấy hướng dẫn):")
                lines += [f"- {s['name']}: {s['description']}" for s in menu]
        lines.append(
            "\nCÁCH LÀM VIỆC (Sources First):"
            "\n- Ưu tiên tìm bằng chứng THẬT trước khi trả lời: dùng grep/list_dir/read_file trên"
            " project, log_read trên server, db_query để xem dữ liệu."
            "\n- Chỉ khẳng định điều tra được từ nguồn; luôn trích đường dẫn file:dòng hoặc tên"
            " bảng/host làm dẫn chứng."
            "\n- Không thấy bằng chứng thì nói rõ 'chưa tìm thấy / không kiểm chứng được', KHÔNG bịa."
            " Kiến thức nền có thể lỗi thời — nguồn trong project mới là chuẩn."
        )
        lines.append("\nGiới hạn an toàn: KHÔNG xóa file OS trên server, KHÔNG ALTER/DELETE/UPDATE DB "
                     "trừ khi được duyệt tường minh. Khi bị chặn, giải thích và đề xuất cách an toàn.")
        return "\n".join(lines)

    async def chat(self, message: str, *, session_key: str = "main", turn_id: str | None = None) -> TurnResult:
        system = self.workspace.build_system_prompt(
            self.capabilities_summary(), token_budget=self.cfg.budget.context_token_budget // 2
        )
        # Nhớ hội thoại xuyên turn: nạp lại lượt gần nhất của session (provider-agnostic).
        history = self.sessions.history(
            session_key, token_budget=self.cfg.budget.context_token_budget // 4
        )
        ctx = assemble_context(system, message, history=history)
        tid = turn_id or f"turn-{int(self._clock()*1000)}"
        # Complexity router: câu dễ → ít vòng lặp (tiết kiệm chi phí); câu khó → nhiều vòng.
        max_iter = self.complexity.classify(message).max_iterations if self.cfg.router.enabled else None
        # session_ctx mang allowed_tools = tất cả (cho phép delegate ở cấp cha)
        parent = _MainCtx(session_key, set(self.registry.names()))
        res = await self.loop.run_turn(
            ctx, session_key=session_key, turn_id=tid, session_ctx=parent, max_iterations=max_iter
        )
        # Ghi lượt vào lịch sử (chỉ khi turn xong bình thường, có nội dung trả lời).
        if res.status == "done" and res.text:
            self.sessions.record_turn(session_key, message, res.text, ts=self._clock())
        return res

    def set_approver(self, approver) -> None:
        """Gắn approver (vd web ApprovalCenter.request) — turn sẽ hỏi duyệt khi Gate cần."""
        self.loop._approver = approver

    def close(self) -> None:
        self.spanstore.close()
        self.checkpoints.close()
        self.sessions.close()


class _MainCtx:
    """session_ctx cấp cha: mang allowed_tools (để delegate kiểm toolset con ⊆ cha)."""

    def __init__(self, session_key: str, allowed_tools: set[str]) -> None:
        self.session_key = session_key
        self.allowed_tools = allowed_tools
        self.is_subagent = False
        self.parent_span_id: str | None = None
