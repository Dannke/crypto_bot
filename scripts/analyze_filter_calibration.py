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

import pandas as pd

# Реальные индикаторы проекта — подставить актуальный путь импорта, если
# отличается (см. features/builder.py для точных имён).
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

    def print_report(self) -> None:
        print(f"\n=== {self.symbol} {self.timeframe}  (n={self.n_bars} валидных баров) ===")
        print(f"  ADX percentiles: {self._fmt(self.percentiles_adx)}")
        print(f"  ATR% percentiles: {self._fmt(self.percentiles_atr_pct)}")
        print(f"  --- Pass rate по отдельным условиям ---")
        print(f"  adx >= adx_min:                  {self.adx_pass_rate:.1%}")
        print(f"  volatility в диапазоне:            {self.volatility_pass_rate:.1%}")
        print(f"  volume spike >= порога:            {self.volume_pass_rate:.1%}")
        print(f"  --- Составные (что реально нужно для сигнала) ---")
        print(f"  EMA-стек чист (bull/bear) + ADX:   {self.direction_clean_rate:.1%}")
        print(f"  + RSI в нужной зоне:               {self.rsi_zone_rate:.1%}")
        print(f"  ВСЁ сразу (верхняя граница частоты сигналов): {self.fully_tradeable_rate:.1%}")

    @staticmethod
    def _fmt(d: dict[str, float]) -> str:
        return ", ".join(f"{k}={v:.2f}" for k, v in d.items())


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


def analyze(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    *,
    adx_min: float,
    atr_min_pct: float,
    atr_max_pct: float,
    spike_ratio: float,
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
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/crypto_bot.db")
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--timeframe", default=None)
    parser.add_argument("--all", action="store_true", help="прогнать по всем (symbol, tf) в candles")
    parser.add_argument("--adx-min", type=float, default=20.0)
    parser.add_argument("--atr-min-pct", type=float, default=0.5)
    parser.add_argument("--atr-max-pct", type=float, default=8.0)
    parser.add_argument("--spike-ratio", type=float, default=1.5)
    args = parser.parse_args()

    db_path = Path(args.db)

    if args.all:
        pairs = list_symbol_timeframes(db_path)
    else:
        if not args.symbol or not args.timeframe:
            parser.error("укажите --symbol и --timeframe, либо используйте --all")
        pairs = [(args.symbol, args.timeframe)]

    for symbol, timeframe in pairs:
        df = load_candles(db_path, symbol, timeframe)
        report = analyze(
            df, symbol, timeframe,
            adx_min=args.adx_min, atr_min_pct=args.atr_min_pct,
            atr_max_pct=args.atr_max_pct, spike_ratio=args.spike_ratio,
        )
        if report is None:
            print(f"\n=== {symbol} {timeframe}: недостаточно баров для анализа (нужно >= 205) ===")
            continue
        report.print_report()


if __name__ == "__main__":
    main()