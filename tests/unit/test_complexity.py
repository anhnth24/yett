"""Test complexity router — phân loại độ khó → step budget (tiết kiệm chi phí)."""

from __future__ import annotations

from yett.config.models import RouterCfg
from yett.core.complexity import ComplexityRouter


def _r() -> ComplexityRouter:
    return ComplexityRouter(RouterCfg())


def test_trivial_greeting() -> None:
    r = _r().classify("chào")
    assert r.tier == "trivial"
    assert r.max_iterations == 2


def test_simple_question() -> None:
    r = _r().classify("hàm này làm gì?")
    assert r.tier == "simple"
    assert r.max_iterations == 4


def test_moderate_keyword() -> None:
    r = _r().classify("kiểm tra vì sao đơn 123 chưa thanh toán")
    assert r.tier == "moderate"


def test_complex_multistep() -> None:
    r = _r().classify("phân tích lỗi trên server rồi sửa và deploy lại UAT")
    assert r.tier == "complex"
    assert r.max_iterations == 20


def test_complex_with_code_block() -> None:
    r = _r().classify("sửa đoạn này:\n```go\nfunc x(){}\n```")
    assert r.tier == "complex"


def test_capped_by_config() -> None:
    r = ComplexityRouter(RouterCfg(complex_steps=99)).classify(
        "phân tích và sửa và deploy toàn bộ"
    )
    assert r.max_iterations == 99  # router trả về; loop mới chặn theo budget
