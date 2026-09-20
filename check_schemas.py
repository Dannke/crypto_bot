with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

idx = content.find(b'Build from a parsed YAML')
print(f'Found at: {idx}')
print('Bytes:', repr(content[idx:idx+90]))
print()
# Count quotes
quote_count = content[idx:idx+80].count(b'"')
print(f'Quote count: {quote_count}')

# Try to compile
try:
    with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'r', encoding='utf-8-sig') as f:
        text = f.read()
    compile(text, 'schemas.py', 'exec')
    print('Syntax OK')
except SyntaxError as e:
    print(f'SyntaxError: {e}')
    lines = text.splitlines()
    for i in range(max(0, e.lineno-3), min(len(lines), e.lineno+3)):
        print(f'{i+1}: {lines[i]}')