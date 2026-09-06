"""bridge.py — pywebview js_api 桥：前端可调用的全部方法（契约见 contracts.md）。

设计约束（与 AGENTS.md 红线一致）：
- GUI 是零侵入观察者：不加载模型做索引、不写 Chroma；检索/语义边只走读路径，
  在调用线程内阻塞执行（pywebview 每个api调用独立线程，不卡窗口）。
- 索引走 worker.IndexWorker 子进程；stop 只停本进程拉起的子进程。
- 提取试验台走 multiprocessing 隔离子进程 + 一次性临时缓存目录（父进程清理）。
"""
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GUI_DIR = ROOT / "gui"
for _p in (str(ROOT), str(GUI_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import store  # noqa: E402
from worker import IndexWorker, read_history  # noqa: E402
import config_editor  # noqa: E402
from config import CFG  # noqa: E402

ISSUE_ORDER = ["scanned", "unreadable", "extract-failed", "empty", "tbd", "unknown"]


def _fmt_ts(ts):
    if not ts:
        return "从未"
    from datetime import datetime
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


# ---------------------------------------------------------------------------
# 检索结果文本协议 → 结构化（移植 gui/widgets.py 的解析规则，后端集中一处）
# ---------------------------------------------------------------------------
# 置信度标记可带·分档词后缀（问题43，如 [置信度 0.87·高相关]），也可无后缀（兼容旧输出）
_RE_CONF = re.compile(r"\[置信度 ([\d.]+)(?:·[^\]]*)?\]")
_RE_CHUNK = re.compile(r"\[块 (\d+)/(\d+)\]")
_RE_HEAD = re.compile(r"\((##+ [^)]+)\)")
_RE_LOWNOTE = re.compile(r"（低置信度 [\d.]+，仅供参考）$")


def parse_search_text(text):
    """hybrid_search 文本输出 → 结果列表。块结构：
    {lib, rel, heading, chunk_idx, chunk_total, confidence, body, notice?}。

    - 来源行真实格式（retriever._format_results）：
      `[来源] 库/rel (## 标题) [块 k/N] [置信度 0.87]（低置信度 0.42，仅供参考）?`
      尾三个标记可选；「仅供参考」尾巴只出现在低置信行，剥掉避免混进 rel。
    - 非 [来源] 开头的游离行（「本次查询整体置信度偏低…」「同一文件最多展示…」
      等整体提示，Flet 版会被当成幽灵结果渲染成畸形来源行）在这里归为
      notice=True 的提示条目，前端渲染为提示横幅而非结果卡。
    """
    results = []
    cur = None
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        if line.strip() == "---":
            if cur:
                results.append(cur)
            cur = None
            continue
        if cur is None:
            if not line.lstrip().startswith("[来源]"):
                results.append({"lib": "", "rel": "", "heading": None,
                                "chunk_idx": None, "chunk_total": None,
                                "confidence": None,
                                "body": line.strip(), "notice": True})
                continue
            s = _RE_LOWNOTE.sub("", line.strip())
            s = s[len("[来源] "):]
            conf_m = _RE_CONF.search(s)
            conf = float(conf_m.group(1)) if conf_m else None
            s = _RE_CONF.sub("", s).strip()
            chunk_m = _RE_CHUNK.search(s)
            chunk_idx = int(chunk_m.group(1)) if chunk_m else None
            chunk_total = int(chunk_m.group(2)) if chunk_m else None
            s = _RE_CHUNK.sub("", s).strip()
            head_m = _RE_HEAD.search(s)
            heading = head_m.group(1) if head_m else None
            s = _RE_HEAD.sub("", s).strip()
            lib, rel = "", s
            if "/" in s:
                lib, rel = s.split("/", 1)
            cur = {"lib": lib, "rel": rel, "heading": heading,
                   "chunk_idx": chunk_idx, "chunk_total": chunk_total,
                   "confidence": conf, "body": []}
        else:
            cur["body"].append(line)
    if cur:
        results.append(cur)
    for r in results:
        r["body"] = "\n".join(r["body"])
    return results


def fmt_setting_value(kind, val):
    """配置值 → 设置页字符串（契约约定：bool→true/false，list→a,b，其余 str）。"""
    if kind == "bool":
        return "true" if val else "false"
    if kind == "list":
        return ",".join(val or [])
    return "" if val is None else str(val)


class Bridge:
    """js_api：前端唯一后端入口。方法名即契约方法名。"""

    def __init__(self):
        self.worker = IndexWorker(on_output=self._on_worker_line)
        self._wnd = None                      # pywebview 窗口引用（推送用）
        self._lib_cache = (0.0, [])           # vault_file_count TTL 缓存
        self._device = None                   # {"model","rerank","cuda"} 懒填
        self._relations_cache = {}
        self._graph_cache = (None, None)      # (fingerprint, graph)
        self._dead_alerted = False
        # 提取试验台状态
        self._pv_proc = None
        self._pv_queue = None
        self._pv_dir = None
        self._pv_started = 0.0
        self._pv_result = None
        self._pv_done = False

    # ---------- 推送通道 ----------
    def bind_window(self, wnd):
        self._wnd = wnd

    def _push(self, ev_type, payload):
        if self._wnd is None:
            return
        try:
            self._wnd.evaluate_js(
                "window.__push && window.__push(%s, %s)"
                % (json.dumps(ev_type), json.dumps(payload, ensure_ascii=False)))
        except Exception:
            pass  # 窗口已关闭等场景：推送失败静默

    def _log(self, text, is_error=False):
        """GUI 自身动作写进与索引子进程同一份日志（前缀区分）。"""
        line = "%s [GUI]%s %s" % (time.strftime("%H:%M:%S"),
                                  " ERROR" if is_error else "", text)
        try:
            from worker import LOG_FILE
            LOG_FILE.parent.mkdir(exist_ok=True)
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        self._push("log", {"lines": [line], "cursor": -1})

    def _on_worker_line(self, line):
        self._push("log", {"lines": [line], "cursor": -1})

    # ---------- 快照 ----------
    def get_snapshot(self):
        try:
            agg, libs = store.library_snapshot()
        except Exception as e:  # noqa: BLE001
            return {"error": "快照失败：%s" % e}
        files, chunks = store.meta_stats()
        prog = store.read_progress()
        hb = store.heartbeat_state(prog)
        if hb == store.HB_DEAD and not self._dead_alerted:
            self._dead_alerted = True
            self._push("alert", {"level": "dead",
                                 "text": "索引疑似卡死：心跳已停止，请到索引页查看或停止任务"})
        if hb != store.HB_DEAD:
            self._dead_alerted = False
        issues = []
        for cfg in store.library_entries():
            for reason, count in sorted(store.meta_issues_for(cfg).items(),
                                        key=lambda kv: ISSUE_ORDER.index(kv[0])
                                        if kv[0] in ISSUE_ORDER else 99):
                label, advice = store.ISSUE_TEXT.get(reason, (reason, ""))
                issues.append({"lib": cfg["name"], "reason": reason,
                               "count": count, "label": label, "advice": advice})
        if self._device is None:
            threading.Thread(target=self._probe_device, daemon=True).start()
        return {
            "libs": [{"name": n, "state": st, "files": f, "chunks": c, "path": p}
                     for n, st, f, c, p in libs],
            "agg_state": agg,
            "files": files, "chunks": chunks,
            "vault_files": self._vault_files_cached(),
            "progress": {
                "running": bool(prog.get("running")),
                "phase": prog.get("phase") or "idle",
                "files_done": prog.get("files_done") or 0,
                "files_total": prog.get("files_total") or 0,
                "chunks_done": prog.get("chunks_done") or 0,
                "chunks_total": prog.get("chunks_total") or 0,
                "pct": store.progress_ratio(prog),
                "elapsed": prog.get("elapsed_s"),
                "library": store.progress_library(prog),
                "busy": store.index_busy(),
                "heartbeat": hb,
                "heartbeat_note": store.heartbeat_note(prog),
            },
            "last_elapsed": store.last_elapsed(prog),
            "issues": issues,
            "wemm": self.wemm_backend_state(),
            "device": self._device,
        }

    def _vault_files_cached(self):
        now = time.time()
        ts, val = self._lib_cache
        if now - ts > 30:
            val = store.vault_file_count()
            self._lib_cache = (now, val)
        return val

    def _probe_device(self):
        dev = {"model": CFG.get("model_name"), "rerank": CFG.get("rerank_model"),
               "cuda": None}
        try:
            import torch
            dev["cuda"] = bool(torch.cuda.is_available())
        except Exception:
            dev["cuda"] = False
        self._device = dev

    # ---------- 库管理 ----------
    def list_libraries(self):
        from library import list_summary
        rows = []
        for row in list_summary():
            cfgs = {c["name"]: c for c in store.library_entries()}
            cfg = cfgs.get(row["name"])
            state = "none"
            issues = {}
            if cfg:
                state, _, _ = store.library_state(cfg)
                issues = store.meta_issues_for(cfg)
            rows.append({**row, "last_indexed": _fmt_ts(row["last_indexed"]),
                         "state": state, "issues": issues})
        return rows

    def get_library_config(self, name):
        from library import effective_config, load_registry, OVERRIDE_KEYS
        entry = next((e for e in load_registry() if e["name"] == name), None)
        if entry is None:
            return {"error": "库不存在：%s" % name}
        cfg = effective_config(entry)
        overrides = {k: v for k, v in entry.items()
                     if k in OVERRIDE_KEYS and v is not None}
        all_keys = ["extensions", "exclude_dirs", "exclude_files",
                    "exclude_patterns", "chunk_char_limit",
                    "short_doc_char_limit", "collection"]
        return {"effective": cfg, "overrides": overrides, "all_keys": all_keys}

    def add_library(self, path, name=None):
        from library import add_library as _add
        try:
            _add(path, name)
            self._log("添加库：%s（%s）" % (name or Path(path).name, path))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def remove_library(self, name, drop=False):
        from library import remove_library as _rm
        try:
            _rm(name, drop=drop, yes=True)
            self._log("移除库：%s（%s）" % (name, "连数据删除" if drop else "仅注销"))
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def set_library_config(self, name, updates):
        from library import set_config, unset_config
        errors = {}
        for key, val in (updates or {}).items():
            try:
                if isinstance(val, str) and val.strip() == "":
                    unset_config(name, key)
                else:
                    set_config(name, key, val)
            except Exception as e:  # noqa: BLE001
                errors[key] = str(e)
        if not errors:
            self._log("更新库配置：%s（%s）" % (name, ", ".join(updates or {})))
        return {"ok": not errors, "errors": errors}

    # ---------- 勾选范围（问题44：库内文件/文件夹级勾选建模） ----------
    def selection_tree(self, name, sub=""):
        """列某目录下一层的文件/文件夹 + 勾选态（懒加载：一次只列一层）。

        每项：{name, dir, explicit: "in"|"out"|null, state, state_text}。
        state ∈ in(入库) / out(排除) / auto_in(中性·格式判定入库) / auto_out(中性·排除)。
        sub 越出库根或非法 → error。零模型加载。
        """
        try:
            from library import (effective_config, load_registry, norm_sel_path,
                                 resolve_selection)
            entry = next((e for e in load_registry() if e["name"] == name), None)
            if entry is None:
                return {"error": "库不存在：%s" % name}
            root = Path(entry["path"]).resolve()
            if not root.is_dir():
                return {"error": "库路径不存在：%s" % root}
            sub_n = ""
            if sub:
                sub_n = norm_sel_path(sub)
            cur = (root / sub_n).resolve() if sub_n else root
            if root != cur and root not in cur.parents:
                return {"error": "路径越出库范围"}
            if not cur.is_dir():
                return {"error": "目录不存在：%s" % sub_n}
            cfg = effective_config(entry)
            sel_in, sel_out = cfg["selection_in"], cfg["selection_out"]
            default = cfg.get("selection_default", "follow")

            ex_dirs = set(cfg.get("exclude_dirs") or [])
            ex_files = set(cfg.get("exclude_files") or [])
            ex_pats = tuple(cfg.get("exclude_patterns") or [])

            def _hard_excluded(rel, name):
                # 与 collect_md_files 的 exclude_* 规则逐条对齐（显示=实际）
                if any(part in ex_dirs for part in rel.split("/")):
                    return True
                if name in ex_files or name.startswith(ex_pats):
                    return True
                return False

            def _state(rel, is_dir=False, name=""):
                # exclude_* 硬排除优先于一切（与扫描漏斗同序），且不可被显式勾选穿透
                if _hard_excluded(rel, name or rel.rsplit("/", 1)[-1]):
                    return ("out", None, "已排除（排除名单）")
                v = resolve_selection(sel_in, sel_out, rel)
                explicit = None
                if v == "in":
                    explicit = "in"
                elif v == "out":
                    explicit = "out"
                if v is None:
                    if is_dir:
                        # 文件夹是容器：中性 = 跟随子内容（文件夹没有扩展名，
                        # 不能落进格式判定——否则全部显示"排除"误导用户）
                        v = "in"
                    elif default == "exclude":
                        v = "out"
                    elif default == "include":
                        v = "in"
                    else:
                        v = "in" if rel.rsplit(".", 1)[-1].lower() in cfg["extensions"]                             else "out"
                state = v if explicit else ("auto_" + v)
                text = {"in": "已入库（显式勾选）", "out": "已排除（显式取消）",
                        "auto_in": "入库（跟随子内容）" if is_dir else "入库（跟随格式）",
                        "auto_out": "排除（跟随格式）"}[state]
                return state, explicit, text

            dirs, files = [], []
            for it in sorted(cur.iterdir(), key=lambda x: x.name.lower()):
                if it.name.startswith(".") and it.is_dir():
                    continue  # 隐藏目录（.obsidian 等）不进面板
                rel = (sub_n + "/" if sub_n else "") + it.name
                if it.is_dir():
                    st, ex, tx = _state(rel, is_dir=True, name=it.name)
                    dirs.append({"name": it.name, "dir": True, "path": rel,
                                 "explicit": ex, "state": st, "state_text": tx,
                                 "n_children": sum(1 for _ in it.iterdir())})
                else:
                    st, ex, tx = _state(rel, name=it.name)
                    files.append({"name": it.name, "dir": False, "path": rel,
                                  "explicit": ex, "state": st, "state_text": tx,
                                  "ext": it.suffix.lower().lstrip(".")})
            folders = [{"path": "", "depth": 0, "name": name,
                        "explicit": None, "state": "root", "state_text": "库根"}]
            SKIP_TOP = {root.name}

            def _walk_dirs(base, depth):
                try:
                    entries = sorted(base.iterdir(), key=lambda x: x.name.lower())
                except OSError:
                    return
                for it in entries:
                    if not it.is_dir() or it.name.startswith("."):
                        continue
                    rel = str(it.relative_to(root)).replace("\\", "/")
                    st, ex, tx = _state(rel, is_dir=True, name=it.name)
                    folders.append({"path": rel, "depth": depth, "name": it.name,
                                    "explicit": ex, "state": st, "state_text": tx})
                    if len(folders) < 4000:
                        _walk_dirs(it, depth + 1)

            _walk_dirs(root, 1)
            return {"lib": name, "sub": sub_n, "root": str(root), "dirs": dirs,
                    "files": files, "folders": folders,
                    "selection_in": sel_in, "selection_out": sel_out,
                    "extensions": cfg["extensions"], "default": default,
                    "error": None}
        except ValueError as e:  # noqa: BLE001
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"error": "列目录失败：%s" % e}

    def selection_format_bulk(self, name, ext, include):
        """格式快捷批量：include=false 把 selection_in 中该格式文件条目移入
        selection_out（显式勾选跟着取消）；include=true 反向移除。文件夹级不动。
        （GUI 侧调用前应有"将清除该格式单独勾选"的确认提示。）"""
        try:
            from library import format_selection_bulk
            n = format_selection_bulk(name, ext, bool(include))
            self._log("勾选格式批量（%s）：%s %s，影响 %d 项"
                      % (name, ext, "纳入" if include else "排除", n))
            return {"ok": True, "changed": n, "error": None}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "changed": 0, "error": str(e)}

    def selection_update(self, name, changes):
        """GUI 直接应用勾选变更（GUI 操作=用户本人，无需确认码）。

        changes = [{path, action: "in"|"out"|"neutral"}]，语义与 MCP 提案一致。
        """
        try:
            from library import set_selection
            entry = set_selection(name, changes or [])
            ch_text = "；".join("%s→%s" % (c.get("path"), c.get("action"))
                                for c in (changes or []))
            self._log("勾选范围更新（%s）：%s" % (name, ch_text))
            return {"ok": True, "error": None,
                    "selection_in": entry.get("selection_in") or [],
                    "selection_out": entry.get("selection_out") or []}
        except Exception as e:  # noqa: BLE001
            self._log("勾选范围更新失败（%s）：%s" % (name, e), is_error=True)
            return {"ok": False, "error": str(e)}

    def unset_library_config(self, name, keys):
        from library import unset_config
        for key in keys or []:
            try:
                unset_config(name, key)
            except Exception:
                pass
        return {"ok": True}

    # ---------- 索引 ----------
    def start_index(self, full=False, libraries=""):
        if store.index_busy():
            return {"ok": False, "already_running": True,
                    "error": "已有索引任务在运行"}
        ok = self.worker.start(full=full, library=libraries or "")
        if ok:
            self._log("触发%s重建%s" % ("全量" if full else "增量",
                                       ("（库=%s）" % libraries) if libraries else "（全部库）"))
        return {"ok": ok}

    def stop_index(self):
        """停本 GUI 拉起的索引子进程（taskkill 整树）。跨进程任务停不了。"""
        if self.worker.running:
            pid = self.worker.proc.pid
            try:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=10)
                else:
                    self.worker.proc.terminate()
                self._log("已请求停止索引子进程（pid=%s）" % pid, is_error=True)
                return {"ok": True, "stopped": True}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "stopped": False, "error": str(e)}
        prog = store.read_progress()
        if prog.get("running") and store.index_busy():
            return {"ok": False, "stopped": False,
                    "reason": "该任务不是本应用启动的（可能来自 MCP 或旧进程），"
                              "请等待完成或用 python gui/stop.py 处理"}
        return {"ok": True, "stopped": False, "reason": "当前没有运行中的索引任务"}

    # ---------- 检索 ----------
    def search(self, query, top_k=None, libraries="", include_body=True):
        if not (query or "").strip():
            return {"results": [], "error": "请输入问题"}
        t0 = time.time()
        try:
            from retriever import hybrid_search
            text = hybrid_search(query.strip(), top_k=int(top_k or CFG["default_top_k"]),
                                 libraries=libraries or "",
                                 include_body=bool(include_body), with_scores=True)
        except Exception as e:  # noqa: BLE001
            self._log("搜索失败：%s" % e, is_error=True)
            return {"results": [], "error": str(e)}
        elapsed = round(time.time() - t0, 1)
        self._log("检索「%s」%s 耗时 %.1fs"
                  % (query.strip(), ("（%s）" % libraries) if libraries else "（全部库）",
                     elapsed))
        return {"results": parse_search_text(text), "elapsed": elapsed, "error": None}

    def note_relations(self, lib, rel):
        key = (lib, rel)
        if key in self._relations_cache:
            return self._relations_cache[key]
        cfg = next((c for c in store.library_entries() if c["name"] == lib), None)
        out = store.note_relations_for(cfg, rel) if cfg else \
            {"resolved": False, "file": None, "outlinks": [], "inlinks": []}
        self._relations_cache[key] = out
        return out

    def open_source(self, lib, rel, heading=""):
        """打开源文件：Obsidian vault 走 URI，普通目录直接打开（移植 Flet 逻辑）。"""
        import os
        import urllib.parse
        cfg = next((c for c in store.library_entries() if c["name"] == lib), None)
        try:
            if cfg and not store.is_library_dir(cfg["path"]):
                os.startfile(str(Path(cfg["path"]) / rel))
            else:
                vault_path = cfg["path"] if cfg else CFG.get("vault", "")
                file_part = urllib.parse.quote(rel, safe="/")
                url = "obsidian://open?vault=%s&file=%s" % (
                    urllib.parse.quote(Path(vault_path).name), file_part)
                if heading:
                    url += "#" + urllib.parse.quote(heading)
                os.startfile(url)
            self._log("打开 %s" % rel)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            self._log("打开失败 %s：%s" % (rel, e), is_error=True)
            return {"ok": False, "error": str(e)}

    def open_path(self, path):
        import os
        try:
            os.startfile(path)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def pick_path(self, mode="dir", start=""):
        """原生文件/文件夹选择弹窗（pywebview 编排到 GUI 线程）。mode: 'dir'|'file'。
        start 是输入框现值，用于定位起始目录；取消返回 {"path": None}。"""
        import os
        try:
            import webview
            if self._wnd is None:
                return {"path": None, "error": "窗口未就绪"}
            start_dir = start or ""
            if start_dir and not os.path.isdir(start_dir):
                start_dir = os.path.dirname(start_dir) or ""
            if not os.path.isdir(start_dir):
                start_dir = ""
            if mode == "file":
                res = self._wnd.create_file_dialog(
                    webview.OPEN_DIALOG, allow_multiple=False, directory=start_dir,
                    file_types=("文档 (*.pdf;*.docx;*.md;*.txt)", "所有文件 (*.*)"))
            else:
                res = self._wnd.create_file_dialog(
                    webview.FOLDER_DIALOG, allow_multiple=False, directory=start_dir)
            path = res[0] if res else None
            if path:
                self._log("选择路径 %s" % path)
            return {"path": path, "error": None}
        except Exception as e:  # noqa: BLE001
            return {"path": None, "error": str(e)}

    def get_static_path(self, name):
        mapping = {"logs": str(Path(__import__("worker").LOG_FILE.parent)),
                   "root": str(ROOT)}
        return {"path": mapping.get(name, "")}

    # ---------- 设置 ----------
    def get_settings(self):
        from config import load_config
        fresh = load_config()
        groups = []
        for g in config_editor.GROUPS:
            fields = []
            for key, kind in g["fields"]:
                meta = config_editor.FIELD_META.get(key, {})
                val = fmt_setting_value(kind, fresh.get(key, CFG.get(key)))
                fields.append({
                    "key": key, "label": meta.get("label", key), "kind": kind,
                    "hint": meta.get("hint", ""), "rebuild": meta.get("rebuild", False),
                    "secret": meta.get("secret", False),
                    "choices": [list(c) for c in meta.get("choices", [])],
                    "suggest": [list(s) for s in meta.get("suggest", [])],
                    "value": val,
                })
            groups.append({"title": g["title"], "level": g["level"],
                           "desc": g["desc"], "fields": fields})
        return {"groups": groups, "missing_keys": config_editor.missing_keys()}

    def save_settings(self, updates):
        updates = updates or {}
        payload = {}
        for key, val in updates.items():
            kind = config_editor.kind_of(key)
            if kind is None:
                continue
            payload[key] = (kind, val)
        errors = config_editor.apply_updates(payload)
        if not errors:
            self._log("设置已保存并热读生效（%s）" % ", ".join(updates))
        else:
            self._log("设置保存失败：%s" % errors, is_error=True)
        return {"errors": errors}

    # ---------- 图谱 ----------
    def graph(self, libraries=""):
        try:
            from guiweb import graph_data
            fp = (libraries,)
            if self._graph_cache[0] == fp:
                return self._graph_cache[1]
            g = graph_data.build_graph(libraries)
            self._graph_cache = (fp, g)
            return g
        except Exception as e:  # noqa: BLE001
            return {"nodes": [], "edges": [], "libs": [], "stats": {"nodes": 0, "edges": 0},
                    "error": str(e)}

    def semantic_edges(self, libraries="", threshold=0.62):
        try:
            from guiweb import graph_data, semantic
            g = self.graph(libraries)
            if g.get("error"):
                return {"edges": [], "error": g["error"]}
            cb = (lambda msg: self._push("notice", {"text": msg})) if self._wnd else None
            edges, err = semantic.compute_edges(g["nodes"], threshold=threshold,
                                                progress=cb)
            if err:
                self._log("语义边失败：%s" % err, is_error=True)
            return {"edges": edges, "error": err}
        except Exception as e:  # noqa: BLE001
            return {"edges": [], "error": str(e)}

    # ---------- 诊断 ----------
    def dedup_run(self, threshold=None):
        """近似去重（读全库文件，阻塞在调用线程）。clusters 是 MinHash+LSH
        连通分量：{files:[rel...], links:[(relA,relB,jaccard)...]}，输出打平的对。"""
        try:
            import dedup
            out = []
            stats = None
            for cfg in store.library_entries():
                if threshold is not None:
                    clusters, st = dedup.find_duplicates(cfg, threshold=threshold)
                else:
                    clusters, st = dedup.find_duplicates(cfg)
                if st:
                    stats = st
                for c in clusters or []:
                    for a, b, sim in c.get("links", []):
                        out.append({"lib": cfg["name"], "a": a, "b": b, "sim": sim})
            self._log("近似去重：%d 组近似对" % len(out))
            return {"clusters": out, "stats": stats, "error": None}
        except Exception as e:  # noqa: BLE001
            return {"clusters": [], "stats": None, "error": str(e)}

    def failures(self, lib):
        cfg = next((c for c in store.library_entries() if c["name"] == lib), None)
        if cfg is None:
            return {"total": 0, "rows": [], "error": "库不存在：%s" % lib}
        data = store.file_index_rows_for(cfg)
        return {"total": data["total"],
                "rows": [{"rel": r, "reason": reason, "will_retry": wr}
                         for r, reason, wr in data["rows"]]}

    def wemm_status(self, lib):
        cfg = next((c for c in store.library_entries() if c["name"] == lib), None)
        if cfg is None:
            return {"exists": False, "total_pages": 0, "rows": [],
                    "error": "库不存在：%s" % lib}
        return store.wemm_status_for(cfg)

    def wemm_backend_state(self):
        backend, url = store.wemm_backend_state()
        return {"backend": backend, "url": url}

    def wemm_probe(self):
        _, url = store.wemm_backend_state()
        alive, detail = store.wemm_service_probe(url)
        self._log("WEMM 探测：%s" % detail)
        return {"alive": alive, "detail": detail}

    # ---------- 提取试验台 ----------
    def preview_start(self, path, backend=None):
        if not path or not Path(path).is_file():
            return {"ok": False, "error": "文件不存在：%s" % path}
        if self._pv_proc and self._pv_proc.is_alive():
            return {"ok": False, "error": "已有预览在运行，请先取消"}
        import multiprocessing as mp
        import tempfile
        import extractors
        self._pv_dir = tempfile.mkdtemp(prefix="extract_preview_guiweb_")
        self._pv_queue = mp.get_context("spawn").Queue()
        self._pv_proc = mp.get_context("spawn").Process(
            target=extractors._preview_job,
            args=(self._pv_queue, str(path), backend, self._pv_dir),
            daemon=True)
        self._pv_started = time.time()
        self._pv_result = None
        self._pv_done = False
        self._pv_proc.start()
        self._log("提取试验台启动：%s（后端=%s）" % (Path(path).name, backend or "跟随全局"))
        return {"ok": True}

    def preview_poll(self):
        if self._pv_proc is None:
            return {"running": False, "done": True, "result": None}
        if self._pv_done:
            return {"running": False, "done": True, "result": self._pv_result}
        if not self._pv_queue.empty():
            payload = self._pv_queue.get()
            info = payload.get("info") or {}
            self._pv_result = {
                "ok": payload.get("ok"),
                "error": payload.get("error"),
                "markdown": info.get("markdown", ""),
                "rendered_html": self._md_to_html(info.get("markdown", "")),
            }
            self._pv_done = True
            self._cleanup_preview_dir()
            return {"running": False, "done": True, "result": self._pv_result}
        if not self._pv_proc.is_alive():
            self._pv_result = {"ok": False, "error": "预览进程异常退出",
                               "markdown": "", "rendered_html": ""}
            self._pv_done = True
            self._cleanup_preview_dir()
            return {"running": False, "done": True, "result": self._pv_result}
        if time.time() - self._pv_started > 180:
            self.preview_cancel()
            self._pv_result = {"ok": False, "error": "预览超时（180s），已强制终止",
                               "markdown": "", "rendered_html": ""}
            self._pv_done = True
            return {"running": False, "done": True, "result": self._pv_result}
        return {"running": True, "done": False, "result": None}

    def preview_cancel(self):
        if self._pv_proc and self._pv_proc.is_alive():
            self._pv_proc.terminate()
            self._log("提取试验台已取消", is_error=True)
        self._cleanup_preview_dir()
        self._pv_done = True
        return {"ok": True}

    def _cleanup_preview_dir(self):
        import shutil
        if self._pv_dir:
            shutil.rmtree(self._pv_dir, ignore_errors=True)
            self._pv_dir = None

    @staticmethod
    def _md_to_html(md):
        """极简 Markdown→HTML（标题/列表/代码/粗体），仅试验台预览用。"""
        import html as _h
        out = []
        for line in (md or "").splitlines():
            s = _h.escape(line)
            if s.startswith("### "):
                out.append("<h3>%s</h3>" % s[4:])
            elif s.startswith("## "):
                out.append("<h2>%s</h2>" % s[3:])
            elif s.startswith("# "):
                out.append("<h1>%s</h1>" % s[2:])
            elif s.startswith("- "):
                out.append("<li>%s</li>" % s[2:])
            elif s.startswith("```"):
                out.append("<pre>")
            else:
                s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
                out.append("<p>%s</p>" % s if s else "<p></p>")
        return "\n".join(out)

    # ---------- 日志 ----------
    def log_tail(self, cursor=None):
        lines, ncur = self.worker.lines_since(cursor)
        if cursor is None:
            hist = read_history()[-300:]
            return {"lines": hist, "cursor": len(self.worker.lines)}
        return {"lines": lines, "cursor": ncur}

    # ---------- 导出 / 导入（子进程启动器，输出进日志流）----------
    def export_run(self):
        return self._run_tool([sys.executable, "export.py"], "导出")

    def import_run(self, confirm_text):
        if confirm_text != "我确认导入":
            return {"ok": False, "error": "确认文本不匹配，未执行导入"}
        return self._run_tool([sys.executable, "import.py"], "导入")

    def _run_tool(self, cmd, label):
        def run():
            self._log("%s工具启动：%s" % (label, " ".join(cmd[1:])))
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                    errors="replace", bufsize=1,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if line:
                        self._push("log", {"lines": [line], "cursor": -1})
                rc = proc.wait()
                self._log("%s工具结束（退出码 %s）" % (label, rc),
                          is_error=(rc != 0))
            except Exception as e:  # noqa: BLE001
                self._log("%s工具失败：%s" % (label, e), is_error=True)
        threading.Thread(target=run, daemon=True).start()
        return {"ok": True}
