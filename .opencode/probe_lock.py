import os
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
p = Path("data/index_progress.json")
tmp = p.with_suffix(".json.tmp")
fail = 0
for i in range(50):
    tmp.write_text('{"probe": %d}' % i, encoding="utf-8")
    try:
        os.replace(tmp, p)
        time.sleep(0.01)
    except OSError as e:
        fail += 1
        print("第", i, "次失败:", e)
print("50 次原子替换，失败:", fail)
