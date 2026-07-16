from typing import List, Optional

from gpustack.http_proxy.strategies import (
    LoadBalancingStrategy,
    RequestProfile,
    SmartLoadBalancingStrategy,
)
from gpustack.schemas.models import ModelInstance


class LoadBalancer:
    def __init__(self, strategy: LoadBalancingStrategy = None):
        if strategy is None:
            strategy = SmartLoadBalancingStrategy()
        self._strategy = strategy

    def set_strategy(self, strategy: LoadBalancingStrategy):
        self._strategy = strategy

    async def get_instance(
        self,
        instances: List[ModelInstance],
        prompt_tokens: int = 0,
        max_tokens: int = 0,
        session_key: Optional[str] = None,
        prefix_key: Optional[str] = None,
    ) -> ModelInstance:
        profile = RequestProfile(
            prompt_tokens=prompt_tokens,
            max_tokens=max_tokens,
            session_key=session_key,
            prefix_key=prefix_key,
        )
        return await self._strategy.select_instance(instances, profile)
