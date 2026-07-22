"""Tests for LBMetricsCollector Prometheus metrics."""

from gpustack.http_proxy.lb_metrics import LBMetricsCollector, _histogram_stats


class TestHistogramStats:
    def test_empty_list(self):
        count, total = _histogram_stats([])
        assert count == 0
        assert total == 0.0

    def test_single_value(self):
        count, total = _histogram_stats([100.0])
        assert count == 1
        assert total == 100.0

    def test_multiple_values(self):
        count, total = _histogram_stats([100.0, 200.0, 300.0])
        assert count == 3
        assert total == 600.0


class TestLBMetricsCollector:
    def _make_collector(self) -> LBMetricsCollector:
        return LBMetricsCollector()

    def test_record_selection(self):
        collector = self._make_collector()
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="pot_score",
            request_class="short",
            score=1.5,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )

        metrics = list(collector.collect())
        assert len(metrics) > 0

    def test_record_instance_state(self):
        collector = self._make_collector()
        collector.record_instance_state(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            score=1.5,
            ewma_kv=0.3,
            wlc_weight=1000.0,
            slow_start_weight=0.5,
            affinity_streak=3,
            kv_cache_usage=0.25,
        )

        metrics = list(collector.collect())
        assert len(metrics) > 0

    def test_record_pool_size(self):
        collector = self._make_collector()
        collector.record_pool_size("1", "llama3", 4)

        metrics = list(collector.collect())
        assert len(metrics) > 0

    def test_record_streak_reset(self):
        collector = self._make_collector()
        collector.record_streak_reset("1", "llama3", "42")

        metrics = list(collector.collect())
        assert len(metrics) > 0

    def test_collect_yields_all_metric_families(self):
        collector = self._make_collector()
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="pot_score",
            request_class="short",
            score=1.5,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )
        collector.record_instance_state(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            score=1.5,
            ewma_kv=0.3,
            wlc_weight=1000.0,
            slow_start_weight=0.5,
            affinity_streak=3,
            kv_cache_usage=0.25,
        )
        collector.record_pool_size("1", "llama3", 4)

        metrics = list(collector.collect())
        # We expect: selections, requests, streak_resets,
        # 6 instance gauges, pool_size, 4 histograms = 14 families
        assert len(metrics) == 14

    def test_multiple_selections_accumulate(self):
        collector = self._make_collector()
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="pot_score",
            request_class="short",
            score=1.0,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="pot_score",
            request_class="short",
            score=1.0,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="affinity_soft",
            request_class="short",
            score=1.0,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )

        # Check that the counter accumulated correctly
        metrics = list(collector.collect())
        # First metric family is selections
        selections = metrics[0]
        # Find the pot_score entry
        for sample in selections.samples:
            if sample.labels["reason"] == "pot_score":
                assert sample.value == 2
            elif sample.labels["reason"] == "affinity_soft":
                assert sample.value == 1

    def test_different_models_are_separate(self):
        collector = self._make_collector()
        collector.record_selection(
            model_id="1",
            model_name="llama3",
            instance_id="42",
            reason="pot_score",
            request_class="short",
            score=1.0,
            prompt_tokens=100,
            max_tokens=50,
            latency=0.001,
        )
        collector.record_selection(
            model_id="2",
            model_name="gpt2",
            instance_id="99",
            reason="pot_score",
            request_class="heavy",
            score=2.0,
            prompt_tokens=50000,
            max_tokens=1000,
            latency=0.002,
        )

        metrics = list(collector.collect())
        selections = metrics[0]
        values = [s.value for s in selections.samples]
        # Each (model_id, model_name, instance_id, reason) combo has count=1
        assert values == [1, 1]
