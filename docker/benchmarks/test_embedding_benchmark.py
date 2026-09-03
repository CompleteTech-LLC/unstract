"""Tests for CPU/GPU comparison reporting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from embedding_benchmark import compare_modes  # noqa: E402


def result(
    model: str,
    mode: str,
    single_p50: float,
    throughput: float,
    mrr: float,
    ndcg_at_5: float,
    status: str = "ok",
) -> dict[str, object]:
    """Build the subset of a benchmark result used by comparisons."""
    return {
        "model": model,
        "mode": mode,
        "service": f"{model}-{mode}",
        "status": status,
        "single_latency_ms": {"p50": single_p50},
        "batch_throughput_items_per_second": throughput,
        "quality": {"mrr": mrr, "ndcg_at_5": ndcg_at_5},
    }


def test_compare_modes_reports_speedup_and_quality_delta() -> None:
    """GPU speedups and quality deltas are calculated in the expected direction."""
    comparisons = compare_modes(
        [
            result("qwen3-embedding-06b", "cpu", 300, 2, 0.70, 0.75),
            result("qwen3-embedding-06b", "gpu", 30, 20, 0.80, 0.85),
        ]
    )

    assert comparisons == [
        {
            "model": "qwen3-embedding-06b",
            "cpu_service": "qwen3-embedding-06b-cpu",
            "gpu_service": "qwen3-embedding-06b-gpu",
            "single_latency_speedup": 10.0,
            "batch_throughput_speedup": 10.0,
            "mrr_delta": 0.1,
            "ndcg_at_5_delta": 0.1,
        }
    ]


def test_compare_modes_skips_incomplete_or_failed_pairs() -> None:
    """A failed or one-sided pair is not presented as a comparison."""
    comparisons = compare_modes(
        [
            result("failed", "cpu", 300, 2, 0.70, 0.75, status="error"),
            result("failed", "gpu", 30, 20, 0.80, 0.85),
            result("incomplete", "cpu", 300, 2, 0.70, 0.75),
        ]
    )

    assert comparisons == []
