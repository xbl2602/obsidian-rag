"""library.py — 多库注册表（data/libraries.json）：增删改查 + 生效配置合并 + 检索选择解析。

CLI 入口（AI 可调）：
  python library.py list
  python library.py add <路径> [--name 名]
  python library.py remove <名> [--drop] [--yes]
  python library.py config <名> --set key=value [--unset key]

每库可覆盖字段：exclude_dirs / exclude_files / exclude_patterns /
chunk_char_limit / short_doc_char_limit / extensions / collection。
null = 继承 config.json 全局值。embedding 模型保持全局（跨库并查要求同一向量空间）。

选择语义（search_knowledge 的 libraries/exclude）：
  最终范围 = (libraries 非空 ? libraries : 全部库) − exclude
  未知库名 / 结果为空集 → 抛 ValueError（含可用库名清单）。
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from config import CFG, DATA_DIR
from extractors import SUPPORTED_EXTS

LIBRARIES_FILE = DATA_DIR / "libraries.json"

GLOBAL_KEYS = ("exclude_dirs", "exclude_files", "exclude_patterns",
               "chunk_char_limit", "short_doc_char_limit")
OVERRIDE_KEYS = GLOBAL_KEYS + ("extensions", "collection")
LIST_KEYS = ("exclude_dirs", "exclude_files", "exclude_patterns", "extensions")
INT_KEYS = ("chunk_char_limit", "short_doc_char_limit")
INVALID_NAME_CHARS = set('/\\:*?"<>|')


def log(*args):
    print(*args, file=sys.stderr)


def _blank_entry(name, path):
    return {k: None for k in OVERRIDE_KEYS} | {"name": name, "path": path}


def validate_name(name):
    if not name or name.strip() != name or len(name) > 64:
        raise ValueError(f"库名非法：{name!r}（须非空、无首尾空格、≤64 字符）")
    if any(c in INVALID_NAME_CHARS for c in name):
        raise ValueError(f"库名含非法字符（不可含 /\\:*?\"<>|）：{name!r}")


def collection_for(name, override=None):
    """库的 Chroma collection 名：显式 override 优先，否则由库名派生。

    非 [a-zA-Z0-9._-] 字符（中文/空格等）替换为 _。若替换后的名字仍然"干净"
    （以字母数字开头结尾、无连续下划线），沿用历史派生结果 kb_<sanitized>，
    保证既有库的 collection 名不变、已建索引不被孤立。

    否则（典型：纯中文库名）追加库名的 md5 前 8 位：
        '火箭笔记' -> 'kb_' + hash   （旧规则会得到 'kb_____'）
    2026-08-14（审计 F15）：旧规则把所有非 ASCII 字符压成下划线，导致
      - 任意两个等长纯中文库名派生出完全相同的 collection（'火箭笔记' 与
        '工程日志' 都是 'kb_____'），第二个库注册时被硬性挡住；
      - 名字以下划线结尾，不符合 Chroma 经典命名规则
        ^[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]$。
    新规则同时解决唯一性与合法性，且对已有的 ASCII 系库名完全向后兼容。
    """
    if override:
        return override
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", name.lower())
    legacy = f"kb_{base}"[:63]
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*[a-zA-Z0-9]", legacy) and "__" not in legacy:
        return legacy
    digest = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
    stem = re.sub(r"_+", "_", base).strip("._-")[:45]
    return f"kb_{stem}_{digest}" if stem else f"kb_{digest}"


def meta_path(name):
    return DATA_DIR / f"index_meta_{name}.json"


def load_registry(create=True):
    """读注册表；文件缺失时（单库迁移场景）自动合成第一个库并落盘。

    解析失败：先改名 .bak 备份再按空注册表处理（避免后续 save_registry
    覆盖损坏文件导致其余库的注册信息永久丢失）。非法库名条目跳过（防手改逃逸）。
    """
    if not LIBRARIES_FILE.exists():
        return _migrate_legacy() if create else []
    try:
        data = json.loads(LIBRARIES_FILE.read_text(encoding="utf-8"))
        entries = data.get("libraries", []) if isinstance(data, dict) else []
    except Exception as e:
        log(f"注册表解析失败（{e}），备份为 libraries.json.bak 后按空注册表处理")
        try:
            LIBRARIES_FILE.replace(LIBRARIES_FILE.with_suffix(".json.bak"))
        except OSError:
            pass
        return []
    out = []
    for e in entries:
        if not isinstance(e, dict) or not e.get("name") or not e.get("path"):
            continue
        try:
            validate_name(e["name"])
        except ValueError:
            log(f"注册表条目库名非法，已跳过：{e.get('name')!r}")
            continue
        out.append(e)
    return out


def save_registry(entries):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = LIBRARIES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"libraries": entries}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    tmp.replace(LIBRARIES_FILE)


def _migrate_legacy():
    """旧单库配置 → 注册表首个库（零重建：沿用 collection 与指纹文件）。"""
    vault = CFG.get("vault", "")
    if not vault:
        log("配置的 vault 路径为空，跳过自动迁移；请用 library.py add <路径> 注册库")
        save_registry([])
        return []
    name = Path(vault).name
    if not name or any(c in INVALID_NAME_CHARS for c in name):
        name = "default"
    entry = _blank_entry(name, vault)
    entry["collection"] = CFG.get("collection_name", "obsidian_kb")
    legacy_meta = DATA_DIR / "index_meta.json"
    if legacy_meta.exists():
        try:
            legacy_meta.rename(meta_path(name))
            log(f"迁移：index_meta.json → index_meta_{name}.json（指纹保留，免重建）")
        except OSError as e:
            log(f"迁移：指纹文件改名失败（{e}），下次索引将自动全量重建")
    save_registry([entry])
    log(f"已自动注册首个库：{name} → {vault}")
    return [entry]


def effective_config(entry):
    """库的生效配置 = 全局默认值 + 该库覆盖；含派生的 collection 与 meta 路径。"""
    cfg = {
        "name": entry["name"],
        "path": entry["path"],
        "collection": collection_for(entry["name"], entry.get("collection")),
        "exclude_dirs": list(CFG["exclude_dirs"]),
        "exclude_files": list(CFG["exclude_files"]),
        "exclude_patterns": list(CFG["exclude_patterns"]),
        "chunk_char_limit": CFG["chunk_char_limit"],
        "short_doc_char_limit": CFG["short_doc_char_limit"],
        "extensions": entry.get("extensions") or ["md"],
    }
    for k in GLOBAL_KEYS:
        v = entry.get(k)
        if v is not None:
            cfg[k] = v
    return cfg


def add_library(path, name=None):
    entries = load_registry()
    p = str(Path(path).resolve())
    if not Path(p).is_dir():
        raise ValueError(f"路径不存在或不是目录：{p}")
    if name is None:
        name = Path(p).name or "default"
    validate_name(name)
    if any(e["name"] == name for e in entries):
        raise ValueError(f"库名已存在：{name}")
    if any(str(Path(e["path"]).resolve()) == p for e in entries):
        raise ValueError(f"该路径已注册：{p}")
    col = collection_for(name, None)
    if any(effective_config(e)["collection"] == col for e in entries):
        raise ValueError(f"派生 collection 与现有库冲突：{col}（可换 --name 或手动设 collection）")
    entry = _blank_entry(name, p)
    entries.append(entry)
    save_registry(entries)
    return entry


def remove_library(name, drop=False, yes=False):
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    if drop:
        if not yes:
            try:
                ans = input(f"将删除库 {name} 的 Chroma collection 与指纹文件，数据不可恢复。继续？[y/N] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans != "y":
                raise RuntimeError("用户取消")
        col = effective_config(entry)["collection"]
        try:
            import chromadb
            client = chromadb.PersistentClient(path=str(DATA_DIR / "chroma"))
            try:
                client.delete_collection(col)
                log(f"已删除 collection：{col}")
            except Exception:
                pass
        except Exception as e:
            log(f"删除 collection 失败（忽略）：{e}")
        try:
            meta_path(name).unlink(missing_ok=True)
        except OSError:
            pass
    entries = [e for e in entries if e["name"] != name]
    save_registry(entries)
    return entry


def _parse_value(key, value):
    if key in LIST_KEYS:
        return [v.strip() for v in value.split(",") if v.strip()]
    if key in INT_KEYS:
        return int(value)
    return value


def set_config(name, key, value):
    if key not in OVERRIDE_KEYS:
        raise ValueError(f"非法配置键：{key}（合法：{', '.join(OVERRIDE_KEYS)}）")
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    parsed = _parse_value(key, value)
    if key == "extensions":
        # 白名单校验引用 extractors.SUPPORTED_EXTS（单一事实来源）；
        # 小写归一 + 去重 + 保序，堵手滑写出的 .PDF / PDF / 大小写混排
        exts = parsed if isinstance(parsed, list) else []
        norm = []
        for e in exts:
            e2 = str(e).lower().lstrip(".")
            if e2 and e2 not in norm:
                norm.append(e2)
        bad = [e for e in norm if e not in SUPPORTED_EXTS]
        if bad:
            raise ValueError(f"不支持的扩展名：{', '.join(bad)}"
                             f"（当前支持：md, txt, {', '.join(sorted(SUPPORTED_EXTS - {'md', 'txt'}))}；"
                             f"扫描件 OCR 计划末轮）")
        if not norm:
            raise ValueError("extensions 不能为空")
        parsed = norm
    if key == "collection":
        if not isinstance(parsed, str) or not parsed:
            raise ValueError("collection 不能为空")
        if not (3 <= len(parsed) <= 63) or not re.fullmatch(r"[a-zA-Z0-9._-]+", parsed):
            raise ValueError(f"collection 非法（须 3-63 字符，仅含字母/数字/._-）：{parsed}")
        for e in entries:
            if e["name"] != name and effective_config(e)["collection"] == parsed:
                raise ValueError(f"collection 与现有库冲突：{parsed}")
    entry[key] = parsed
    save_registry(entries)
    return entry


def unset_config(name, key):
    if key not in OVERRIDE_KEYS:
        raise ValueError(f"非法配置键：{key}（合法：{', '.join(OVERRIDE_KEYS)}）")
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    entry[key] = None
    save_registry(entries)
    return entry


def list_summary():
    """注册表摘要（供 list_libraries 工具 / library.py list）：块数与最近索引不加载模型。"""
    entries = load_registry()
    rows = []
    client = None
    try:
        import chromadb
        client = chromadb.PersistentClient(path=str(DATA_DIR / "chroma"))
    except Exception as e:
        log(f"Chroma 连接失败（块数将显示 -）：{e}")
    for e in entries:
        cfg = effective_config(e)
        blocks = 0
        if client is not None:
            try:
                blocks = client.get_collection(cfg["collection"]).count()
            except Exception:
                blocks = 0  # collection 未创建 = 从未索引
        last = None
        mp = meta_path(e["name"])
        try:
            last = mp.stat().st_mtime
        except OSError:
            pass
        overrides = {k: v for k, v in e.items() if k in OVERRIDE_KEYS and v is not None}
        rows.append({
            "name": cfg["name"],
            "path": cfg["path"],
            "collection": cfg["collection"],
            "blocks": blocks,
            "last_indexed": last,
            "overrides": ",".join(f"{k}={v!r}" for k, v in overrides.items()),
        })
    return rows


def resolve_entries(libraries="", exclude="", defaults=None):
    """按白名单减法解析检索库列表；未知库名 / 空集抛 ValueError（含可用库名清单）。

    defaults：libraries 为空（未显式指定）时的默认范围（来自 config 的
    default_libraries）。defaults 中的库不存在于注册表则忽略；全部失效则回退
    全部库（向后兼容）。显式传 libraries 时 defaults 不参与。
    """
    entries = load_registry()
    by_name = {e["name"]: e for e in entries}
    if not by_name:
        raise ValueError("当前没有已注册库。请先用 library.py add <路径> 注册。")
    if libraries:
        names = list(dict.fromkeys(n.strip() for n in libraries.split(",") if n.strip()))
        if names and names[0].lower() == "all":
            names = list(by_name)
    elif defaults:
        names = [n for n in defaults if n in by_name]
        if not names:
            names = list(by_name)  # 默认库全部失效 → 回退全部库（旧行为）
    else:
        names = list(by_name)
    ex = [n.strip() for n in exclude.split(",") if n.strip()]
    unknown = sorted({n for n in names + ex if n not in by_name})
    if unknown:
        raise ValueError(f"未知库名：{', '.join(unknown)}。可用库：{', '.join(sorted(by_name))}")
    final = [n for n in names if n not in ex]
    if not final:
        raise ValueError(f"所选范围为空：exclude 覆盖了全部指定库"
                         f"（libraries={libraries or '全部'}，exclude={exclude or '无'}）。"
                         f"可用库：{', '.join(sorted(by_name))}")
    return [by_name[n] for n in final]


def _fmt_ts(ts):
    if not ts:
        return "从未"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def main():
    ap = argparse.ArgumentParser(description="多库注册表管理（库 = 一个可检索的 md 文件夹）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", aliases=["ls"], help="列出全部库")

    p_add = sub.add_parser("add", help="注册新库")
    p_add.add_argument("path", help="文件夹绝对路径")
    p_add.add_argument("--name", default=None, help="库名（默认取文件夹名）")

    p_rm = sub.add_parser("remove", help="注销库（默认仅从注册表移除，数据保留）")
    p_rm.add_argument("name")
    p_rm.add_argument("--drop", action="store_true", help="同时删除 Chroma collection 与指纹文件")
    p_rm.add_argument("--yes", action="store_true", help="跳过删除确认（AI 非交互执行）")

    p_cfg = sub.add_parser("config", help="查看/修改库独立配置（null=继承全局）")
    p_cfg.add_argument("name")
    p_cfg.add_argument("--set", metavar="key=value", help="设置（列表键用逗号分隔）")
    p_cfg.add_argument("--unset", metavar="key", help="恢复继承全局")

    args = ap.parse_args()
    try:
        if args.cmd in ("list", "ls"):
            rows = list_summary()
            if not rows:
                log("（暂无注册库。用 library.py add <路径> 注册第一个库）")
                return
            for r in rows:
                over = f"（覆盖：{r['overrides']}）" if r["overrides"] else ""
                print(f"{r['name']:<20} 块={r['blocks'] if r['blocks'] >= 0 else '?'}  "
                      f"最近索引={_fmt_ts(r['last_indexed'])}  {r['path']} {over}")
            return
        if args.cmd == "add":
            entry = add_library(args.path, args.name)
            log(f"已注册库：{entry['name']} → {entry['path']}")
            log(f"collection：{collection_for(entry['name'], entry['collection'])}")
            log("下一步：python index.py --library <名> 建索引后即可检索")
            return
        if args.cmd == "remove":
            remove_library(args.name, drop=args.drop, yes=args.yes)
            log(f"已注销库：{args.name}" + ("（数据已删除）" if args.drop else "（数据保留）"))
            return
        if args.cmd == "config":
            if args.set and args.unset:
                raise ValueError("--set 与 --unset 不能同时使用")
            if args.set:
                key, _, value = args.set.partition("=")
                if not value:
                    raise ValueError("--set 格式：key=value")
                entry = set_config(args.name, key.strip(), value.strip())
                log(f"库 {args.name}：{key.strip()} = {entry[key.strip()]!r}")
            elif args.unset:
                entry = unset_config(args.name, args.unset.strip())
                log(f"库 {args.name}：{args.unset.strip()} 恢复继承全局")
            else:
                entries = load_registry()
                entry = next((e for e in entries if e["name"] == args.name), None)
                if entry is None:
                    raise ValueError(f"库不存在：{args.name}")
                cfg = effective_config(entry)
                print(f"库名：{cfg['name']}")
                print(f"路径：{cfg['path']}")
                print(f"collection：{cfg['collection']}")
                print(f"扩展名：{cfg['extensions']}")
                for k in GLOBAL_KEYS:
                    v = entry.get(k)
                    tag = "" if v is None else "（覆盖）"
                    print(f"{k}：{cfg[k]}{tag}")
            return
    except (ValueError, RuntimeError) as e:
        log(f"错误：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
