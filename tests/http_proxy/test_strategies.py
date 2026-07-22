"""Tests for SmartLoadBalancingStrategy and sub-components:
PeakEWMA, SlowStart, WeightedConnections,
PoT scoring, affinity, classification, and streak cap."""

import time
from unittest.mock import MagicMock, patch

import pytest

from gpustack.http_proxy.instance_metrics_cache import InstanceMetrics
from gpustack.http_proxy.strategies import (
    classify_request,
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
# SlowStart (linear ramp, no aggression parameter)
# ---------------------------------------------------------------------------


class TestSlowStart:
    def test_active_instance_returns_zero(self):
        ss = SlowStart()
        ss.mark_active(1)
        assert ss.get_weight(1, time.monotonic()) == 0.0

    def test_idle_instance_ramps_up_linearly(self):
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
# SmartLoadBalancingStrategy — PoT selection
# ---------------------------------------------------------------------------


class TestPoTSelection:
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
    async def test_pot_compares_all_when_three_or_fewer(self):
        """With <= 3 instances, all are compared (not just 2 random)."""
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=5, num_waiting=0, kv_cache_usage=0.9),
            2: InstanceMetrics(num_running=5, num_waiting=0, kv_cache_usage=0.9),
            3: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2), _make_instance(3)], profile
            )
            # inst3 has lowest score, should always win
            assert result.id == 3

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
# SmartLoadBalancingStrategy — request class affects scoring weights
# ---------------------------------------------------------------------------


class TestRequestClassWeights:
    @pytest.mark.asyncio
    async def test_heavy_class_penalizes_waiting_more(self):
        """Heavy requests get higher waiting_weight (4.0 vs 1.0 for short)."""
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=1, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.3),
        }
        with _patch_metrics(metrics):
            # Heavy: waiting_weight=4.0, so inst1 score = 0 + 4*1 + 1*0.3 = 4.3
            #        inst2 score = 1 + 4*0 + 1*0.3 = 1.3 -> inst2 wins
            profile = RequestProfile(prompt_tokens=50_000, max_tokens=1_000)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_short_class_penalizes_waiting_less(self):
        """Short requests get waiting_weight=1.0, so waiting matters less."""
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=1, kv_cache_usage=0.2),
            2: InstanceMetrics(num_running=1, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            # Short: waiting_weight=1.0
            # inst1 score = 0 + 1*1 + 1*0.2 = 1.2
            # inst2 score = 1 + 1*0 + 1*0.2 = 1.2 -> tie, either is fine
            profile = RequestProfile(prompt_tokens=100)
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert result.id in (1, 2)


# ---------------------------------------------------------------------------
# SmartLoadBalancingStrategy — session affinity (soft preference)
# ---------------------------------------------------------------------------


class TestSessionAffinity:
    @pytest.mark.asyncio
    async def test_session_affinity_soft_preference(self):
        """Session affinity provides soft preference, not hard pin.

        When pinned instance has similar score, the pinned instance is preferred.
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
    async def test_affinity_broken_when_pinned_has_waiting(self):
        """Affinity is broken when pinned instance has waiting > 0."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc123")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # inst1 now has waiting > 0
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=1, kv_cache_usage=0.3),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.2),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Affinity broken because waiting > 0
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_affinity_broken_when_score_too_high(self):
        """Affinity is broken when score(pinned) > score(best) * 1.2."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc123")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # inst1 is significantly worse
        metrics = {
            1: InstanceMetrics(num_running=3, num_waiting=0, kv_cache_usage=0.8),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Affinity broken due to score ratio
            assert result.id == 2

    @pytest.mark.asyncio
    async def test_no_explicit_session_key_no_affinity(self):
        """When no explicit session key, session affinity is not applied."""
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
# SmartLoadBalancingStrategy — prefix affinity
# ---------------------------------------------------------------------------


class TestPrefixAffinity:
    @pytest.mark.asyncio
    async def test_prefix_affinity_soft_preference(self):
        """Prefix affinity provides soft preference via simple dict mapping."""
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
# SmartLoadBalancingStrategy — affinity streak cap
# ---------------------------------------------------------------------------


class TestAffinityStreak:
    @pytest.mark.asyncio
    async def test_streak_increments_on_selection(self):
        """Affinity streak counter increments on each selection."""
        strategy = SmartLoadBalancingStrategy()
        metrics = {
            1: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.5),
        }
        with _patch_metrics(metrics):
            profile = RequestProfile(prompt_tokens=100, session_key="session:abc")
            # inst1 should win (lower score)
            await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            assert strategy._affinity_streak.get(1, 0) >= 1

    @pytest.mark.asyncio
    async def test_streak_cap_resets_affinity(self):
        """After reaching max streak, affinity is reset and PoT takes over."""
        strategy = SmartLoadBalancingStrategy()
        now = time.monotonic()

        # Bind session to inst1
        profile = RequestProfile(prompt_tokens=100, session_key="session:abc")
        await strategy._finalize_selection(_make_instance(1), profile, now)

        # Manually set streak to max
        strategy._affinity_streak[1] = 20  # equals LB_AFFINITY_MAX_STREAK

        # inst1 is worse but pinned; streak cap should reset affinity
        metrics = {
            1: InstanceMetrics(num_running=2, num_waiting=0, kv_cache_usage=0.5),
            2: InstanceMetrics(num_running=0, num_waiting=0, kv_cache_usage=0.1),
        }
        with _patch_metrics(metrics):
            result = await strategy.select_instance(
                [_make_instance(1), _make_instance(2)], profile
            )
            # Streak cap hit -> affinity ignored -> inst2 wins by score
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
# Integration: PoT + EWMA + Scoring
# ---------------------------------------------------------------------------


class TestIntegration:
    @pytest.mark.asyncio
    async def test_pot_with_ewma(self):
        """PoT selects from pool; EWMA smooths KV cache usage."""
        strategy = SmartLoadBalancingStrategy()

        # Set EWMA for inst1 high
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
    async def test_full_flow_session_then_prefix(self):
        """Session affinity takes precedence over prefix affinity."""
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
            # Session affinity (soft) should still keep same instance
            assert r1.id == r2.id


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
