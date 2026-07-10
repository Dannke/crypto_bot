from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DiffEntry:
    key: tuple[str, str, int]
    field: str
    live_value: object
    bt_value: object


@dataclass
class DiffReport:
    missing_in_backtest: list[tuple[str, str, int]] = field(default_factory=list)
    missing_in_live: list[tuple[str, str, int]] = field(default_factory=list)
    mismatches: list[DiffEntry] = field(default_factory=list)

    @property
    def is_clean(self) -> bool:
        return not self.missing_in_backtest and not self.missing_in_live and not self.mismatches

    def summary(self) -> str:
        lines: list[str] = []
        if self.is_clean:
            lines.append("OK: live and backtest decisions match perfectly.")
            return "\n".join(lines)

        if self.missing_in_backtest:
            lines.append(
                f"MISSING IN BACKTEST ({len(self.missing_in_backtest)} entries):"
            )
            for sym, tf, ts in self.missing_in_backtest[:20]:
                lines.append(f"  {sym} {tf} ts={ts}")
            if len(self.missing_in_backtest) > 20:
                lines.append(f"  ... and {len(self.missing_in_backtest) - 20} more")

        if self.missing_in_live:
            lines.append(
                f"MISSING IN LIVE ({len(self.missing_in_live)} entries):"
            )
            for sym, tf, ts in self.missing_in_live[:20]:
                lines.append(f"  {sym} {tf} ts={ts}")
            if len(self.missing_in_live) > 20:
                lines.append(f"  ... and {len(self.missing_in_live) - 20} more")

        if self.mismatches:
            lines.append(f"MISMATCHES ({len(self.mismatches)} entries):")
            for e in self.mismatches[:30]:
                lines.append(
                    f"  {e.key[0]} {e.key[1]} ts={e.key[2]} "
                    f"{e.field}: live={e.live_value!r} bt={e.bt_value!r}"
                )
            if len(self.mismatches) > 30:
                lines.append(f"  ... and {len(self.mismatches) - 30} more")

        return "\n".join(lines)


def compare_decisions(
    live: dict[tuple[str, str, int], dict],
    backtest: dict[tuple[str, str, int], dict],
    *,
    score_tolerance: float = 1.0,
) -> DiffReport:
    """Compare two aggregated decision dicts and report all differences.

    Parameters
    ----------
    live:
        Aggregated decisions from the live bot DB (see ``aggregate_raw_rows``).
    backtest:
        Aggregated decisions from the backtest run.
    score_tolerance:
        Maximum allowed absolute difference for the ``score`` field
        (e.g. cross-correlation jitter).  Differences within tolerance
        are considered a match.

    Returns
    -------
    DiffReport with lists of missing keys and field-level mismatches.
    """
    report = DiffReport()

    live_keys = set(live)
    bt_keys = set(backtest)

    report.missing_in_backtest = sorted(live_keys - bt_keys)
    report.missing_in_live = sorted(bt_keys - live_keys)

    _COMPARE_FIELDS = ["accepted", "reject_reason", "signal"]

    for key in live_keys & bt_keys:
        lr = live[key]
        br = backtest[key]
        for field in _COMPARE_FIELDS:
            lv = lr.get(field)
            bv = br.get(field)
            if lv != bv:
                report.mismatches.append(DiffEntry(key, field, lv, bv))

        # Score: use tolerance
        ls = lr.get("score")
        bs = br.get("score")
        if ls is not None and bs is not None:
            if abs(ls - bs) > score_tolerance:
                report.mismatches.append(DiffEntry(key, "score", ls, bs))
        elif ls != bs:
            report.mismatches.append(DiffEntry(key, "score", ls, bs))

    return report
