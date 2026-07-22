"""VPN tool (spec P2 §2.3). Bọc CLI OpenVPN/openfortivpn.

Credentials từ secret store tại điểm dùng — KHÔNG vào context/span/log/error message.
CLI runner injectable để test offline. Thực thi argv-only (không shell). Model chỉ được
chọn TÊN profile trong allowlist config — không truyền flag tùy ý.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from yett.config.models import VpnProfileCfg
from yett.core.cancel import CancelToken, Cancelled
from yett.errors import SecretNotFound, UserFacingError
from yett.security.filters import redact
from yett.tools.base import ToolCtx, ToolResult

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_TERMINATE_SIGNAL = signal.SIGTERM
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
_BINARIES = {
    "openfortivpn": "openfortivpn",
    "openvpn": "openvpn",
}
_SUCCESS_MARKERS = {
    "openfortivpn": b"Tunnel is up",
    "openvpn": b"Initialization Sequence Completed",
}
_DISCONNECT_WAIT_SEC = 5.0
_READINESS_STABLE_SEC = 0.25
_MAX_CAPTURE_BYTES = 64_000
_MAX_CONFIG_BYTES = 1_048_576
_UNSAFE_OPENVPN_DIRECTIVES = {
    "askpass",
    "auth-gen-token-secret",
    "auth-user-pass",
    "auth-user-pass-verify",
    "cd",
    "chroot",
    "client-connect",
    "client-config-dir",
    "client-crresponse",
    "client-disconnect",
    "config",
    "daemon",
    "down",
    "down-pre",
    "engine",
    "ipchange",
    "iproute",
    "learn-address",
    "log",
    "log-append",
    "management",
    "management-client",
    "management-client-auth",
    "management-external-cert",
    "management-external-key",
    "management-hold",
    "management-query-passwords",
    "management-signal",
    "management-up-down",
    "plugin",
    "pkcs11-providers",
    "route-pre-down",
    "route-up",
    "script-security",
    "status",
    "tls-export-cert",
    "tls-verify",
    "tmp-dir",
    "up",
    "up-delay",
    "up-restart",
    "writepid",
}


class VpnRunner(Protocol):
    async def connect(
        self, profile: str, *, creds: dict[str, str], cancel: CancelToken | None = None
    ) -> bool: ...
    async def disconnect(self, profile: str) -> bool: ...
    async def status(self, profile: str) -> bool: ...  # True = đang kết nối
    def close(self) -> None: ...


@dataclass
class OwnedProcess:
    """Process spawned by yett, retaining an exact-process signaling handle when possible."""

    pid: int
    argv0: str
    _proc: Any  # subprocess.Popen | test double
    _stdout_buf: bytearray = field(default_factory=bytearray)
    _stderr_buf: bytearray = field(default_factory=bytearray)
    _reader_threads: list[threading.Thread] = field(default_factory=list)
    _output_lock: threading.Lock = field(default_factory=threading.Lock)
    _capture_output: bool = True
    _identity: str | None = None
    _pidfd: int | None = None
    _owns_process_group: bool = False

    def returncode(self) -> int | None:
        poll = getattr(self._proc, "poll", None)
        value = poll() if callable(poll) else getattr(self._proc, "returncode", None)
        if value is not None:
            self._close_pidfd()
        return int(value) if value is not None else None

    def terminate(self) -> None:
        self._signal(_TERMINATE_SIGNAL)

    def kill(self) -> None:
        self._signal(_KILL_SIGNAL)

    def terminate_group(self) -> None:
        self._signal_owned_group(_TERMINATE_SIGNAL)

    def kill_group(self) -> None:
        self._signal_owned_group(_KILL_SIGNAL)

    def _signal_owned_group(self, sig: signal.Signals) -> None:
        """Signal only the session created at spawn, while its unreaped leader proves ownership."""
        if not self._owns_process_group or self.returncode() is not None:
            self._signal(sig)
            return
        try:
            if self._identity is not None and _process_identity(self.pid) != self._identity:
                return
            getpgid = getattr(os, "getpgid", None)
            killpg = getattr(os, "killpg", None)
            if not callable(getpgid) or not callable(killpg):
                self._signal(sig)
                return
            if getpgid(self.pid) != self.pid:
                self._signal(sig)
                return
            killpg(self.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            self._signal(sig)

    def _signal(self, sig: signal.Signals) -> None:
        """Never signal a numeric process group that could now belong to another process."""
        if self.returncode() is not None:
            return
        try:
            if self._pidfd is not None and hasattr(signal, "pidfd_send_signal"):
                signal.pidfd_send_signal(self._pidfd, sig)
                return
            if self._identity is not None and _process_identity(self.pid) != self._identity:
                return
            send_signal = getattr(self._proc, "send_signal", None)
            if callable(send_signal):
                send_signal(sig)
            elif sig == _KILL_SIGNAL:
                self._proc.kill()
            else:
                self._proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass

    async def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            rc = self.returncode()
            if rc is not None:
                return rc
            if deadline is not None and time.monotonic() >= deadline:
                raise asyncio.TimeoutError
            await asyncio.sleep(0.02)

    async def drain_output(self) -> None:
        """Discard sensitive output while reader threads keep pipes from blocking the child."""
        self.suppress_output()
        if self.returncode() is not None:
            for thread in self._reader_threads:
                await asyncio.to_thread(thread.join, 0.2)
            self._reader_threads.clear()

    def suppress_output(self) -> None:
        with self._output_lock:
            self._capture_output = False
            _zero_bytearray(self._stdout_buf)
            _zero_bytearray(self._stderr_buf)

    def _close_pidfd(self) -> None:
        if self._pidfd is None:
            return
        try:
            os.close(self._pidfd)
        except OSError:
            pass
        self._pidfd = None


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
    """Loop-independent, argv-only subprocess owner; never invokes a shell."""

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
        if os.name != "posix":
            raise UserFacingError(
                "VPN runtime không hỗ trợ Windows native; hãy chạy yett và VPN trong WSL2"
            )
        clean: list[str] = []
        for a in argv:
            if not isinstance(a, str):
                raise UserFacingError("VPN: mọi phần tử argv phải là chuỗi")
            if "\x00" in a:
                raise UserFacingError("VPN: argv chứa null byte — từ chối")
            clean.append(a)
        proc = subprocess.Popen(  # noqa: S603 -- fixed argv, shell explicitly disabled
            clean,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env) if env is not None else None,
            shell=False,
            close_fds=True,
            start_new_session=True,
        )
        pid = int(proc.pid)
        pidfd: int | None = None
        if hasattr(os, "pidfd_open"):
            try:
                pidfd = os.pidfd_open(pid)
            except OSError:
                pass
        owned = OwnedProcess(
            pid=pid,
            argv0=clean[0],
            _proc=proc,
            _identity=_process_identity(pid),
            _pidfd=pidfd,
            _owns_process_group=True,
        )
        try:
            if stdin is not None and proc.stdin is not None:
                proc.stdin.write(stdin)
                proc.stdin.flush()
                proc.stdin.close()
            for stream, buf in ((proc.stdout, owned._stdout_buf), (proc.stderr, owned._stderr_buf)):
                if stream is None:
                    continue
                thread = threading.Thread(
                    target=_pump,
                    args=(stream, buf, owned),
                    daemon=True,
                    name="yett-vpn-output",
                )
                owned._reader_threads.append(thread)
                thread.start()
        except BaseException:
            owned.kill_group()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            owned.suppress_output()
            owned._close_pidfd()
            raise
        return owned


def _pump(stream: Any, buf: bytearray, proc: OwnedProcess) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            with proc._output_lock:
                if proc._capture_output and len(buf) < _MAX_CAPTURE_BYTES:
                    buf.extend(chunk[: _MAX_CAPTURE_BYTES - len(buf)])
    except Exception:
        return
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _process_identity(pid: int) -> str | None:
    """Linux process start-time token, used only when pidfd signaling is unavailable."""
    if not sys.platform.startswith("linux") or pid <= 0:
        return None
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2 :].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def _zero_bytearray(buf: bytearray) -> None:
    for index in range(len(buf)):
        buf[index] = 0
    buf.clear()


@dataclass
class _Session:
    process: OwnedProcess
    cleanup_paths: list[Path]
    kind: str
    ready: bool = False


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
        self._locks = {name: threading.Lock() for name in self._profiles}
        self._closed = threading.Event()

    def _resolve_binary(self, kind: str) -> str:
        name = _BINARIES.get(kind)
        if not name:
            raise UserFacingError(f"VPN: kind '{kind}' không hỗ trợ")
        path = self._binary_paths.get(kind)
        if path is None:
            path = self._commander.which(name)
        if not path:
            raise UserFacingError(
                f"VPN: không tìm thấy lệnh '{name}' trong PATH — chạy 'yett doctor'"
            )
        candidate = Path(str(path))
        if not candidate.is_absolute() or candidate.name != name:
            raise UserFacingError(f"VPN: đường dẫn binary '{name}' không hợp lệ")
        if isinstance(self._commander, SubprocessArgvCommander):
            try:
                resolved = candidate.resolve(strict=True)
                mode = resolved.stat().st_mode
            except OSError:
                raise UserFacingError(f"VPN: binary '{name}' không đọc được") from None
            if not stat.S_ISREG(mode) or not os.access(resolved, os.X_OK):
                raise UserFacingError(f"VPN: binary '{name}' không phải executable regular file")
            if mode & (stat.S_IWGRP | stat.S_IWOTH):
                raise UserFacingError(f"VPN: binary '{name}' cho phép user khác ghi — từ chối")
            getuid = getattr(os, "getuid", None)
            current_uid = int(getuid()) if callable(getuid) else 0
            if resolved.stat().st_uid not in {current_uid, 0}:
                raise UserFacingError(f"VPN: binary '{name}' không thuộc current user/root")
            return str(resolved)
        return str(candidate)

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
        lock = await self._acquire(profile)
        try:
            sess = self._sessions.get(profile)
            if sess is None:
                return False
            if sess.process.returncode() is not None:
                await self._drop_session(profile, kill=False)
                return False
            return sess.ready
        finally:
            lock.release()

    async def connect(
        self, profile: str, *, creds: dict[str, str], cancel: CancelToken | None = None
    ) -> bool:
        cfg = self._require_profile(profile)
        lock = await self._acquire(profile, cancel=cancel)
        try:
            sess = self._sessions.get(profile)
            if sess is not None:
                if sess.process.returncode() is None and sess.ready:
                    return True
                await self._drop_session(profile, kill=True)
            if self._closed.is_set():
                raise UserFacingError("VPN: runner đã đóng")
            if isinstance(self._commander, SubprocessArgvCommander) and os.name != "posix":
                raise UserFacingError(
                    "VPN runtime không hỗ trợ Windows native; hãy dùng WSL2"
                )

            binary = self._resolve_binary(cfg.kind)
            cleanup: list[Path] = []
            proc: OwnedProcess | None = None
            try:
                argv, stdin, cleanup = self._build_argv(cfg, binary, creds, cleanup)
                proc = await self._commander.start(argv, stdin=stdin, env=_scrubbed_env())
                self._sessions[profile] = _Session(
                    process=proc,
                    cleanup_paths=list(cleanup),
                    kind=cfg.kind,
                )
                ok = await self._wait_until_up(proc, cfg, cancel=cancel)
                if not ok:
                    await self._drop_session(profile, kill=True)
                    return False
                sess = self._sessions.get(profile)
                if sess is None or sess.process is not proc or proc.returncode() is not None:
                    await self._abort_partial(profile, proc, cleanup)
                    return False
                sess.ready = True
                _cleanup_paths(sess.cleanup_paths)
                sess.cleanup_paths.clear()
                proc.suppress_output()
                return True
            except FileNotFoundError:
                await self._abort_partial(profile, proc, cleanup)
                raise UserFacingError(
                    f"VPN: không tìm thấy binary cho '{cfg.kind}' — chạy 'yett doctor'"
                ) from None
            except (Cancelled, asyncio.CancelledError):
                await self._abort_partial(profile, proc, cleanup)
                raise
            except UserFacingError as exc:
                await self._abort_partial(profile, proc, cleanup)
                raise UserFacingError(_redact_credentials(str(exc), creds)) from None
            except BaseException as exc:
                await self._abort_partial(profile, proc, cleanup)
                if not isinstance(exc, Exception):
                    raise
                # Never include child output, argv, config, or credentials in this error.
                raise UserFacingError(f"VPN: lỗi kết nối profile '{profile}'") from None
        finally:
            lock.release()

    async def disconnect(self, profile: str) -> bool:
        self._require_profile(profile)
        lock = await self._acquire(profile)
        try:
            sess = self._sessions.get(profile)
            if sess is None:
                raise UserFacingError(
                    f"VPN: không sở hữu phiên '{profile}' — chỉ ngắt được VPN do yett bật"
                )
            return await self._drop_session(profile, kill=True)
        finally:
            lock.release()

    async def _acquire(
        self, profile: str, *, cancel: CancelToken | None = None
    ) -> threading.Lock:
        lock = self._locks[profile]
        while not lock.acquire(blocking=False):
            if cancel is not None:
                cancel.check()
            if self._closed.is_set():
                raise UserFacingError("VPN: runner đã đóng")
            await asyncio.sleep(0.02)
        return lock

    async def _drop_session(self, profile: str, *, kill: bool) -> bool:
        sess = self._sessions.pop(profile, None)
        if sess is None:
            return False
        try:
            if kill and sess.process.returncode() is None:
                sess.process.terminate_group()
                try:
                    await sess.process.wait(timeout=_DISCONNECT_WAIT_SEC)
                except (asyncio.TimeoutError, TimeoutError):
                    sess.process.kill_group()
                    try:
                        await sess.process.wait(timeout=2.0)
                    except Exception:
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
        if proc is not None:
            if proc.returncode() is None:
                proc.kill_group()
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
        deadline = asyncio.get_running_loop().time() + float(cfg.connect_timeout_sec)
        marker_seen_at: float | None = None
        while True:
            if cancel is not None:
                cancel.check()
            if self._closed.is_set():
                return False
            rc = proc.returncode()
            if rc is not None:
                return False
            with proc._output_lock:
                blob = bytes(proc._stdout_buf) + bytes(proc._stderr_buf)
            if marker and marker in blob:
                if marker_seen_at is None:
                    marker_seen_at = asyncio.get_running_loop().time()
                elif asyncio.get_running_loop().time() - marker_seen_at >= _READINESS_STABLE_SEC:
                    return True
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def close(self) -> None:
        """Best-effort synchronous shutdown for App.close and hot reload."""
        self._closed.set()
        for profile, lock in self._locks.items():
            if not lock.acquire(timeout=_DISCONNECT_WAIT_SEC + 2.0):
                continue
            try:
                sess = self._sessions.pop(profile, None)
                if sess is None:
                    continue
                sess.process.suppress_output()
                if sess.process.returncode() is None:
                    sess.process.terminate_group()
                    deadline = time.monotonic() + _DISCONNECT_WAIT_SEC
                    while sess.process.returncode() is None and time.monotonic() < deadline:
                        time.sleep(0.02)
                    if sess.process.returncode() is None:
                        sess.process.kill_group()
                        kill_deadline = time.monotonic() + 2.0
                        while (
                            sess.process.returncode() is None
                            and time.monotonic() < kill_deadline
                        ):
                            time.sleep(0.02)
                for thread in sess.process._reader_threads:
                    thread.join(timeout=0.2)
                sess.process._reader_threads.clear()
                sess.process._close_pidfd()
                _cleanup_paths(sess.cleanup_paths)
            finally:
                lock.release()

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
        user = _safe_credential(user, "username")
        password = _safe_credential(password, "password")
        # Config tạm chứa secret — 0600, cleanup bắt buộc. Không đưa password lên argv.
        body = (
            f"host = {cfg.host}\n"
            f"port = {int(cfg.port)}\n"
            f"username = {user}\n"
            f"password = {password}\n"
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
        config_data, config_dir = _read_safe_openvpn_config(cfg.config_file)
        config_copy = _write_secret_temp(config_data, suffix=".ovpn")
        cleanup.append(config_copy)
        argv = [
            binary,
            "--cd",
            str(config_dir),
            "--config",
            str(config_copy),
            "--verb",
            "1",
        ]
        password = _safe_credential(creds.get("password", ""), "password")
        user = _safe_credential(creds.get("username", ""), "username")
        if cfg.cred_secret or password or user:
            # --auth-user-pass <file> với username\npassword; không đưa secret lên argv.
            auth = f"{user}\n{password}\n".encode("utf-8")
            auth_path = _write_secret_temp(auth, suffix=".auth")
            cleanup.append(auth_path)
            argv.extend(["--auth-user-pass", str(auth_path)])
        return argv, None, cleanup


def _safe_credential(value: str, field_name: str) -> str:
    if not isinstance(value, str) or any(c in value for c in ("\x00", "\r", "\n")):
        raise UserFacingError(f"VPN: {field_name} chứa ký tự điều khiển không hợp lệ")
    return value


def _redact_credentials(message: str, creds: Mapping[str, str]) -> str:
    for value in creds.values():
        if isinstance(value, str) and value:
            message = message.replace(value, "[REDACTED]")
    return redact(message)


def _scrubbed_env() -> dict[str, str]:
    """Env tối thiểu cho VPN child — không kế thừa secret từ process cha nếu có thể."""
    keep = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
    out = {
        "PATH": (
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:"
            "/opt/homebrew/sbin:/opt/homebrew/bin"
        )
    }
    for k in keep:
        if k in os.environ:
            out[k] = os.environ[k]
    return out


def _write_secret_temp(data: bytes, *, suffix: str) -> Path:
    """Tạo file tạm owner-only (0600), O_EXCL|O_NOFOLLOW, trong dir 0700."""
    tmpdir = tempfile.mkdtemp(prefix="yett-vpn-")
    path = Path(tmpdir) / f"cred{suffix}"
    try:
        os.chmod(tmpdir, 0o700)
        if stat.S_IMODE(os.stat(tmpdir).st_mode) != 0o700:
            raise OSError("temp directory permissions")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(str(path), flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("cred temp không phải regular file")
            fchmod = getattr(os, "fchmod", None)
            if not callable(fchmod):
                raise OSError("owner-only temp files require POSIX")
            fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
        finally:
            os.close(fd)
        st = os.lstat(path)
        if (
            stat.S_ISLNK(st.st_mode)
            or not stat.S_ISREG(st.st_mode)
            or stat.S_IMODE(st.st_mode) != 0o600
        ):
            raise OSError("unsafe temp file")
        return path
    except BaseException:
        _cleanup_paths([path, Path(tmpdir)])
        raise


def _open_config_nofollow(config_file: str) -> str:
    """Validate an OpenVPN profile path and contents without following symlinks."""
    _read_safe_openvpn_config(config_file)
    return config_file


def _read_safe_openvpn_config(config_file: str) -> tuple[bytes, Path]:
    p = Path(config_file)
    if not p.is_absolute() or ".." in p.parts or p.suffix.lower() != ".ovpn":
        raise UserFacingError("VPN: config_file không hợp lệ")
    try:
        resolved = p.resolve(strict=True)
    except OSError:
        raise UserFacingError("VPN: không tìm thấy config_file") from None
    if resolved != p:
        raise UserFacingError("VPN: từ chối config_file hoặc thư mục cha là symlink")
    expected = resolved.stat()
    parent_stat = resolved.parent.stat()
    getuid = getattr(os, "getuid", None)
    current_uid = int(getuid()) if callable(getuid) else 0
    if expected.st_uid not in {current_uid, 0}:
        raise UserFacingError("VPN: config_file không thuộc current user/root")
    if parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise UserFacingError("VPN: thư mục chứa config_file cho phép user khác ghi")
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
        if (st.st_dev, st.st_ino) != (expected.st_dev, expected.st_ino):
            raise UserFacingError("VPN: config_file thay đổi trong lúc kiểm tra — từ chối")
        if st.st_size > _MAX_CONFIG_BYTES:
            raise UserFacingError("VPN: config_file vượt giới hạn 1 MiB")
        if st.st_mode & stat.S_IWOTH:
            raise UserFacingError("VPN: config_file cho phép user khác ghi — từ chối")
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _MAX_CONFIG_BYTES:
            raise UserFacingError("VPN: config_file vượt giới hạn 1 MiB")
    finally:
        os.close(fd)
    _validate_openvpn_directives(data)
    return data, p.parent


def _validate_openvpn_directives(data: bytes) -> None:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserFacingError("VPN: config_file phải là UTF-8 hợp lệ") from None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";", "<")):
            continue
        try:
            tokens = shlex.split(line, comments=True, posix=True)
        except ValueError:
            raise UserFacingError("VPN: config_file có cú pháp quote/escape không hợp lệ") from None
        if not tokens:
            continue
        directive = tokens[0].lstrip("-").split("=", 1)[0].lower()
        if directive in _UNSAFE_OPENVPN_DIRECTIVES or directive.startswith("management-"):
            raise UserFacingError(
                f"VPN: config_file chứa directive bị cấm '{directive}'"
            )


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
        # Never trust the cache: a child may have exited since the previous successful check.
        if await self._runner.status(profile):
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

    def close(self) -> None:
        self._connected.clear()
        self._runner.close()

    def _creds(self, profile: str) -> dict[str, str]:
        cfg = self._profiles[profile]
        out: dict[str, str] = {}
        secret_name = ""
        try:
            if cfg.username:
                out["username"] = cfg.username
            elif cfg.username_secret:
                secret_name = cfg.username_secret
                out["username"] = str(self._secrets.get(cfg.username_secret))
            if cfg.cred_secret:
                secret_name = cfg.cred_secret
                out["password"] = str(self._secrets.get(cfg.cred_secret))
            return out
        except SecretNotFound as exc:
            out.clear()
            raise _safe_secret_name_error(secret_name) from exc
        except BaseException:
            out.clear()
            raise


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
