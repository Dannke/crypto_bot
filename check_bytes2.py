with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

idx = content.find(b'Build from a parsed YAML')
# Show more bytes
print('Bytes after "keys.":')
for i, b in enumerate(content[idx+60:idx+100]):
    ch = chr(b) if 32 <= b < 127 else '?'
    print(f'{idx+60+i:5d}: {b:02x} ({ch})')