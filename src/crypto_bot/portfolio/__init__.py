"""Typed portfolio-domain data transfer objects and the cross-sectional
market snapshot API.

The package contains immutable snapshots and commands passed between
portfolio construction, execution, and storage adapters.  It deliberately
contains no allocation, sizing, execution, or other business rules —
except the structural guarantees of the snapshot API (same timestamp,
same timeframe, closed bars only).
"""

from .market_snapshot import (
    CandleSource,
    MarketSnapshot,
    get_market_snapshot,
    get_universe_snapshot,
)
from .mean_reversion_features import (
    ZScoreSnapshot,
    compute_zscore_snapshot,
)
from .models import (
    CrossSectionalFeatureSnapshot,
    Fill,
    OrderRequest,
    PortfolioIntent,
    PortfolioState,
    PositionIntent,
    RegimeSnapshot,
    UniverseSnapshot,
    WeightedPosition,
)
from .regime import (
    classify_regime,
)
from .risk import (
    PortfolioRiskEngine,
    PortfolioRiskLimits,
    PortfolioRiskReport,
    PositionRiskResult,
    VolatilitySizingParams,
)

__all__ = [
    "CandleSource",
    "CrossSectionalFeatureSnapshot",
    "Fill",
    "MarketSnapshot",
    "OrderRequest",
    "PortfolioIntent",
    "PortfolioRiskEngine",
    "PortfolioRiskLimits",
    "PortfolioRiskReport",
    "PortfolioState",
    "PositionIntent",
    "PositionRiskResult",
    "RegimeSnapshot",
    "UniverseSnapshot",
    "VolatilitySizingParams",
    "WeightedPosition",
    "ZScoreSnapshot",
    "classify_regime",
    "compute_zscore_snapshot",
    "get_market_snapshot",
    "get_universe_snapshot",
]
