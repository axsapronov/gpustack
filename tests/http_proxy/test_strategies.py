"""Tests for SmartLoadBalancingStrategy and sub-components:
PeakEWMA, ConsistentHashRing, CircuitBreaker, SlowStart, WeightedConnections,
PoT scoring, admission control, affinity, and classification."""

import time
from unittest.mock import MagicMock, patch

import pytest

from gpustack.http_proxy.instance_metrics_cache import InstanceMetrics
from gpustack.http_proxy.strategies import (
    CircuitBreaker,
    classify_request,
    ConsistentHashRing,
    PeakEWMA,
    RequestProfile,
    SmartLoadBalancingStrategy,
    SlowStart,
    WeightedConnections,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_instance(inst_id: int) -> MagicMock:
    inst = MagicMock()
    inst.id = inst_id
    return inst


def _patch_metrics(metrics_map: dict[int, InstanceMetrics]) -> patch:
    """Patch get_metrics so that get_metrics(id) returns metrics_map[id]."""
    return patch(
        "gpustack.http_proxy.strategies.get_metrics",
        side_effect=lambda iid: metrics_map.get(iid, InstanceMetrics()),
    )


# ---------------------------------------------------------------------------
# classify_request
# ---------------------------------------------------------------------------


class TestClassifyRequest:
    def test_short(self):
        assert classify_request(100, 200) == "short"

    def test_medium_by_prompt(self):
        assert classify_request(12_000, 100) == "medium"

    def test_medium_by_total(self):
        assert classify_request(5_000, 15_001) == "medium"

    def test_heavy_by_prompt(self):
        assert classify_request(48_000, 100) == "heavy"

    def test_heavy_by_total(self):
        assert classify_request(30_000, 26_001) == "heavy"

    def test_boundary_short(self):
        assert classify_request(11_999, 8_000) == "short"

    def test_boundary_medium(self):
        assert classify_request(47_999, 8_000) == "medium"


# ---------------------------------------------------------------------------
# RequestProfile
# ---------------------------------------------------------------------------


class TestRequestProfile:
    def test_total_expected_tokens(self):
        p = RequestProfile(prompt_tokens=1000, max_tokens=500)
        assert p.total_expected_tokens == 1500

    def test_defaults_to_zero(self):
        p = RequestProfile()
        assert p.total_expected_tokens == 0


# ---------------------------------------------------------------------------
# PeakEWMA
# ---------------------------------------------------------------------------


class TestPeakEWMA:
    def test_initial_value(self):
        ewma = PeakEWMA()
        val = ewma.update(1, 0.5)
        assert val == 0.5
        assert ewma.get(1) == 0.5

    def test_rising_load_fast_reaction(self):
        ewma = PeakEWMA()
        ewma.update(1, 0.3)
        # Rise to 0.8 — alpha_rise=0.7, so ewma = 0.7*0.8 + 0.3*0.3 = 0.65
        val = ewma.update(1, 0.8)
        assert val == pytest.approx(0.65, abs=0.01)

    def test_falling_load_slow_decay(self):
        ewma = PeakEWMA()
        ewma.update(1, 0.8)
        # Fall to 0.2 — alpha_fall=0.3, so ewma = 0.3*0.2 + 0.7*0.8 = 0.62
        val = ewma.update(1, 0.2)
        assert val == pytest.approx(0.62, abs=0.01)

    def test_unknown_instance_returns_zero(self):
        ewma = PeakEWMA()
        assert ewma.get(999) == 0.0

    def test_clear(self):
        ewma = PeakEWMA()
        ewma.update(1, 0.5)
        ewma.clear(1)
        assert ewma.get(1) == 0.0


# ---------------------------------------------------------------------------
# ConsistentHashRing
# ---------------------------------------------------------------------------


class TestConsistentHashRing:
    def test_basic_lookup(self):
        ring = ConsistentHashRing(vnodes=50)
        ring.add(1)
        ring.add(2)

        # Same key should map to same instance
        result1 = ring.get("key:abc", [1, 2], lambda x: False)
        result2 = ring.get("key:abc", [1, 2], lambda x: False)
        assert result1 == result2
        assert result1 in (1, 2)

    def test_different_keys_different_instances(self):
        ring = ConsistentHashRing(vnodes=50)
        ring.add(1)
        ring.add(2)

        r1 = ring.get("key:a", [1, 2], lambda x: False)
        r2 = ring.get("key:b", [1, 2], lambda x: False)
        # Keys are different; instances may or may not differ, but both valid
        assert r1 in (1, 2)
        assert r2 in (1, 2)

    def test_skips_overloaded_instance(self):
        ring = ConsistentHashRing(vnodes=50)
        ring.add(1)
        ring.add(2)

        # Instance 1 is overloaded
        def is_overloaded(x):
            return x == 1

        result = ring.get("key:abc", [1, 2], is_overloaded)
        assert result == 2

    def test_returns_none_when_all_overloaded(self):
        ring = ConsistentHashRing(vnodes=50)
        ring.add(1)
        ring.add(2)

        result = ring.get("key:abc", [1, 2], lambda x: True)
        assert result is None

    def test_removes_instance(self):
        ring = ConsistentHashRing(vnodes=50)
        ring.add(1)
        ring.add(2)
        ring.remove(1)

        result = ring.get("key:abc", [2], lambda x: False)
        assert result == 2

    def test_empty_ring(self):
        ring = ConsistentHashRing(vnodes=50)
        assert ring.is_empty is True
        ring.add(1)
        assert ring.is_empty is False


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_initially_closed(self):
        cb = CircuitBreaker()
        assert cb.should_allow(1, time.monotonic()) is True

    def test_trip_opens_circuit(self):
        cb = CircuitBreaker()
        now = time.monotonic()
        cb.trip(1, now)
        assert cb.should_allow(1, now + 1) is False

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker()
        now = time.monotonic()
        cb.trip(1, now)

        # After timeout, should transition to HALF-OPEN and allow
        with patch("gpustack.http_proxy.strategies.envs") as mock_envs:
            mock_envs.LB_CIRCUIT_BREAKER_TIMEOUT = 2.0
            assert cb.should_allow(1, now + 3.0) is True
            assert cb._get_state(1) == "HALF-OPEN"

    def test_success_closes_circuit(self):
        cb = CircuitBreaker()
        now = time.monotonic()
        cb.trip(1, now)
        cb.record_success(1, now + 1)
        assert cb._get_state(1) == "CLOSED"
        assert cb.should_allow(1, now + 1) is True

    def test_failure_from_half_open_goes_back_to_open(self):
        cb = CircuitBreaker()
        now = time.monotonic()
        cb.trip(1, now)

        with patch("gpustack.http_proxy.strategies.envs") as mock_envs:
            mock_envs.LB_CIRCUIT_BREAKER_TIMEOUT = 2.0
            cb.should_allow(1, now + 3.0)  # transitions to HALF-OPEN
            cb.record_failure(1, now + 3.0)
            assert cb._get_state(1) == "OPEN"

    def test_clear(self):
        cb = CircuitBreaker()
        cb.trip(1, time.monotonic())
        cb.clear(1)
        assert cb._get_state(1) == "CLOSED"


# ---------------------------------------------------------------------------
# SlowStart
# ---------------------------------------------------------------------------


class TestSlowStart:
    def test_active_instance_returns_zero(self):
        ss = SlowStart()
        ss.mark_active(1)
        assert ss.get_weight(1, time.monotonic()) == 0.0

    def test_idle_instance_ramps_up(self):
        ss = SlowStart()
        now = time.monotonic()
        ss.mark_idle(1, now)

        # Halfway through window (15s default)
        weight = ss.get_weight(1, now + 7.5)
        assert weight == pytest.approx(0.5, abs=0.05)

    def test_fully_warmed_up(self):
        ss = SlowStart()
        now = time.monotonic()
        ss.mark_idle(1, now)

        # After full window
        weight = ss.get_weight(1, now + 20.0)
        assert weight == 1.0

    def test_mark_active_stops_tracking(self):
        ss = SlowStart()
        now = time.monotonic()
        ss.mark_idle(1, now)
        ss.mark_active(1)
        assert ss.get_weight(1, now + 20.0) == 0.0

    def test_re_mark_idle_resets(self):
        ss = SlowStart()
        now = time.monotonic()
        ss.mark_idle(1, now)
        ss.mark_active(1)
        ss.mark_idle(1, now + 10)

        # Should start from 0 again
        weight = ss.get_weight(1, now + 15)
        assert weight == pytest.approx(0.33, abs=0.1)


# ---------------------------------------------------------------------------
# WeightedConnections
# ---------------------------------------------------------------------------


class TestWeightedConnections:
    def test_add_weight(self):
        wc = WeightedConnections()
        wc.add(1, 5000)
        assert wc.get(1) == 5000

    def test_accumulates(self):
        wc = WeightedConnections()
        wc.add(1, 3000)
        wc.add(1, 2000)
        assert wc.get(1) == 5000

    def test_decay_on_running_drop(self):
        wc = WeightedConnections()
        wc.add(1, 10000)
        wc._last_running[1] = 4

        wc.decay(1, 2)  # dropped from 4 to 2
        assert wc.get(1) == 5000

    def test_zero_running_clears(self):
        wc = WeightedConnections()
        wc.add(1, 10000)
        wc._last_running[1] = 3

        wc.decay(1, 0)
        assert wc.get(1) == 0

    def test_no_decay_when_running_same(self):
        wc = WeightedConnections()
        wc.add(1, 10000)
        wc._last_running[1] = 2

        wc.decay(1, 2)
        assert wc.get(1) == 10000

    def test_clear(self):
        wc = WeightedConnections()
        wc.add(1, 5000)
        wc.clear(1)
        assert wc.get(1) == 0


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — single instance
# ---------------------------------------------------------------------------


class TestSingleInstance:
    @pytest.mark.asyncio
    async def test_single_instance_returns_directly(self):
        strategy = SmartLoadBalancingStrategy()
        inst = _make_instance(1)
        profile = RequestProfile(prompt_tokens=100)
        result = await strategy.select_instance([inst], profile)
        assert result is inst


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — admission control
# ---------------------------------------------------------------------------


class TestAdmissionControl:
    @pytest.mark.asyncio
    async def test_heavy_rejects_overloaded_replicas(self):
        strategy = SmartLoadBalancingStrategy()
        # inst1: running=2, kv=0.5 — inadmissible for heavy (max_running=1)
        # inst2: running=0, kv=0.3 — admissible
        metrics = {
            1: InstanceMetrics(num_running=2, num_waiting=0, kv_cache_usage=0.5),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=50_000, max_tokens=1_000)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_short_accepts_more_replicas(self):
        strategy = SmartLoadBalancingStrategy()
        # inst1: running=3, kv=0.80 — admissible for short (max_running=3, max_kv=0.85)
        # inst2: running=0, kv=0.3 — also admissible
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.80),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # PoT picks 2 random; both admissible. inst2 has lower score.
            # With only 2 candidates, PoT picks both and returns best.
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_soft_fallback_when_no_admissible(self):
        strategy = SmartLoadBalancingStrategy()
        # Both replicas overloaded for heavy (max_kv=0.45)
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=2, kv_cache_usage=0.88),
            2: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.50),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=50_000, max_tokens=1_000)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # inst2 passes soft fallback (running<=2, waiting==0, kv<0.60)
            # inst1 doesn't (running=3 > 2)
            assert result.id == 2


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — session affinity (soft preference)
# ---------------------------------------------------------------------------


class TestSessionAffinity:
    @pytest.mark.asyncio
    async def test_session_affinity_soft_preference(self):
        """Session affinity provides soft preference, not hard pin.

        When cluster is balanced and pinned instance has similar score,
        the pinned instance is preferred.
        """
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                session_key="session:abc123",
            )
            # First request: no affinity yet, binds to some instance
            await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            first_id = strategy._session_affinity.get("session:abc123")

            # Second request: affinity provides soft preference
            # Both instances have same metrics, so pinned should be preferred
            result2 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result2.id == first_id

    @pytest.mark.asyncio
    async def test_session_affinity_released_when_circuit_open(self):
        """Session affinity is broken when circuit breaker is OPEN."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc123")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # Trip circuit breaker for inst1
        strategy._circuit_breaker.trip(1, now)

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Session affinity broken because circuit is OPEN
            assert result.id != 1

    @pytest.mark.asyncio
    async def test_no_explicit_session_key_no_affinity(self):
        """When no explicit session key, session affinity is not applied.

        This is the key fix: user.id and api token should NOT create affinity.
        """
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            # No session_key at all
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Should select based on load (inst2 has lower kv), not affinity
            assert result.id == 2
            # No session affinity should be created
            assert not strategy._session_affinity


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — cluster balance detection
# ---------------------------------------------------------------------------


class TestClusterBalance:
    def test_balanced_cluster(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.35),
            ),
        ]
        assert strategy._is_cluster_balanced(pool) is True

    def test_imbalanced_by_queue(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.35),
            ),
        ]
        # queue_spread = 3 - 0 = 3 >= 2 (threshold)
        assert strategy._is_cluster_balanced(pool) is False

    def test_imbalanced_by_kv(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.2),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.5),
            ),
        ]
        # kv_spread = 0.5 - 0.2 = 0.3 >= 0.20 (threshold)
        assert strategy._is_cluster_balanced(pool) is False

    def test_single_instance_is_balanced(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=5, num_waiting=2, kv_cache_usage=0.9),
            ),
        ]
        assert strategy._is_cluster_balanced(pool) is True


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — affinity breaker
# ---------------------------------------------------------------------------


class TestAffinityBreaker:
    @pytest.mark.asyncio
    async def test_breaks_when_pinned_has_waiting(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=1, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        broken, reason = strategy._should_break_affinity(
            1, pool, "short", time.monotonic()
        )
        assert broken is True
        assert reason == "waiting"

    @pytest.mark.asyncio
    async def test_breaks_when_pinned_running_exceeds_limit(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=2, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        # heavy: max_running=1, pinned has 2
        broken, reason = strategy._should_break_affinity(
            1, pool, "heavy", time.monotonic()
        )
        assert broken is True
        assert reason == "running"

    @pytest.mark.asyncio
    async def test_breaks_when_pinned_kv_exceeds_limit(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.50),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        # heavy: max_kv=0.45, pinned has 0.50
        broken, reason = strategy._should_break_affinity(
            1, pool, "heavy", time.monotonic()
        )
        assert broken is True
        assert reason == "kv"

    @pytest.mark.asyncio
    async def test_does_not_break_when_pinned_is_best(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.4),
            ),
        ]
        broken, reason = strategy._should_break_affinity(
            1, pool, "short", time.monotonic()
        )
        assert broken is False
        assert reason == ""

    @pytest.mark.asyncio
    async def test_breaks_when_pinned_not_in_pool(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        broken, reason = strategy._should_break_affinity(
            1, pool, "short", time.monotonic()
        )
        assert broken is True
        assert reason == "pinned_not_in_pool"


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — imbalanced cluster behavior
# ---------------------------------------------------------------------------


class TestImbalancedCluster:
    @pytest.mark.asyncio
    async def test_imbalanced_cluster_ignores_session_affinity(self):
        """In imbalanced cluster, session affinity is ignored."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1 (which will be overloaded)
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # inst1 has high load, inst2 is clean -> imbalanced
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=1, kv_cache_usage=0.6),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Imbalanced cluster: affinity broken, load-aware selection wins
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_imbalanced_cluster_ignores_prefix_affinity(self):
        """In imbalanced cluster, prefix affinity is ignored."""
        strategy = SmartLoadBalancingStrategy()

        # inst1 overloaded, inst2 clean -> imbalanced
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=1, kv_cache_usage=0.7),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                prefix_key="prefix:abc123",
            )
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Imbalanced: load-aware wins
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_balanced_cluster_uses_prefix_affinity(self):
        """In balanced cluster, prefix affinity is used as soft preference."""
        strategy = SmartLoadBalancingStrategy()

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                prefix_key="prefix:abc123",
            )
            # First call: establishes mapping
            r1 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Second call: same prefix should prefer same instance
            r2 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert r1.id == r2.id


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — scoring (PoT)
# ---------------------------------------------------------------------------


class TestPoTScoring:
    @pytest.mark.asyncio
    async def test_lower_score_wins(self):
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
            2: InstanceMetrics(num_running=2, num_waiting=1, kv_cache_usage=0.7),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 1

    @pytest.mark.asyncio
    async def test_heavy_request_gets_higher_penalty_on_loaded_replica(self):
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.4),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=50_000, max_tokens=1_000)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 2


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — WLC affects scoring
# ---------------------------------------------------------------------------


class TestWLCScoring:
    @pytest.mark.asyncio
    async def test_wlc_affects_score(self):
        strategy = SmartLoadBalancingStrategy()
        strategy._wlc.add(1, 50000)
        strategy._wlc.add(2, 0)

        metrics = {
            1: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # inst2 has lower WLC, should win
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_wlc_accumulated_on_selection(self):
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=50000, max_tokens=1000)
            await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # WLC should have been added for the selected instance
            assert strategy._wlc.get(1) > 0 or strategy._wlc.get(2) > 0


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — Circuit Breaker integration
# ---------------------------------------------------------------------------


class TestCircuitBreakerIntegration:
    @pytest.mark.asyncio
    async def test_open_circuit_excludes_instance(self):
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()
        strategy._circuit_breaker.trip(1, now)

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_ewma_trips_circuit(self):
        strategy = SmartLoadBalancingStrategy()
        # Set EWMA above threshold
        strategy._ewma._ewma[1] = 0.90

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.90),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # After selection, inst1 EWMA >= threshold -> circuit trips
            assert strategy._circuit_breaker._get_state(1) == "OPEN"


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — Slow Start integration
# ---------------------------------------------------------------------------


class TestSlowStartIntegration:
    @pytest.mark.asyncio
    async def test_idle_instance_gets_slow_start_bonus(self):
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # inst2 has been idle for 20 seconds (fully warmed up)
        strategy._slow_start.mark_idle(2, now - 20)
        strategy._slow_start.mark_active(1)

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            # Both instances identical except slow start
            # PoT picks 2 candidates (only 2 exist), compares scores
            # inst2 has slow_start bonus, lower score
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 2


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — affinity for heavy (stricter)
# ---------------------------------------------------------------------------


class TestHeavyAffinity:
    @pytest.mark.asyncio
    async def test_heavy_rejects_affinity_when_running(self):
        strategy = SmartLoadBalancingStrategy()
        m = InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.2)
        assert strategy._affinity_allowed(m, "heavy") is False

    @pytest.mark.asyncio
    async def test_heavy_rejects_affinity_when_kv_high(self):
        strategy = SmartLoadBalancingStrategy()
        m = InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.40)
        assert strategy._affinity_allowed(m, "heavy") is False

    @pytest.mark.asyncio
    async def test_heavy_accepts_affinity_when_clean(self):
        strategy = SmartLoadBalancingStrategy()
        m = InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.35)
        assert strategy._affinity_allowed(m, "heavy") is True

    @pytest.mark.asyncio
    async def test_medium_affinity_kv_threshold(self):
        strategy = SmartLoadBalancingStrategy()
        m = InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.54)
        assert strategy._affinity_allowed(m, "medium") is True
        m2 = InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.55)
        assert strategy._affinity_allowed(m2, "medium") is False


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — clear_affinity
# ---------------------------------------------------------------------------


class TestClearAffinity:
    @pytest.mark.asyncio
    async def test_clear_session_affinity(self):
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()
        profile = RequestProfile(prompt_tokens=100, session_key="uid:42")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        assert strategy._session_affinity.get("uid:42") == 1
        await strategy.clear_affinity("uid:42")
        assert strategy._session_affinity.get("uid:42") is None


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — no instances
# ---------------------------------------------------------------------------


class TestNoInstances:
    @pytest.mark.asyncio
    async def test_raises_on_empty_list(self):
        strategy = SmartLoadBalancingStrategy()
        profile = RequestProfile(prompt_tokens=100)

        with pytest.raises(RuntimeError, match="No running instances"):
            await strategy.select_instance([], profile)


# ---------------------------------------------------------------------------
# Integration: PoT + CHWBL + Circuit Breaker
# ---------------------------------------------------------------------------


class TestIntegration:
    @pytest.mark.asyncio
    async def test_pot_with_circuit_breaker_and_ewma(self):
        """PoT selects from admissible pool; circuit breaker excludes; EWMA smooths."""
        strategy = SmartLoadBalancingStrategy()

        # Set EWMA for inst1 high (but below circuit breaker threshold)
        strategy._ewma._ewma[1] = 0.60
        strategy._ewma._ewma[2] = 0.20

        metrics = {
            1: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.5),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # inst2 has lower EWMA and no running requests — should win
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_chwbl_prefix_affinity_with_fallback(self):
        """CHWBL routes to correct instance; falls back if overloaded."""
        strategy = SmartLoadBalancingStrategy()

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                prefix_key="prefix:abc123",
            )
            # First call: establishes CHWBL mapping
            r1 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Second call: same prefix should map to same instance (via hash ring)
            r2 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert r1.id == r2.id

    @pytest.mark.asyncio
    async def test_full_flow_session_then_prefix(self):
        """Session affinity takes precedence over prefix affinity in balanced mode."""
        strategy = SmartLoadBalancingStrategy()

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            # First: session + prefix
            profile1 = RequestProfile(
                prompt_tokens=100,
                session_key="session:chat1",
                prefix_key="prefix:abc",
            )
            r1 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile1
            )

            # Second: same session, different prefix — session affinity wins
            profile2 = RequestProfile(
                prompt_tokens=100,
                session_key="session:chat1",
                prefix_key="prefix:xyz",
            )
            r2 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile2
            )
            # Session affinity (soft) should still keep same instance when balanced
            assert r1.id == r2.id


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — session affinity (soft preference)
# ---------------------------------------------------------------------------


class TestSessionAffinitySoft:
    @pytest.mark.asyncio
    async def test_session_affinity_soft_preference(self):
        """Session affinity provides soft preference, not hard pin.

        When cluster is balanced and pinned instance has similar score,
        the pinned instance is preferred.
        """
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                session_key="session:abc123",
            )
            # First request: no affinity yet, binds to some instance
            await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            first_id = strategy._session_affinity.get("session:abc123")

            # Second request: affinity provides soft preference
            # Both instances have same metrics, so pinned should be preferred
            result2 = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result2.id == first_id

    @pytest.mark.asyncio
    async def test_session_affinity_released_when_circuit_open(self):
        """Session affinity is broken when circuit breaker is OPEN."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc123")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # Trip circuit breaker for inst1
        strategy._circuit_breaker.trip(1, now)

        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Session affinity broken because circuit is OPEN
            assert result.id != 1

    @pytest.mark.asyncio
    async def test_no_explicit_session_key_no_affinity(self):
        """When no explicit session key, session affinity is not applied.

        This is the key fix: user.id and api token should NOT create affinity.
        """
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            # No session_key at all
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Should select based on load (inst2 has lower kv), not affinity
            assert result.id == 2
            # No session affinity should be created
            assert not strategy._session_affinity


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — cluster balance detection
# ---------------------------------------------------------------------------


class TestClusterBalanceV2:
    def test_balanced_cluster(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.35),
            ),
        ]
        assert strategy._is_cluster_balanced(pool) is True

    def test_imbalanced_by_queue(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.35),
            ),
        ]
        # queue_spread = 3 - 0 = 3 >= 2 (threshold)
        assert strategy._is_cluster_balanced(pool) is False

    def test_imbalanced_by_kv(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.6),
            ),
        ]
        # kv_spread = 0.6 - 0.3 = 0.3 >= 0.20 (threshold)
        assert strategy._is_cluster_balanced(pool) is False


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — affinity breaker
# ---------------------------------------------------------------------------


class TestAffinityBreakerV2:
    @pytest.mark.asyncio
    async def test_breaks_when_pinned_has_waiting(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=1, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        broken, reason = strategy._should_break_affinity(
            1, pool, "short", time.monotonic()
        )
        assert broken is True
        assert reason == "waiting"

    @pytest.mark.asyncio
    async def test_breaks_when_pinned_running_exceeds_limit(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=2, num_waiting=0, kv_cache_usage=0.3),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        # heavy: max_running=1, pinned has 2
        broken, reason = strategy._should_break_affinity(
            1, pool, "heavy", time.monotonic()
        )
        assert broken is True
        assert reason == "running"

    @pytest.mark.asyncio
    async def test_breaks_when_pinned_kv_exceeds_limit(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.50),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
            ),
        ]
        # heavy: max_kv=0.45, pinned has 0.50
        broken, reason = strategy._should_break_affinity(
            1, pool, "heavy", time.monotonic()
        )
        assert broken is True
        assert reason == "kv"

    @pytest.mark.asyncio
    async def test_does_not_break_when_pinned_is_best(self):
        strategy = SmartLoadBalancingStrategy()
        pool = [
            (
                _make_instance(1),
                InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
            ),
            (
                _make_instance(2),
                InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.4),
            ),
        ]
        broken, reason = strategy._should_break_affinity(
            1, pool, "short", time.monotonic()
        )
        assert broken is False


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — imbalanced cluster ignores affinity
# ---------------------------------------------------------------------------


class TestImbalancedClusterV2:
    @pytest.mark.asyncio
    async def test_imbalanced_cluster_ignores_session_affinity(self):
        """When cluster is imbalanced, session affinity is not used as preferred."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # inst1 has running=3, inst2 has running=0 -> imbalanced
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Imbalanced: affinity broken, load-aware selection
            # inst1 is inadmissible for short (running=3 is at limit but still admissible)
            # But affinity breaker will break due to running_delta
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_imbalanced_cluster_ignores_prefix_affinity(self):
        """When cluster is imbalanced, prefix affinity is not used."""
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.45),
        }
        # kv_spread = 0.35 >= 0.20 -> imbalanced
        with _patch_metrics(metrics):
            profile = RequestProfile(
                prompt_tokens=100,
                prefix_key="prefix:abc123",
            )
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Imbalanced: load-aware, inst2 has higher kv, inst1 wins
            assert result.id == 1

    @pytest.mark.asyncio
    async def test_session_affinity_soft_not_hard(self):
        """Session affinity is soft: pinned is only chosen if score is within ratio.

        When pinned instance is significantly worse than best candidate,
        affinity is broken and the better instance is selected.
        """
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # inst1 is significantly worse: running=3, kv=0.8
        # inst2 is clean: running=0, kv=0.1
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.8),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Affinity broken due to running > limit and kv > limit
            assert result.id == 2


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — _extract_session_key (routes/openai.py)
# ---------------------------------------------------------------------------


class TestExtractSessionKey:
    """Test that _extract_session_key does not use user/token as sticky key."""

    def test_returns_none_for_auth_user_only(self):
        """When only auth user is available (no explicit session ids), return None."""
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {}
        mock_user = MagicMock()
        mock_user.id = 42

        result = _extract_session_key(mock_request, None, mock_user)
        assert result is None

    def test_returns_none_for_body_user_only(self):
        """body_json['user'] field (OpenAI-compatible) is NOT used as session key."""
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"user": "some-user-id"}

        result = _extract_session_key(mock_request, body, None)
        assert result is None

    def test_uses_x_session_id_header(self):
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {"x-session-id": "sess-123"}

        result = _extract_session_key(mock_request, None, None)
        assert result == "session:sess-123"

    def test_uses_x_conversation_id_header(self):
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {"x-conversation-id": "conv-456"}

        result = _extract_session_key(mock_request, None, None)
        assert result == "conversation:conv-456"

    def test_uses_conversation_id_from_body(self):
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"conversation_id": "conv-789"}

        result = _extract_session_key(mock_request, body, None)
        assert result == "body:conversation_id:conv-789"

    def test_uses_thread_id_from_body(self):
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"thread_id": "thread-abc"}

        result = _extract_session_key(mock_request, body, None)
        assert result == "body:thread_id:thread-abc"

    def test_uses_x_project_id_as_fallback(self):
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {"x-project-id": "proj-alpha"}

        result = _extract_session_key(mock_request, None, None)
        assert result == "project:proj-alpha"

    def test_priority_order(self):
        """X-Session-Id takes priority over other headers."""
        from gpustack.routes.openai import _extract_session_key

        mock_request = MagicMock()
        mock_request.headers = {
            "x-session-id": "sess-123",
            "x-conversation-id": "conv-456",
            "x-project-id": "proj-alpha",
        }
        body = {"conversation_id": "conv-789"}

        result = _extract_session_key(mock_request, body, None)
        assert result == "session:sess-123"
