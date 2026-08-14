"""Thin adapter for gpustack-lb integration.

Delegates all integration logic to gpustack_lb.GPUStackIntegration.
Falls back to RoundRobin via LoadBalancer if gpustack-lb is not installed.
"""

import importlib.util
import logging

from gpustack.schemas.models import ModelInstance

logger = logging.getLogger(__name__)

# Check if gpustack-lb is available
_GPUSTACK_LB_AVAILABLE = importlib.util.find_spec("gpustack_lb") is not None

# Integration instance (only created if gpustack-lb is available)
_integration = None
if _GPUSTACK_LB_AVAILABLE:
    from gpustack_lb import GPUStackIntegration

    _integration = GPUStackIntegration()


def is_gpustack_lb_available() -> bool:
    """Check if gpustack-lb package is installed."""
    return _GPUSTACK_LB_AVAILABLE


async def select_instance(
    model_id: int,
    model_name: str,
    request_body: dict,
    headers: dict,
    available_instances: list[ModelInstance],
) -> ModelInstance:
    """Select the best instance for the request.

    Uses gpustack-lb GPUStackIntegration if available, otherwise falls back
    to RoundRobin via LoadBalancer.
    """
    if _integration:
        return await _integration.select_instance(
            model_id=model_id,
            model_name=model_name,
            request_body=request_body,
            headers=headers,
            available_instances=available_instances,
        )

    # Fallback to RoundRobin
    from gpustack.http_proxy.load_balancer import LoadBalancer

    lb = LoadBalancer()
    return await lb.get_instance(available_instances)


def start_lb_metrics_cache(get_instances_fn):
    """Start the background metrics cache task.

    Call once at application startup. Returns None if gpustack-lb is not available.
    """
    if not _GPUSTACK_LB_AVAILABLE:
        logger.info(
            "gpustack-lb is not installed. "
            "Load balancing will use RoundRobin fallback strategy."
        )
        return None

    if _integration:
        _integration.start_metrics_cache(get_instances_fn)


def stop_lb_metrics_cache():
    """Stop the background metrics cache task.

    Call at application shutdown.
    """
    if _integration:
        _integration.stop_metrics_cache()
