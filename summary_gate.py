"""summary_gate.py — 库简介覆盖提案的确认门禁（问题60，仿 selection_gate.py）。

用户手写的简介（library.get_library_summary(...)["source"] == "user"）优先权
最高：AI（对话中的 agent，经 server.py 的 propose_library_summary/
apply_library_summary）想覆盖它，必须走两段式确认——propose 生成提案
（含 6 位确认码，10 分钟有效）→ apply 校验码 + TTL + 一次性消费后放行，
与 selection_gate.py 完全同一套机制，两个门禁互不依赖、各管各的数据面。

source 为 none/ai 时不需要这条门禁：调用方（server.py）应直接写，不必
先 propose——门禁只保护"AI 想覆盖用户已写内容"这一种场景。用户自己在 GUI
编辑框手动改并保存同样不走这里，无条件生效（不存在"用户向自己确认"的说法）。
"""
import hashlib
import secrets
import time

from config import DATA_DIR
from library import SUMMARY_MAX_CHARS

PENDING_FILE = DATA_DIR / "summary_pending.json"
TTL_S = 600.0


class GateError(ValueError):
    """提案校验失败（原因在消息里，直接透出给 agent/用户）。"""


def _pending_load():
    import json
    try:
        d = json.loads(PENDING_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _pending_save(d):
    import json
    DATA_DIR.mkdir(exist_ok=True)
    tmp = PENDING_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PENDING_FILE)


def make_proposal(lib_name, text):
    """生成提案：写盘（提案号 + 码哈希 + 待写文本），返回 (提案号, 明文确认码, diff 文本)。

    明文确认码只出现在返回值里（agent 必须转述给用户）；盘上只存哈希。
    """
    if not isinstance(text, str) or not text.strip():
        raise GateError("简介文本不能为空")
    text = text.strip()
    if len(text) > SUMMARY_MAX_CHARS:
        raise GateError(f"简介超长（{len(text)} 字，上限 {SUMMARY_MAX_CHARS} 字）：请精简后再提案")
    proposal_id = "sum-" + secrets.token_hex(4)
    code = "".join(str(secrets.randbelow(10)) for _ in range(6))
    _pending_save({
        "proposal_id": proposal_id,
        "library": lib_name,
        "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
        "created": time.time(),
        "text": text,
    })
    lines = [
        f"库「{lib_name}」当前简介是用户手写的，覆盖需要确认。",
        f"新简介：{text}",
        f"提案号：{proposal_id}（10 分钟内有效）",
        f"确认码：{code}",
        "请把新简介完整展示给用户；用户明确同意后，携带提案号与确认码"
        "调用 apply_library_summary 生效。用户未同意则不得调用。",
    ]
    return proposal_id, code, "\n".join(lines)


def consume_proposal(lib_name, proposal_id, code):
    """校验提案号 + 确认码 + TTL，通过则消费（一次性）并返回待写文本。

    失败抛 GateError（提案不存在/库名不匹配/过期/确认码错误）。
    """
    pend = _pending_load()
    if not pend or pend.get("proposal_id") != proposal_id:
        raise GateError("提案不存在或已失效：请先 propose_library_summary 重新提案")
    if pend.get("library") != lib_name:
        raise GateError("提案与库名不匹配")
    if time.time() - pend.get("created", 0) > TTL_S:
        PENDING_FILE.unlink(missing_ok=True)
        raise GateError("提案已过期（>10 分钟）：请重新 propose 并重新向用户确认")
    digest = hashlib.sha256(str(code).strip().encode()).hexdigest()
    if digest != pend.get("code_sha256"):
        raise GateError("确认码错误：以 propose 返回的 6 位数字为准")
    text = pend.get("text") or ""
    PENDING_FILE.unlink(missing_ok=True)
    return text
