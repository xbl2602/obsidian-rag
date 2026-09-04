import collections
import sys
from pathlib import Path

sys.path.insert(0, ".")
from library import load_registry, effective_config, meta_path  # noqa
from index import load_meta, collect_md_files  # noqa

e = next(x for x in load_registry() if x["name"] == "Obsidian Vault")
cfg = effective_config(e)
print("生效 extensions:", cfg["extensions"], "| agent_formats:", cfg["agent_formats"])
meta = load_meta(meta_path("Obsidian Vault"))
c = collections.Counter(Path(k).suffix.lower() for k, v in meta.items()
                        if isinstance(v, dict))
print("已入库文件按格式:", dict(c))
files = collect_md_files(cfg["path"], cfg["exclude_dirs"], cfg["exclude_files"],
                         cfg["exclude_patterns"], cfg["extensions"])
print("磁盘上待索引文件按格式:",
      dict(collections.Counter(p.suffix.lower() for p in files)))
