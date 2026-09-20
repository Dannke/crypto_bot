import pandas as pd
df = pd.read_csv('data/regime_timeline.csv')
print(f'Total rows: {len(df)}')
print(f'Date range: {df.as_of_iso.min()} to {df.as_of_iso.max()}')
print(f'Regime values: {df.regime.unique()}')
df['year'] = df.as_of_iso.str[:4]
print(f'2025 rows: {len(df[df.year == "2025"])}')
print(f'2026 rows: {len(df[df.year == "2026"])}')
# Check regime distribution in 2025
df_2025 = df[df.year == "2025"]
if len(df_2025) > 0:
    print(f'2025 regime distribution: {df_2025.regime.value_counts().to_dict()}')
# Check regime distribution in 2026
df_2026 = df[df.year == "2026"]
if len(df_2026) > 0:
    print(f'2026 regime distribution: {df_2026.regime.value_counts().to_dict()}')