with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

idx = content.find(b'Build from a parsed YAML')
print(f'Found at: {idx}')
print('Bytes:')
for i, b in enumerate(content[idx:idx+60]):
    ch = chr(b) if 32 <= b < 127 else '?'
    print(f'{idx+i:5d}: {b:02x} ({ch})')