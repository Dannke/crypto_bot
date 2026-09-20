with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

# Check the very beginning
print('First 100 bytes:')
for i, b in enumerate(content[:100]):
    ch = chr(b) if 32 <= b < 127 else '?'
    print(f'{i}: {b:02x} ({ch})')

print('\n---')

# Find all triple quotes
triple_quote_positions = []
idx = 0
while True:
    idx = content.find(b'"""', idx)
    if idx == -1:
        break
    triple_quote_positions.append(idx)
    idx += 3

print(f'Triple quote positions: {triple_quote_positions}')
print(f'Count: {len(triple_quote_positions)}')

# Check if count is even (should be for pairs)
print(f'Even count: {len(triple_quote_positions) % 2 == 0}')