"""library.py — 多库注册表（data/libraries.json）：增删改查 + 生效配置合并 + 检索选择解析。

CLI 入口（AI 可调）：
  python library.py list
  python library.py add <路径> [--name 名]
  python library.py remove <名> [--drop] [--yes]
  python library.py config <名> --set key=value [--unset key]

每库可覆盖字段：exclude_dirs / exclude_files / exclude_patterns /
chunk_char_limit / short_doc_char_limit / extensions / collection /
agent_formats（Agent 长期授权的二进制格式；注意持久键叫 agent_formats，
agent_allowed 只是 index_library/server 的运行期参数名，不要混用）。
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
import time
from pathlib import Path

from config import CFG, DATA_DIR
from extractors import BINARY_EXTS, SUPPORTED_EXTS

LIBRARIES_FILE = DATA_DIR / "libraries.json"

# 多格式默认开启：库未显式设置 extensions 时的生效清单（人经 GUI/CLI 可随时改）
DEFAULT_EXTENSIONS = ["md", "pdf", "docx"]

GLOBAL_KEYS = ("exclude_dirs", "exclude_files", "exclude_patterns",
               "chunk_char_limit", "short_doc_char_limit")
OVERRIDE_KEYS = GLOBAL_KEYS + ("extensions", "collection", "agent_formats")
LIST_KEYS = ("exclude_dirs", "exclude_files", "exclude_patterns", "extensions",
             "agent_formats")
INT_KEYS = ("chunk_char_limit", "short_doc_char_limit")
INVALID_NAME_CHARS = set('/\\:*?"<>|')

# ---------------------------------------------------------------------------
# 库内路径级勾选（问题44）：selection_in / selection_out 存注册表条目，
# 不进 OVERRIDE_KEYS（不走 set_config 的逗号分割——路径可能含逗号），
# 专用 set_selection / format_selection_bulk 增删，读侧经 effective_config 规范化。
# 语义（问题47 谁具体听谁的）：最近显式命中 vs 目录排除按深度，更具体的赢；
# 同位置打架（纳入目标本身在 exclude_dirs 里）排除站住，set_selection 拒绝新建；
# 文件名/格式类规则一律最弱，显式静默穿透。裁决唯一实现 decide_included，
# 显示（bridge）与漏斗（collect）共用，勿各写一份。
# ---------------------------------------------------------------------------
SELECTION_ACTIONS = ("in", "out", "neutral")


def norm_sel_path(p):
    """勾选路径规范化：反斜杠→/、剥首尾 / 与空白；拒绝绝对路径与 .. 逃逸。

    返回不带尾斜杠的相对路径（文件夹与文件同形——解析靠前缀匹配，无需区分）。
    只接受 str（int/dict 等一律拒绝——读侧防御手改，写侧拒绝 agent 手滑）。
    """
    if not isinstance(p, str):
        raise ValueError(f"勾选路径必须是字符串：{p!r}")
    raw = p
    s = raw.strip().replace("\\", "/")
    if s.startswith("/") or s.startswith("//") or re.match(r"^[A-Za-z]:", s) \
            or s.startswith("~"):
        raise ValueError(f"必须是库内相对路径（不含盘符/UNC/~/正斜杠根）：{p!r}")
    s = s.strip("/")
    if not s:
        raise ValueError("勾选路径不能为空")
    parts = s.split("/")
    if any(part.strip() in ("", ".", "..") for part in parts):
        raise ValueError(f"路径非法（不得含空段/./..）：{p!r}")
    return "/".join(part.strip() for part in parts)


def selection_hit(sel_in, sel_out, rel):
    """最近显式命中 → (action, depth, prefix)；无命中 → (None, 0, "")。

    depth = 命中的前缀段数（文件自身 = 全长，最具体）。同一层级同时命中两表
    属非法状态（set_selection 已防止），此处 out 优先（宁可少索引）。
    """
    sin = set(sel_in or ())
    sout = set(sel_out or ())
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    for i in range(len(parts), 0, -1):
        pre = "/".join(parts[:i])
        if pre in sout:
            return ("out", i, pre)
        if pre in sin:
            return ("in", i, pre)
    return (None, 0, "")


def resolve_selection(sel_in, sel_out, rel):
    """最近显式赢：从 rel 自身逐级向上找第一个出现在任一列表的祖先（含自身）。

    返回 "in" | "out" | None（中性 = 无任何显式选择覆盖）。同一层级同时命中
    两表属于非法状态（set_selection 已防止），此处 out 优先（宁可少索引）。
    """
    action, _, _ = selection_hit(sel_in, sel_out, rel)
    return action


def norm_ex_dir_entries(ex_dirs):
    """目录排除条目归一集合（问题47：同位置判定用）。

    注意 collect 漏斗侧是子串语义（条目 'TEMP' 会命中部件 'MYTEMP'），此处
    只做字符串归一不改语义：同位置打架只认字符串相等，子串覆盖面走深度规则。
    """
    out = set()
    for e in (ex_dirs or ()):
        s = str(e).replace("\\", "/").strip().strip("/")
        if s:
            out.add(s)
    return out


def excluded_dir_depth(ex_dirs, rel):
    """目录排除的最深命中深度（1-based；无命中 0）。

    匹配语义与 collect 漏斗逐字一致：条目为任一路径部件的子串即命中，取最深
    的部件位置。单文件库根（无斜杠）depth=1。
    """
    best = 0
    parts = str(rel).replace("\\", "/").strip("/").split("/")
    entries = [s for s in (str(e).replace("\\", "/").strip().strip("/")
                           for e in (ex_dirs or ())) if s]
    if not entries:
        return 0
    for i, part in enumerate(parts, 1):
        for en in entries:
            if en in part:
                best = i
                break
    return best


def decide_included(sel_in, sel_out, ex_dirs, rel):
    """谁具体听谁的（问题47 用户拍板）：返回 ("in"|"out"|None, tie)。

    - 最近显式命中 M（深度 dm）vs 最深目录排除 E（深度 de，无=0）：
      M 存在且 dm > de → 跟随 M（in/out），tie=False——窄例外静默生效；
      M 存在且 M 命中的前缀字符串恰在排除名单里 → ("out", True)——同位置
      打架的手工态，排除站住（安全），UI/MCP 侧拒绝新建此类状态；
      其余（无 M 且 E>0；M 存在但 de 更深）→ ("out", False)；
      无 M 无 E → (None, False)，调用方走文件名/格式默认。
    - 文件名/格式类规则（exclude_files/patterns/extensions）一律视为最弱：
      显式永远静默穿透它们（AGENT 例：全局按名排除某类文档，点名要其中
      一份即生效，不弹窗）。
    """
    action, dm, prefix = selection_hit(sel_in, sel_out, rel)
    de = excluded_dir_depth(ex_dirs, rel)
    if action is None:
        return ("out", False) if de else (None, False)
    if action == "out":
        return ("out", False)
    normed = norm_ex_dir_entries(ex_dirs)
    if prefix in normed:
        return ("out", True)
    if de == 0 or dm > de:
        return ("in", False)
    return ("out", False)


def is_same_place_blocked(ex_dirs, rel):
    """该节点本身是否被目录排除名单指名道姓（问题47：即时弹窗触发条件）。

    只认字符串相等（单段目录名最常见）；文件名/格式类规则是"按类匹配"，
    点具体文件属个别例外，永远不触发。调用方另需确认 action == "in"。
    """
    s = str(rel).replace("\\", "/").strip().strip("/")
    return bool(s) and s in norm_ex_dir_entries(ex_dirs)


# ---------------------------------------------------------------------------
# 库简介（问题60）：导航/澄清性质的一段话，帮 AI 在通读全文前判断"这个库
# 值不值得查"。source=none（从未生成）/ ai（生成写入，可被覆盖）/
# user（用户手写，覆盖前须走 summary_gate 两段式确认，见 server.py）。
# 内容指纹 fingerprint 由 library_summary.content_fingerprint 计算（依赖
# index.load_meta，library.py 不反向导入 index 以免循环导入，故只存不算）。
# 唯一写入口 set_library_summary：GUI 直改、GUI「刷新简介」、MCP 门禁通过后
# 的写入都经这里，语义收口在一处（同 set_selection 的先例）。
# ---------------------------------------------------------------------------
SUMMARY_SOURCES = ("none", "ai", "user")
SUMMARY_MAX_CHARS = 300
_BLANK_SUMMARY = {"text": "", "source": "none", "updated_at": None,
                  "fingerprint": None, "model": None}


def get_library_summary(entry):
    """条目的简介（读侧防御：非 dict/字段缺失一律回退空白态）。"""
    s = entry.get("summary")
    if not isinstance(s, dict):
        return dict(_BLANK_SUMMARY)
    out = dict(_BLANK_SUMMARY)
    out.update({k: s.get(k) for k in _BLANK_SUMMARY if k in s})
    if out["source"] not in SUMMARY_SOURCES:
        out["source"] = "ai" if out.get("text") else "none"
    if not isinstance(out.get("text"), str):
        out["text"] = ""
    return out


def set_library_summary(name, text, source, fingerprint=None, model=None):
    """写简介（唯一落盘口）。source 必须是 ai/user；none 只用于表示"未生成"，
    不通过本函数写入（清空简介用本函数写 text=""）。"""
    if source not in ("ai", "user"):
        raise ValueError(f"非法 source：{source!r}（合法：ai/user）")
    if not isinstance(text, str):
        raise ValueError("简介必须是字符串")
    text = text.strip()
    if len(text) > SUMMARY_MAX_CHARS:
        raise ValueError(f"简介超长（{len(text)} 字，上限 {SUMMARY_MAX_CHARS} 字）：请精简后再写入")
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    entry["summary"] = {"text": text, "source": source, "updated_at": time.time(),
                        "fingerprint": fingerprint, "model": model}
    save_registry(entries)
    return entry


def _sel_entry_lists(entry):
    """条目的勾选两表（读侧防御：非列表/非法路径元素静默丢弃，防手改逃逸）。"""
    out = []
    for key in ("selection_in", "selection_out"):
        vals = entry.get(key)
        cleaned = []
        if isinstance(vals, list):
            for v in vals:
                try:
                    cleaned.append(norm_sel_path(v))
                except ValueError:
                    continue
        out.append(cleaned)
    return out[0], out[1]


def set_selection(name, changes):
    """应用勾选变更并落盘。changes = [{"path": rel, "action": "in"|"out"|"neutral"}]。

    校验：库存在、路径合法且确实位于库内磁盘目录下（防 MCP 提案逃逸到库外）。
    同一路径多条变更按顺序生效（后写覆盖先写）。返回更新后的条目 dict。
    """
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    root = Path(entry["path"]).resolve()
    if not root.is_dir():
        raise ValueError(f"库路径不存在：{root}")
    sin, sout = _sel_entry_lists(entry)
    sin_set, sout_set = set(sin), set(sout)
    for ch in changes or []:
        rel = norm_sel_path((ch or {}).get("path"))
        action = (ch or {}).get("action")
        if action not in SELECTION_ACTIONS:
            raise ValueError(f"非法 action：{action!r}（合法：{'/'.join(SELECTION_ACTIONS)}）")
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise ValueError(f"路径越出库范围：{rel}")
        sin_set.discard(rel)
        sout_set.discard(rel)
        if action == "in":
            sin_set.add(rel)
        elif action == "out":
            sout_set.add(rel)
        # neutral：已在上面的 discard 中移除显式记录
    # 同位置打架拒绝（问题47 用户拍板）：纳入的目标本身就躺在目录排除名单
    # 里（字符串相等，指名道姓），落盘即制造"存上但永远不生效"的矛盾态。
    # 调用方（GUI 即时弹窗 / MCP 提案校验）应在事前拦截，此处是最后兜底。
    # 文件名/格式类规则不在此列——点具体文件属个别例外，静默生效。
    eff_ex_dirs = effective_config(entry)["exclude_dirs"]
    blocked = norm_ex_dir_entries(eff_ex_dirs)
    clash = sorted(r for r in sin_set if r in blocked)
    if clash:
        raise ValueError(
            "勾选与排除名单打架（同位置矛盾）：%s 已在目录排除名单（exclude_dirs）里，"
            "纳入不会生效。请先从排除名单移除（库配置 exclude_dirs，仅本库生效即可），"
            "或改勾它下面的具体文件（个别例外直接生效，无需弹窗）。"
            % "、".join(clash))
    entry["selection_in"] = sorted(sin_set)
    entry["selection_out"] = sorted(sout_set)
    save_registry(entries)
    return entry


def format_selection_bulk(name, ext, include):
    """格式批量语义（问题44 用户拍板③：全局开关 = 对该格式文件的批量勾/取消）。

    include=False：selection_in 中扩展名为 ext 的**文件级**条目移入 selection_out
    （用户显式勾选的该格式文件跟着取消）；include=True：selection_out 中该格式的
    文件级条目移除。文件夹级条目永不被批量操作触碰。
    返回受影响的条目数。ext 大小写不敏感（内部小写归一）。
    """
    entries = load_registry()
    entry = next((e for e in entries if e["name"] == name), None)
    if entry is None:
        raise ValueError(f"库不存在：{name}")
    ext_l = str(ext or "").lower().lstrip(".")

    def _file_fmt(p):
        # 文件级判定按扩展名（子目录里的文件同样含斜杠，不能以 "/" 有无区分；
        # 文件夹条目极少以 .ext 结尾，误伤面可忽略）
        return p.lower().endswith("." + ext_l)

    sin, sout = _sel_entry_lists(entry)
    if include:
        kept_out = [p for p in sout if not _file_fmt(p)]
        if len(kept_out) == len(sout):
            return 0
        entry["selection_out"] = sorted(kept_out)
        save_registry(entries)
        return len(sout) - len(kept_out)
    moved = [p for p in sin if _file_fmt(p)]
    if not moved:
        return 0
    entry["selection_in"] = sorted(p for p in sin if not _file_fmt(p))
    entry["selection_out"] = sorted(set(sout) | set(moved))
    save_registry(entries)
    return len(moved)


def bulk_for_extensions(name, old_exts, new_exts):
    """extensions 变更 → 批量勾选语义收口（单一漏斗：GUI/CLI/MCP 都经 set_config）。

    被移除的格式：显式勾选的文件跟着取消（移入 selection_out，用户拍板③）；
    新增的格式：selection_out 中的该格式文件条目移除（恢复跟随=纳入）。
    """
    old = {e.lower().lstrip(".") for e in (old_exts or [])}
    new = {e.lower().lstrip(".") for e in (new_exts or [])}
    n = 0
    for fmt in sorted(old - new):
        n += format_selection_bulk(name, fmt, include=False)
    for fmt in sorted(new - old):
        n += format_selection_bulk(name, fmt, include=True)
    return n



def log(*args):
    print(*args, file=sys.stderr)


def _blank_entry(name, path):
    return {k: None for k in OVERRIDE_KEYS} | {
        "name": name, "path": path, "selection_in": [], "selection_out": [],
        "summary": None}


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
    """库的生效配置 = 全局默认值 + 该库覆盖；含派生的 collection 与 meta 路径。

    extensions 缺省继承 DEFAULT_EXTENSIONS（多格式默认开启）；
    agent_formats = Agent 获准索引的二进制格式，与当前 extensions 取交集
    （用户在 extensions 里取消某格式时，授权自动随之失效——单一事实来源是 extensions）。
    """
    exts = list(entry.get("extensions") or DEFAULT_EXTENSIONS)
    approved = set(entry.get("agent_formats") or [])
    cfg = {
        "name": entry["name"],
        "path": entry["path"],
        "collection": collection_for(entry["name"], entry.get("collection")),
        "exclude_dirs": list(CFG["exclude_dirs"]),
        "exclude_files": list(CFG["exclude_files"]),
        "exclude_patterns": list(CFG["exclude_patterns"]),
        "chunk_char_limit": CFG["chunk_char_limit"],
        "short_doc_char_limit": CFG["short_doc_char_limit"],
        "extensions": exts,
        "agent_formats": [f for f in exts if f in approved],
    }
    for k in GLOBAL_KEYS:
        v = entry.get(k)
        if v is not None:
            cfg[k] = v
    # 问题44 路径级勾选：读侧规范化 + 中性默认（config.selection_new_files）
    sel_in, sel_out = _sel_entry_lists(entry)
    cfg["selection_in"] = sel_in
    cfg["selection_out"] = sel_out
    cfg["selection_default"] = CFG.get("selection_new_files", "follow")
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
    norm: list = []
    if key in ("extensions", "agent_formats"):
        # 白名单校验引用 extractors.SUPPORTED_EXTS（单一事实来源）；
        # 小写归一 + 去重 + 保序，堵手滑写出的 .PDF / PDF / 大小写混排
        exts = parsed if isinstance(parsed, list) else []
        norm = []
        for e in exts:
            e2 = str(e).lower().lstrip(".")
            if e2 and e2 not in norm:
                norm.append(e2)
    old_exts = None
    if key == "extensions":
        bad = [e for e in norm if e not in SUPPORTED_EXTS]
        if bad:
            raise ValueError(f"不支持的扩展名：{', '.join(bad)}"
                             f"（当前支持：md, txt, {', '.join(sorted(SUPPORTED_EXTS - {'md', 'txt'}))}；"
                             f"扫描件 OCR 计划末轮）")
        if not norm:
            raise ValueError("extensions 不能为空")
        parsed = norm
        old_exts = list(entry.get("extensions") or DEFAULT_EXTENSIONS)
    if key == "agent_formats":
        # Agent 授权清单：仅二进制格式、且须已在当前 extensions 中启用；
        # 授权随 extensions 收窄自动失效（effective_config 取交集），此处挡手误。
        # 允许为空 = 收回全部授权。
        cur_exts = set(entry.get("extensions") or DEFAULT_EXTENSIONS)
        bad_fmt = [e for e in norm if e not in BINARY_EXTS]
        if bad_fmt:
            raise ValueError(f"agent_formats 仅接受二进制格式"
                             f"（{', '.join(sorted(BINARY_EXTS))}）：{', '.join(bad_fmt)}")
        not_on = [e for e in norm if e not in cur_exts]
        if not_on:
            raise ValueError(f"以下格式未在该库 extensions 中启用，无法授权："
                             f"{', '.join(not_on)}")
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
    if key == "extensions":
        # 问题44 批量勾选语义收口：格式移除 → 显式勾选的该格式文件移入
        # selection_out；格式新增 → selection_out 中该格式文件条目移除。
        # 必须在 save 之后调（其内部重读注册表做读改写，先调会被本次 save 覆盖）。
        bulk_for_extensions(name, old_exts, parsed)
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
            "summary": get_library_summary(e),
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
                for k in GLOBAL_KEYS:
                    v = entry.get(k)
                    tag = "" if v is None else "（覆盖）"
                    print(f"{k}：{cfg[k]}{tag}")
                for k in ("extensions", "agent_formats", "collection"):
                    v = entry.get(k)
                    tag = "" if v is None else "（覆盖）"
                    print(f"{k}：{cfg[k]}{tag}")
            return
    except (ValueError, RuntimeError) as e:
        log(f"错误：{e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
