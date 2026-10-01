"""Single-venue risk manager, adapted from PolyBot `src/polybot/risk/manager.py`.

Kept from PolyBot's `check_pre_trade`: position-size limit, daily-loss limit,
total-exposure limit (with size reduction), and open-orders limit, plus the
position / PnL / open-order bookkeeping methods. Omitted: per-venue limits,
concentration, delta and hedging (multi-venue concerns), alerts, snapshots, and
the global singleton.

Added for this venue:
- price validation against the documented tick grid;
- exposure measured as worst-case loss on a binary contract.

All amounts are in the tournament's currency (e.g. SUSQies), not USD.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from predictions_cup.risk.models import RiskCheckResult

logger = logging.getLogger(__name__)

# Docs: "Fresh limit-order prices must be on the `0.005` tick between `0.005` and `0.995`."
TICK_SIZE = 0.005
MIN_PRICE = 0.005
MAX_PRICE = 0.995


def is_valid_price(price: float) -> bool:
    if not MIN_PRICE <= price <= MAX_PRICE:
        return False
    ticks = price / TICK_SIZE
    return abs(ticks - round(ticks)) < 1e-6


def worst_case_loss(side: str, size: float, price: float) -> float:
    """Maximum loss of a YES-denominated binary trade.

    Buying YES at p can lose p per share; selling YES at p (equivalent to holding
    NO) can lose 1 - p per share. Docs, Realtime: "For a NO position the
    effective per-share cost is `1 - price`."
    """
    return size * price if side.lower() == "buy" else size * (1 - price)


@dataclass(frozen=True)
class RiskConfig:
    """Risk limits. Env vars use PolyBot's `RISK_` prefix; defaults match PolyBot."""

    max_position_size: float = 1000.0
    max_total_exposure: float = 10000.0
    daily_loss_limit: float = 500.0
    max_open_orders: int = 50

    @classmethod
    def from_env(cls) -> RiskConfig:
        load_dotenv()

        def read(name: str, default: float) -> float:
            raw = os.environ.get(name, "").strip()
            return float(raw) if raw else default

        return cls(
            max_position_size=read("RISK_MAX_POSITION_SIZE", cls.max_position_size),
            max_total_exposure=read("RISK_MAX_TOTAL_EXPOSURE", cls.max_total_exposure),
            daily_loss_limit=read("RISK_DAILY_LOSS_LIMIT", cls.daily_loss_limit),
            max_open_orders=int(read("RISK_MAX_OPEN_ORDERS", cls.max_open_orders)),
        )


class RiskManager:
    """Pre-trade checks and exposure bookkeeping for one venue."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self._config = config or RiskConfig.from_env()
        self._exposure: Dict[str, float] = {}  # symbol -> worst-case loss
        self._daily_pnl = 0.0
        self._open_orders_count = 0

    @property
    def config(self) -> RiskConfig:
        return self._config

    @property
    def total_exposure(self) -> float:
        return sum(self._exposure.values())

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    # =========================================================================
    # Pre-Trade Risk Checks
    # =========================================================================

    def check_pre_trade(
        self,
        symbol: str,
        side: str,
        size: float,
        price: float,
        strategy: Optional[str] = None,
    ) -> RiskCheckResult:
        """Validate an opening trade. `side` is "buy" or "sell" (YES-denominated)."""
        warnings: list[str] = []

        if size <= 0:
            return RiskCheckResult(approved=False, reason=f"Size must be positive, got {size}")

        # Rejects off-tick prices and the venue's empty-book placeholders (0.0 / 1.0).
        if not is_valid_price(price):
            return RiskCheckResult(
                approved=False,
                reason=f"Price {price} is not on the {TICK_SIZE} tick within [{MIN_PRICE}, {MAX_PRICE}]",
            )

        exposure = worst_case_loss(side, size, price)

        # 1. Position size limit
        if exposure > self._config.max_position_size:
            return RiskCheckResult(
                approved=False,
                reason=f"Position risk {exposure:.2f} exceeds limit {self._config.max_position_size:.2f}",
            )

        # 2. Daily loss limit
        if self._daily_pnl < -self._config.daily_loss_limit:
            return RiskCheckResult(approved=False, reason=f"Daily loss limit reached: {self._daily_pnl:.2f}")

        # 3. Total exposure limit (reduce size if partially available)
        current = self.total_exposure
        if current + exposure > self._config.max_total_exposure:
            available = self._config.max_total_exposure - current
            if available <= 0:
                return RiskCheckResult(
                    approved=False,
                    reason=f"Total exposure limit {self._config.max_total_exposure:.2f} reached",
                )
            per_share = exposure / size
            adjusted = available / per_share
            warnings.append(f"Size reduced from {size:.4f} to {adjusted:.4f} due to exposure limit")
            size, exposure = adjusted, available

        # 4. Open orders limit
        if self._open_orders_count >= self._config.max_open_orders:
            return RiskCheckResult(
                approved=False,
                reason=f"Max open orders limit ({self._config.max_open_orders}) reached",
            )

        return RiskCheckResult(
            approved=True,
            warnings=warnings,
            adjusted_size=size if warnings else None,
            metadata={"exposure": exposure, "new_total_exposure": current + exposure, "strategy": strategy},
        )

    # =========================================================================
    # Position / PnL / Order bookkeeping
    # =========================================================================

    def update_position(self, symbol: str, exposure: float) -> None:
        """Set the worst-case-loss exposure currently held in `symbol`."""
        if exposure <= 0:
            self._exposure.pop(symbol, None)
        else:
            self._exposure[symbol] = exposure

    def close_position(self, symbol: str, realized_pnl: float = 0) -> None:
        self._exposure.pop(symbol, None)
        if realized_pnl:
            self.record_pnl(realized_pnl)

    def record_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl

    def increment_open_orders(self) -> None:
        self._open_orders_count += 1

    def decrement_open_orders(self) -> None:
        self._open_orders_count = max(0, self._open_orders_count - 1)

    def set_open_orders_count(self, count: int) -> None:
        self._open_orders_count = count

    def daily_reset(self) -> None:
        """Reset daily counters. The caller decides when a day boundary occurs."""
        self._daily_pnl = 0.0
        logger.info("Daily risk counters reset")

    def get_risk_status(self) -> Dict[str, Any]:
        cfg = self._config
        return {
            "total_exposure": round(self.total_exposure, 2),
            "daily_pnl": round(self._daily_pnl, 2),
            "position_count": len(self._exposure),
            "open_orders": self._open_orders_count,
            "limits": {
                "max_position_size": cfg.max_position_size,
                "max_total_exposure": cfg.max_total_exposure,
                "daily_loss_limit": cfg.daily_loss_limit,
                "max_open_orders": cfg.max_open_orders,
                "exposure_utilization_pct": round(self.total_exposure / cfg.max_total_exposure * 100, 1)
                if cfg.max_total_exposure > 0
                else 0,
            },
        }
