import asyncio
import logging
import time
from typing import Dict, Optional
from dataclasses import dataclass, field

import aiohttp
from prometheus_client.parser import text_string_to_metric_families

from gpustack import envs
from gpustack.schemas.models import BackendEnum, ModelInstance

logger = logging.getLogger(__name__)

# Backend'ы которые поддерживают /metrics на API порту
METRICS_SUPPORTED_BACKENDS = {
    BackendEnum.VLLM.value,
    BackendEnum.SGLANG.value,
}

# Сколько секунд не пытаться запрашивать метрики после ошибки
_METRICS_ERROR_COOLDOWN = envs.LB_METRICS_REFRESH_INTERVAL * 3


@dataclass
class InstanceMetrics:
    num_running: float = 0.0
    num_waiting: float = 0.0
    kv_cache_usage: float = 0.0  # 0.0 – 1.0
    updated_at: float = field(default_factory=time.monotonic)

    def is_stale(self) -> bool:
        return (time.monotonic() - self.updated_at) > envs.LB_METRICS_STALE_THRESHOLD


# Глобальный кэш: instance_id -> InstanceMetrics
_cache: Dict[int, InstanceMetrics] = {}
_lock = asyncio.Lock()
_bg_task: Optional[asyncio.Task] = None

# Кэш "отравленных" инстансов: instance_id -> monotonic timestamp когда expires
_poisoned: Dict[int, float] = {}


def get_metrics(instance_id: int) -> InstanceMetrics:
    """Возвращает закешированные метрики. Если устарели — возвращает нули (безопасный fallback)."""
    m = _cache.get(instance_id)
    if m is None:
        logger.debug(
            "[metrics_cache] instance=%d no cached metrics (returning zeros)",
            instance_id,
        )
        return InstanceMetrics()
    if m.is_stale():
        logger.warning(
            "[metrics_cache] instance=%d metrics stale (age=%.1fs > %.1fs threshold, returning zeros)",
            instance_id,
            time.monotonic() - m.updated_at,
            envs.LB_METRICS_STALE_THRESHOLD,
        )
        return InstanceMetrics()
    return m


def _get_serving_port(instance: ModelInstance) -> Optional[int]:
    """Получить порт API инстанса. Приоритет: ports[0] > port."""
    if instance.ports:
        return instance.ports[0]
    return instance.port


def _should_skip_instance(instance: ModelInstance, now: float) -> Optional[str]:
    """
    Проверить стоит ли пропускать данный инстанс.
    Возвращает причину пропуска или None если можно запрашивать.
    """
    # Проверить poison-кэш: если инстанс недавно возвращал ошибку, пропустить
    expires_at = _poisoned.get(instance.id)
    if expires_at and now < expires_at:
        return "poisoned"

    # Проверить backend: только vLLM и SGLang гарантированно имеют /metrics
    backend = instance.backend
    if backend and backend not in METRICS_SUPPORTED_BACKENDS:
        return f"unsupported_backend({backend})"

    # Проверить наличие порта
    port = _get_serving_port(instance)
    if not instance.worker_ip or not port:
        return "no_host_or_port"

    return None


async def _fetch_instance_metrics(
    session: aiohttp.ClientSession,
    instance: ModelInstance,
) -> None:
    now = time.monotonic()

    skip_reason = _should_skip_instance(instance, now)
    if skip_reason:
        logger.debug(
            "[metrics_cache] instance=%d skipped (%s)",
            instance.id,
            skip_reason,
        )
        return

    host = instance.worker_ip
    port = _get_serving_port(instance)

    url = f"http://{host}:{port}/metrics"
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=envs.LB_METRICS_FETCH_TIMEOUT)
        ) as resp:
            if resp.status != 200:
                # Запомнить инстанс как "отравленный" на время cooldown
                _poisoned[instance.id] = now + _METRICS_ERROR_COOLDOWN
                logger.info(
                    "[metrics_cache] instance=%d at %s:%d HTTP %d "
                    "(poisoned for %.0fs, backend=%s)",
                    instance.id,
                    host,
                    port,
                    resp.status,
                    _METRICS_ERROR_COOLDOWN,
                    instance.backend,
                )
                return

            # Успешный ответ — очистить poison-статус
            _poisoned.pop(instance.id, None)

            text = await resp.text()
            running = waiting = kv = 0.0
            found_running = False
            found_waiting = False
            found_kv = False
            for family in text_string_to_metric_families(text):
                name = family.name
                for sample in family.samples:
                    if name in (
                        "vllm:num_requests_running",
                        "gpustack:num_requests_running",
                    ):
                        running = sample.value
                        found_running = True
                    elif name in (
                        "vllm:num_requests_waiting",
                        "gpustack:num_requests_waiting",
                    ):
                        waiting = sample.value
                        found_waiting = True
                    elif name in (
                        "vllm:gpu_cache_usage_perc",
                        "vllm:kv_cache_usage_perc",
                        "gpustack:kv_cache_usage_ratio",
                    ):
                        kv = sample.value
                        found_kv = True
            async with _lock:
                _cache[instance.id] = InstanceMetrics(
                    num_running=running,
                    num_waiting=waiting,
                    kv_cache_usage=kv,
                    updated_at=time.monotonic(),
                )
            logger.debug(
                "[metrics_cache] instance=%d running=%.0f waiting=%.0f kv=%.3f "
                "(found_running=%s found_waiting=%s found_kv=%s)",
                instance.id,
                running,
                waiting,
                kv,
                found_running,
                found_waiting,
                found_kv,
            )
    except Exception as e:
        # Ошибка сети — тоже помечаем как poisoned
        _poisoned[instance.id] = now + _METRICS_ERROR_COOLDOWN
        logger.warning(
            "[metrics_cache] instance=%d at %s:%d error: %s (poisoned for %.0fs)",
            instance.id,
            host,
            port,
            e,
            _METRICS_ERROR_COOLDOWN,
        )


async def _refresh_loop(get_instances_fn) -> None:
    """Фоновая корутина, которая периодически обновляет метрики всех running инстансов."""
    connector = aiohttp.TCPConnector(limit=32)
    async with aiohttp.ClientSession(connector=connector) as session:
        while True:
            try:
                instances: list[ModelInstance] = await get_instances_fn()
                tasks = [_fetch_instance_metrics(session, inst) for inst in instances]
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.warning(f"[metrics_cache] refresh loop error: {e}")
            await asyncio.sleep(envs.LB_METRICS_REFRESH_INTERVAL)


def start_metrics_cache(get_instances_fn) -> asyncio.Task:
    """Запускает фоновый таск. Вызывать один раз при старте приложения."""
    global _bg_task
    _bg_task = asyncio.create_task(_refresh_loop(get_instances_fn))
    return _bg_task


def stop_metrics_cache() -> None:
    """Останавливает фоновый таск при завершении приложения."""
    global _bg_task
    if _bg_task:
        _bg_task.cancel()
        _bg_task = None
