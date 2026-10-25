"""Tests for CPU/GPU comparison reporting."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import embedding_benchmark  # noqa: E402
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


def test_embed_preserves_input_order_for_shuffled_response(monkeypatch) -> None:
    """Use response indices to keep document vectors aligned with their texts."""
    monkeypatch.setattr(
        embedding_benchmark,
        "request_json",
        lambda *args: {
            "data": [
                {"index": 1, "embedding": [0.0, 1.0]},
                {"index": 0, "embedding": [1.0, 0.0]},
            ]
        },
    )

    vectors = embedding_benchmark.embed(
        "http://localhost/v1", "synthetic", ["first", "second"], 1.0
    )

    assert vectors == [[1.0, 0.0], [0.0, 1.0]]
    metrics = embedding_benchmark.quality_metrics(
        [[1.0, 0.0]],
        vectors,
        [{"text": "first", "relevant": ["first"]}],
        [{"id": "first"}, {"id": "second"}],
    )
    assert metrics["mrr"] == 1.0


@pytest.mark.parametrize(
    "indices",
    [[0, 0], [-1, 0], [0, 2], [0, None], [0, True], [0, "1"], [0, 1.0]],
)
def test_embed_rejects_invalid_response_indices(monkeypatch, indices) -> None:
    """Ambiguous, repeated or invalid indices cannot produce quality metrics."""
    monkeypatch.setattr(
        embedding_benchmark,
        "request_json",
        lambda *args: {
            "data": [{"index": index, "embedding": [1.0, 0.0]} for index in indices]
        },
    )

    with pytest.raises(ValueError, match="embedding index"):
        embedding_benchmark.embed(
            "http://localhost/v1", "synthetic", ["first", "second"], 1.0
        )


def test_embed_rejects_missing_response_index(monkeypatch) -> None:
    """An index is required by the OpenAI-compatible response contract."""
    monkeypatch.setattr(
        embedding_benchmark,
        "request_json",
        lambda *args: {"data": [{"embedding": [1.0, 0.0]}]},
    )

    with pytest.raises(ValueError, match="embedding index"):
        embedding_benchmark.embed("http://localhost/v1", "synthetic", ["first"], 1.0)


def test_embed_in_batches_uses_indices_relative_to_each_batch(monkeypatch) -> None:
    """Response indices restart at zero for each bounded embedding request."""

    def respond(url, payload, timeout):
        return {
            "data": [
                {"index": index, "embedding": [float(text)]}
                for index, text in reversed(list(enumerate(payload["input"])))
            ]
        }

    monkeypatch.setattr(embedding_benchmark, "request_json", respond)

    assert embedding_benchmark.embed_in_batches(
        "http://localhost/v1", "synthetic", ["1", "2", "3"], 2, 1.0
    ) == [[1.0], [2.0], [3.0]]
