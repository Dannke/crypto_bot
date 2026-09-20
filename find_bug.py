with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'r', encoding='utf-8-sig') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '"""' in line:
        print(f'{i+1}: {repr(line)}')