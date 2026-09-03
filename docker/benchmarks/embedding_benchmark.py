#!/usr/bin/env python3
"""Benchmark local OpenAI-compatible Qwen3 embedding endpoints."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "local-embeddings.config.json"
DEFAULT_DATASET = Path(__file__).resolve().parent / "qwen3_smoke_dataset.json"
DEFAULT_TIMEOUT = 300.0
DEFAULT_HEALTH_TIMEOUT = 600.0
MODES = ("cpu", "gpu")
QUALITY_K_VALUES = (1, 3, 5)


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object from disk."""
    with path.open(encoding="utf-8") as file:
        value = json.load(file)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def percentile(values: list[float], quantile: float) -> float:
    """Return a nearest-rank percentile in milliseconds."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


def request_json(
    url: str,
    payload: dict[str, Any] | None,
    timeout: float,
) -> dict[str, Any]:
    """Send a JSON request and return its JSON object response."""
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310
            value = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"{error.code} from {url}: {detail}") from error
    except URLError as error:
        raise RuntimeError(f"request to {url} failed: {error.reason}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{url} returned a non-object JSON response")
    return value


def service_root(base_url: str) -> str:
    """Convert an OpenAI base URL to the service root."""
    root = base_url.rstrip("/")
    if root.endswith("/v1"):
        return root[:-3].rstrip("/")
    return root


def wait_for_health(base_url: str, timeout: float) -> None:
    """Wait until the embedding service reports healthy."""
    health_url = f"{service_root(base_url)}/health"
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            with urlopen(  # noqa: S310
                Request(health_url, method="GET"), timeout=10.0
            ) as response:
                if response.status == 200:
                    return
                last_error = f"HTTP {response.status}"
        except HTTPError as error:
            last_error = f"HTTP {error.code}"
        except (OSError, URLError) as error:
            last_error = str(error)
        time.sleep(5.0)
    raise TimeoutError(f"{health_url} did not become healthy: {last_error}")


def embed(
    base_url: str,
    model_id: str,
    texts: list[str],
    timeout: float,
) -> list[list[float]]:
    """Generate embeddings through the OpenAI-compatible endpoint."""
    response = request_json(
        f"{base_url.rstrip('/')}/embeddings",
        {
            "model": model_id,
            "input": texts,
            "encoding_format": "float",
        },
        timeout,
    )
    data = response.get("data")
    if not isinstance(data, list) or len(data) != len(texts):
        raise ValueError(
            f"expected {len(texts)} embeddings from {base_url}, got {data!r}"
        )
    vectors: list[list[float]] = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise ValueError(f"invalid embedding response from {base_url}")
        vectors.append(item["embedding"])
    return vectors


def embed_in_batches(
    base_url: str,
    model_id: str,
    texts: list[str],
    batch_size: int,
    timeout: float,
) -> list[list[float]]:
    """Generate embeddings in bounded requests for backends with batch caps."""
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        vectors.extend(
            embed(base_url, model_id, texts[start : start + batch_size], timeout)
        )
    return vectors


def prefixed(text: str, prefix: str) -> str:
    """Apply a configured query/document prefix."""
    return f"{prefix}{text}" if prefix else text


def cosine(left: list[float], right: list[float]) -> float:
    """Calculate cosine similarity without third-party dependencies."""
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right, strict=True)) / (left_norm * right_norm)


def quality_metrics(
    query_vectors: list[list[float]],
    document_vectors: list[list[float]],
    queries: list[dict[str, Any]],
    documents: list[dict[str, Any]],
) -> dict[str, float]:
    """Calculate retrieval metrics for the supplied labeled fixture."""
    document_ids = [str(document["id"]) for document in documents]
    reciprocal_ranks: list[float] = []
    recalls = {k: [] for k in QUALITY_K_VALUES}
    ndcgs = {k: [] for k in QUALITY_K_VALUES}

    for query_vector, query in zip(query_vectors, queries, strict=True):
        relevant = {str(item) for item in query["relevant"]}
        ranked = sorted(
            (
                (cosine(query_vector, document_vector), document_id)
                for document_id, document_vector in zip(
                    document_ids, document_vectors, strict=True
                )
            ),
            reverse=True,
        )
        ranked_ids = [document_id for _, document_id in ranked]
        positions = [
            index + 1
            for index, document_id in enumerate(ranked_ids)
            if document_id in relevant
        ]
        reciprocal_ranks.append(1.0 / positions[0] if positions else 0.0)

        for k in QUALITY_K_VALUES:
            top_ids = ranked_ids[:k]
            recalls[k].append(len(set(top_ids) & relevant) / max(1, len(relevant)))
            dcg = sum(
                1.0 / math.log2(index + 2)
                for index, document_id in enumerate(top_ids)
                if document_id in relevant
            )
            ideal_hits = min(k, len(relevant))
            ideal_dcg = sum(1.0 / math.log2(index + 2) for index in range(ideal_hits))
            ndcgs[k].append(dcg / ideal_dcg if ideal_dcg else 0.0)

    result = {"mrr": statistics.mean(reciprocal_ranks)}
    result.update(
        {f"recall_at_{k}": statistics.mean(values) for k, values in recalls.items()}
    )
    result.update(
        {f"ndcg_at_{k}": statistics.mean(values) for k, values in ndcgs.items()}
    )
    return {key: round(value, 6) for key, value in result.items()}


def validate_vectors(vectors: list[list[float]], expected_dimension: int) -> None:
    """Ensure an endpoint returns the configured vector dimension."""
    actual_dimensions = {len(vector) for vector in vectors}
    if actual_dimensions != {expected_dimension}:
        raise ValueError(
            f"expected {expected_dimension} dimensions, got {sorted(actual_dimensions)}"
        )


def nested_float(result: dict[str, Any], *keys: str) -> float | None:
    """Read a numeric value from a nested benchmark result."""
    value: Any = result
    for key in keys:
        if not isinstance(value, dict) or key not in value:
            return None
        value = value[key]
    return float(value) if isinstance(value, (int, float)) else None


def rounded_ratio(numerator: float | None, denominator: float | None) -> float | None:
    """Return a rounded ratio, or ``None`` when the denominator is unavailable."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 3)


def compare_modes(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compare CPU and GPU results for each model in a completed matrix."""
    by_model: dict[str, dict[str, dict[str, Any]]] = {}
    for result in results:
        if result.get("status", "ok") != "ok":
            continue
        model = str(result["model"])
        mode = str(result["mode"])
        if mode in MODES:
            by_model.setdefault(model, {})[mode] = result

    comparisons: list[dict[str, Any]] = []
    for model in sorted(by_model):
        pair = by_model[model]
        if not all(mode in pair for mode in MODES):
            continue
        cpu = pair["cpu"]
        gpu = pair["gpu"]
        comparison: dict[str, Any] = {
            "model": model,
            "cpu_service": cpu["service"],
            "gpu_service": gpu["service"],
            "single_latency_speedup": rounded_ratio(
                nested_float(cpu, "single_latency_ms", "p50"),
                nested_float(gpu, "single_latency_ms", "p50"),
            ),
            "batch_throughput_speedup": rounded_ratio(
                nested_float(gpu, "batch_throughput_items_per_second"),
                nested_float(cpu, "batch_throughput_items_per_second"),
            ),
        }
        cpu_mrr = nested_float(cpu, "quality", "mrr")
        gpu_mrr = nested_float(gpu, "quality", "mrr")
        cpu_ndcg = nested_float(cpu, "quality", "ndcg_at_5")
        gpu_ndcg = nested_float(gpu, "quality", "ndcg_at_5")
        if cpu_mrr is not None and gpu_mrr is not None:
            comparison["mrr_delta"] = round(gpu_mrr - cpu_mrr, 6)
        if cpu_ndcg is not None and gpu_ndcg is not None:
            comparison["ndcg_at_5_delta"] = round(gpu_ndcg - cpu_ndcg, 6)
        comparisons.append(comparison)
    return comparisons


def print_comparisons(comparisons: list[dict[str, Any]]) -> None:
    """Print CPU/GPU comparison rows for a completed matrix."""
    if not comparisons:
        return
    print("\nCPU/GPU comparison")
    print(
        "model single_latency_speedup batch_throughput_speedup mrr_delta ndcg_at_5_delta"
    )
    for comparison in comparisons:
        print(
            f"{comparison['model']} "
            f"{comparison['single_latency_speedup']} "
            f"{comparison['batch_throughput_speedup']} "
            f"{comparison.get('mrr_delta', '-')} "
            f"{comparison.get('ndcg_at_5_delta', '-')}"
        )


def benchmark_endpoint(
    model: dict[str, Any],
    mode: str,
    config: dict[str, Any],
    dataset: dict[str, Any],
    repetitions: int,
    batch_size: int,
    timeout: float,
    health_timeout: float,
    skip_quality: bool,
) -> dict[str, Any]:
    """Benchmark one model/mode endpoint."""
    endpoint = model[mode]
    base_url = str(endpoint["base_url"])
    model_id = str(model["id"])
    expected_dimension = int(model["dimension"])
    documents = dataset["documents"]
    queries = dataset["queries"]
    query_prefix = str(config.get("query_prefix", ""))
    passage_prefix = str(config.get("passage_prefix", ""))
    query_text = prefixed(str(queries[0]["text"]), query_prefix)
    passage_texts = [
        prefixed(str(document["text"]), passage_prefix)
        for document in documents[:batch_size]
    ]
    if len(passage_texts) < batch_size:
        passage_texts *= math.ceil(batch_size / len(passage_texts))
        passage_texts = passage_texts[:batch_size]

    wait_for_health(base_url, health_timeout)
    for _ in range(2):
        validate_vectors(
            embed(base_url, model_id, [query_text], timeout), expected_dimension
        )

    single_latencies: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        validate_vectors(
            embed(base_url, model_id, [query_text], timeout), expected_dimension
        )
        single_latencies.append((time.perf_counter() - started) * 1000)

    batch_latencies: list[float] = []
    for _ in range(repetitions):
        started = time.perf_counter()
        vectors = embed(base_url, model_id, passage_texts, timeout)
        validate_vectors(vectors, expected_dimension)
        batch_latencies.append((time.perf_counter() - started) * 1000)

    result: dict[str, Any] = {
        "model": model_id,
        "repository": model["repository"],
        "revision": model.get("revision"),
        "mode": mode,
        "service": endpoint["service"],
        "base_url": base_url,
        "dimension": expected_dimension,
        "single_latency_ms": {
            "p50": round(percentile(single_latencies, 0.50), 3),
            "p95": round(percentile(single_latencies, 0.95), 3),
        },
        "batch_latency_ms": {
            "p50": round(percentile(batch_latencies, 0.50), 3),
            "p95": round(percentile(batch_latencies, 0.95), 3),
        },
        "batch_size": batch_size,
        "batch_throughput_items_per_second": round(
            batch_size / (statistics.mean(batch_latencies) / 1000), 3
        ),
    }

    if not skip_quality:
        document_vectors = embed_in_batches(
            base_url,
            model_id,
            [prefixed(str(document["text"]), passage_prefix) for document in documents],
            batch_size,
            timeout,
        )
        query_vectors = embed_in_batches(
            base_url,
            model_id,
            [prefixed(str(query["text"]), query_prefix) for query in queries],
            batch_size,
            timeout,
        )
        validate_vectors(document_vectors, expected_dimension)
        validate_vectors(query_vectors, expected_dimension)
        result["quality"] = quality_metrics(
            query_vectors, document_vectors, queries, documents
        )

    return result


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Benchmark the local Qwen3 CPU/GPU embedding matrix."
    )
    parser.add_argument(
        "--mode",
        choices=("cpu", "gpu", "both"),
        default="both",
        help="Endpoint variation to test; both tests CPU and GPU endpoints.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"Endpoint config JSON (default: {DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"Labeled benchmark fixture (default: {DEFAULT_DATASET})",
    )
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--health-timeout", type=float, default=DEFAULT_HEALTH_TIMEOUT)
    parser.add_argument("--skip-quality", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    """Run the selected benchmark matrix."""
    args = parse_args()
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be at least 1")
    if args.batch_size < 1:
        raise SystemExit("--batch-size must be at least 1")

    config = load_json(args.config)
    dataset = load_json(args.dataset)
    modes = MODES if args.mode == "both" else (args.mode,)
    results: list[dict[str, Any]] = []
    models = config.get("models")
    if not isinstance(models, list) or not models:
        raise SystemExit("config must define at least one model")

    for model in models:
        for mode in modes:
            print(f"Benchmarking {model['id']} ({mode})...", flush=True)
            try:
                results.append(
                    benchmark_endpoint(
                        model,
                        mode,
                        config,
                        dataset,
                        args.repetitions,
                        args.batch_size,
                        args.timeout,
                        args.health_timeout,
                        args.skip_quality,
                    )
                )
            except Exception as error:  # noqa: BLE001 - continue the matrix
                results.append(
                    {
                        "model": model["id"],
                        "repository": model["repository"],
                        "revision": model.get("revision"),
                        "mode": mode,
                        "service": model[mode]["service"],
                        "base_url": model[mode]["base_url"],
                        "status": "error",
                        "error": str(error),
                    }
                )
                print(f"  ERROR: {error}", file=sys.stderr)

    report = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "mode": args.mode,
        "config": str(args.config),
        "dataset": str(args.dataset),
        "settings": {
            "repetitions": args.repetitions,
            "batch_size": args.batch_size,
            "timeout_seconds": args.timeout,
            "health_timeout_seconds": args.health_timeout,
            "quality_fixture": not args.skip_quality,
        },
        "results": results,
    }
    comparisons = compare_modes(results) if args.mode == "both" else []
    report["comparisons"] = comparisons
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Report written to {args.output}")

    print("\nSummary")
    print("mode model status dimension single_p50_ms batch_items_per_second mrr")
    for result in results:
        quality = result.get("quality", {})
        print(
            f"{result['mode']} {result['model']} "
            f"{result.get('status', 'ok')} "
            f"{result.get('dimension', '-')} "
            f"{result.get('single_latency_ms', {}).get('p50', '-')} "
            f"{result.get('batch_throughput_items_per_second', '-')} "
            f"{quality.get('mrr', '-')}"
        )
    print_comparisons(comparisons)

    has_errors = any(result.get("status") == "error" for result in results)
    return 1 if args.strict and has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
