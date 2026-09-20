with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'r') as f:
    content = f.read()

# Fix the triple-quoted string - ensure it has proper closing quotes
old = '        """Build from a parsed YAML mapping, failing fast on unknown keys."""'
new = '        """Build from a parsed YAML mapping, failing fast on unknown keys."""\n'
content = content.replace(old, new)

with open(r'C:\Pythonpro\crypto_bot\src\crypto_bot\config\schemas.py', 'w') as f:
    f.write(content)
print('Fixed')