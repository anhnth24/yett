"""App assembly (spec P0-P1 §7 tích hợp). Ghép mọi thành phần thành một harness chạy được.

Tách khỏi cli.py để test được. clock/provider injectable → test offline deterministic.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from yett.tools.remote.vpn import VpnManager

from yett.config.models import HarnessCfg
from yett.core.cancel import CancelToken
from yett.core.checkpoint import CheckpointStore
from yett.sched.cron import CronStore
from yett.core.complexity import ComplexityRouter
from yett.core.context import assemble_context
from yett.core.loop import AgentLoop, LoopConfig, TurnResult
from yett.errors import UserFacingError
from yett.memory.paths import (
    MEMORY_AUDIT_FILENAME,
    MEMORY_FILENAME,
    MEMORY_LOCK_FILENAME,
    PENDING_PARTS,
)
from yett.memory.review_gate import MemoryReviewGate
from yett.memory.session import SessionStore
from yett.memory.tasks import TaskStore
from yett.memory.workspace import WorkspaceMemory
from yett.tools.builtin.memory import MemoryProposeTool
from yett.obs.cost import compute_call_cost, compute_cost
from yett.obs.spanstore import SpanStore
from yett.provider.base import ChatResult, Provider
from yett.provider.failover import FailoverRouter
from yett.policy.immutable import ImmutableCore
from yett.security.basic_gate import BasicGate
from yett.security.filters import redact_attrs
from yett.security.gate import Decision, PolicyGate, SessionCtx
from yett.tools.assist.image_backend import ImageGenFn
from yett.tools.assist.web_search import SearchFn
from yett.tools.builtin.codenav import GrepTool, ListDirTool, SearchTool
from yett.tools.builtin.exec import ExecTool
from yett.tools.builtin.files import ReadFileTool, WriteFileTool
from yett.tools.builtin.http_fetcher import SafeHttpFetcher
from yett.tools.builtin.notify import NotifyTool
from yett.tools.builtin.tasks import TaskAddTool, TaskListTool, TaskUpdateTool
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
        fallback=fallback, secrets=secrets, config_path=config_path,
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


class _ImmutableFirstGate:
    """ImmutableCore hardline chạy TRƯỚC gate cấu hình — không allowlist/rule nào đảo được
    (spec P3 §2.3: "immutable core cannot be overridden"). Giữ nguyên model allowlist/denylist
    của BasicGate cho phần còn lại; chỉ thêm lớp hardline phía trước, KHÔNG thay backend gate."""

    def __init__(self, immutable: ImmutableCore, inner: PolicyGate) -> None:
        self._immutable = immutable
        self._inner = inner

    def evaluate(self, tool: str, args: dict, ctx: SessionCtx) -> Decision:
        if hit := self._immutable.check(tool, args):
            return hit
        return self._inner.evaluate(tool, args, ctx)


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
        search_fn: SearchFn | None = None,
        image_fn: ImageGenFn | None = None,
        secrets=None,
        clock: Callable[[], float] = time.time,
        config_path: str | Path | None = None,
    ) -> None:
        self.cfg = cfg
        state_dir.mkdir(parents=True, exist_ok=True)
        self._pricing = pricing or {}
        self._clock = clock

        self.spanstore = SpanStore(state_dir / "traces.db", redactor=redact_attrs)
        self.checkpoints = CheckpointStore(state_dir / "scheduler.db")
        self.cron = CronStore(state_dir / "cron.db")
        self.sessions = SessionStore(state_dir / "sessions.db")
        self.tasks = TaskStore(state_dir / "tasks.db", clock=clock)
        Path(cfg.workspace_root).mkdir(parents=True, exist_ok=True)
        self.scope = ProjectScope(cfg.workspace_root, {n: p.path for n, p in cfg.projects.items()})
        self.workspace = WorkspaceMemory(cfg.workspace_root)
        # Memory review gate: agent chỉ đề xuất vào staging qua tool memory_propose;
        # MEMORY.md chỉ được ghi khi người vận hành duyệt (CLI `yett memory approve`).
        self.memory_gate = MemoryReviewGate(Path(cfg.workspace_root))
        self.memory_gate.ensure_storage()
        self.complexity = ComplexityRouter(cfg.router)

        roots = _project_roots(cfg)
        sb: Sandbox
        if sandbox is not None:
            sb = sandbox
        elif cfg.sandbox.backend == "docker":
            # Fail-closed: KHÔNG bao giờ hạ cấp âm thầm về host khi backend=docker.
            probe_docker()
            placeholder = (state_dir / ".memory-readonly-overlay").resolve()
            try:
                placeholder_fd = os.open(
                    placeholder,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o400,
                )
            except FileExistsError:
                placeholder_fd = os.open(
                    placeholder, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                )
            try:
                if not stat.S_ISREG(os.fstat(placeholder_fd).st_mode):
                    raise UserFacingError("memory readonly overlay không phải regular file")
            finally:
                os.close(placeholder_fd)
            ws = Path(cfg.workspace_root).resolve()
            pending = self.memory_gate.pending_dir
            overlay_fallback = str(placeholder)
            overlays = {
                "/workspace/MEMORY.md": (str(ws / MEMORY_FILENAME), overlay_fallback),
                "/workspace/memory/pending": (str(pending), str(pending)),
                "/workspace/memory/review-audit.jsonl": (
                    str(ws / "memory" / MEMORY_AUDIT_FILENAME),
                    overlay_fallback,
                ),
                "/workspace/.yett-memory-review.lock": (
                    str(ws / MEMORY_LOCK_FILENAME),
                    overlay_fallback,
                ),
            }
            sb = DockerSandbox(
                cfg.sandbox,
                mounts={str(h): c for h, c in roots.items()},
                readonly_overlays=overlays,
            )
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
        self.registry.register(TaskAddTool(self.tasks))
        self.registry.register(TaskListTool(self.tasks))
        self.registry.register(TaskUpdateTool(self.tasks))
        self.registry.register(MemoryProposeTool(self.memory_gate))
        # notify: kênh gắn muộn (serve_forever) qua set_notifier; getter để late-bind + hot-reload.
        self._notifier: Callable[[str], int] | None = None
        self.registry.register(NotifyTool(lambda: self._notifier))
        # [RT-7] fetcher=None (không inject, vd test) + có allowlist -> dựng fetcher SSRF-safe
        # thật (SafeHttpFetcher) thay vì để web_fetch không bao giờ được đăng ký trong runtime
        # thật. allowlist rỗng -> không đăng ký tool (sẽ luôn deny, không có ích).
        if fetcher is None and cfg.egress.allowlist:
            fetcher = SafeHttpFetcher(cfg.egress.allowlist)
        if fetcher is not None:
            self.registry.register(WebFetchTool(cfg.egress.allowlist, fetcher))

        # Token hủy theo session (web POST /api/cancel → set cờ; loop check ở ranh giới stage).
        self._cancel_tokens: dict[str, CancelToken] = {}
        # --- Nhóm 2 wired vào App ---
        self._secrets = secrets
        self.skill_loader: SkillLoader | None = None
        if cfg.skills_enabled:
            self._wire_skills(state_dir)
        if cfg.databases:
            self._wire_db()
        # web_search: cần SearchCfg + secret store. search_fn injectable (test offline);
        # không inject → dựng Brave backend thật (host API phải nằm trong egress allowlist).
        if cfg.search is not None and secrets is not None:
            self._wire_search(search_fn)
        # image_gen: cần ImageCfg + secret store. image_fn injectable (test / local offline);
        # không inject → dựng OpenAI-compatible Images backend (host trong egress allowlist).
        if cfg.image is not None and secrets is not None:
            self._wire_image(image_fn)

        # Remote ops (SSH/VPN/log): host registry + VPN cần trước khi tạo Gate
        # (Gate phân lớp theo host / allowlist vpn profile).
        self.host_registry: HostRegistry | None = None
        self.vpn_manager: VpnManager | None = None
        if cfg.remote.hosts or cfg.remote.vpn_profiles:
            self._wire_remote()

        # Immutable core hardline TRƯỚC gate cấu hình: cấm sửa policy/identity + hardline
        # exec/ssh/db, không allowlist/rule nào đảo được. Protected = file config harness (chính
        # sách của agent) khi biết đường dẫn; luôn phủ hardline lệnh/SQL kể cả khi rỗng.
        # MEMORY.md cũng protected: agent không write_file/exec ghi thẳng — chỉ merge qua
        # MemoryReviewGate.approve (ngoài tool path). Staging + lock/audit metadata cũng là
        # control-plane state: protect them from exec in local/dev mode as defense in depth
        # (Docker additionally overlays all four paths read-only).
        protected: list[Path] = []
        if config_path is not None:
            try:
                protected.append(Path(config_path).resolve())
            except (OSError, ValueError):
                pass
        try:
            memory_root = Path(cfg.workspace_root)
            protected.extend(
                [
                    (memory_root / MEMORY_FILENAME).resolve(),
                    memory_root.joinpath(*PENDING_PARTS).resolve(),
                    (memory_root / "memory" / MEMORY_AUDIT_FILENAME).resolve(),
                    (memory_root / MEMORY_LOCK_FILENAME).resolve(),
                ]
            )
        except (OSError, ValueError):
            pass
        vpn_names = set(cfg.remote.vpn_profiles) if cfg.remote.vpn_profiles else None
        self.gate: PolicyGate = _ImmutableFirstGate(
            ImmutableCore(protected),
            BasicGate(cfg.security, hosts=self.host_registry, vpn_profiles=vpn_names),
        )
        # Hooks: rỗng mặc định (điểm cắm sẵn; nạp hook từ config sau).
        from yett.hooks.runner import HookRunner
        self.hooks = HookRunner([])

        router = FailoverRouter(provider, fallback, max_retries=cfg.provider.max_retries)

        def cost_fn(res: ChatResult) -> float:
            return compute_cost(
                res.provider_name or provider.name(), res.raw_model, res.usage, self._pricing
            )

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

    def _wire_search(self, search_fn: SearchFn | None = None) -> None:
        """Đăng ký `web_search` vào registry. Fail-closed: thiếu SearchCfg/secrets thì
        caller không gọi hàm này; api_key_secret rỗng → không đăng ký (tool không tồn tại
        thay vì chạy với key trống)."""
        from yett.tools.assist.search_backend import BraveSearchBackend
        from yett.tools.assist.web_search import WebSearchTool

        scfg = self.cfg.search
        if scfg is None or self._secrets is None:
            return
        if not scfg.api_key_secret:
            return
        allow = list(self.cfg.egress.allowlist)
        # provider đã validate bởi SearchCfg (Literal); hiện chỉ brave.
        fn: SearchFn = search_fn or BraveSearchBackend(allow, base_url=scfg.base_url)
        cost = compute_call_cost(scfg.provider, "web_search", self._pricing)
        self.registry.register(
            WebSearchTool(
                fn,
                self._secrets,
                scfg.api_key_secret,
                allowlist=allow,
                cost_usd=cost,
                cost_provider=scfg.provider,
                cost_model="web_search",
            )
        )

    def _wire_image(self, image_fn: ImageGenFn | None = None) -> None:
        """Đăng ký `image_gen` vào registry. Fail-closed: thiếu ImageCfg/secrets thì
        caller không gọi; api_key_secret rỗng → không đăng ký."""
        from yett.tools.assist.image_backend import OpenAICompatImageBackend
        from yett.tools.assist.image_gen import ImageGenTool

        icfg = self.cfg.image
        if icfg is None or self._secrets is None:
            return
        if not icfg.api_key_secret:
            return
        allow = list(self.cfg.egress.allowlist)
        fn: ImageGenFn = image_fn or OpenAICompatImageBackend(
            allow,
            model=icfg.model,
            base_url=icfg.base_url,
            response_format=icfg.response_format,
            timeout_sec=icfg.timeout_sec,
        )
        cost = compute_call_cost(icfg.provider, icfg.model, self._pricing)
        self.registry.register(
            ImageGenTool(
                fn,
                self._secrets,
                icfg.api_key_secret,
                scope=self.scope,
                cost_usd=cost,
                cost_provider=icfg.provider,
                cost_model=icfg.model,
                timeout_sec=icfg.timeout_sec,
            )
        )

    def _wire_remote(self) -> None:
        from yett.tools.remote.hostprofile import HostProfile
        from yett.tools.remote.ssh_backend import AsyncSSHBackend
        from yett.tools.remote.ssh_exec import LogReadTool, SshExecTool
        from yett.tools.remote.vpn import SubprocessVpnRunner, VpnManager, VpnTool

        hosts = {}
        for name, h in self.cfg.remote.hosts.items():
            addr = f"{h.user}@{h.address}" if h.user else h.address
            hosts[name] = HostProfile(
                address=addr, auth=h.auth, port=h.port, vpn_required=h.vpn_required,
                tier=h.tier, log_paths=h.log_paths, deploy_script=h.deploy_script,
            )
        self.host_registry = HostRegistry(hosts) if hosts else None
        vpn = None
        profiles = dict(self.cfg.remote.vpn_profiles)
        if profiles:
            if self._secrets is None:
                raise UserFacingError(
                    "remote.vpn_profiles đã khai nhưng secret store không có — fail-closed"
                )
            # Fail-closed sớm: host.vpn_required phải ∈ vpn_profiles.
            for hn, h in self.cfg.remote.hosts.items():
                if h.vpn_required and h.vpn_required not in profiles:
                    raise UserFacingError(
                        f"host '{hn}' vpn_required='{h.vpn_required}' không có trong "
                        "remote.vpn_profiles — fail-closed"
                    )
            runner = SubprocessVpnRunner(profiles)
            vpn = VpnManager(runner, self._secrets, profiles)
            self.vpn_manager = vpn
            self.registry.register(VpnTool(vpn))
        elif any(h.vpn_required for h in self.cfg.remote.hosts.values()):
            raise UserFacingError(
                "có host.vpn_required nhưng remote.vpn_profiles trống — fail-closed "
                "(khai VPN profile hoặc bỏ vpn_required)"
            )
        if self.host_registry is not None:
            backend = AsyncSSHBackend()
            self.registry.register(
                SshExecTool(self.host_registry, backend, self._secrets, vpn=vpn)
            )
            self.registry.register(
                LogReadTool(self.host_registry, backend, self._secrets, vpn=vpn)
            )

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
            "image_gen": "sinh ảnh (lưu vào workspace)",
            "db_query": "query DB CHỈ ĐỌC (không sửa/xóa)", "db_config": "quản lý profile DB",
            "ssh_exec": "chạy lệnh trên server qua SSH (deploy phải duyệt; cấm xóa file)",
            "log_read": "đọc log server (read-only)", "vpn": "bật/tắt VPN",
            "load_skill": "nạp hướng dẫn skill", "delegate": "giao việc cho subagent",
            "task_add": "thêm việc/mục tiêu cần làm", "task_list": "xem việc cần làm",
            "task_update": "cập nhật/hoàn thành việc",
            "memory_propose": "đề xuất ghi nhớ dài hạn vào staging (chờ duyệt mới vào MEMORY.md)",
            "notify": "đẩy thông báo cho người dùng qua kênh đã bật (Telegram/Zalo)",
        }
        channel_bits: list[str] = []
        if getattr(self.cfg.channels, "telegram", None) is not None and self.cfg.channels.telegram.enabled:
            channel_bits.append("Telegram")
        if getattr(self.cfg.channels, "zalo", None) is not None and self.cfg.channels.zalo.enabled:
            channel_bits.append("Zalo Bot API")
        if channel_bits:
            tool_desc["notify"] = f"đẩy thông báo cho người dùng qua {', '.join(channel_bits)}"
        lines = ["Bạn là yett — trợ lý DevOps cá nhân, fail-closed (mặc định từ chối, chặn trước khi chạy).",
                 "", "KHẢ NĂNG (tool đang bật):"]
        for name in self.registry.names():
            lines.append(f"- {name}: {tool_desc.get(name, name)}")
        if channel_bits:
            lines.append(f"\nKênh chat ngoài: {', '.join(channel_bits)} (gating/allowlist hoặc pairing).")
        if self.cfg.projects:
            lines.append(f"\nProject đã đăng ký: {', '.join(self.cfg.projects)}")
        if self.cfg.databases:
            lines.append(f"Database (chỉ đọc): {', '.join(self.cfg.databases)}")
        if self.cfg.remote.hosts:
            lines.append(f"Server SSH: {', '.join(self.cfg.remote.hosts)}")
        if self.cfg.remote.vpn_profiles:
            lines.append(f"VPN profile: {', '.join(self.cfg.remote.vpn_profiles)}")
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
        lines.append(
            "\nTRỢ LÝ CÁ NHÂN: anh quản lý việc/mục tiêu qua task_add/task_list/task_update."
            " Khi người dùng nói kiểu 'nhắc tôi…', 'ghi lại việc…', 'tôi cần làm…' → tạo task."
            " Khi được hỏi 'tôi đang làm gì / hôm nay có gì' → dùng task_list, ưu tiên việc"
            " quá hạn/đến hạn. Chủ động gợi ý bước tiếp và hỏi lại khi thiếu thông tin."
        )
        lines.append(
            "\nBỘ NHỚ DÀI HẠN: sự kiện/ưu tiên đáng nhớ lâu → memory_propose (chỉ staging)."
            " KHÔNG ghi thẳng MEMORY.md / memory/pending bằng write_file. Người dùng duyệt bằng"
            " `yett memory list|approve|reject`."
        )
        lines.append("\nGiới hạn an toàn: KHÔNG xóa file OS trên server, KHÔNG ALTER/DELETE/UPDATE DB "
                     "trừ khi được duyệt tường minh. Khi bị chặn, giải thích và đề xuất cách an toàn.")
        return "\n".join(lines)

    def briefing(self) -> str:
        """Tóm tắt 'hôm nay có gì' từ việc + hoạt động thật (dùng cho web, cron, chat)."""
        from yett.brief import daily_briefing

        return daily_briefing(self)

    async def chat(self, message: str, *, session_key: str = "main", turn_id: str | None = None,
                   project: str | None = None) -> TurnResult:
        system = self.workspace.build_system_prompt(
            self.capabilities_summary(), token_budget=self.cfg.budget.context_token_budget // 2
        )
        # Nhớ hội thoại xuyên turn: nạp lại lượt gần nhất của session (provider-agnostic).
        history = self.sessions.history(
            session_key, token_budget=self.cfg.budget.context_token_budget // 4
        )
        # Bối cảnh project (nếu chọn): báo agent làm việc ở project nào + path thật.
        # File tool đã có scope tới path này; đây là inject bối cảnh, không phải sandbox riêng.
        user_msg = message
        if project and project in self.cfg.projects:
            pc = self.cfg.projects[project]
            parts = [f"project '{project}' tại {pc.path}"]
            if pc.hosts:
                parts.append(f"server SSH: {', '.join(pc.hosts)}")
            if pc.databases:
                parts.append(f"database: {', '.join(pc.databases)}")
            user_msg = f"[Bối cảnh: làm việc trong {'; '.join(parts)}]\n\n{message}"
        ctx = assemble_context(system, user_msg, history=history)
        tid = turn_id or f"turn-{int(self._clock()*1000)}"
        # Complexity router: câu dễ → ít vòng lặp (tiết kiệm chi phí); câu khó → nhiều vòng.
        max_iter = self.complexity.classify(message).max_iterations if self.cfg.router.enabled else None
        # session_ctx mang allowed_tools = tất cả (cho phép delegate ở cấp cha)
        parent = _MainCtx(session_key, set(self.registry.names()))
        # Token hủy: đăng ký theo session để /api/cancel (thread khác) gọi được giữa turn.
        token = CancelToken()
        self._cancel_tokens[session_key] = token
        try:
            res = await self.loop.run_turn(
                ctx, session_key=session_key, turn_id=tid, cancel=token,
                session_ctx=parent, max_iterations=max_iter,
            )
        finally:
            self._cancel_tokens.pop(session_key, None)
        # Ghi lượt vào lịch sử (chỉ khi turn xong bình thường, có nội dung trả lời).
        if res.status == "done" and res.text:
            self.sessions.record_turn(session_key, message, res.text, ts=self._clock())
        return res

    def cancel(self, session_key: str = "main") -> bool:
        """Hủy turn đang chạy của session (nếu có). Loop sẽ dừng sạch ở ranh giới stage,
        checkpoint status='canceled' → resume được. True nếu có turn để hủy."""
        token = self._cancel_tokens.get(session_key)
        if token is None:
            return False
        token.cancel()
        return True

    def set_approver(self, approver) -> None:
        """Gắn approver (vd web ApprovalCenter.request) — turn sẽ hỏi duyệt khi Gate cần."""
        self.loop._approver = approver

    def set_notifier(self, notifier: Callable[[str], int] | None) -> None:
        """Gắn kênh đẩy tin (vd TelegramChannel.notify) cho tool notify + briefing tự động."""
        self._notifier = notifier

    def close(self) -> None:
        if self.vpn_manager is not None:
            self.vpn_manager.close()
        self.spanstore.close()
        self.checkpoints.close()
        self.cron.close()
        self.sessions.close()
        self.tasks.close()


class _MainCtx:
    """session_ctx cấp cha: mang allowed_tools (để delegate kiểm toolset con ⊆ cha)."""

    def __init__(self, session_key: str, allowed_tools: set[str]) -> None:
        self.session_key = session_key
        self.allowed_tools = allowed_tools
        self.is_subagent = False
        self.parent_span_id: str | None = None
