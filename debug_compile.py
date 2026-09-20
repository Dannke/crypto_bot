with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

idx = content.find(b'Build from a parsed YAML')
print(f'Found at: {idx}')

# Check the exact bytes of the triple quotes
for i in range(idx+60, idx+85):
    c = content[i]
    ch = chr(c) if 32 <= c < 127 else '?'
    print(f'{i}: {c:02x} ({ch})')

# Now try to compile just that section
test_code = content[19000:19150]
try:
    compile(test_code, 'test', 'exec')
    print('Test compile: OK')
except SyntaxError as e:
    print(f'Test compile error: {e}')

# Also try the whole file
try:
    with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'r', encoding='utf-8-sig') as f:
        text = f.read()
    compile(text, 'schemas.py', 'exec')
    print('Full compile: OK')
except SyntaxError as e:
    print(f'Full compile error: {e}')
    lines = text.splitlines()
    for i in range(max(0, e.lineno-3), min(len(lines), e.lineno+3)):
        print(f'{i+1}: {lines[i]}')