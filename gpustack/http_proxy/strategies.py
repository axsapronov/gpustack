from abc import ABC, abstractmethod
import asyncio
import logging
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

import itertools

from gpustack import envs
from gpustack.http_proxy.instance_metrics_cache import get_metrics, InstanceMetrics
from gpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)


class LoadBalancingStrategy(ABC):
    @abstractmethod
    async def select_instance(
        self, instances: List[ModelInstance], **kwargs
    ) -> ModelInstance:
        pass


class RoundRobinStrategy(LoadBalancingStrategy):
    def __init__(self):
        self._iterators: Dict[int, itertools.cycle] = {}
        self._instance_lists: Dict[int, List[ModelInstance]] = {}

    async def select_instance(
        self, instances: List[ModelInstance], **kwargs
    ) -> ModelInstance:
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


# Пороги классификации — можно переопределить через env
_PROMPT_HEAVY = envs.LB_PROMPT_HEAVY_THRESHOLD
_TOTAL_HEAVY = envs.LB_TOTAL_HEAVY_THRESHOLD
_PROMPT_MEDIUM = envs.LB_PROMPT_MEDIUM_THRESHOLD
_TOTAL_MEDIUM = envs.LB_TOTAL_MEDIUM_THRESHOLD


def classify_request(prompt_tokens: int, max_tokens: int) -> str:
    total = prompt_tokens + max_tokens
    if prompt_tokens >= _PROMPT_HEAVY or total >= _TOTAL_HEAVY:
        return "heavy"
    if prompt_tokens >= _PROMPT_MEDIUM or total >= _TOTAL_MEDIUM:
        return "medium"
    return "short"


# ---------------------------------------------------------------------------
# Scoring weights per request class
# ---------------------------------------------------------------------------


def _get_waiting_weight(req_class: str) -> float:
    if req_class == "heavy":
        return envs.LB_WAITING_WEIGHT_HEAVY
    if req_class == "medium":
        return envs.LB_WAITING_WEIGHT_MEDIUM
    return envs.LB_WAITING_WEIGHT_SHORT


def _get_kv_weight(req_class: str) -> float:
    if req_class == "heavy":
        return envs.LB_KV_WEIGHT_HEAVY
    if req_class == "medium":
        return envs.LB_KV_WEIGHT_MEDIUM
    return envs.LB_KV_WEIGHT_SHORT


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
# Slow Start — gradually increase weight after idle period (linear ramp)
# ---------------------------------------------------------------------------


class SlowStart:
    """
    Slow Start gives progressively increasing weight to instances
    that have been idle, ramping up linearly over a configurable window.

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
        Linear ramp: progress = elapsed / window.
        """
        if not self._tracking.get(instance_id, False):
            return 0.0

        idle_since = self._idle_since.get(instance_id)
        if idle_since is None:
            return 0.0

        window = envs.LB_SLOW_START_RAMP_SECONDS
        elapsed = now - idle_since

        if elapsed >= window:
            return 1.0
        if elapsed <= 0:
            return 0.0

        # Linear ramp (replaces progress**aggression)
        return elapsed / window


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
    Smart load balancing using a unified scoring pipeline:

    - Power of Two Choices (PoT) for load-aware selection
    - Unified scoring with class-based weights (waiting_weight, kv_weight)
    - Peak EWMA for KV cache smoothing
    - Weighted Least Connections for request-size awareness
    - Slow Start (linear ramp) for warm-up after idle periods
    - Soft session/prefix affinity with breaker (waiting>0 or score×1.2)
    - Affinity streak cap (max 20 consecutive hits)
    - Request classification (short/medium/heavy) as source of scoring weights
    """

    def __init__(self) -> None:
        # session_key -> instance_id (soft affinity)
        self._session_affinity: Dict[str, int] = {}
        # prefix_key -> instance_id (soft affinity, replaces ConsistentHashRing)
        self._prefix_affinity: Dict[str, int] = {}
        # instance_id -> consecutive affinity hit count (capped at 20)
        self._affinity_streak: Dict[int, int] = {}
        # Sub-components
        self._ewma = PeakEWMA()
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

        start_time = time.monotonic()

        if len(instances) == 1:
            inst = instances[0]
            logger.debug(
                "[smart_lb] single instance inst=%d (no balancing needed)",
                inst.id,
            )
            return inst

        req_class = classify_request(profile.prompt_tokens, profile.max_tokens)
        now = time.monotonic()

        # Step 1: Collect healthy replicas (all instances are candidates)
        healthy: List[ModelInstance] = list(instances)

        if not healthy:
            raise RuntimeError("No healthy instances available")

        # Record pool size metric
        try:
            from gpustack.http_proxy.lb_metrics import get_lb_metrics_collector

            collector = get_lb_metrics_collector()
            collector.record_pool_size(
                model_id=str(healthy[0].model_id),
                model_name=healthy[0].model_name,
                size=len(healthy),
            )
        except Exception:
            pass

        # Step 2: Update states — EWMA, WLC decay, slow start ramp
        for inst in healthy:
            m = get_metrics(inst.id)
            self._ewma.update(inst.id, m.kv_cache_usage)
            self._wlc.decay(inst.id, m.num_running)

            # Update slow start: mark idle if clean, active otherwise
            if m.num_running == 0 and m.num_waiting == 0 and m.kv_cache_usage < 0.25:
                self._slow_start.mark_idle(inst.id, now)
            else:
                self._slow_start.mark_active(inst.id)

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
        for inst in healthy:
            m = get_metrics(inst.id)
            ewma_val = self._ewma.get(inst.id)
            logger.debug(
                "[smart_lb] inst=%d running=%.0f waiting=%.0f kv=%.3f "
                "ewma=%.3f wlc=%d slow_start=%.2f streak=%d",
                inst.id,
                m.num_running,
                m.num_waiting,
                m.kv_cache_usage,
                ewma_val,
                self._wlc.get(inst.id),
                self._slow_start.get_weight(inst.id, now),
                self._affinity_streak.get(inst.id, 0),
            )

        # Step 3: Find pinned candidate by session_key / prefix_key
        pinned_instance: Optional[ModelInstance] = None

        if envs.LB_ENABLE_SESSION_AFFINITY and profile.session_key:
            pinned_id = self._session_affinity.get(profile.session_key)
            if pinned_id is not None:
                pinned = next((i for i in healthy if i.id == pinned_id), None)
                if pinned:
                    # Check streak cap: force reset after max streak
                    streak = self._affinity_streak.get(pinned_id, 0)
                    if streak >= envs.LB_AFFINITY_MAX_STREAK:
                        logger.debug(
                            "[smart_lb] affinity streak cap reached for inst=%d "
                            "(streak=%d >= %d), resetting",
                            pinned_id,
                            streak,
                            envs.LB_AFFINITY_MAX_STREAK,
                        )
                        self._affinity_streak[pinned_id] = 0
                        # Record streak reset metric
                        try:
                            from gpustack.http_proxy.lb_metrics import (
                                get_lb_metrics_collector,
                            )

                            collector = get_lb_metrics_collector()
                            collector.record_streak_reset(
                                model_id=str(pinned.model_id),
                                model_name=pinned.model_name,
                                instance_id=str(pinned_id),
                            )
                        except Exception:
                            pass
                    else:
                        pinned_instance = pinned

        if (
            pinned_instance is None
            and envs.LB_ENABLE_PREFIX_AFFINITY
            and profile.prefix_key
        ):
            pinned_id = self._prefix_affinity.get(profile.prefix_key)
            if pinned_id is not None:
                pinned = next((i for i in healthy if i.id == pinned_id), None)
                if pinned:
                    # Check streak cap
                    streak = self._affinity_streak.get(pinned_id, 0)
                    if streak >= envs.LB_AFFINITY_MAX_STREAK:
                        logger.debug(
                            "[smart_lb] prefix affinity streak cap reached for inst=%d "
                            "(streak=%d >= %d), resetting",
                            pinned_id,
                            streak,
                            envs.LB_AFFINITY_MAX_STREAK,
                        )
                        self._affinity_streak[pinned_id] = 0
                        # Record streak reset metric
                        try:
                            from gpustack.http_proxy.lb_metrics import (
                                get_lb_metrics_collector,
                            )

                            collector = get_lb_metrics_collector()
                            collector.record_streak_reset(
                                model_id=str(pinned.model_id),
                                model_name=pinned.model_name,
                                instance_id=str(pinned_id),
                            )
                        except Exception:
                            pass
                    else:
                        pinned_instance = pinned

        # Step 4: Power of Two Choices — pick 2 random candidates from healthy pool
        pot_candidates = self._pot_choose(healthy)

        # Step 5: Compute scores for each candidate
        scored: Dict[int, float] = {}
        for inst in pot_candidates:
            m = get_metrics(inst.id)
            s = self._compute_score(inst.id, m, req_class, now)
            scored[inst.id] = s

            logger.debug(
                "[smart_lb] score inst=%d final=%.2f "
                "(running=%.0f waiting=%.0f ewma_kv=%.3f wlc=%d slow_start=%.2f)",
                inst.id,
                s,
                m.num_running,
                m.num_waiting,
                self._ewma.get(inst.id),
                self._wlc.get(inst.id),
                self._slow_start.get_weight(inst.id, now),
            )

        # Step 6: Decide — pinned with affinity breaker or PoT best
        reason: str = ""
        selected: Optional[ModelInstance] = None

        ids_map = {inst.id: inst for inst in healthy}

        # Also score the pinned instance if it's not already in scored
        if pinned_instance and pinned_instance.id not in scored:
            pm = get_metrics(pinned_instance.id)
            scored[pinned_instance.id] = self._compute_score(
                pinned_instance.id, pm, req_class, now
            )

        if pinned_instance:
            pinned_id = pinned_instance.id
            pm = get_metrics(pinned_id)

            # Affinity breaker condition 1: waiting > 0
            if pm.num_waiting > 0:
                reason = "affinity_broken_waiting"
            else:
                # Affinity breaker condition 2: score comparison
                best_id = min(scored, key=scored.get)
                best_score = scored[best_id]
                pinned_score = scored[pinned_id]

                if pinned_score <= best_score * envs.LB_AFFINITY_BREAK_MULTIPLIER:
                    # Affinity holds — choose pinned
                    selected = pinned_instance
                    reason = "affinity_soft"
                else:
                    reason = "affinity_broken_score"

        if reason.startswith("affinity_broken"):
            # Affinity broken — select best from scored candidates
            best_id = min(scored, key=scored.get)
            selected = ids_map[best_id]
            reason = "pot_score"
        elif selected is None:
            # No pinned candidate — select best from scored candidates
            best_id = min(scored, key=scored.get)
            selected = ids_map[best_id]
            reason = "pot_score"

        if selected is None:
            raise RuntimeError("No suitable instance after scoring")

        # Step 7: Finalize — bind affinity, update WLC/slow-start
        await self._finalize_selection(selected, profile, now)

        # Step 8: Log selection
        self._log_selection(
            selected,
            req_class,
            profile,
            reason,
            scored.get(selected.id, 0.0),
            start_time,
        )

        return selected

    # ---- Scoring ----

    def _compute_score(
        self,
        inst_id: int,
        m: InstanceMetrics,
        req_class: str,
        now: float,
    ) -> float:
        """
        Unified score formula:

        score = running
                + waiting_weight[req_class] * waiting
                + kv_weight[req_class] * ewma_kv
                + wlc_penalty
                - slow_start_bonus

        Lower is better.
        """
        ewma_kv = self._ewma.get(inst_id)
        wlc_weight = self._wlc.get(inst_id)
        waiting_w = _get_waiting_weight(req_class)
        kv_w = _get_kv_weight(req_class)

        score = (
            m.num_running
            + waiting_w * m.num_waiting
            + kv_w * ewma_kv
            + wlc_weight * 0.0001
        )

        # Slow start bonus: idle instances get lower score
        ss_weight = self._slow_start.get_weight(inst_id, now)
        if ss_weight > 0:
            score -= ss_weight * 2.0

        return score

    # ---- Power of Two Choices ----

    def _pot_choose(self, healthy: List[ModelInstance]) -> List[ModelInstance]:
        """
        Power of Two Choices: pick 2 random candidates from healthy pool.

        If pool has <= 3 instances, compare the entire pool.
        """
        if len(healthy) <= 3:
            return list(healthy)

        return random.sample(healthy, 2)

    # ---- Finalize ----

    async def _finalize_selection(
        self, inst: ModelInstance, profile: RequestProfile, now: float
    ) -> None:
        """Update state after selecting an instance."""
        async with self._lock:
            # Bind session affinity
            if profile.session_key:
                self._session_affinity[profile.session_key] = inst.id

            # Bind prefix affinity
            if profile.prefix_key:
                self._prefix_affinity[profile.prefix_key] = inst.id

            # Add WLC weight
            self._wlc.add(inst.id, profile.total_expected_tokens)

            # Mark active for slow start
            self._slow_start.mark_active(inst.id)

            # Increment affinity streak
            self._affinity_streak[inst.id] = self._affinity_streak.get(inst.id, 0) + 1

    async def clear_affinity(self, session_key: str) -> None:
        async with self._lock:
            self._session_affinity.pop(session_key, None)

    # ---- Logging ----

    def _log_selection(
        self,
        inst: ModelInstance,
        req_class: str,
        profile: RequestProfile,
        reason: str,
        score: float,
        start_time: float,
    ) -> None:
        m = get_metrics(inst.id)
        ewma_val = self._ewma.get(inst.id)
        latency = time.monotonic() - start_time

        # Record Prometheus metrics
        try:
            from gpustack.http_proxy.lb_metrics import get_lb_metrics_collector

            collector = get_lb_metrics_collector()
            model_id = str(inst.model_id)
            model_name = inst.model_name
            instance_id = str(inst.id)

            collector.record_selection(
                model_id=model_id,
                model_name=model_name,
                instance_id=instance_id,
                reason=reason,
                request_class=req_class,
                score=score,
                prompt_tokens=profile.prompt_tokens,
                max_tokens=profile.max_tokens,
                latency=latency,
            )

            collector.record_instance_state(
                model_id=model_id,
                model_name=model_name,
                instance_id=instance_id,
                score=score,
                ewma_kv=ewma_val,
                wlc_weight=float(self._wlc.get(inst.id)),
                slow_start_weight=self._slow_start.get_weight(
                    inst.id, time.monotonic()
                ),
                affinity_streak=self._affinity_streak.get(inst.id, 0),
                kv_cache_usage=m.kv_cache_usage,
            )
        except Exception:
            # Metrics recording should not fail the selection
            pass

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
            "running=%.0f waiting=%.0f kv=%.3f ewma=%.3f score=%.2f reason=%s "
            "wlc=%d slow_start=%.2f streak=%d latency=%.4fs "
            "session_key=%s session_key_source=%s "
            "pinned_session=%s prefix_key=%s",
            inst.id,
            req_class,
            profile.prompt_tokens,
            profile.max_tokens,
            m.num_running,
            m.num_waiting,
            m.kv_cache_usage,
            ewma_val,
            score,
            reason,
            self._wlc.get(inst.id),
            self._slow_start.get_weight(inst.id, time.monotonic()),
            self._affinity_streak.get(inst.id, 0),
            latency,
            profile.session_key,
            session_key_source,
            pinned_session_id,
            profile.prefix_key,
        )
