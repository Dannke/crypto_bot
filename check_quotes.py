with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

idx = content.find(b'Build from a parsed YAML')
print(f'Found at: {idx}')
print('Bytes around quotes:')
for i in range(idx+60, idx+85):
    c = content[i]
    ch = chr(c) if 32 <= c < 127 else '?'
    print(f'{i}: {c:02x} ({ch})')

# Count total triple quotes in file
total_triple = content.count(b'"""')
print(f'\nTotal """ in file: {total_triple}')