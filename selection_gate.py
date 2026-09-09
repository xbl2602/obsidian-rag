"""selection_gate.py — 勾选变更提案的确认门禁（问题44，问题只管硬门禁数据面）。

两段式：propose 生成提案（含 6 位确认码，10 分钟有效）→ apply 校验码 + TTL +
一次性消费后放行。故意不依赖 mcp SDK：server.py 的工具只是薄封装，本模块可独立
单测。诚实边界：agent 理论上可以不真问用户直接带码 apply——这是 MCP 通道的信任
边界；两段式 + 过期 + 一次性 + 审计日志把"未经确认就生效"的风险压到最低。
"""
import hashlib
import json
import secrets
import time
from pathlib import Path

from config import DATA_DIR
from extractors import SUPPORTED_EXTS
from library import norm_sel_path, resolve_selection

PENDING_FILE = DATA_DIR / "selection_pending.json"
TTL_S = 600.0
ACTIONS = ("in", "out", "neutral")
_ACTION_TEXT = {"in": "已入库（显式勾选）", "out": "已排除（显式取消）",
                "neutral": "中性（跟随格式开关）"}


class GateError(ValueError):
    """提案校验失败（原因在消息里，直接透出给 agent/用户）。"""


def _pending_load():
    try:
        d = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pending_save(d):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def effective_state_text(cfg, rel):
    """路径的现生效态（人读文案）：显式勾选/取消 > 中性默认 > 格式开关。"""
    v = resolve_selection(cfg.get("selection_in"), cfg.get("selection_out"), rel)
    if v == "in":
        return "已入库（显式勾选）"
    if v == "out":
        return "已排除（显式取消）"
    ext = rel.rsplit(".", 1)[-1].lower() if "." in rel.rsplit("/", 1)[-1] else ""
    if ext not in SUPPORTED_EXTS:
        return "不在库（不受支持的格式）"
    if cfg.get("selection_default") == "exclude":
        return "已排除（中性默认=exclude）"
    return "已入库（格式开关）" if ext in (cfg.get("extensions") or []) \
        else "已排除（格式开关）"


def normalize_changes(cfg_entry, changes):
    """校验并规范化变更列表：路径合法 + 在库内 + action 合法；整体通过或整体拒绝。

    问题47 同位置打架事前拦截：action=in 且目标本身就在目录排除名单里
    （字符串相等，指名道姓）→ 直接拒绝并指引先清排除（set_library_config
    改 exclude_dirs，仅本库覆盖即可），而不是等到 apply 才失败——确认码
    不应该花在注定无效的提案上。文件名/格式类规则不在此列：点具体文件属
    个别例外，静默生效（与漏斗/显示同一规则）。
    """
    from library import norm_ex_dir_entries
    root = Path(cfg_entry["path"]).resolve()
    blocked = norm_ex_dir_entries((cfg_entry.get("exclude_dirs") or []))
    norm = []
    for ch in changes or []:
        rel = norm_sel_path((ch or {}).get("path"))
        action = (ch or {}).get("action")
        if action not in ACTIONS:
            raise GateError(f"非法 action：{action!r}（只接受 {'/'.join(ACTIONS)}）")
        target = (root / rel).resolve()
        if root != target and root not in target.parents:
            raise GateError(f"路径越出库范围，已拒绝：{rel}")
        if action == "in" and rel in blocked:
            raise GateError(
                f"同位置矛盾已拒绝：{rel} 本身就在目录排除名单（exclude_dirs）里，"
                f"纳入不会生效。请先用 set_library_config 去掉 exclude_dirs 中的"
                f"这一项（仅本库覆盖即可，不影响全局与其他库），再重新提案；"
                f"或改勾它下面的具体文件（个别例外直接生效）。")
        norm.append({"path": rel, "action": action})
    if not norm:
        raise GateError("changes 为空：至少提供一项 {path, action}")
    return norm


def make_proposal(lib_name, cfg, changes):
    """生成提案：写盘（提案号 + 码哈希 + 变更），返回 (提案号, 明文确认码, diff 文本)。

    明文确认码只出现在返回值里（agent 必须转述给用户）；盘上只存哈希。
    """
    norm = normalize_changes(cfg, changes)
    lines = [f"库「{lib_name}」勾选变更提案："]
    for ch in norm:
        before = effective_state_text(cfg, ch["path"])
        after = _ACTION_TEXT[ch["action"]]
        mark = "（状态不变）" if before == after else ""
        lines.append(f"  [{ch['action']:>7}] {ch['path']}：{before} → {after} {mark}")
    proposal_id = "sel-" + secrets.token_hex(4)
    code = "".join(str(secrets.randbelow(10)) for _ in range(6))
    _pending_save({
        "proposal_id": proposal_id,
        "library": lib_name,
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "created": time.time(),
        "changes": norm,
    })
    lines.append(f"提案号：{proposal_id}（10 分钟内有效）")
    lines.append(f"确认码：{code}")
    lines.append("请把以上变更清单展示给用户；用户明确同意后，携带提案号与确认码"
                 "调用 apply_selection_changes 生效。用户未同意则不得调用。")
    return proposal_id, code, "\n".join(lines)


def consume_proposal(lib_name, proposal_id, code):
    """校验提案号 + 确认码 + TTL，通过则消费（一次性）并返回变更列表。

    失败抛 GateError（提案不存在/库名不匹配/过期/确认码错误）。确认码错误记为
    可审计事件（调用方负责写审计日志）。
    """
    pend = _pending_load()
    if not pend or pend.get("proposal_id") != proposal_id:
        raise GateError("提案不存在或已失效：请先 propose_selection_changes 重新提案")
    if pend.get("library") != lib_name:
        raise GateError("提案与库名不匹配")
    if time.time() - pend.get("created", 0) > TTL_S:
        PENDING_FILE.unlink(missing_ok=True)
        raise GateError("提案已过期（>10 分钟）：请重新 propose 并重新向用户确认")
    digest = hashlib.sha256(str(code).strip().encode()).hexdigest()
    if digest != pend.get("code_sha256"):
        raise GateError("确认码错误：以 propose 返回的 6 位数字为准；已记入审计日志")
    changes = pend.get("changes") or []
    PENDING_FILE.unlink(missing_ok=True)
    return changes
