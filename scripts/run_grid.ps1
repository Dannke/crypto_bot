$symbols = "BTC/USDT ETH/USDT SOL/USDT XRP/USDT"
$timeframe = "1h"
$min_score = 55
$results = @()

$rsi_presets = @{
    "narrow" = @{ rsi_long_min=55; rsi_long_max=65; rsi_short_min=35; rsi_short_max=45 }
    "wide"   = @{ rsi_long_min=40; rsi_long_max=75; rsi_short_min=25; rsi_short_max=55 }
}
$spike_ratios = @(1.2, 1.5, 2.0)

foreach ($width in @("narrow","wide")) {
    $p = $rsi_presets[$width]
    foreach ($sr in $spike_ratios) {
        $overrides = "--override scoring__min_score=$min_score"
        $overrides += " --override strategy__volume__spike_ratio=$sr"
        foreach ($kv in $p.GetEnumerator()) {
            $overrides += " --override strategy__momentum__$($kv.Name)=$($kv.Value)"
        }

        Write-Host "`n`n========== RSI=$width spike=$sr ==========" -ForegroundColor Cyan

        $output = python scripts/walk_forward.py --symbols $symbols --timeframe $timeframe $overrides 2>&1

        # Extract key lines
        $bars = $output | Select-String "^\s+Bars\s+" | ForEach-Object { $_ -replace '\s+', ' ' }
        $trades = $output | Select-String "^\s+Trades\s+" | ForEach-Object { $_ -replace '\s+', ' ' }
        $pnl = $output | Select-String "^\s+Total P&L" | ForEach-Object { $_ -replace '\s+', ' ' }
        $wr = $output | Select-String "^\s+Win rate" | ForEach-Object { $_ -replace '\s+', ' ' }
        $dd = $output | Select-String "^\s+Max DD" | ForEach-Object { $_ -replace '\s+', ' ' }
        $sharpe = $output | Select-String "^\s+Sharpe" | ForEach-Object { $_ -replace '\s+', ' ' }
        $sl = $output | Select-String "^\s+Closed by SL" | ForEach-Object { $_ -replace '\s+', ' ' }
        $tp = $output | Select-String "^\s+Closed by TP" | ForEach-Object { $_ -replace '\s+', ' ' }
        $hint = $output | Select-String "NOT ENOUGH DATA|POSSIBLE OVERFIT|looks consistent" | ForEach-Object { $_ -replace '\s+', ' ' }

        Write-Host "  $bars"
        Write-Host "  $trades"
        Write-Host "  $pnl"
        Write-Host "  $wr"
        Write-Host "  $dd"
        Write-Host "  $sharpe"
        Write-Host "  $sl"
        Write-Host "  $tp"
        Write-Host "  $hint"
    }
}
