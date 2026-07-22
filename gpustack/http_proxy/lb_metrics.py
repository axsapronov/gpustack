"""Prometheus metrics for the smart load balancer, pulled at scrape time."""

import bisect
import threading
from typing import Dict, Iterator, List, Optional, Tuple

from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector

from gpustack.utils.name import metric_name

# Prompt token buckets
_PROMPT_TOKEN_BUCKETS = [
    128,
    512,
    1024,
    4096,
    8192,
    16384,
    32768,
    65536,
    131072,
]
# Max tokens buckets
_MAX_TOKEN_BUCKETS = [
    32,
    128,
    256,
    512,
    1024,
    2048,
    4096,
    8192,
    16384,
]
# Total tokens buckets
_TOTAL_TOKEN_BUCKETS = [
    256,
    1024,
    4096,
    8192,
    16384,
    32768,
    65536,
    131072,
    262144,
]
# Selection latency buckets (seconds)
_LATENCY_BUCKETS = [
    0.001,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
]


class LBMetricsCollector(Collector):
    """Expose smart load balancer decisions and instance states as Prometheus metrics.

    Thread-safe: all record methods use a lock; collect() reads a snapshot.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

        # --- Counters ---
        # (model_id, model_name, instance_id, reason) -> count
        self._selections: Dict[Tuple[str, str, str, str], int] = {}
        # (model_id, model_name, request_class) -> count
        self._requests: Dict[Tuple[str, str, str], int] = {}
        # (model_id, model_name, instance_id) -> count
        self._streak_resets: Dict[Tuple[str, str, str], int] = {}

        # --- Gauges (latest snapshot) ---
        # (model_id, model_name, instance_id) -> value
        self._instance_score: Dict[Tuple[str, str, str], float] = {}
        self._instance_ewma_kv: Dict[Tuple[str, str, str], float] = {}
        self._instance_wlc_weight: Dict[Tuple[str, str, str], float] = {}
        self._instance_slow_start: Dict[Tuple[str, str, str], float] = {}
        self._instance_affinity_streak: Dict[Tuple[str, str, str], int] = {}
        self._instance_kv_cache: Dict[Tuple[str, str, str], float] = {}
        # (model_id, model_name) -> pool_size
        self._pool_size: Dict[Tuple[str, str], int] = {}

        # --- Histograms (bucketed samples) ---
        # (model_id, model_name, route) -> list of values
        self._prompt_tokens: Dict[Tuple[str, str, str], List[float]] = {}
        self._max_tokens: Dict[Tuple[str, str, str], List[float]] = {}
        self._total_tokens: Dict[Tuple[str, str, str], List[float]] = {}
        # (model_id, model_name) -> list of latencies
        self._selection_latency: Dict[Tuple[str, str], List[float]] = {}

    # ---- Recording methods ----

    def record_selection(
        self,
        model_id: str,
        model_name: str,
        instance_id: str,
        reason: str,
        request_class: str,
        score: float,
        prompt_tokens: int,
        max_tokens: int,
        latency: float,
        route: str = "",
    ) -> None:
        """Record a single load-balancer decision.

        Call once per select_instance() at the end of the pipeline.
        """
        with self._lock:
            key_sel = (model_id, model_name, instance_id, reason)
            self._selections[key_sel] = self._selections.get(key_sel, 0) + 1

            key_req = (model_id, model_name, request_class)
            self._requests[key_req] = self._requests.get(key_req, 0) + 1

            key_hist = (model_id, model_name, route)
            self._prompt_tokens.setdefault(key_hist, []).append(float(prompt_tokens))
            self._max_tokens.setdefault(key_hist, []).append(float(max_tokens))
            self._total_tokens.setdefault(key_hist, []).append(
                float(prompt_tokens + max_tokens)
            )

            key_lat = (model_id, model_name)
            self._selection_latency.setdefault(key_lat, []).append(latency)

    def record_instance_state(
        self,
        model_id: str,
        model_name: str,
        instance_id: str,
        score: float,
        ewma_kv: float,
        wlc_weight: float,
        slow_start_weight: float,
        affinity_streak: int,
        kv_cache_usage: float,
    ) -> None:
        """Record the current state of a single instance.

        Call once per instance after updating EWMA/WLC/slow_start states.
        """
        with self._lock:
            key = (model_id, model_name, instance_id)
            self._instance_score[key] = score
            self._instance_ewma_kv[key] = ewma_kv
            self._instance_wlc_weight[key] = wlc_weight
            self._instance_slow_start[key] = slow_start_weight
            self._instance_affinity_streak[key] = affinity_streak
            self._instance_kv_cache[key] = kv_cache_usage

    def record_pool_size(self, model_id: str, model_name: str, size: int) -> None:
        """Record the number of healthy replicas in the pool."""
        with self._lock:
            self._pool_size[(model_id, model_name)] = size

    def record_streak_reset(
        self, model_id: str, model_name: str, instance_id: str
    ) -> None:
        """Record a forced affinity streak reset."""
        with self._lock:
            key = (model_id, model_name, instance_id)
            self._streak_resets[key] = self._streak_resets.get(key, 0) + 1

    # ---- Prometheus collect() ----

    def collect(self) -> Iterator[Metric]:
        with self._lock:
            snapshot = self._snapshot()

        yield from self._yield_selections(snapshot)
        yield from self._yield_requests(snapshot)
        yield from self._yield_streak_resets(snapshot)
        yield from self._yield_instance_gauges(snapshot)
        yield from self._yield_pool_size(snapshot)
        yield from self._yield_prompt_tokens(snapshot)
        yield from self._yield_max_tokens(snapshot)
        yield from self._yield_total_tokens(snapshot)
        yield from self._yield_selection_latency(snapshot)

    def _snapshot(self) -> dict:
        """Return a copy of all internal state under the lock."""
        return {
            "selections": dict(self._selections),
            "requests": dict(self._requests),
            "streak_resets": dict(self._streak_resets),
            "instance_score": dict(self._instance_score),
            "instance_ewma_kv": dict(self._instance_ewma_kv),
            "instance_wlc_weight": dict(self._instance_wlc_weight),
            "instance_slow_start": dict(self._instance_slow_start),
            "instance_affinity_streak": dict(self._instance_affinity_streak),
            "instance_kv_cache": dict(self._instance_kv_cache),
            "pool_size": dict(self._pool_size),
            "prompt_tokens": dict(self._prompt_tokens),
            "max_tokens": dict(self._max_tokens),
            "total_tokens": dict(self._total_tokens),
            "selection_latency": dict(self._selection_latency),
        }

    # ---- Counter yielders ----

    def _yield_selections(self, snap: dict) -> Iterator[Metric]:
        fam = CounterMetricFamily(
            metric_name("lb_selections_total"),
            "Total number of instance selections by the load balancer.",
            labels=["model_id", "model_name", "instance_id", "reason"],
        )
        for (model_id, model_name, instance_id, reason), count in snap[
            "selections"
        ].items():
            fam.add_metric([model_id, model_name, instance_id, reason], count)
        yield fam

    def _yield_requests(self, snap: dict) -> Iterator[Metric]:
        fam = CounterMetricFamily(
            metric_name("lb_requests_total"),
            "Total number of requests by class (short/medium/heavy).",
            labels=["model_id", "model_name", "request_class"],
        )
        for (model_id, model_name, req_class), count in snap["requests"].items():
            fam.add_metric([model_id, model_name, req_class], count)
        yield fam

    def _yield_streak_resets(self, snap: dict) -> Iterator[Metric]:
        fam = CounterMetricFamily(
            metric_name("lb_affinity_streak_resets_total"),
            "Total number of forced affinity streak resets (cap exceeded).",
            labels=["model_id", "model_name", "instance_id"],
        )
        for (model_id, model_name, instance_id), count in snap["streak_resets"].items():
            fam.add_metric([model_id, model_name, instance_id], count)
        yield fam

    # ---- Gauge yielders ----

    def _yield_instance_gauges(self, snap: dict) -> Iterator[Metric]:
        score_fam = GaugeMetricFamily(
            metric_name("lb_instance_score"),
            "Current score of each instance (last computed).",
            labels=["model_id", "model_name", "instance_id"],
        )
        ewma_fam = GaugeMetricFamily(
            metric_name("lb_instance_ewma_kv"),
            "EWMA KV cache usage of each instance.",
            labels=["model_id", "model_name", "instance_id"],
        )
        wlc_fam = GaugeMetricFamily(
            metric_name("lb_instance_wlc_weight"),
            "WLC weight (weighted in-flight connections) of each instance.",
            labels=["model_id", "model_name", "instance_id"],
        )
        ss_fam = GaugeMetricFamily(
            metric_name("lb_instance_slow_start_weight"),
            "Slow start weight of each instance (0.0=active, 1.0=fully idle).",
            labels=["model_id", "model_name", "instance_id"],
        )
        streak_fam = GaugeMetricFamily(
            metric_name("lb_instance_affinity_streak"),
            "Current consecutive affinity hit streak per instance.",
            labels=["model_id", "model_name", "instance_id"],
        )
        kv_fam = GaugeMetricFamily(
            metric_name("lb_request_kv_cache_usage"),
            "KV cache usage at the time of selection (raw, not EWMA).",
            labels=["model_id", "model_name", "instance_id"],
        )

        for key, val in snap["instance_score"].items():
            score_fam.add_metric(list(key), val)
        for key, val in snap["instance_ewma_kv"].items():
            ewma_fam.add_metric(list(key), val)
        for key, val in snap["instance_wlc_weight"].items():
            wlc_fam.add_metric(list(key), val)
        for key, val in snap["instance_slow_start"].items():
            ss_fam.add_metric(list(key), val)
        for key, val in snap["instance_affinity_streak"].items():
            streak_fam.add_metric(list(key), val)
        for key, val in snap["instance_kv_cache"].items():
            kv_fam.add_metric(list(key), val)

        yield score_fam
        yield ewma_fam
        yield wlc_fam
        yield ss_fam
        yield streak_fam
        yield kv_fam

    def _yield_pool_size(self, snap: dict) -> Iterator[Metric]:
        fam = GaugeMetricFamily(
            metric_name("lb_pool_size"),
            "Number of healthy replicas in the selection pool.",
            labels=["model_id", "model_name"],
        )
        for (model_id, model_name), size in snap["pool_size"].items():
            fam.add_metric([model_id, model_name], size)
        yield fam

    # ---- Histogram yielders ----

    def _yield_prompt_tokens(self, snap: dict) -> Iterator[Metric]:
        fam = HistogramMetricFamily(
            metric_name("lb_request_prompt_tokens"),
            "Size of incoming prompt in tokens.",
            labels=["model_id", "model_name", "route"],
        )
        for key, values in snap["prompt_tokens"].items():
            fam.add_metric(
                list(key),
                _bucketed_values(values, _PROMPT_TOKEN_BUCKETS),
                sum(values),
            )
        yield fam

    def _yield_max_tokens(self, snap: dict) -> Iterator[Metric]:
        fam = HistogramMetricFamily(
            metric_name("lb_request_max_tokens"),
            "Requested max_tokens per request.",
            labels=["model_id", "model_name", "route"],
        )
        for key, values in snap["max_tokens"].items():
            fam.add_metric(
                list(key),
                _bucketed_values(values, _MAX_TOKEN_BUCKETS),
                sum(values),
            )
        yield fam

    def _yield_total_tokens(self, snap: dict) -> Iterator[Metric]:
        fam = HistogramMetricFamily(
            metric_name("lb_request_total_tokens"),
            "Total expected tokens (prompt + max_tokens).",
            labels=["model_id", "model_name", "route"],
        )
        for key, values in snap["total_tokens"].items():
            fam.add_metric(
                list(key),
                _bucketed_values(values, _TOTAL_TOKEN_BUCKETS),
                sum(values),
            )
        yield fam

    def _yield_selection_latency(self, snap: dict) -> Iterator[Metric]:
        fam = HistogramMetricFamily(
            metric_name("lb_selection_latency_seconds"),
            "Time to make a load-balancer decision (select_instance latency).",
            labels=["model_id", "model_name"],
        )
        for key, values in snap["selection_latency"].items():
            fam.add_metric(
                list(key),
                _bucketed_values(values, _LATENCY_BUCKETS),
                sum(values),
            )
        yield fam


def _histogram_stats(values: List[float]) -> Tuple[int, float]:
    """Compute (sample_count, sample_sum) for a list of histogram values."""
    return (len(values), sum(values))


def _bucketed_values(
    values: List[float],
    bucket_boundaries: List[float],
) -> List[Tuple[str, float]]:
    """Build bucket counts for HistogramMetricFamily.add_metric().

    Returns a list of (le_label, cumulative_count) tuples, sorted by boundary,
    with a final '+inf' entry.
    """
    sorted_values = sorted(values)
    bucket_counts: List[Tuple[str, float]] = []
    for bound in bucket_boundaries:
        count = bisect.bisect_right(sorted_values, bound)
        bucket_counts.append((str(bound), count))
    bucket_counts.append(("+inf", len(sorted_values)))
    return bucket_counts


# Singleton collector — register this on the global REGISTRY
lb_metrics_collector: Optional[LBMetricsCollector] = None


def get_lb_metrics_collector() -> LBMetricsCollector:
    """Get or create the singleton LBMetricsCollector."""
    global lb_metrics_collector
    if lb_metrics_collector is None:
        lb_metrics_collector = LBMetricsCollector()
    return lb_metrics_collector
