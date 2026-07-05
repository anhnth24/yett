"""Test Policy Gate v0.1: denylist hardline, allowlist, default-deny (RG1-2, RG1-3)."""

from __future__ import annotations

from yett.config.models import SecurityCfg, ToolRule
from yett.security.basic_gate import BasicGate
from yett.security.gate import safe_evaluate


class _Ctx:
    session_key = "t"


def _gate(rules=None) -> BasicGate:
    return BasicGate(SecurityCfg(allowlist=rules or []))


def test_default_deny() -> None:
    dec = safe_evaluate(_gate(), "exec", {"cmd": "ls"}, _Ctx())
    assert dec.verdict == "deny"
    assert dec.rule_id == "DEFAULT_DENY"


def test_allowlist_allows() -> None:
    # [allowlist-anchor] pattern giờ phải khớp TOÀN BỘ cmd (fullmatch) — "(\s.*)?$" giữ ý định
    # gốc "ls hoặc ls kèm tham số" thay vì prefix ngầm định qua search().
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": r"^ls(\s.*)?$"}, effect="allow")]
    dec = safe_evaluate(_gate(rules), "exec", {"cmd": "ls -la"}, _Ctx())
    assert dec.verdict == "allow"


def test_allowlist_anchor_rejects_appended_command() -> None:
    # [P2/allowlist-anchor] Pattern KHÔNG tự anchor (rule tác giả không viết ^/$) trước đây
    # khớp nhầm qua re.search vì "ls" là substring hợp lệ của "ls; whoami". fullmatch đóng lỗ
    # này: cmd phải khớp CHÍNH XÁC "ls" mới allow, có gắn thêm gì cũng rơi về default-deny.
    # Dùng "whoami" (không phải "rm") để tách riêng lỗ anchor khỏi hardline denylist (đã có
    # test riêng ở test_hardline_overrides_allowlist) — muốn chứng minh CHÍNH allowlist chặn,
    # không phải hardline chặn hộ.
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": "ls"}, effect="allow")]
    dec = safe_evaluate(_gate(rules), "exec", {"cmd": "ls; whoami"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "DEFAULT_DENY"
    ok = safe_evaluate(_gate(rules), "exec", {"cmd": "ls"}, _Ctx())
    assert ok.verdict == "allow"


def test_allowlist_path_normalized_before_match() -> None:
    # [P2/allowlist path-normalize] Anchor một mình không đủ: "/workspace/../etc/passwd" VẪN
    # literally bắt đầu bằng "/workspace/" nên fullmatch "^/workspace/.*$" vẫn "khớp" nếu
    # không chuẩn hóa trước — path traversal lọt qua rule tưởng đã giới hạn đúng thư mục.
    rules = [ToolRule(tool="read_file", arg_patterns={"path": r"^/workspace/.*$"}, effect="allow")]
    dec = safe_evaluate(_gate(rules), "read_file", {"path": "/workspace/../etc/passwd"}, _Ctx())
    assert dec.verdict == "deny" and dec.rule_id == "DEFAULT_DENY"
    ok = safe_evaluate(_gate(rules), "read_file", {"path": "/workspace/notes.txt"}, _Ctx())
    assert ok.verdict == "allow"


def test_hardline_overrides_allowlist() -> None:
    # Dù allowlist cho phép mọi cmd, rm vẫn bị hardline chặn.
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": r".*"}, effect="allow")]
    dec = safe_evaluate(_gate(rules), "exec", {"cmd": "rm -rf /data"}, _Ctx())
    assert dec.verdict == "deny"
    assert dec.rule_id == "DENY_EXEC"


def test_hardline_delete_variants() -> None:
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": r".*"}, effect="allow")]
    g = _gate(rules)
    for cmd in ["rm x", "shred f", "dd of=/dev/sda", "mkfs.ext4 /dev/sdb", ":(){ :|:& };:"]:
        assert safe_evaluate(g, "exec", {"cmd": cmd}, _Ctx()).verdict == "deny", cmd


def test_hardline_sensitive_path() -> None:
    dec = safe_evaluate(_gate(), "read_file", {"path": "/etc/shadow"}, _Ctx())
    assert dec.verdict == "deny"
    assert dec.rule_id == "DENY_PATH"


def test_approval_effect() -> None:
    rules = [ToolRule(tool="deploy", arg_patterns={}, effect="need_approval")]
    dec = safe_evaluate(_gate(rules), "deploy", {}, _Ctx())
    assert dec.verdict == "need_approval"


def test_hardline_windows_delete_commands() -> None:
    # Lệnh xóa của Windows cũng bị chặn (cho chạy native Windows).
    rules = [ToolRule(tool="exec", arg_patterns={"cmd": r".*"}, effect="allow")]
    g = _gate(rules)
    for cmd in ["del C:\\data\\x.txt", "rd /s /q C:\\data", "rmdir /s C:\\logs",
                "Remove-Item -Recurse C:\\data", "format C:", "Clear-Content x.log",
                "diskpart", "erase file.txt"]:
        assert safe_evaluate(g, "exec", {"cmd": cmd}, _Ctx()).verdict == "deny", cmd


def test_hardline_windows_sensitive_path() -> None:
    dec = safe_evaluate(_gate(), "read_file", {"path": r"C:\Windows\System32\config\SAM"}, _Ctx())
    assert dec.verdict == "deny"
