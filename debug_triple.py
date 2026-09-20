with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'rb') as f:
    content = f.read()

# Check the problematic area around 11481 and 11486
for pos in [11470, 11481, 11486, 11495]:
    print(f'\nPosition {pos}:')
    for i in range(pos-10, pos+20):
        c = content[i]
        ch = chr(c) if 32 <= c < 127 else '?'
        print(f'  {i}: {c:02x} ({ch})')
    print()

# Also check around 16247 and 16322
for pos in [16235, 16247, 16322, 16335]:
    print(f'\nPosition {pos}:')
    for i in range(pos-10, pos+20):
        c = content[i]
        ch = chr(c) if 32 <= c < 127 else '?'
        print(f'  {i}: {c:02x} ({ch})')
    print()

# And around 18174, 18370
for pos in [18165, 18174, 18370, 18380]:
    print(f'\nPosition {pos}:')
    for i in range(pos-10, pos+20):
        c = content[i]
        ch = chr(c) if 32 <= c < 127 else '?'
        print(f'  {i}: {c:02x} ({ch})')
    print()

# And around 19008, 19074
for pos in [19000, 19008, 19074, 19085]:
    print(f'\nPosition {pos}:')
    for i in range(pos-10, pos+20):
        c = content[i]
        ch = chr(c) if 32 <= c < 127 else '?'
        print(f'  {i}: {c:02x} ({ch})')
    print()