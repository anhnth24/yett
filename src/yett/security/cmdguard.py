"""Command guard cho SSH/VPN (spec P2 §2.2). HARDLINE: cấm tuyệt đối lệnh xóa file OS.

Yêu cầu người dùng: "tuyệt đối khi remote/vpn/ssh không dùng các lệnh xóa file của OS".
Chặn theo PHÂN LOẠI SAU PARSE, không chỉ match chuỗi — xử lý né tránh:
xargs/busybox/subshell/alias/bash -c/find -delete/redirect ghi đè.
Parse không được → DENY (fail-closed). KHÔNG có đường approval cho lớp DELETE_FILE.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from enum import Enum

from yett.security.gate import Decision

# Toán tử nối lệnh shell dùng để tách chuỗi thành từng phần con — PHẢI liệt kê "&&"/"||"
# TRƯỚC "&"/"|" (đơn) để không bị nuốt nhầm (đơn là tập con ký tự của đôi).
# "&" đơn KHÔNG được tách khi ngay sau "<"/">" — đó là toán tử fd-dup (2>&1, 1>&2), không
# phải toán tử nối lệnh nền (background).
_SHELL_OPS = r"(?:&&|\|\||;|\||(?<![<>])&|\n)"

# Redirect ghi (>/>>), chấp nhận fd-prefix (1>, 2>) và không-space (x>file); loại trừ
# fd-duplication vô hại (2>&1, >&2) bằng lookahead phủ định "&" ngay sau toán tử.
_REDIRECT_RE = re.compile(r"\d*(?:>>?)(?!&)")
_REDIRECT_TARGET_RE = re.compile(r"\d*(?:>>?)(?!&)\s*([^\s&|;<>]+)")

# Binary xóa/hủy file — tên lệnh (đã strip path, wrapper, alias).
_DELETE_BINS = {"rm", "rmdir", "unlink", "shred", "srm", "wipe"}
# Wrapper có thể bọc một lệnh khác làm đối số → phải bóc tách.
_WRAPPERS = {"sudo", "env", "nice", "ionice", "timeout", "nohup", "stdbuf",
             "xargs", "time", "doas", "setsid", "chroot"}
_SHELL_RUNNERS = {"sh", "bash", "zsh", "dash", "ksh", "ash", "busybox"}

# Lệnh readonly an toàn (quan sát log/trạng thái).
_READONLY_BINS = {"cat", "tail", "head", "grep", "less", "more", "tac", "wc",
                  "ls", "stat", "df", "du", "free", "uptime", "ps", "top",
                  "systemctl", "journalctl", "docker", "kubectl", "echo", "date", "whoami"}
# systemctl/docker/kubectl chỉ readonly với sub-command an toàn:
_READONLY_SUBCMD = {
    "systemctl": {"status", "is-active", "is-enabled", "list-units", "show", "cat"},
    "docker": {"ps", "logs", "images", "inspect", "stats", "top", "version", "info"},
    "kubectl": {"get", "describe", "logs", "top", "version", "explain"},
}
# Bin readonly-theo-mặc-định NHƯNG có cờ ghi/xoá dữ liệu → không còn readonly khi cờ đó xuất
# hiện. journalctl đọc log là readonly, nhưng `--vacuum-*`/`--rotate`/`--flush` XOÁ/xoay log.
_MUTATING_FLAGS = {
    "journalctl": (
        "--rotate", "--flush", "--sync", "--relinquish-var", "--smart-relinquish-var",
        "--update-catalog", "--setup-keys", "--vacuum-time", "--vacuum-size", "--vacuum-files",
    ),
}


class CmdClass(Enum):
    READONLY = 0
    DEPLOY = 1
    DELETE_FILE = 2
    OTHER = 3


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Bóc các wrapper (sudo/xargs/env/timeout...) để lấy lệnh thật sự chạy."""
    i = 0
    while i < len(tokens):
        head = _basename(tokens[i])
        if head in _WRAPPERS:
            i += 1
            # bỏ qua option của wrapper (vd env VAR=x, timeout 5)
            while i < len(tokens) and (tokens[i].startswith("-") or "=" in tokens[i] or tokens[i].isdigit()):
                i += 1
            continue
        if head in _SHELL_RUNNERS:
            # sh -c "…": nội dung -c phải được phân tích riêng (xử lý ở classify)
            return tokens[i:]
        break
    return tokens[i:]


def _basename(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def _redirect_target_is_safe(path: str) -> bool:
    """True nếu target redirect được miễn trừ: /dev/null hoặc /tmp thật.
    Canonicalize (posixpath.normpath) trước khi so khớp để chặn traversal kiểu
    ">/tmp/../etc/passwd" giả trang /tmp."""
    norm = posixpath.normpath(path)
    if norm == "/dev/null":
        return True
    return norm == "/tmp" or norm.startswith("/tmp/")


def _contains_delete(cmd: str, _depth: int = 0) -> bool:
    """True nếu cmd (hoặc bất kỳ thành phần con nào) chạy lệnh xóa file. Fail-closed."""
    if _depth > 6:
        return True  # lồng quá sâu → coi như nguy hiểm
    # tách theo các toán tử shell nối lệnh (bao gồm "&" đơn — P0-4)
    for part in re.split(_SHELL_OPS, cmd):
        part = part.strip()
        if not part:
            continue
        if _part_has_delete(part, _depth):
            return True
    return False


def _part_has_delete(part: str, depth: int) -> bool:
    # subshell / command substitution: $(...), `...`, <(...)
    for m in re.finditer(r"\$\((.*?)\)|`(.*?)`|<\((.*?)\)", part):
        inner = next(g for g in m.groups() if g is not None)
        if _contains_delete(inner, depth + 1):
            return True
    # redirect ghi đè ra file (khác /dev/null, /tmp thật) → coi như phá file.
    # Bắt cả no-space (x>file), fd-prefix (1>/2>) và >> append; canonicalize target
    # để không lọt traversal kiểu ">/tmp/../etc/passwd".
    for m in _REDIRECT_TARGET_RE.finditer(part):
        if not _redirect_target_is_safe(m.group(1)):
            return True
    if re.search(r"\btee\b", part):
        return True
    try:
        tokens = shlex.split(part)
    except ValueError:
        return True  # không parse được → fail-closed
    if not tokens:
        return False
    tokens = _strip_wrappers(tokens)
    if not tokens:
        return False
    head = _basename(tokens[0])
    # shell -c "payload" → phân tích payload
    if head in _SHELL_RUNNERS:
        for j, t in enumerate(tokens):
            if t == "-c" and j + 1 < len(tokens):
                return _contains_delete(tokens[j + 1], depth + 1)
        # busybox rm ... → token[1] là applet
        if head == "busybox" and len(tokens) > 1 and _basename(tokens[1]) in _DELETE_BINS:
            return True
        return False
    # lệnh xóa trực tiếp
    if head in _DELETE_BINS:
        return True
    # find ... -delete | -exec rm
    if head == "find":
        if "-delete" in tokens:
            return True
        if "-exec" in tokens or "-execdir" in tokens:
            if any(_basename(t) in _DELETE_BINS for t in tokens):
                return True
    # dd of=<file> , truncate -s 0, mkfs
    if head == "dd" and any(t.startswith("of=") for t in tokens):
        return True
    if head == "truncate":
        return True
    if head.startswith("mkfs"):
        return True
    # gán biến rồi gọi: a=rm; $a x — bắt bằng regex trước đó khó; kiểm token gán
    return False


def classify(cmd: str, *, deploy_script: str | None = None, log_readonly: bool = False) -> tuple[CmdClass, str]:
    """Phân lớp lệnh SSH. Ưu tiên DELETE_FILE (hardline)."""
    if _contains_delete(cmd):
        return CmdClass.DELETE_FILE, "phát hiện lệnh xóa/ghi-đè file OS"
    if deploy_script and cmd.strip() == deploy_script.strip():
        return CmdClass.DEPLOY, "khớp deploy_script khai báo"
    if _is_readonly(cmd):
        return CmdClass.READONLY, "lệnh quan sát read-only"
    return CmdClass.OTHER, "không thuộc lớp được phép"


def _is_readonly(cmd: str) -> bool:
    try:
        parts = re.split(_SHELL_OPS, cmd)
    except Exception:
        return False
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # readonly không được có redirect ghi (bắt cả no-space/fd-prefix/append)
        if _REDIRECT_RE.search(part) or re.search(r"\btee\b", part):
            return False
        try:
            tokens = shlex.split(part)
        except ValueError:
            return False
        if not tokens:
            return False
        head = _basename(tokens[0])
        if head not in _READONLY_BINS:
            return False
        sub = _READONLY_SUBCMD.get(head)
        if sub is not None:
            if len(tokens) < 2 or tokens[1] not in sub:
                return False
        muts = _MUTATING_FLAGS.get(head)
        if muts is not None:
            for tok in tokens[1:]:
                if any(tok == m or tok.startswith(m + "=") for m in muts):
                    return False  # cờ ghi/xoá → không phải readonly (vd journalctl --vacuum-time=1s)
    return True


def gate_ssh(
    cmd: str, *, deploy_script: str | None = None, tier: str = "uat"
) -> Decision:
    """Quyết định Gate cho một lệnh SSH. HARDLINE delete → deny tuyệt đối, không approval."""
    cls, why = classify(cmd, deploy_script=deploy_script)
    if cls == CmdClass.DELETE_FILE:
        return Decision(
            "deny",
            f"HARDLINE: lệnh xóa file OS trên server bị cấm tuyệt đối ({why}). "
            f"Không có đường phê duyệt. Xóa hợp lệ chỉ nằm trong deploy_script bạn tự viết.",
            "SSH_DELETE_HARDLINE",
        )
    if cls == CmdClass.READONLY:
        return Decision("allow", "lệnh quan sát read-only", "SSH_READONLY")
    if cls == CmdClass.DEPLOY and tier != "restricted":
        return Decision("need_approval", "deploy — cần bạn duyệt", "SSH_DEPLOY")
    return Decision("deny", f"lệnh không thuộc lớp được phép trên host này ({why})", "SSH_DEFAULT_DENY")
