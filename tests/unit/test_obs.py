"""Test tracer/spanstore + cost ledger (RG1-5)."""

from __future__ import annotations

from yett.obs import cost
from yett.obs.spanstore import SpanStore
from yett.obs.tracer import SpanKind
from yett.config.models import PriceRow
from yett.provider.base import Usage
from yett.security.filters import redact_attrs


def test_span_roundtrip_and_tree(tmp_path) -> None:
    store = SpanStore(tmp_path / "traces.db", redactor=redact_attrs)
    root = store.start_span(SpanKind.AGENT, "turn", start_ts=1.0, session_key="s1")
    store.end_span(root, end_ts=2.0)
    child = store.start_span(
        SpanKind.LLM_CALL, "think", start_ts=1.1, trace_id=root.trace_id, parent_id=root.id
    )
    store.end_span(child, end_ts=1.5, cost_usd=0.02, provider="fake", model="m", input_tokens=10, output_tokens=5)
    store.flush()

    spans = store.get_trace(root.trace_id)
    assert len(spans) == 2
    kinds = {s["kind"] for s in spans}
    assert kinds == {"AGENT", "LLM_CALL"}
    # cây: child.parent_id = root.id
    child_row = [s for s in spans if s["kind"] == "LLM_CALL"][0]
    assert child_row["parent_id"] == root.id
    store.close()


def test_cost_from_pricing() -> None:
    pricing = {"fake": {"m": PriceRow(input_per_mtok=3.0, output_per_mtok=15.0)}}
    u = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost.compute_cost("fake", "m", u, pricing) == 18.0


def test_cost_unknown_provider_is_zero() -> None:
    assert cost.compute_cost("nope", "m", Usage(100, 100), {}) == 0.0


def test_aggregate_by_provider() -> None:
    spans = [
        {"attrs": {"cost_usd": 0.02, "provider": "fake", "input_tokens": 10, "output_tokens": 5}, "start_ts": 1.0},
        {"attrs": {"cost_usd": 0.03, "provider": "fake", "input_tokens": 20, "output_tokens": 8}, "start_ts": 2.0},
        {"attrs": {"note": "no cost"}, "start_ts": 3.0},  # bỏ qua span không có cost
    ]
    agg = cost.aggregate(spans, by="provider")
    assert agg["fake"]["cost_usd"] == 0.05
    assert agg["fake"]["calls"] == 2
    assert agg["fake"]["input_tokens"] == 30
