"""VPN tool (spec P2 §2.3). Bọc CLI OpenVPN/openfortivpn.

Credentials từ secret store tại điểm dùng — KHÔNG vào context/span/log/error message.
CLI runner injectable để test offline. Thực thi argv-only (không shell). Model chỉ được
chọn TÊN profile trong allowlist config — không truyền flag tùy ý.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from yett.config.models import VpnProfileCfg
from yett.core.cancel import CancelToken, Cancelled
from yett.errors import SecretNotFound, UserFacingError
from yett.security.filters import redact
from yett.tools.base import ToolCtx, ToolResult

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_BINARIES = {
    "openfortivpn": "openfortivpn",
    "openvpn": "openvpn",
}
_SUCCESS_MARKERS = {
    "openfortivpn": b"Tunnel is up",
    "openvpn": b"Initialization Sequence Completed",
}
# Grace period: process còn sống sau marker hoặc hết grace → coi như connected.
_STABLE_WITHOUT_MARKER_SEC = 2.0
_DISCONNECT_WAIT_SEC = 5.0


class VpnRunner(Protocol):
    async def connect(
        self, profile: str, *, creds: dict[str, str], cancel: CancelToken | None = None
    ) -> bool: ...
    async def disconnect(self, profile: str) -> bool: ...
    async def status(self, profile: str) -> bool: ...  # True = đang kết nối


@dataclass
class OwnedProcess:
    """Process do yett spawn — chỉ disconnect được phiên mình sở hữu."""

    pid: int
    argv0: str
    _proc: Any  # asyncio.subprocess.Process | test double
    _stdout_buf: bytearray = field(default_factory=bytearray)
    _stderr_buf: bytearray = field(default_factory=bytearray)
    _reader_tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def returncode(self) -> int | None:
        return getattr(self._proc, "returncode", None)

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except ProcessLookupError:
            pass

    def kill(self) -> None:
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass

    async def wait(self, timeout: float | None = None) -> int:
        if timeout is None:
            return int(await self._proc.wait())
        return int(await asyncio.wait_for(self._proc.wait(), timeout=timeout))

    async def drain_output(self) -> None:
        """Đọc hết stdout/stderr đã buffer (không lộ ra ngoài trừ khi caller redact)."""
        for t in self._reader_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        self._reader_tasks.clear()


class ArgvCommander(Protocol):
    """Injectable argv-only process layer — KHÔNG được dùng shell."""

    async def start(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> OwnedProcess: ...

    which: Any  # Callable[[str], str | None] — gắn shutil.which hoặc fake


class SubprocessArgvCommander:
    """asyncio.create_subprocess_exec — argv list, không shell."""

    which = staticmethod(shutil.which)

    async def start(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
    ) -> OwnedProcess:
        if not argv:
            raise UserFacingError("VPN: argv rỗng")
        # Fail-closed: từ chối nếu phần tử argv không phải str (tránh bytes/path object lạ).
        clean = [str(a) for a in argv]
        for a in clean:
            if "\x00" in a:
                raise UserFacingError("VPN: argv chứa null byte — từ chối")
        proc = await asyncio.create_subprocess_exec(
            *clean,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=dict(env) if env is not None else None,
            start_new_session=True,  # process group riêng → disconnect gửi tín hiệu cả nhóm
        )
        owned = OwnedProcess(pid=int(proc.pid or 0), argv0=clean[0], _proc=proc)
        if stdin is not None and proc.stdin is not None:
            proc.stdin.write(stdin)
            await proc.stdin.drain()
            proc.stdin.close()
        if proc.stdout is not None:
            owned._reader_tasks.append(asyncio.create_task(_pump(proc.stdout, owned._stdout_buf)))
        if proc.stderr is not None:
            owned._reader_tasks.append(asyncio.create_task(_pump(proc.stderr, owned._stderr_buf)))
        return owned


async def _pump(stream: asyncio.StreamReader, buf: bytearray) -> None:
    try:
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return
            # Giới hạn buffer để không giữ secret/log lớn trong RAM.
            if len(buf) < 64_000:
                buf.extend(chunk[: 64_000 - len(buf)])
    except (asyncio.CancelledError, Exception):
        return


@dataclass
class _Session:
    process: OwnedProcess
    cleanup_paths: list[Path]
    kind: str


class SubprocessVpnRunner:
    """VpnRunner thật: openfortivpn / openvpn qua argv cố định từ profile allowlist.

    - Injectable `commander` để test offline.
    - Credentials → file tạm 0600 (hoặc stdin cho auth) + cleanup bắt buộc.
    - Không nhận flag từ model; chỉ tên profile.
    - Chỉ disconnect process do runner này spawn (ownership).
    """

    def __init__(
        self,
        profiles: Mapping[str, VpnProfileCfg],
        *,
        commander: ArgvCommander | None = None,
        binary_paths: Mapping[str, str] | None = None,
    ) -> None:
        self._profiles = dict(profiles)
        self._commander: ArgvCommander = commander or SubprocessArgvCommander()
        self._binary_paths = dict(binary_paths or {})
        self._sessions: dict[str, _Session] = {}

    def _resolve_binary(self, kind: str) -> str:
        if kind in self._binary_paths:
            return str(self._binary_paths[kind])
        name = _BINARIES.get(kind)
        if not name:
            raise UserFacingError(f"VPN: kind '{kind}' không hỗ trợ")
        path = self._commander.which(name)
        if not path:
            raise UserFacingError(
                f"VPN: không tìm thấy lệnh '{name}' trong PATH — chạy 'yett doctor'"
            )
        return str(path)

    def _require_profile(self, profile: str) -> VpnProfileCfg:
        if not profile or _PROFILE_NAME_RE.fullmatch(profile) is None:
            raise UserFacingError("VPN: tên profile không hợp lệ")
        cfg = self._profiles.get(profile)
        if cfg is None:
            raise UserFacingError(
                f"VPN: profile '{profile}' không nằm trong allowlist config (fail-closed)"
            )
        return cfg

    async def status(self, profile: str) -> bool:
        self._require_profile(profile)
        sess = self._sessions.get(profile)
        if sess is None:
            return False
        rc = sess.process.returncode()
        if rc is not None:
            await self._drop_session(profile, kill=False)
            return False
        return True

    async def connect(
        self, profile: str, *, creds: dict[str, str], cancel: CancelToken | None = None
    ) -> bool:
        cfg = self._require_profile(profile)
        if await self.status(profile):
            return True  # idempotent — đã sở hữu phiên đang chạy
        # Duplicate connect lúc đang connect: từ chối (không spawn song song).
        if profile in self._sessions:
            raise UserFacingError(f"VPN: profile '{profile}' đang kết nối — không connect trùng")

        binary = self._resolve_binary(cfg.kind)
        cleanup: list[Path] = []
        proc: OwnedProcess | None = None
        try:
            argv, stdin, cleanup = self._build_argv(cfg, binary, creds, cleanup)
            proc = await self._commander.start(argv, stdin=stdin, env=_scrubbed_env())
            self._sessions[profile] = _Session(process=proc, cleanup_paths=list(cleanup), kind=cfg.kind)
            # Cred file có thể xoá sau khi process đọc (openfortivpn/openvpn đọc lúc start).
            # Giữ tới khi connect xong/fail để chắc process đã mở file; rồi cleanup sớm.
            ok = await self._wait_until_up(proc, cfg, cancel=cancel)
            if not ok:
                await self._drop_session(profile, kill=True)
                return False
            # Cleanup temp sớm (process đã đọc); session vẫn giữ process handle.
            sess = self._sessions.get(profile)
            if sess is not None:
                _cleanup_paths(sess.cleanup_paths)
                sess.cleanup_paths.clear()
            return True
        except FileNotFoundError:
            await self._abort_partial(profile, proc, cleanup)
            raise UserFacingError(
                f"VPN: không tìm thấy binary cho '{cfg.kind}' — chạy 'yett doctor'"
            ) from None
        except Cancelled:
            await self._abort_partial(profile, proc, cleanup)
            raise
        except UserFacingError:
            await self._abort_partial(profile, proc, cleanup)
            raise
        except Exception:
            await self._abort_partial(profile, proc, cleanup)
            # Không nhúng stdout/stderr/creds vào message (có thể chứa password).
            raise UserFacingError(f"VPN: lỗi kết nối profile '{profile}'") from None

    async def disconnect(self, profile: str) -> bool:
        self._require_profile(profile)
        sess = self._sessions.get(profile)
        if sess is None:
            raise UserFacingError(
                f"VPN: không sở hữu phiên '{profile}' — chỉ ngắt được VPN do yett bật"
            )
        return await self._drop_session(profile, kill=True)

    async def _drop_session(self, profile: str, *, kill: bool) -> bool:
        sess = self._sessions.pop(profile, None)
        if sess is None:
            return False
        try:
            if kill and sess.process.returncode() is None:
                _signal_group(sess.process, signal.SIGTERM)
                try:
                    await sess.process.wait(timeout=_DISCONNECT_WAIT_SEC)
                except (asyncio.TimeoutError, TimeoutError):
                    _signal_group(sess.process, signal.SIGKILL)
                    try:
                        await sess.process.wait(timeout=2.0)
                    except (asyncio.TimeoutError, TimeoutError, Exception):
                        pass
            await sess.process.drain_output()
        finally:
            _cleanup_paths(sess.cleanup_paths)
        return True

    async def _abort_partial(
        self, profile: str, proc: OwnedProcess | None, cleanup: list[Path]
    ) -> None:
        if profile in self._sessions:
            await self._drop_session(profile, kill=True)
            return
        if proc is not None and proc.returncode() is None:
            _signal_group(proc, signal.SIGKILL)
            try:
                await proc.wait(timeout=2.0)
            except Exception:
                pass
            await proc.drain_output()
        _cleanup_paths(cleanup)

    async def _wait_until_up(
        self, proc: OwnedProcess, cfg: VpnProfileCfg, *, cancel: CancelToken | None
    ) -> bool:
        marker = _SUCCESS_MARKERS.get(cfg.kind, b"")
        deadline = asyncio.get_event_loop().time() + float(cfg.connect_timeout_sec)
        started = asyncio.get_event_loop().time()
        while True:
            if cancel is not None:
                cancel.check()
            rc = proc.returncode()
            if rc is not None:
                return False
            blob = bytes(proc._stdout_buf) + bytes(proc._stderr_buf)
            if marker and marker in blob:
                return True
            now = asyncio.get_event_loop().time()
            if not marker and (now - started) >= _STABLE_WITHOUT_MARKER_SEC:
                return True
            # Fallback: sống quá stable window + có output hoạt động → connected.
            if marker and (now - started) >= max(_STABLE_WITHOUT_MARKER_SEC, 5.0):
                # Vẫn chưa thấy marker nhưng process sống — một số bản CLI im lặng.
                # Fail-closed nhẹ: chỉ chấp nhận nếu đã qua ½ timeout và process còn sống.
                if (now - started) >= min(float(cfg.connect_timeout_sec) * 0.5, 15.0):
                    return True
            if now >= deadline:
                return False
            await asyncio.sleep(0.05)

    def _build_argv(
        self,
        cfg: VpnProfileCfg,
        binary: str,
        creds: Mapping[str, str],
        cleanup: list[Path],
    ) -> tuple[list[str], bytes | None, list[Path]]:
        """Dựng argv cố định — KHÔNG lấy flag từ model/creds keys lạ."""
        if cfg.kind == "openfortivpn":
            return self._argv_openfortivpn(cfg, binary, creds, cleanup)
        if cfg.kind == "openvpn":
            return self._argv_openvpn(cfg, binary, creds, cleanup)
        raise UserFacingError(f"VPN: kind '{cfg.kind}' không hỗ trợ")

    def _argv_openfortivpn(
        self,
        cfg: VpnProfileCfg,
        binary: str,
        creds: Mapping[str, str],
        cleanup: list[Path],
    ) -> tuple[list[str], bytes | None, list[Path]]:
        user = creds.get("username", "")
        password = creds.get("password", "")
        # Config tạm chứa secret — 0600, cleanup bắt buộc. Không đưa password lên argv.
        body = (
            f"host = {cfg.host}\n"
            f"port = {int(cfg.port)}\n"
            f"username = {_cfg_escape(user)}\n"
            f"password = {_cfg_escape(password)}\n"
        )
        cred_path = _write_secret_temp(body.encode("utf-8"), suffix=".conf")
        cleanup.append(cred_path)
        # argv cố định: binary -c <temp>. Không thêm flag từ ngoài.
        return [binary, "-c", str(cred_path)], None, cleanup

    def _argv_openvpn(
        self,
        cfg: VpnProfileCfg,
        binary: str,
        creds: Mapping[str, str],
        cleanup: list[Path],
    ) -> tuple[list[str], bytes | None, list[Path]]:
        ovpn = _open_config_nofollow(cfg.config_file)
        argv = [binary, "--config", ovpn, "--verb", "1"]
        password = creds.get("password", "")
        user = creds.get("username", "")
        if cfg.cred_secret or password or user:
            # --auth-user-pass <file> với username\npassword; không đưa secret lên argv.
            auth = f"{user}\n{password}\n".encode("utf-8")
            auth_path = _write_secret_temp(auth, suffix=".auth")
            cleanup.append(auth_path)
            argv.extend(["--auth-user-pass", str(auth_path)])
        return argv, None, cleanup


def _cfg_escape(value: str) -> str:
    """Escape tối thiểu cho file config openfortivpn — không dùng shell."""
    return value.replace("\n", "").replace("\r", "")


def _scrubbed_env() -> dict[str, str]:
    """Env tối thiểu cho VPN child — không kế thừa secret từ process cha nếu có thể."""
    keep = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "HOME", "USER", "LOGNAME")
    out: dict[str, str] = {}
    for k in keep:
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def _write_secret_temp(data: bytes, *, suffix: str) -> Path:
    """Tạo file tạm owner-only (0600), O_EXCL|O_NOFOLLOW, trong dir 0700."""
    tmpdir = tempfile.mkdtemp(prefix="yett-vpn-")
    try:
        os.chmod(tmpdir, 0o700)
    except OSError:
        pass
    name = f"cred{suffix}"
    path = Path(tmpdir) / name
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("cred temp không phải regular file")
        os.write(fd, data)
        try:
            os.fchmod(fd, 0o600)
        except OSError:
            pass
    finally:
        os.close(fd)
    # Chống swap symlink giữa open và dùng.
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        _cleanup_paths([path, Path(tmpdir)])
        raise UserFacingError("VPN: từ chối cred temp không an toàn (symlink/non-file)")
    return path


def _open_config_nofollow(config_file: str) -> str:
    """Xác nhận .ovpn là regular file, không symlink — trả path đã kiểm."""
    p = Path(config_file)
    if not p.is_absolute() or ".." in p.parts:
        raise UserFacingError("VPN: config_file không hợp lệ")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(str(p), flags)
    except FileNotFoundError:
        raise UserFacingError("VPN: không tìm thấy config_file") from None
    except OSError as e:
        # ELOOP / tương đương khi gặp symlink với O_NOFOLLOW
        raise UserFacingError("VPN: từ chối config_file (symlink hoặc không đọc được)") from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UserFacingError("VPN: config_file phải là regular file")
    finally:
        os.close(fd)
    # lstat thêm lần nữa — phát hiện nếu path là symlink (khi O_NOFOLLOW không có).
    lst = os.lstat(p)
    if stat.S_ISLNK(lst.st_mode):
        raise UserFacingError("VPN: từ chối config_file là symlink")
    return str(p)


def _cleanup_paths(paths: list[Path]) -> None:
    """Xoá file/dir tạm best-effort; không raise (cleanup phải chạy trong finally)."""
    seen: set[Path] = set()
    for p in paths:
        if p is None or p in seen:
            continue
        seen.add(p)
        try:
            if p.is_symlink() or p.is_file():
                p.unlink(missing_ok=True)
            elif p.is_dir():
                for child in p.iterdir():
                    try:
                        child.unlink(missing_ok=True)
                    except OSError:
                        pass
                p.rmdir()
        except OSError:
            pass
        # Parent dir yett-vpn-* nếu còn.
        parent = p.parent
        if parent.name.startswith("yett-vpn-"):
            try:
                if parent.is_dir():
                    for child in parent.iterdir():
                        try:
                            child.unlink(missing_ok=True)
                        except OSError:
                            pass
                    parent.rmdir()
            except OSError:
                pass


def _signal_group(proc: OwnedProcess, sig: signal.Signals) -> None:
    """Gửi tín hiệu tới process group (start_new_session) — ownership của yett."""
    pid = proc.pid
    if pid <= 0:
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()
        return
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        if sig == signal.SIGKILL:
            proc.kill()
        else:
            proc.terminate()


def _safe_secret_name_error(name: str) -> UserFacingError:
    # Chỉ lộ TÊN secret, không lộ giá trị.
    return UserFacingError(f"VPN: thiếu secret '{name}' trong secret store (fail-closed)")


class VpnManager:
    """Quản lý trạng thái VPN + đảm bảo bật trước khi SSH (ensure)."""

    def __init__(
        self,
        runner: VpnRunner,
        secrets: Any,
        profiles: Mapping[str, VpnProfileCfg],
    ) -> None:
        self._runner = runner
        self._secrets = secrets
        self._profiles = dict(profiles)
        self._connected: set[str] = set()

    async def ensure(self, profile: str, *, cancel: CancelToken | None = None) -> None:
        if not profile or _PROFILE_NAME_RE.fullmatch(profile) is None:
            raise UserFacingError("VPN: tên profile không hợp lệ")
        if profile not in self._profiles:
            raise UserFacingError(
                f"VPN: profile '{profile}' không nằm trong allowlist config (fail-closed)"
            )
        if profile in self._connected or await self._runner.status(profile):
            self._connected.add(profile)
            return
        creds = self._creds(profile)
        try:
            ok = await self._runner.connect(profile, creds=creds, cancel=cancel)
        finally:
            # Không giữ password trong dict sau điểm dùng.
            creds.clear()
        if not ok:
            raise UserFacingError(f"không kết nối được VPN '{profile}'")
        self._connected.add(profile)

    async def disconnect(self, profile: str) -> None:
        if profile not in self._profiles:
            raise UserFacingError(
                f"VPN: profile '{profile}' không nằm trong allowlist config (fail-closed)"
            )
        await self._runner.disconnect(profile)
        self._connected.discard(profile)

    async def status(self, profile: str) -> bool:
        if profile not in self._profiles:
            raise UserFacingError(
                f"VPN: profile '{profile}' không nằm trong allowlist config (fail-closed)"
            )
        connected = await self._runner.status(profile)
        if connected:
            self._connected.add(profile)
        else:
            self._connected.discard(profile)
        return connected

    def _creds(self, profile: str) -> dict[str, str]:
        cfg = self._profiles[profile]
        out: dict[str, str] = {}
        if cfg.username:
            out["username"] = cfg.username
        elif cfg.username_secret:
            try:
                out["username"] = str(self._secrets.get(cfg.username_secret))
            except SecretNotFound as e:
                raise _safe_secret_name_error(cfg.username_secret) from e
        if cfg.cred_secret:
            try:
                out["password"] = str(self._secrets.get(cfg.cred_secret))
            except SecretNotFound as e:
                raise _safe_secret_name_error(cfg.cred_secret) from e
        return out


class VpnTool:
    """Bật/tắt/xem trạng thái VPN."""

    name = "vpn"
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["connect", "disconnect", "status"]},
            "profile": {"type": "string"},
        },
        "required": ["action", "profile"],
    }

    def __init__(self, manager: VpnManager) -> None:
        self._m = manager

    def validate(self, args: dict) -> None:
        if args.get("action") not in ("connect", "disconnect", "status"):
            raise UserFacingError("action phải là connect|disconnect|status")
        profile = args.get("profile")
        if not profile or not isinstance(profile, str):
            raise UserFacingError("thiếu 'profile'")
        if _PROFILE_NAME_RE.fullmatch(profile) is None:
            raise UserFacingError("VPN: tên profile không hợp lệ")

    async def run(self, args: dict, ctx: ToolCtx) -> ToolResult:
        action, profile = args["action"], args["profile"]
        cancel = getattr(ctx, "cancel", None)
        try:
            if action == "connect":
                await self._m.ensure(profile, cancel=cancel if isinstance(cancel, CancelToken) else None)
                return ToolResult.success(f"VPN '{profile}' đã kết nối")
            if action == "disconnect":
                await self._m.disconnect(profile)
                return ToolResult.success(f"VPN '{profile}' đã ngắt")
            connected = await self._m.status(profile)
            return ToolResult.success(
                f"VPN '{profile}': {'connected' if connected else 'disconnected'}"
            )
        except UserFacingError as e:
            # Redact phòng message vô tình chứa pattern secret.
            return ToolResult.error(redact(str(e)))
