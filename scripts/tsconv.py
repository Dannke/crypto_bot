import sys
from datetime import datetime, timezone

for arg in sys.argv[1:]:
    dt = datetime.fromisoformat(arg)
    print(int(dt.timestamp() * 1000))