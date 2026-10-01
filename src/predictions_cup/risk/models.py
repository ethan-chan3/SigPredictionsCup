"""Risk data models, adapted from PolyBot `src/polybot/risk/models.py`.

Only `RiskCheckResult` is ported (as a dataclass). PolyBot's multi-venue and
Greeks models (VenuePosition, PortfolioSnapshot, HedgeRecommendation,
RiskMetrics) are omitted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RiskCheckResult:
    """Result of a pre-trade risk check."""

    approved: bool
    reason: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    adjusted_size: Optional[float] = None  # Set when the size was reduced
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.approved
