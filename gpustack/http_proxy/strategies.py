from abc import ABC, abstractmethod
import asyncio
import hashlib
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import itertools

from gpustack import envs
from gpustack.http_proxy.instance_metrics_cache import get_metrics, InstanceMetrics
from gpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)


class LoadBalancingStrategy(ABC):
    @abstractmethod
    async def select_instance(self, instances: List[ModelInstance]) -> ModelInstance:
        pass


class RoundRobinStrategy(LoadBalancingStrategy):
    def __init__(self):
        self._iterators: Dict[int, itertools.cycle] = {}
        self._instance_lists: Dict[int, List[ModelInstance]] = {}

    async def select_instance(self, instances: List[ModelInstance]) -> ModelInstance:
        if len(instances) == 0:
            raise Exception("No instances available")
        model_id = instances[0].model_id
        if (
            model_id not in self._iterators
            or self._instance_lists[model_id] != instances
        ):
            logger.debug(f"Creating new iterator for model {model_id}")
            self._iterators[model_id] = itertools.cycle(instances)
            self._instance_lists[model_id] = instances

        return next(self._iterators[model_id])


# ---------------------------------------------------------------------------
# Request classification
# ---------------------------------------------------------------------------


@dataclass
class RequestProfile:
    prompt_tokens: int = 0
    max_tokens: int = 0
    session_key: Optional[str] = None
    prefix_key: Optional[str] = None

    @property
    def total_expected_tokens(self) -> int:
        return self.prompt_tokens + self.max_tokens


@dataclass
class ClassLimits:
    max_running: int
    max_waiting: int
    max_kv: float


# Пороги классификации — можно переопределить через env
_PROMPT_HEAVY = envs.LB_PROMPT_HEAVY_THRESHOLD
_TOTAL_HEAVY = envs.LB_TOTAL_HEAVY_THRESHOLD
_PROMPT_MEDIUM = envs.LB_PROMPT_MEDIUM_THRESHOLD
_TOTAL_MEDIUM = envs.LB_TOTAL_MEDIUM_THRESHOLD

# Лимиты admission control по классам
# Heavy: строгий KV-порог (0.45) — тяжёлые запросы чувствительны к фрагментации
# Medium: умеренный KV-порог (0.50) — баланс между утилизацией и задержкой
# Short: высокий KV-порог (0.85) — лёгкие запросы терпимы к загруженным репликам
LIMITS = {
    "short": ClassLimits(max_running=3, max_waiting=1, max_kv=0.85),
    "medium": ClassLimits(max_running=2, max_waiting=1, max_kv=0.50),
    "heavy": ClassLimits(max_running=1, max_waiting=0, max_kv=0.45),
}


def classify_request(prompt_tokens: int, max_tokens: int) -> str:
    total = prompt_tokens + max_tokens
    if prompt_tokens >= _PROMPT_HEAVY or total >= _TOTAL_HEAVY:
        return "heavy"
    if prompt_tokens >= _PROMPT_MEDIUM or total >= _TOTAL_MEDIUM:
        return "medium"
    return "short"


# ---------------------------------------------------------------------------
# Peak EWMA — exponentially weighted moving average for KV smoothing
# ---------------------------------------------------------------------------


class PeakEWMA:
    """
    Peak EWMA smooths per-instance KV cache usage.

    Uses aggressive alpha when load is rising (fast reaction to overload)
    and conservative alpha when falling (slow decay, remembers overload).
    """

    def __init__(self) -> None:
        # instance_id -> ewma value (0.0 - 1.0)
        self._ewma: Dict[int, float] = {}

    def update(self, instance_id: int, current_kv: float) -> float:
        prev = self._ewma.get(instance_id)

        if prev is None:
            self._ewma[instance_id] = current_kv
            return current_kv

        if current_kv >= prev:
            # Rising — react fast
            alpha = envs.LB_EWMA_ALPHA_RISE
        else:
            # Falling — decay slowly
            alpha = envs.LB_EWMA_ALPHA_FALL

        new_ewma = alpha * current_kv + (1 - alpha) * prev
        self._ewma[instance_id] = new_ewma
        return new_ewma

    def get(self, instance_id: int) -> float:
        return self._ewma.get(instance_id, 0.0)

    def clear(self, instance_id: int) -> None:
        self._ewma.pop(instance_id, None)


# ---------------------------------------------------------------------------
# Consistent Hashing with Bounded Loads (CHWBL)
# ---------------------------------------------------------------------------


class ConsistentHashRing:
    """
    Consistent hashing ring with virtual nodes.

    Each physical instance gets `vnodes` virtual positions on the ring.
    Lookup hashes the key and finds the next virtual node clockwise.
    Bounded loads: if the target instance is overloaded, walk to the next
    available node on the ring.
    """

    def __init__(self, vnodes: int = 100) -> None:
        self._vnodes = vnodes
        # Sorted list of (hash_value, instance_id)
        self._ring: List[Tuple[int, int]] = []
        self._hash_map: Dict[int, List[int]] = {}  # instance_id -> [hash_values]

    def _hash(self, key: str) -> int:
        return int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)

    def add(self, instance_id: int) -> None:
        # Remove old entries for this instance first
        if instance_id in self._hash_map:
            for hv in self._hash_map[instance_id]:
                self._ring.remove((hv, instance_id))
            del self._hash_map[instance_id]

        hashes = []
        for i in range(self._vnodes):
            hv = self._hash(f"{instance_id}:{i}")
            hashes.append(hv)
            self._ring.append((hv, instance_id))
        self._hash_map[instance_id] = hashes
        self._ring.sort(key=lambda x: x[0])

    def remove(self, instance_id: int) -> None:
        if instance_id in self._hash_map:
            for hv in self._hash_map[instance_id]:
                self._ring.remove((hv, instance_id))
            del self._hash_map[instance_id]

    def get(
        self,
        key: str,
        instance_ids: List[int],
        is_overloaded: callable,
    ) -> Optional[int]:
        """
        Find the instance for `key`, skipping overloaded instances.

        Args:
            key: the hash key (e.g., prefix digest)
            instance_ids: currently available instances
            is_overloaded: callable(instance_id) -> bool

        Returns:
            instance_id or None if all instances are overloaded
        """
        if not self._ring or not instance_ids:
            return None

        available = set(instance_ids)
        key_hash = self._hash(key)

        # Find starting position on the ring
        idx = 0
        for i, (hv, _) in enumerate(self._ring):
            if hv >= key_hash:
                idx = i
                break
        else:
            idx = 0  # wrap around

        # Walk the ring clockwise
        visited = set()
        for _ in range(len(self._ring)):
            hv, inst_id = self._ring[idx % len(self._ring)]
            idx += 1

            if inst_id not in available:
                continue
            if inst_id in visited:
                # Full circle — no available instance
                return None
            visited.add(inst_id)

            if not is_overloaded(inst_id):
                return inst_id

        return None

    @property
    def is_empty(self) -> bool:
        return len(self._ring) == 0


# ---------------------------------------------------------------------------
# Circuit Breaker per-instance
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """
    Per-instance circuit breaker with states: CLOSED, OPEN, HALF-OPEN.

    - CLOSED: normal operation
    - OPEN: instance is excluded (tripped by high KV or error)
    - HALF-OPEN: after timeout, allow one probe request
    """

    def __init__(self) -> None:
        # instance_id -> state string
        self._state: Dict[int, str] = {}
        # instance_id -> monotonic timestamp when state changed
        self._changed_at: Dict[int, float] = {}

    def _get_state(self, instance_id: int) -> str:
        return self._state.get(instance_id, "CLOSED")

    def should_allow(self, instance_id: int, now: float) -> bool:
        state = self._get_state(instance_id)

        if state == "CLOSED":
            return True
        if state == "OPEN":
            changed = self._changed_at.get(instance_id, 0)
            if now - changed >= envs.LB_CIRCUIT_BREAKER_TIMEOUT:
                self._state[instance_id] = "HALF-OPEN"
                self._changed_at[instance_id] = now
                return True  # allow probe
            return False
        # HALF-OPEN — allow probe
        return True

    def record_success(self, instance_id: int, now: float) -> None:
        self._state[instance_id] = "CLOSED"
        self._changed_at[instance_id] = now

    def record_failure(self, instance_id: int, now: float) -> None:
        if self._get_state(instance_id) == "HALF-OPEN":
            # Probe failed — go back to OPEN
            self._state[instance_id] = "OPEN"
        else:
            self._state[instance_id] = "OPEN"
        self._changed_at[instance_id] = now

    def trip(self, instance_id: int, now: float) -> None:
        """Explicitly trip the circuit (e.g., KV threshold exceeded)."""
        self._state[instance_id] = "OPEN"
        self._changed_at[instance_id] = now

    def clear(self, instance_id: int) -> None:
        self._state.pop(instance_id, None)
        self._changed_at.pop(instance_id, None)


# ---------------------------------------------------------------------------
# Slow Start — gradually increase weight after idle period
# ---------------------------------------------------------------------------


class SlowStart:
    """
    Slow Start gives progressively increasing weight to instances
    that have been idle, ramping up over a configurable window.

    Weight goes from 0 (just became idle) to 1 (fully warmed up).
    """

    def __init__(self) -> None:
        # instance_id -> monotonic timestamp when instance became idle
        self._idle_since: Dict[int, float] = {}
        # instance_id -> whether currently tracking (idle)
        self._tracking: Dict[int, bool] = {}

    def mark_active(self, instance_id: int) -> None:
        """Instance received a request — stop tracking."""
        self._tracking.pop(instance_id, None)
        self._idle_since.pop(instance_id, None)

    def mark_idle(self, instance_id: int, now: float) -> None:
        """Instance became idle — start tracking."""
        if not self._tracking.get(instance_id, False):
            self._tracking[instance_id] = True
            self._idle_since[instance_id] = now

    def get_weight(self, instance_id: int, now: float) -> float:
        """
        Return slow-start weight (0.0 - 1.0).

        Returns 0 if instance is not being tracked (active).
        Returns 1 if fully warmed up (idle for >= window seconds).
        """
        if not self._tracking.get(instance_id, False):
            return 0.0

        idle_since = self._idle_since.get(instance_id)
        if idle_since is None:
            return 0.0

        window = envs.LB_SLOW_START_WINDOW
        elapsed = now - idle_since

        if elapsed >= window:
            return 1.0
        if elapsed <= 0:
            return 0.0

        progress = elapsed / window
        aggression = envs.LB_SLOW_START_AGGRESSION

        # power function: progress^aggression
        # aggression=1.0 -> linear, >1 -> convex (slower start), <1 -> concave
        return progress**aggression


# ---------------------------------------------------------------------------
# Weighted Least Connections (WLC) — tracks weighted in-flight load
# ---------------------------------------------------------------------------


class WeightedConnections:
    """
    Tracks weighted in-flight connections per instance.

    Weight = prompt_tokens + max_tokens for each request.
    Decay proportional to num_running drops.
    """

    def __init__(self) -> None:
        # instance_id -> sum of weights of in-flight requests
        self._weights: Dict[int, int] = {}
        # instance_id -> num_running at last update
        self._last_running: Dict[int, int] = {}

    def add(self, instance_id: int, weight: int) -> None:
        self._weights[instance_id] = self._weights.get(instance_id, 0) + weight

    def decay(self, instance_id: int, current_running: float) -> None:
        prev = self._last_running.get(instance_id)
        if prev is None:
            self._last_running[instance_id] = int(current_running)
            return

        if current_running == 0:
            self._weights[instance_id] = 0
        elif prev > 0 and current_running < prev:
            ratio = current_running / prev
            self._weights[instance_id] = int(self._weights.get(instance_id, 0) * ratio)

        self._last_running[instance_id] = int(current_running)

    def get(self, instance_id: int) -> int:
        return self._weights.get(instance_id, 0)

    def clear(self, instance_id: int) -> None:
        self._weights.pop(instance_id, None)
        self._last_running.pop(instance_id, None)


# ---------------------------------------------------------------------------
# Smart Strategy
# ---------------------------------------------------------------------------


class SmartLoadBalancingStrategy(LoadBalancingStrategy):
    """
    Smart load balancing using standard algorithms:

    - Power of Two Choices (PoT) for load-aware selection
    - Consistent Hashing with Bounded Loads (CHWBL) for prefix affinity
    - Peak EWMA for KV cache smoothing (replaces cooldown/hysteresis)
    - Circuit Breaker for overload protection (replaces burst + headroom)
    - Weighted Least Connections for request-size awareness
    - Slow Start for warm-up after idle periods
    - Admission Control with request classification (short/medium/heavy)
    - Soft session affinity (explicit session ids only, no user/token sticky)
    - Cluster imbalance detection with affinity breaker
    """

    def __init__(self) -> None:
        # session_key -> instance_id
        self._session_affinity: Dict[str, int] = {}
        # Sub-components
        self._ewma = PeakEWMA()
        self._hash_ring = ConsistentHashRing(envs.LB_CHWBL_VNODES)
        self._circuit_breaker = CircuitBreaker()
        self._slow_start = SlowStart()
        self._wlc = WeightedConnections()
        self._lock = asyncio.Lock()

    async def select_instance(
        self,
        instances: List[ModelInstance],
        profile: RequestProfile,
    ) -> ModelInstance:
        if not instances:
            raise RuntimeError("No running instances available")

        if len(instances) == 1:
            inst = instances[0]
            logger.debug(
                "[smart_lb] single instance inst=%d (no balancing needed)",
                inst.id,
            )
            return inst

        req_class = classify_request(profile.prompt_tokens, profile.max_tokens)
        now = time.monotonic()
        instance_ids = [inst.id for inst in instances]

        # Update EWMA and WLC decay for all instances
        for inst in instances:
            m = get_metrics(inst.id)
            self._ewma.update(inst.id, m.kv_cache_usage)
            self._wlc.decay(inst.id, m.num_running)

            # Update circuit breaker: trip if EWMA exceeds threshold
            ewma_val = self._ewma.get(inst.id)
            if ewma_val >= envs.LB_CIRCUIT_BREAKER_KV_THRESHOLD:
                self._circuit_breaker.trip(inst.id, now)

            # Update slow start: mark idle if clean, active otherwise
            if m.num_running == 0 and m.num_waiting == 0 and m.kv_cache_usage < 0.25:
                self._slow_start.mark_idle(inst.id, now)
            else:
                self._slow_start.mark_active(inst.id)

        # Update hash ring with current instances
        self._sync_hash_ring(instance_ids)

        # Log: request classification
        logger.debug(
            "[smart_lb] classify prompt=%d max=%d total=%d -> class=%s "
            "session_key=%s prefix_key=%s",
            profile.prompt_tokens,
            profile.max_tokens,
            profile.total_expected_tokens,
            req_class,
            profile.session_key,
            profile.prefix_key,
        )

        # Log: instance metrics
        for inst in instances:
            m = get_metrics(inst.id)
            ewma_val = self._ewma.get(inst.id)
            logger.debug(
                "[smart_lb] inst=%d running=%.0f waiting=%.0f kv=%.3f "
                "ewma=%.3f cb=%s wlc=%d slow_start=%.2f stale=%s",
                inst.id,
                m.num_running,
                m.num_waiting,
                m.kv_cache_usage,
                ewma_val,
                self._circuit_breaker._get_state(inst.id),
                self._wlc.get(inst.id),
                self._slow_start.get_weight(inst.id, now),
                m.is_stale(),
            )

        # 1) Admission control: split into admissible and fallback pools
        admissible: List[Tuple[ModelInstance, InstanceMetrics]] = []
        fallback: List[Tuple[ModelInstance, InstanceMetrics, str]] = []

        for inst in instances:
            m = get_metrics(inst.id)

            # Circuit breaker check
            if not self._circuit_breaker.should_allow(inst.id, now):
                fallback.append((inst, m, "circuit_open"))
                continue

            if self._admissible(m, req_class):
                admissible.append((inst, m))
            else:
                lim = LIMITS[req_class]
                logger.debug(
                    "[smart_lb] inst=%d inadmissible for %s "
                    "(running=%.0f>%d waiting=%.0f>%d kv=%.3f>%.2f)",
                    inst.id,
                    req_class,
                    m.num_running,
                    lim.max_running,
                    m.num_waiting,
                    lim.max_waiting,
                    m.kv_cache_usage,
                    lim.max_kv,
                )
                fallback.append((inst, m, "inadmissible"))

        logger.debug(
            "[smart_lb] admission result: %d admissible, %d fallback "
            "(admissible_ids=%s fallback_ids=%s)",
            len(admissible),
            len(fallback),
            [a[0].id for a in admissible],
            [f[0].id for f in fallback],
        )

        # 2) Build selection pool
        pool = (
            admissible if admissible else self._soft_fallback_pool(fallback, req_class)
        )

        if not admissible:
            logger.warning(
                "[smart_lb] no admissible replicas, using soft fallback (%d candidates)",
                len(pool),
            )

        if not pool:
            raise RuntimeError("No suitable instance after admission control")

        # 3) Determine cluster balance
        balanced = self._is_cluster_balanced(pool)

        # 4) Resolve session and prefix affinity candidates (soft preference)
        session_candidate: Optional[ModelInstance] = None
        session_broken_reason: Optional[str] = None

        if envs.LB_ENABLE_SESSION_AFFINITY and profile.session_key:
            session_candidate = self._get_session_candidate(
                instances, profile, pool, req_class, now
            )
            if session_candidate is None:
                # Check if it was broken (for logging)
                pinned_id = self._session_affinity.get(profile.session_key)
                if pinned_id is not None:
                    pool_ids = set(inst.id for inst, _ in pool)
                    if pinned_id in pool_ids:
                        _, reason = self._should_break_affinity(
                            pinned_id,
                            pool,
                            req_class,
                            now,
                        )
                        session_broken_reason = reason
                    else:
                        session_broken_reason = "not_in_pool"

        prefix_candidate: Optional[ModelInstance] = None
        prefix_broken_reason: Optional[str] = None

        if balanced and envs.LB_ENABLE_PREFIX_AFFINITY and profile.prefix_key:
            prefix_candidate = self._get_prefix_candidate(
                instances, profile, pool, req_class, now
            )
            if prefix_candidate is None:
                # Check if it was broken (for logging)
                if not self._hash_ring.is_empty:
                    prefix_broken_reason = "broken_or_not_in_pool"

        # 5) Determine preferred candidate
        # In balanced mode: session > prefix > None
        # In imbalanced mode: None (load-aware only)
        preferred: Optional[ModelInstance] = None

        if balanced:
            if session_candidate:
                preferred = session_candidate
            elif prefix_candidate:
                preferred = prefix_candidate

        # 6) Power of Two Choices selection with affinity bonus for preferred
        best = self._pot_select(
            pool,
            preferred.id if preferred else None,
            req_class,
            now,
        )

        if best is None:
            raise RuntimeError("No suitable instance after admission control")

        # 7) If preferred exists and its score is within 10% of selected, use preferred
        if preferred and best.id != preferred.id:
            pm = get_metrics(preferred.id)
            bm = get_metrics(best.id)
            score_preferred = self._pot_score(
                preferred.id,
                pm,
                req_class,
                True,
                now,
            )
            score_selected = self._pot_score(
                best.id,
                bm,
                req_class,
                False,
                now,
            )
            if score_preferred <= score_selected * envs.LB_AFFINITY_BREAK_SCORE_RATIO:
                best = preferred

        # 8) Determine log reason
        reason = self._determine_reason(
            best,
            preferred,
            session_broken_reason,
            prefix_broken_reason,
            balanced,
            req_class,
            profile,
        )

        # 9) Finalize: bind affinity, update state
        await self._finalize_selection(best, profile, now)

        # 10) Log selection
        self._log_selection(
            best,
            req_class,
            profile,
            reason,
            balanced=balanced,
            session_broken_reason=session_broken_reason,
            prefix_broken_reason=prefix_broken_reason,
        )
        return best

    # ---- Determine log reason ----

    def _determine_reason(
        self,
        selected: ModelInstance,
        preferred: Optional[ModelInstance],
        session_broken_reason: Optional[str],
        prefix_broken_reason: Optional[str],
        balanced: bool,
        req_class: str,
        profile: RequestProfile,
    ) -> str:
        if session_broken_reason:
            return f"affinity_broken_{session_broken_reason}"
        if prefix_broken_reason:
            return f"affinity_broken_{prefix_broken_reason}"
        if preferred and preferred.id == selected.id:
            if preferred_type := (
                "session"
                if profile.session_key
                and self._session_affinity.get(profile.session_key) == selected.id
                else "prefix"
            ):
                return f"{preferred_type}_affinity_soft"
        if not balanced:
            return "least_load_fallback"
        return "power_of_two_choices"

    # ---- hash ring sync ----

    def _sync_hash_ring(self, instance_ids: List[int]) -> None:
        """Add/remove instances from the consistent hash ring."""
        current_ids = set(self._hash_ring._hash_map.keys())
        new_ids = set(instance_ids)

        for rid in current_ids - new_ids:
            self._hash_ring.remove(rid)
        for aid in new_ids:
            self._hash_ring.add(aid)

    # ---- admission ----

    def _admissible(self, m: InstanceMetrics, req_class: str) -> bool:
        lim = LIMITS[req_class]
        return (
            m.num_running <= lim.max_running
            and m.num_waiting <= lim.max_waiting
            and m.kv_cache_usage <= lim.max_kv
        )

    def _affinity_allowed(self, m: InstanceMetrics, req_class: str) -> bool:
        """Softer thresholds for affinity fallback."""
        if req_class == "heavy":
            return m.num_running == 0 and m.num_waiting == 0 and m.kv_cache_usage < 0.40
        if req_class == "medium":
            return m.num_running < 3 and m.num_waiting <= 1 and m.kv_cache_usage < 0.55
        return m.kv_cache_usage < 0.90

    def _soft_fallback_pool(
        self,
        fallback: List[Tuple[ModelInstance, InstanceMetrics, str]],
        req_class: str,
    ) -> List[Tuple[ModelInstance, InstanceMetrics]]:
        ranked: List[Tuple[ModelInstance, InstanceMetrics]] = []
        for inst, m, reason in fallback:
            # affinity_fallback already passed _affinity_allowed
            if reason == "affinity_fallback":
                ranked.append((inst, m))
                continue

            # circuit_open instances — still include as last resort
            if reason == "circuit_open":
                continue

            if req_class == "heavy":
                if (
                    m.num_waiting == 0
                    and m.kv_cache_usage < 0.60
                    and m.num_running <= 2
                ):
                    ranked.append((inst, m))
            elif req_class == "medium":
                if m.num_waiting <= 1 and m.kv_cache_usage < 0.75:
                    ranked.append((inst, m))
            else:
                # short — accept all
                ranked.append((inst, m))
        return ranked

    # ---- cluster balance detection ----

    def _is_cluster_balanced(
        self,
        pool: List[Tuple[ModelInstance, InstanceMetrics]],
    ) -> bool:
        """
        Determine if the cluster is balanced.

        Cluster is imbalanced if:
        - max(queue_len) - min(queue_len) >= IMBALANCED_QUEUE_THRESHOLD
        - or max(kv) - min(kv) >= IMBALANCED_KV_THRESHOLD

        Returns True if balanced, False if imbalanced.
        """
        if len(pool) <= 1:
            return True

        queue_lens = [m.num_running + m.num_waiting for _, m in pool]
        kvs = [m.kv_cache_usage for _, m in pool]

        queue_spread = max(queue_lens) - min(queue_lens)
        kv_spread = max(kvs) - min(kvs)

        if queue_spread >= envs.LB_IMBALANCED_QUEUE_THRESHOLD:
            logger.debug(
                "[smart_lb] cluster imbalanced: queue_spread=%.0f >= %d",
                queue_spread,
                envs.LB_IMBALANCED_QUEUE_THRESHOLD,
            )
            return False

        if kv_spread >= envs.LB_IMBALANCED_KV_THRESHOLD:
            logger.debug(
                "[smart_lb] cluster imbalanced: kv_spread=%.3f >= %.2f",
                kv_spread,
                envs.LB_IMBALANCED_KV_THRESHOLD,
            )
            return False

        return True

    # ---- affinity breaker ----

    def _should_break_affinity(
        self,
        pinned_id: int,
        pool: List[Tuple[ModelInstance, InstanceMetrics]],
        req_class: str,
        now: float,
    ) -> Tuple[bool, str]:
        """
        Determine if pinned instance affinity should be broken.

        Returns (should_break, reason_string).

        Break conditions:
        - pinned has waiting > 0
        - pinned has running > class_running_limit
        - pinned has kv > class_kv_limit
        - score(pinned) > score(best) * AFFINITY_BREAK_SCORE_RATIO
        - running_pinned > min_running + AFFINITY_BREAK_RUNNING_DELTA
        - kv_pinned > min_kv + AFFINITY_BREAK_KV_DELTA

        For heavy requests, thresholds are stricter.
        """
        pinned_metrics = None
        for inst, m in pool:
            if inst.id == pinned_id:
                pinned_metrics = m
                break

        if pinned_metrics is None:
            return True, "pinned_not_in_pool"

        # Circuit breaker OPEN — always break
        if self._circuit_breaker._get_state(pinned_id) == "OPEN":
            return True, "circuit_open"

        lim = LIMITS[req_class]

        # waiting > 0
        if pinned_metrics.num_waiting > 0:
            return True, "waiting"

        # running > class limit
        if pinned_metrics.num_running > lim.max_running:
            return True, "running"

        # kv > class limit
        if pinned_metrics.kv_cache_usage > lim.max_kv:
            return True, "kv"

        # Compare with cluster minimums
        all_running = [m.num_running for _, m in pool]
        all_kv = [m.kv_cache_usage for _, m in pool]
        min_running = min(all_running)
        min_kv = min(all_kv)

        if (
            pinned_metrics.num_running
            > min_running + envs.LB_AFFINITY_BREAK_RUNNING_DELTA
        ):
            return True, "running_delta"

        if pinned_metrics.kv_cache_usage > min_kv + envs.LB_AFFINITY_BREAK_KV_DELTA:
            return True, "kv_delta"

        # Score comparison
        score_pinned = self._pot_score(pinned_id, pinned_metrics, req_class, False, now)
        best_score = math.inf
        for inst, m in pool:
            s = self._pot_score(inst.id, m, req_class, False, now)
            if s < best_score:
                best_score = s

        if score_pinned > best_score * envs.LB_AFFINITY_BREAK_SCORE_RATIO:
            return True, "score"

        return False, ""

    # ---- session affinity (soft preference) ----

    def _get_session_candidate(
        self,
        instances: List[ModelInstance],
        profile: RequestProfile,
        pool: List[Tuple[ModelInstance, InstanceMetrics]],
        req_class: str,
        now: float,
    ) -> Optional[ModelInstance]:
        """
        Check session affinity — returns candidate if affinity should be preferred.

        This is a SOFT preference: the pinned instance is returned only if
        it is healthy and affinity should NOT be broken.
        """
        if not envs.LB_ENABLE_SESSION_AFFINITY:
            return None

        if not profile.session_key:
            return None

        pinned_id = self._session_affinity.get(profile.session_key)
        if pinned_id is None:
            return None

        ids_map = {inst.id: inst for inst in instances}
        if pinned_id not in ids_map:
            return None

        # Check if pinned is in the selection pool
        pool_ids = {inst.id for inst, _ in pool}
        if pinned_id not in pool_ids:
            return None

        # Check affinity breaker
        should_break, reason = self._should_break_affinity(
            pinned_id, pool, req_class, now
        )
        if should_break:
            logger.debug(
                "[smart_lb] session affinity broken: inst=%d reason=%s",
                pinned_id,
                reason,
            )
            return None

        return ids_map[pinned_id]

    # ---- prefix affinity (CHWBL) ----

    def _get_prefix_candidate(
        self,
        instances: List[ModelInstance],
        profile: RequestProfile,
        pool: List[Tuple[ModelInstance, InstanceMetrics]],
        req_class: str,
        now: float,
    ) -> Optional[ModelInstance]:
        """
        Check CHWBL prefix affinity — soft preference, respects load bounds.

        Only returns candidate in balanced mode and if affinity should not be broken.
        """
        if not envs.LB_ENABLE_PREFIX_AFFINITY:
            return None

        if not profile.prefix_key:
            return None

        if self._hash_ring.is_empty:
            return None

        instance_ids = [inst.id for inst in instances]

        def is_overloaded(inst_id: int) -> bool:
            ewma_val = self._ewma.get(inst_id)
            # Overloaded if EWMA KV is high or circuit is OPEN
            if self._circuit_breaker._get_state(inst_id) == "OPEN":
                return True
            return ewma_val > 0.70

        pinned_id = self._hash_ring.get(profile.prefix_key, instance_ids, is_overloaded)

        if pinned_id is None:
            return None

        ids_map = {inst.id: inst for inst in instances}
        inst = ids_map.get(pinned_id)
        if inst is None:
            return None

        # Check if pinned is in the selection pool
        pool_ids = {i.id for i, _ in pool}
        if pinned_id not in pool_ids:
            return None

        # Check affinity breaker
        should_break, reason = self._should_break_affinity(
            pinned_id, pool, req_class, now
        )
        if should_break:
            logger.debug(
                "[smart_lb] prefix affinity broken: inst=%d reason=%s",
                pinned_id,
                reason,
            )
            return None

        return inst

    # ---- PoT scoring ----

    def _pot_score(
        self,
        inst_id: int,
        m: InstanceMetrics,
        req_class: str,
        is_pinned: bool,
        now: float,
    ) -> float:
        """
        Power of Two Choices score.

        score = running + waiting + alpha * ewma_kv + wlc_penalty
                - affinity_bonus - slow_start_bonus

        Lower is better.
        """
        ewma_kv = self._ewma.get(inst_id)
        wlc_weight = self._wlc.get(inst_id)

        # Base score
        score = m.num_running + m.num_waiting + envs.LB_POT_ALPHA * ewma_kv

        # WLC penalty (scaled to be comparable)
        score += wlc_weight * 0.0001

        # Class extras: heavy/medium get extra penalty on loaded replicas
        if req_class == "heavy":
            score += m.num_running * 2.0 + ewma_kv * 3.0
        elif req_class == "medium":
            score += m.num_running * 1.0 + ewma_kv * 1.5

        # Affinity bonus (reduces score)
        if is_pinned:
            score -= envs.LB_AFFINITY_SESSION_BONUS

        # Slow start bonus: idle instances get lower score
        ss_weight = self._slow_start.get_weight(inst_id, now)
        if ss_weight > 0:
            score -= ss_weight * 2.0

        return score

    def _pot_select(
        self,
        pool: List[Tuple[ModelInstance, InstanceMetrics]],
        preferred_id: Optional[int],
        req_class: str,
        now: float,
    ) -> Optional[ModelInstance]:
        """
        Power of Two Choices: pick N random candidates, return the one
        with the lower score.

        preferred_id receives affinity_bonus in scoring.
        """
        if not pool:
            return None

        if len(pool) == 1:
            return pool[0][0]

        # Pick random candidates (configurable count, default 2)
        choice_count = min(envs.LB_POT_CHOICE_COUNT, len(pool))
        candidates = random.sample(pool, choice_count)

        best = None
        best_score = math.inf

        for inst, m in candidates:
            is_pinned = inst.id == preferred_id
            s = self._pot_score(inst.id, m, req_class, is_pinned, now)

            logger.debug(
                "[smart_lb] pot score inst=%d final=%.2f "
                "(running=%.0f waiting=%.0f ewma_kv=%.3f wlc=%d "
                "preferred=%s slow_start=%.2f)",
                inst.id,
                s,
                m.num_running,
                m.num_waiting,
                self._ewma.get(inst.id),
                self._wlc.get(inst.id),
                is_pinned,
                self._slow_start.get_weight(inst.id, now),
            )

            if s < best_score:
                best = inst
                best_score = s

        return best

    # ---- finalize ----

    async def _finalize_selection(
        self, inst: ModelInstance, profile: RequestProfile, now: float
    ) -> None:
        """Update state after selecting an instance."""
        async with self._lock:
            # Bind session affinity
            if profile.session_key:
                self._session_affinity[profile.session_key] = inst.id

            # Add WLC weight
            self._wlc.add(inst.id, profile.total_expected_tokens)

            # Mark active for slow start
            self._slow_start.mark_active(inst.id)

            # Record circuit breaker success
            self._circuit_breaker.record_success(inst.id, now)

    async def clear_affinity(self, session_key: str) -> None:
        async with self._lock:
            self._session_affinity.pop(session_key, None)

    # ---- logging ----

    def _log_selection(
        self,
        inst: ModelInstance,
        req_class: str,
        profile: RequestProfile,
        reason: str,
        balanced: bool = True,
        session_broken_reason: Optional[str] = None,
        prefix_broken_reason: Optional[str] = None,
    ) -> None:
        m = get_metrics(inst.id)
        ewma_val = self._ewma.get(inst.id)

        # Determine session key source for logging
        session_key_source = "none"
        if profile.session_key:
            if profile.session_key.startswith("session:"):
                session_key_source = "x-session-id"
            elif profile.session_key.startswith("conversation:"):
                session_key_source = "x-conversation-id"
            elif profile.session_key.startswith("body:"):
                session_key_source = "body"
            elif profile.session_key.startswith("project:"):
                session_key_source = "x-project-id"

        # Get pinned instance ids for logging
        pinned_session_id = (
            self._session_affinity.get(profile.session_key)
            if profile.session_key
            else None
        )

        logger.info(
            "[smart_lb] >>> SELECTED inst=%d class=%s prompt=%d max=%d "
            "running=%.0f waiting=%.0f kv=%.3f ewma=%.3f reason=%s "
            "wlc=%d cb=%s slow_start=%.2f "
            "balanced=%s "
            "session_key=%s session_key_source=%s "
            "pinned_session=%s pinned_prefix=%s "
            "prefix_key=%s "
            "session_broken=%s prefix_broken=%s",
            inst.id,
            req_class,
            profile.prompt_tokens,
            profile.max_tokens,
            m.num_running,
            m.num_waiting,
            m.kv_cache_usage,
            ewma_val,
            reason,
            self._wlc.get(inst.id),
            self._circuit_breaker._get_state(inst.id),
            self._slow_start.get_weight(inst.id, time.monotonic()),
            balanced,
            profile.session_key,
            session_key_source,
            pinned_session_id,
            None,  # pinned_prefix determined dynamically via hash ring
            profile.prefix_key,
            session_broken_reason,
            prefix_broken_reason,
        )
