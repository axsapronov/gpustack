from abc import ABC, abstractmethod
import asyncio
import logging
import math
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

# Cooldown / hysteresis
_COOLDOWN_KV_HIGH = envs.LB_COOLDOWN_KV_HIGH
_COOLDOWN_KV_LOW = envs.LB_COOLDOWN_KV_LOW
_COOLDOWN_DURATION = envs.LB_COOLDOWN_DURATION

# Idle bonus constants
_IDLE_THRESHOLD = envs.LB_IDLE_BONUS_THRESHOLD
_IDLE_PER_SEC = envs.LB_IDLE_BONUS_PER_SECOND
_IDLE_MAX_HEAVY = envs.LB_IDLE_BONUS_MAX_HEAVY
_IDLE_MAX_MEDIUM = envs.LB_IDLE_BONUS_MAX_MEDIUM
_IDLE_MAX_SHORT = envs.LB_IDLE_BONUS_MAX_SHORT
_IDLE_KV_THRESHOLD = envs.LB_IDLE_KV_THRESHOLD


def classify_request(prompt_tokens: int, max_tokens: int) -> str:
    total = prompt_tokens + max_tokens
    if prompt_tokens >= _PROMPT_HEAVY or total >= _TOTAL_HEAVY:
        return "heavy"
    if prompt_tokens >= _PROMPT_MEDIUM or total >= _TOTAL_MEDIUM:
        return "medium"
    return "short"


# ---------------------------------------------------------------------------
# Smart Strategy
# ---------------------------------------------------------------------------


class SmartLoadBalancingStrategy(LoadBalancingStrategy):
    """
    Стратегия с admission control, request classification,
    health-aware affinity и cooldown/hysteresis.
    """

    def __init__(self) -> None:
        # session_key / prefix_key -> instance_id
        self._affinity: Dict[str, int] = {}
        # instance_id -> monotonic timestamp до которого инстанс "горячий"
        self._hot_until: Dict[int, float] = {}
        # instance_id -> monotonic timestamp последней активности
        self._last_activity: Dict[int, float] = {}
        # instance_id -> сумма total_expected_tokens по всем running запросам
        self._inflight_tokens: Dict[int, int] = {}
        # instance_id -> num_running на момент последнего select_instance
        # используется для decay inflight_tokens
        self._last_running_count: Dict[int, int] = {}
        # Burst mode state
        self._burst_active: bool = False
        self._burst_until: float = 0.0
        # Adaptive headroom multiplier
        self._headroom_multiplier: float = 1.0
        self._last_headroom_update: float = 0.0
        self._lock = asyncio.Lock()

    async def select_instance(
        self,
        instances: List[ModelInstance],
        profile: RequestProfile,
    ) -> ModelInstance:  # noqa: C901, R0914
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

        # 0) Обновить headroom multiplier и burst mode
        self._update_headroom(instances, now)
        self._update_burst_mode(instances, now)

        # Лог: классификация запроса
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

        # Лог: метрики всех инстансов
        for inst in instances:
            m = get_metrics(inst.id)
            idle_s = now - self._last_activity.get(inst.id, now)
            logger.debug(
                "[smart_lb] inst=%d running=%.0f waiting=%.0f kv=%.3f idle=%.1fs "
                "stale=%s",
                inst.id,
                m.num_running,
                m.num_waiting,
                m.kv_cache_usage,
                idle_s,
                m.is_stale(),
            )

        # 1) Health-aware affinity (soft: помечаем pinned, но не возвращаем сразу)
        pinned = self._get_affinity_candidate(instances, profile, req_class, now)
        pinned_id: Optional[int] = pinned.id if pinned else None
        if pinned is not None:
            m = get_metrics(pinned.id)
            logger.debug(
                "[smart_lb] affinity hit inst=%d (running=%.0f waiting=%.0f kv=%.3f)",
                pinned.id,
                m.num_running,
                m.num_waiting,
                m.kv_cache_usage,
            )

        # 2) Admission control: разделить на admissible и fallback
        admissible: List[Tuple[ModelInstance, InstanceMetrics]] = []
        fallback: List[Tuple[ModelInstance, InstanceMetrics, str]] = []

        for inst in instances:
            m = get_metrics(inst.id)

            # Обновить last_activity: если реплика занята — считать её активной
            if m.num_running > 0 or m.num_waiting > 0:
                self._last_activity[inst.id] = now

            # Decay inflight tokens когда num_running упал
            self._decay_inflight_tokens(inst.id, m.num_running)

            if self._is_hot(inst.id, m.kv_cache_usage, now):
                logger.debug(
                    "[smart_lb] inst=%d excluded (hot, kv=%.3f >= %.2f)",
                    inst.id,
                    m.kv_cache_usage,
                    _COOLDOWN_KV_HIGH,
                )
                fallback.append((inst, m, "hot"))
                continue

            if self._admissible(m, req_class):
                admissible.append((inst, m))
            elif inst.id == pinned_id and self._affinity_allowed(m, req_class):
                # Soft affinity: pinned инстанс не прошёл admission, но проходит
                # более мягкие пороги affinity — добавляем в fallback с пометкой
                logger.debug(
                    "[smart_lb] inst=%d affinity-fallback for %s "
                    "(inadmissible but affinity_allowed)",
                    inst.id,
                    req_class,
                )
                fallback.append((inst, m, "affinity_fallback"))
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

        # 3) Выбрать из admissible; если пусто — soft fallback
        pool = (
            admissible if admissible else self._soft_fallback_pool(fallback, req_class)
        )

        if not admissible:
            logger.warning(
                "[smart_lb] no admissible replicas, using soft fallback (%d candidates)",
                len(pool),
            )

        best: Optional[ModelInstance] = None
        best_score = math.inf
        best_reason = "score"

        for inst, m in pool:
            affinity_bonus = self._affinity_bonus(inst.id, profile)
            idle_bonus = self._idle_bonus(inst.id, m, req_class, now)

            # Soft affinity: pinned инстанс получает дополнительный rebalance бонус.
            # Это удерживает сессию на одной реплике при умеренной нагрузке,
            # но позволяет idle-реплике перебить affinity при большом дисбалансе.
            rebalance_bonus = 0.0
            if inst.id == pinned_id:
                rebalance_bonus = envs.LB_SOFT_AFFINITY_REBALANCE_BONUS

            # Idle bonus не перебивает affinity: ограничить до 75% от affinity bonus
            if affinity_bonus > 0:
                idle_bonus = min(idle_bonus, affinity_bonus * 0.75)
            s = self._score(
                inst.id, m, req_class, affinity_bonus + rebalance_bonus, idle_bonus
            )

            logger.debug(
                "[smart_lb] score inst=%d final=%.2f "
                "(raw=%.2f affinity_bonus=%.2f rebalance_bonus=%.2f idle_bonus=%.2f)",
                inst.id,
                s,
                s + affinity_bonus + rebalance_bonus + idle_bonus,
                affinity_bonus,
                rebalance_bonus,
                idle_bonus,
            )

            if s < best_score:
                best = inst
                best_score = s
                if inst.id == pinned_id:
                    best_reason = "affinity"
                elif inst in [a[0] for a in admissible]:
                    best_reason = "admissible"
                else:
                    best_reason = "fallback"

        if best is None:
            raise RuntimeError("No suitable instance after admission control")

        # 4) Зафиксировать affinity и активность
        await self._bind_affinity(best.id, profile, now)

        m = get_metrics(best.id)
        self._log_selection(best, req_class, profile, m, best_score, best_reason)
        return best

    # ---- admission ----

    def _admissible(self, m: InstanceMetrics, req_class: str) -> bool:
        lim = LIMITS[req_class]

        # Применить headroom multiplier к KV-порогу
        max_kv = lim.max_kv * self._headroom_multiplier

        # Применить burst mode для short-запросов
        if self._burst_active and req_class == "short":
            max_kv = max_kv * envs.LB_BURST_KV_MULTIPLIER
            max_running = lim.max_running + envs.LB_BURST_EXTRA_RUNNING
        else:
            max_running = lim.max_running

        return (
            m.num_running <= max_running
            and m.num_waiting <= lim.max_waiting
            and m.kv_cache_usage <= max_kv
        )

    def _soft_fallback_pool(
        self,
        fallback: List[Tuple[ModelInstance, InstanceMetrics, str]],
        req_class: str,
    ) -> List[Tuple[ModelInstance, InstanceMetrics]]:
        ranked: List[Tuple[ModelInstance, InstanceMetrics]] = []
        for inst, m, reason in fallback:
            # affinity_fallback уже прошёл _affinity_allowed — пропускаем без проверок
            if reason == "affinity_fallback":
                ranked.append((inst, m))
                continue

            if req_class == "heavy":
                # Fallback: чуть выше admission, но не выше 0.60
                if (
                    m.num_waiting == 0
                    and m.kv_cache_usage < 0.60
                    and m.num_running <= 2
                ):
                    ranked.append((inst, m))
            elif req_class == "medium":
                # Fallback: чуть выше admission (0.60 -> 0.75)
                if m.num_waiting <= 1 and m.kv_cache_usage < 0.75:
                    ranked.append((inst, m))
            else:
                # short — принимаем всё
                ranked.append((inst, m))
        return ranked

    # ---- scoring ----

    def _score(
        self,
        instance_id: int,
        m: InstanceMetrics,
        req_class: str,
        affinity_bonus: float,
        idle_bonus: float,
    ) -> float:
        score = (
            envs.LB_WEIGHT_RUNNING * m.num_running
            + envs.LB_WEIGHT_WAITING * m.num_waiting
            + envs.LB_WEIGHT_KV * m.kv_cache_usage
        )

        if req_class == "medium":
            score += (
                envs.LB_MEDIUM_EXTRA_RUNNING * m.num_running
                + envs.LB_MEDIUM_EXTRA_KV * m.kv_cache_usage
            )
        elif req_class == "heavy":
            score += (
                envs.LB_HEAVY_EXTRA_RUNNING * m.num_running
                + envs.LB_HEAVY_EXTRA_KV * m.kv_cache_usage
            )

        # In-flight tokens penalty
        inflight = self._inflight_tokens.get(instance_id, 0)
        score += envs.LB_WEIGHT_INFLIGHT_TOKENS * inflight

        return score - affinity_bonus - idle_bonus

    def _affinity_bonus(self, instance_id: int, profile: RequestProfile) -> float:
        bonus = 0.0
        if (
            profile.session_key
            and self._affinity.get(profile.session_key) == instance_id
        ):
            bonus += envs.LB_AFFINITY_SESSION_BONUS
        if profile.prefix_key and self._affinity.get(profile.prefix_key) == instance_id:
            bonus += envs.LB_AFFINITY_PREFIX_BONUS
        return bonus

    # ---- idle bonus ----

    def _idle_bonus(
        self, instance_id: int, m: InstanceMetrics, req_class: str, now: float
    ) -> float:
        """
        Вычислить idle bonus для реплики.

        Применяется только если реплика "чистая" (running=0, waiting=0, kv<0.25).
        Инициализация last_activity — с now (не с 0), чтобы избежать ложного бонуса
        после рестарта балансера.
        """
        # Инициализировать с now, если впервые видим эту реплику
        last = self._last_activity.setdefault(instance_id, now)
        idle_seconds = now - last

        if idle_seconds < _IDLE_THRESHOLD:
            return 0.0

        # Только для "чистых" реплик
        if not (
            m.num_running == 0
            and m.num_waiting == 0
            and m.kv_cache_usage < _IDLE_KV_THRESHOLD
        ):
            return 0.0

        # Вычислить базовый бонус
        bonus = idle_seconds * _IDLE_PER_SEC

        # Ограничить по классу запроса
        if req_class == "heavy":
            bonus = min(bonus, _IDLE_MAX_HEAVY)
        elif req_class == "medium":
            bonus = min(bonus, _IDLE_MAX_MEDIUM)
        else:
            bonus = min(bonus, _IDLE_MAX_SHORT)

        return bonus

    # ---- affinity ----

    def _get_affinity_candidate(
        self,
        instances: List[ModelInstance],
        profile: RequestProfile,
        req_class: str,
        now: float,
    ) -> Optional[ModelInstance]:
        keys = [k for k in [profile.session_key, profile.prefix_key] if k]
        if not keys:
            return None

        ids = {inst.id: inst for inst in instances}
        for key in keys:
            pinned_id = self._affinity.get(key)
            if not pinned_id or pinned_id not in ids:
                continue

            inst = ids[pinned_id]
            m = get_metrics(inst.id)

            if self._is_hot(inst.id, m.kv_cache_usage, now):
                continue
            if self._affinity_allowed(m, req_class):
                return inst

        return None

    def _affinity_allowed(self, m: InstanceMetrics, req_class: str) -> bool:
        if req_class == "heavy":
            return m.num_running == 0 and m.num_waiting == 0 and m.kv_cache_usage < 0.40
        if req_class == "medium":
            return m.num_running < 3 and m.num_waiting <= 1 and m.kv_cache_usage < 0.55
        return m.kv_cache_usage < 0.90

    # ---- cooldown / hysteresis ----

    def _is_hot(self, instance_id: int, kv_usage: float, now: float) -> bool:
        hot_until = self._hot_until.get(instance_id)
        if kv_usage >= _COOLDOWN_KV_HIGH:
            self._hot_until[instance_id] = now + _COOLDOWN_DURATION
            return True
        if hot_until and now < hot_until:
            return kv_usage > _COOLDOWN_KV_LOW
        return False

    # ---- inflight tokens decay ----

    def _decay_inflight_tokens(self, instance_id: int, current_running: float) -> None:
        """
        Когда num_running упал, пропорционально уменьшить inflight_tokens.
        Если num_running стал 0, сбросить полностью.
        """
        prev = self._last_running_count.get(instance_id)
        if prev is None:
            self._last_running_count[instance_id] = int(current_running)
            return

        if current_running == 0:
            self._inflight_tokens[instance_id] = 0
        elif prev > 0 and current_running < prev:
            # Пропорционально уменьшить: сохраним (current / prev) от текущего значения
            ratio = current_running / prev
            self._inflight_tokens[instance_id] = int(
                self._inflight_tokens.get(instance_id, 0) * ratio
            )

        self._last_running_count[instance_id] = int(current_running)

    # ---- burst mode ----

    def _update_burst_mode(self, instances: List[ModelInstance], now: float) -> None:
        """
        Включить burst mode когда все инстансы имеют waiting > 0.
        Выключить когда таймер истёк.
        """
        all_waiting = all(get_metrics(inst.id).num_waiting > 0 for inst in instances)

        if all_waiting and not self._burst_active:
            self._burst_active = True
            self._burst_until = now + envs.LB_BURST_DURATION
            logger.info(
                "[smart_lb] burst mode ENABLED for %.1fs", envs.LB_BURST_DURATION
            )
        elif self._burst_active and now >= self._burst_until:
            self._burst_active = False
            logger.info("[smart_lb] burst mode DISABLED (timer expired)")
        elif not all_waiting and self._burst_active:
            # Если не все waiting, но burst ещё активен — дождаться таймера
            pass

    # ---- adaptive headroom multiplier ----

    def _update_headroom(self, instances: List[ModelInstance], now: float) -> None:
        """
        Обновить headroom multiplier не чаще чем раз в LB_HEADROOM_INTERVAL.
        """
        if now - self._last_headroom_update < envs.LB_HEADROOM_INTERVAL:
            return

        self._last_headroom_update = now

        all_metrics = [get_metrics(inst.id) for inst in instances]
        avg_kv = sum(m.kv_cache_usage for m in all_metrics) / len(all_metrics)
        all_waiting = all(m.num_waiting > 0 for m in all_metrics)

        if all_waiting:
            new_multiplier = max(self._headroom_multiplier, envs.LB_HEADROOM_MAX)
        elif avg_kv < envs.LB_HEADROOM_LOW_KV:
            new_multiplier = min(self._headroom_multiplier, envs.LB_HEADROOM_MIN)
        else:
            new_multiplier = 1.0

        if abs(new_multiplier - self._headroom_multiplier) > 0.001:
            logger.info(
                "[smart_lb] headroom multiplier %.2f -> %.2f (avg_kv=%.3f all_waiting=%s)",
                self._headroom_multiplier,
                new_multiplier,
                avg_kv,
                all_waiting,
            )
            self._headroom_multiplier = new_multiplier

    async def _bind_affinity(
        self, instance_id: int, profile: RequestProfile, now: float
    ) -> None:
        async with self._lock:
            if profile.session_key:
                self._affinity[profile.session_key] = instance_id
            if profile.prefix_key:
                self._affinity[profile.prefix_key] = instance_id
            # Зафиксировать активность выбранной реплики
            self._last_activity[instance_id] = now
            # Зафиксировать inflight tokens
            self._inflight_tokens[instance_id] = (
                self._inflight_tokens.get(instance_id, 0)
                + profile.total_expected_tokens
            )

    async def clear_affinity(self, session_key: str) -> None:
        async with self._lock:
            self._affinity.pop(session_key, None)

    # ---- logging ----

    def _log_selection(
        self,
        inst: ModelInstance,
        req_class: str,
        profile: RequestProfile,
        m: InstanceMetrics,
        score: float,
        reason: str,
    ) -> None:
        inflight = self._inflight_tokens.get(inst.id, 0)
        logger.info(
            "[smart_lb] >>> SELECTED inst=%d class=%s prompt=%d max=%d "
            "running=%.0f waiting=%.0f kv=%.3f score=%.2f reason=%s "
            "inflight=%d burst=%s headroom=%.2f "
            "session_key=%s prefix_key=%s",
            inst.id,
            req_class,
            profile.prompt_tokens,
            profile.max_tokens,
            m.num_running,
            m.num_waiting,
            m.kv_cache_usage,
            score,
            reason,
            inflight,
            self._burst_active,
            self._headroom_multiplier,
            profile.session_key,
            profile.prefix_key,
        )
