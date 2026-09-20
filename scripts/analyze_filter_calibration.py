"""
scripts/analyze_filter_calibration.py

Эмпирическая проверка калибровки порогов фильтров против РЕАЛЬНОЙ истории
свечей, уже накопленной ботом на mainnet. Не требует новых данных и не
трогает бота — читает только candles.

Отвечает на вопрос: пороги (adx_min, atr_min_pct/atr_max_pct, spike_ratio)
реально отсекают рынок, или узкое место — сама стратегия (направление
EMA-стека + RSI-зона), а фильтры почти всегда пропускают.

Использует ваши реальные индикаторные функции (adx, atr_pct, rsi,
volume_spike_ratio) — не переизобретает их, чтобы цифры были той же
математикой, что и в бою.

Запуск:
    python scripts/analyze_filter_calibration.py --db data/crypto_bot.db \
        --symbol BTC/USDT --timeframe 1h
    python scripts/analyze_filter_calibration.py --db data/crypto_bot.db --all
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

# Реальные индикаторы проекта — подставить актуальный путь импорта, если
# отличается (см. features/builder.py для точных имён).
from crypto_bot.config.settings import load_settings
from crypto_bot.indicators.adx import adx
from crypto_bot.indicators.atr import atr_pct
from crypto_bot.indicators.rsi import rsi
from crypto_bot.indicators.volume import volume_spike_ratio


def load_candles(db_path: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    con = sqlite3.connect(str(db_path))
    try:
        df = pd.read_sql_query(
            "SELECT ts_ms, open, high, low, close, volume FROM candles "
            "WHERE symbol=? AND timeframe=? ORDER BY ts_ms ASC",
            con,
            params=(symbol, timeframe),
        )
    finally:
        con.close()
    return df


def list_symbol_timeframes(db_path: Path) -> list[tuple[str, str]]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol, timeframe FROM candles ORDER BY symbol, timeframe"
        ).fetchall()
    finally:
        con.close()
    return [(r[0], r[1]) for r in rows]


@dataclass
class CalibrationReport:
    symbol: str
    timeframe: str
    n_bars: int
    adx_pass_rate: float
    volatility_pass_rate: float
    volume_pass_rate: float
    direction_clean_rate: float  # EMA bull/bear (не mixed) И adx >= adx_min
    rsi_zone_rate: float         # плюс RSI в нужной зоне (bull->long, bear->short)
    fully_tradeable_rate: float  # всё сразу — это и есть верхняя граница частоты сигналов
    percentiles_adx: dict[str, float]
    percentiles_atr_pct: dict[str, float]
    percentiles_volume_spike: dict[str, float]
    n_fully_tradeable: int = 0           # сколько баров прошли все boolean gates
    n_below_min_score: int = 0           # из них сколько не дотягивают до min_score
    optimistic_p50: float = 0.0          # медиана optimistic score среди fully_ok
    optimistic_p75: float = 0.0
    optimistic_p90: float = 0.0

    def print_report(self) -> None:
        print(f"\n=== {self.symbol} {self.timeframe}  (n={self.n_bars} валидных баров) ===")
        print(f"  ADX percentiles: {self._fmt(self.percentiles_adx)}")
        print(f"  ATR% percentiles: {self._fmt(self.percentiles_atr_pct)}")
        print(f"  Volume spike percentiles: {self._fmt(self.percentiles_volume_spike)}")
        print(f"  --- Pass rate по отдельным условиям ---")
        print(f"  adx >= adx_min:                  {self.adx_pass_rate:.1%}")
        print(f"  volatility в диапазоне:            {self.volatility_pass_rate:.1%}")
        print(f"  volume spike >= порога:            {self.volume_pass_rate:.1%}")
        print(f"  --- Составные (что реально нужно для сигнала) ---")
        print(f"  EMA-стек чист (bull/bear) + ADX:   {self.direction_clean_rate:.1%}")
        print(f"  + RSI в нужной зоне:               {self.rsi_zone_rate:.1%}")
        print(f"  ВСЁ сразу (верхняя граница частоты сигналов): {self.fully_tradeable_rate:.1%}")
        if self.n_fully_tradeable:
            print(f"  --- Оптимистичный composite score (liquidity/spread/risk=1.0) ---")
            print(f"  n_fully_tradeable:                {self.n_fully_tradeable}")
            print(f"  n_below_min_score={self.n_below_min_score}  ({self.n_below_min_score/self.n_fully_tradeable:.1%} от fully_tradeable)")
            print(f"  optimistic score p50/p75/p90:     {self.optimistic_p50:.1f} / {self.optimistic_p75:.1f} / {self.optimistic_p90:.1f}")

    @staticmethod
    def _fmt(d: dict[str, float]) -> str:
        return ", ".join(f"{k}={v:.2f}" for k, v in d.items())


def _resolve_tf_param(param: float | dict[str, float], timeframe: str, default: float) -> float:
    """Get per-TF value from a ``float | dict[str, float]`` config field."""
    if isinstance(param, dict):
        return param.get(timeframe, default)
    return param


def _percentiles(series: pd.Series) -> dict[str, float]:
    return {
        "min": float(series.min()),
        "p10": float(series.quantile(0.10)),
        "p25": float(series.quantile(0.25)),
        "median": float(series.median()),
        "p75": float(series.quantile(0.75)),
        "p90": float(series.quantile(0.90)),
        "max": float(series.max()),
    }


SCORE_WEIGHTS = {
    "trend": 0.25,
    "momentum": 0.15,
    "volume": 0.15,
    "volatility": 0.10,
    "liquidity": 0.10,
    "spread": 0.05,
    "risk": 0.20,
}


def _optimistic_score(
    trend: pd.Series, momentum: pd.Series, volume: pd.Series, volatility: pd.Series,
) -> pd.Series:
    """Optimistic composite score: liquidity/spread/risk assumed 1.0 (upper bound)."""
    w = SCORE_WEIGHTS
    weighted = (
        w["trend"] * trend
        + w["momentum"] * momentum
        + w["volume"] * volume
        + w["volatility"] * volatility
        + w["liquidity"] * 1.0
        + w["spread"] * 1.0
        + w["risk"] * 1.0
    )
    return weighted * 100.0


def analyze(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    adx_min: float,
    atr_min_pct: float,
    atr_max_pct: float,
    spike_ratio: float,
    min_score: float = 65.0,
    ema_fast: int = 21,
    ema_mid: int = 50,
    ema_slow: int = 200,
    rsi_period: int = 14,
    rsi_long: tuple[float, float] = (50.0, 70.0),
    rsi_short: tuple[float, float] = (30.0, 50.0),
) -> CalibrationReport | None:
    if len(df) < ema_slow + 5:
        return None

    adx_series = adx(df["high"], df["low"], df["close"], period=14)
    atr_series = atr_pct(df["high"], df["low"], df["close"], period=14)
    rsi_series = rsi(df["close"], period=rsi_period)
    vol_series = volume_spike_ratio(df["volume"], period=20)

    # Та же формула EMA, что в indicators/ema.py: ewm(span=period, adjust=False, min_periods=period)
    ema_fast_s = df["close"].ewm(span=ema_fast, adjust=False, min_periods=ema_fast).mean()
    ema_mid_s = df["close"].ewm(span=ema_mid, adjust=False, min_periods=ema_mid).mean()
    ema_slow_s = df["close"].ewm(span=ema_slow, adjust=False, min_periods=ema_slow).mean()

    valid = (
        adx_series.notna()
        & atr_series.notna()
        & rsi_series.notna()
        & vol_series.notna()
        & ema_slow_s.notna()
    )
    n = int(valid.sum())
    if n == 0:
        return None

    adx_ok = (adx_series >= adx_min) & valid
    vol_ok = (atr_series >= atr_min_pct) & (atr_series <= atr_max_pct) & valid
    volu_ok = (vol_series >= spike_ratio) & valid

    bull = (ema_fast_s > ema_mid_s) & (ema_mid_s > ema_slow_s)
    bear = (ema_fast_s < ema_mid_s) & (ema_mid_s < ema_slow_s)
    direction_clean = (bull | bear) & adx_ok & valid

    rsi_long_ok = bull & rsi_series.between(*rsi_long)
    rsi_short_ok = bear & rsi_series.between(*rsi_short)
    rsi_zone_ok = (rsi_long_ok | rsi_short_ok) & adx_ok & valid

    fully_ok = rsi_zone_ok & vol_ok & volu_ok

    # --- Оптимистичный composite score для fully_ok баров ---
    # Тренд-strength: линейная интерполяция ADX от adx_min..(adx_min+25)
    trend_strength = (adx_series - adx_min) / 25.0
    trend_strength = trend_strength.clip(0.0, 1.0)
    trend_score = trend_strength.where(bull | bear, 0.0).fillna(0.0)

    # Momentum: отклонение RSI от 50
    momentum_score = (rsi_series - 50.0).abs() / 50.0
    momentum_score = momentum_score.clip(0.0, 1.0).fillna(0.0)

    # Volatility: близость ATR к середине диапазона
    mid_vola = (atr_min_pct + atr_max_pct) / 2.0
    half_vola = max(1e-9, (atr_max_pct - atr_min_pct) / 2.0)
    vola_raw = 1.0 - (atr_series - mid_vola).abs() / half_vola
    volatility_score = vola_raw.clip(0.0, 1.0).fillna(0.0)

    # Volume: плавный скор на основе spike_ratio
    below = 0.2 * (vol_series / spike_ratio)
    above = 0.5 + (vol_series - spike_ratio) / spike_ratio
    vol_raw = np.where(vol_series >= spike_ratio, above.clip(0.0, 1.0), below.clip(0.0, None))
    volume_score = pd.Series(vol_raw, index=df.index).fillna(0.0)

    optimistic = _optimistic_score(trend_score, momentum_score, volume_score, volatility_score)

    fully_ok_scores = optimistic[fully_ok]
    n_ft = int(fully_ok.sum())
    n_ft_below = int((fully_ok_scores < min_score).sum()) if n_ft else 0

    return CalibrationReport(
        symbol=symbol,
        timeframe=timeframe,
        n_bars=n,
        adx_pass_rate=float(adx_ok.sum() / n),
        volatility_pass_rate=float(vol_ok.sum() / n),
        volume_pass_rate=float(volu_ok.sum() / n),
        direction_clean_rate=float(direction_clean.sum() / n),
        rsi_zone_rate=float(rsi_zone_ok.sum() / n),
        fully_tradeable_rate=float(fully_ok.sum() / n),
        percentiles_adx=_percentiles(adx_series[valid]),
        percentiles_atr_pct=_percentiles(atr_series[valid]),
        percentiles_volume_spike=_percentiles(vol_series[valid]),
        n_fully_tradeable=n_ft,
        n_below_min_score=n_ft_below,
        optimistic_p50=float(fully_ok_scores.quantile(0.50)) if n_ft else 0.0,
        optimistic_p75=float(fully_ok_scores.quantile(0.75)) if n_ft else 0.0,
        optimistic_p90=float(fully_ok_scores.quantile(0.90)) if n_ft else 0.0,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/crypto_bot.db")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--all", action="store_true", help="прогнать по всем (symbol, tf) в candles")
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--adx-min", type=float, default=None)
    parser.add_argument("--atr-min-pct", type=float, default=None)
    parser.add_argument("--atr-max-pct", type=float, default=None)
    parser.add_argument("--spike-ratio", type=float, default=None)
    parser.add_argument("--min-score", type=float, default=65.0)
    args = parser.parse_args()

    db_path = Path(args.db)

    # Load defaults from live config (which has per-TF thresholds)
    config_obj = load_settings(yaml_path=Path(args.config))
    s = config_obj.settings
    default_adx_min = s.strategy.trend.adx_min
    default_spike_ratio = s.strategy.volume.spike_ratio
    default_atr_min = s.strategy.volatility.atr_min_pct  # float | dict[str, float]
    default_atr_max = s.strategy.volatility.atr_max_pct  # float | dict[str, float]

    adx_min = args.adx_min if args.adx_min is not None else default_adx_min
    spike_ratio = args.spike_ratio if args.spike_ratio is not None else default_spike_ratio
    cli_atr_min = args.atr_min_pct
    cli_atr_max = args.atr_max_pct

    if args.all:
        pairs = list_symbol_timeframes(db_path)
        pairs = [(sym, tf) for sym, tf in pairs if tf != "5m"]
    else:
        if not args.symbol or not args.timeframe:
            parser.error("укажите --symbol и --timeframe, либо используйте --all")
        pairs = [(args.symbol, args.timeframe)]

    for symbol, timeframe in pairs:
        df = load_candles(db_path, symbol, timeframe)
        atr_min = cli_atr_min if cli_atr_min is not None else _resolve_tf_param(default_atr_min, timeframe, 0.5)
        atr_max = cli_atr_max if cli_atr_max is not None else _resolve_tf_param(default_atr_max, timeframe, 8.0)
        report = analyze(
            df, symbol, timeframe,
            adx_min=adx_min, atr_min_pct=atr_min, atr_max_pct=atr_max,
            spike_ratio=spike_ratio, min_score=args.min_score,
        )
        if report is None:
            print(f"\n=== {symbol} {timeframe}: недостаточно баров для анализа (нужно >= 205) ===")
            continue
        report.print_report()


if __name__ == "__main__":
    main()