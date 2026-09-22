"""mineru_server.py — MinerU pipeline 本地解析的 HTTP 服务（用 mineru tool 环境跑）。

R3b：把扫描件 OCR 从云端 API 搬到本机。架构照抄 wemm_server.py 的成功模式：
  - 本服务是"壳"：只绑端口、做串行调度、管显存生命周期；真正的解析由常驻的
    mineru-api 子进程（MinerU 官方 ReusableLocalAPIServer 管理）完成；
  - 第一个 /parse 请求才拉起 mineru-api（懒加载），空闲自动停掉它（用完即卸），
    壳进程超时自退出——8GB 卡上与 WEMM / bge-m3 错峰，绝不共存；
  - 用「mineru tool 环境」（uv tool install 的 py3.12）跑，不给项目 .venv
    塞任何依赖，也不污染全局 3.14（WEMM 的 torch 家当）。

用法：
    <mineru-env-python> mineru_server.py [--port 9102]
        [--unload-after 300] [--idle-exit 1800]
        [--min-vram 4.5] [--vram-wait 300] [--max-pages 200]

  --port          监听端口（默认从 config 的 mineru_local_url 读，缺省 9102；
                  端口顺延由调用方 gpu_arbiter.ensure_mineru 做，本服务只绑指定端口）
  --unload-after  空闲 N 秒后停掉 mineru-api 释放显存（默认 300；0=常驻）
  --idle-exit     内服务已停后再空闲 N 秒壳自退出（默认 1800；0=常驻）
  --min-vram      拉起内服务前要求的最低空闲显存 GB（默认 4.5；不足则等让路）
  --vram-wait     等显存的最长时间秒（默认 300；超时本条请求 503，绝不等 15 分钟拖死整轮）
  --max-pages     单文件页数上限（默认 200；超限直接拒收，提示人工拆分——
                  串行锁下大文件会卡死整轮，见 checker B2）

接口（项目进程序内 extractors._mineru_local_extract 调用）：
  GET  /health              → {"ok":true,"loaded":bool,"inner_url":...,"gpu_mem_gb":...}
  POST /parse {"pdf_path"}  → {"ok":true,"md":...,"pages":N,"seconds":T}
                              {"ok":false,"error":"<短码>: <摘要>"}
  POST /evict               → {"ok":true,"evicted":bool}（停内服务释显存，给 bge/WEMM 让路）

隐私：PDF 只读本机文件、解析全程不出网；日志只有文件名与类型摘要，绝不含正文。
"""
import argparse
import json
import sys
import threading
import time
import urllib.request
from pathlib import Path as _Path

sys.path.insert(0, str(_Path(__file__).resolve().parent))  # cwd 无关地 import 项目模块
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from mineru.cli.api_client import ReusableLocalAPIServer
except Exception as e:  # 启动时给清晰提示，而不是在请求时炸
    print(f"依赖缺失：{e}\n请用 uv tool 装好的 mineru 环境（py3.12）启动本服务。",
          file=sys.stderr)
    sys.exit(2)


def _read_config_defaults():
    defs = {"mineru_local_url": "http://127.0.0.1:9102",
            "mineru_local_max_pages": 200}
    try:
        import config as cm
        for k in defs:
            v = cm.CFG.get(k)
            if v:
                defs[k] = v
    except Exception:
        pass
    try:
        defs["mineru_local_max_pages"] = int(defs["mineru_local_max_pages"])
    except (TypeError, ValueError):
        defs["mineru_local_max_pages"] = 200
    return defs


_CFG = _read_config_defaults()

_MIN_VRAM_GB = 4.5
_VRAM_WAIT_S = 300.0
_IDLE_EXIT_S = 1800
_MAX_PAGES = int(_CFG["mineru_local_max_pages"])
_active_requests = 0

# ------------- 内服务管理（单例 mineru-api，串行解析） -------------
_api_server = ReusableLocalAPIServer()
_API_LOCK = threading.Lock()   # 串行锁：8GB 卡一次只解一份，防显存叠加
_INNER_LOCK = threading.Lock()  # 内服务启停锁
_inner_base_url: "str | None" = None
_INNER_PID_FILE = _Path(__file__).resolve().parent / "data" / "mineru_inner.pid"
_IDle_UNLOAD_SECONDS = 300
_last_use = time.time()


def _gpu_mem() -> float:
    try:
        import torch
        if torch.cuda.is_available():
            return torch.cuda.memory_allocated() / 2 ** 30
    except Exception:
        pass
    return 0.0


def _inner_alive():
    """内 mineru-api 是否在跑（快照读，不阻塞）。"""
    srv = _api_server._server
    if srv is None or srv.base_url is None:
        return False
    try:
        from mineru.cli.api_client import _managed_process_is_running
        return bool(_managed_process_is_running(srv.process))
    except Exception:
        return False


def _ensure_inner() -> str:
    """确保内服务在跑：已跑直接返回 base_url；否则等显存→拉起→等就绪。

    显存互斥：pipeline 约 4GB，与 WEMM / bge-m3 错峰——等不到就抛错本条请求，
    绝不硬上（8GB 卡硬共存 = WDDM 溢出整机卡死，见 gpu_arbiter 头注释）。
    """
    global _inner_base_url, _last_use
    with _INNER_LOCK:
        if _inner_alive() and _inner_base_url:
            _last_use = time.time()
            return _inner_base_url
        # 旧实例死了先清掉再起新的（ReusableLocalAPIServer.ensure_started 自带此语义）
        import gpu_arbiter
        if not gpu_arbiter.wait_for_vram(_MIN_VRAM_GB, timeout_s=_VRAM_WAIT_S,
                                         log=lambda m: print(f"[mineru] {m}", file=sys.stderr)):
            raise RuntimeError(
                f"等待空闲显存 >= {_MIN_VRAM_GB}GB 超时（{_VRAM_WAIT_S:.0f}s），本条请求未执行")
        t0 = time.time()
        server, _started = _api_server.ensure_started()
        base_url = server.base_url
        if not base_url:
            raise RuntimeError("内 mineru-api 拉起后无 base_url")
        _record_inner_pid(server)
        # 就绪等待：miner-api 冷起要 import +  warming，给足 300s
        deadline = time.time() + 300.0
        last_err = None
        while time.time() < deadline:
            try:
                req = urllib.request.Request(base_url.rstrip("/") + "/health")
                with urllib.request.urlopen(req, timeout=5) as r:
                    payload = json.loads(r.read().decode("utf-8"))
                if payload.get("ok", True):
                    _inner_base_url = base_url
                    _last_use = time.time()
                    print(f"[mineru] inner mineru-api ready in {time.time()-t0:.1f}s "
                          f"-> {base_url}", file=sys.stderr)
                    return base_url
            except Exception as e:
                last_err = e
            # 内服务早夭则不等满 300s（arbiter 侧也有同款早夭检查，双保险）
            try:
                from mineru.cli.api_client import _managed_process_exit_code
                if _managed_process_exit_code(server.process) is not None:
                    raise RuntimeError("内 mineru-api 进程启动后退出（依赖/端口问题）")
            except RuntimeError:
                raise
            except Exception:
                pass
            time.sleep(2.0)
        raise RuntimeError(f"内 mineru-api 300s 未就绪（{type(last_err).__name__ if last_err else '无响应'}）")


def _record_inner_pid(server):
    """记下内服务主进程 PID（优雅退出时清掉；被强杀残留时下次启动认领回收）。"""
    try:
        proc = getattr(server, "process", None)
        pid = getattr(proc, "pid", None)
        if pid:
            _INNER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
            _INNER_PID_FILE.write_text(str(int(pid)), encoding="utf-8")
    except Exception:
        pass


def _reclaim_stale_inner():
    """启动时回收上一个壳被强杀后残留的内服务（Windows 下 stdin 守望缺席，
    子进程不会随父进程死，必须显式认领。认领前用 tasklist 验明是 python 进程，
    防 PID 复用误杀；验不出立刻收手，宁可残留不误杀）。"""
    try:
        pid = int(_INNER_PID_FILE.read_text(encoding="utf-8").strip().split()[0])
    except (OSError, ValueError, IndexError):
        return
    if pid <= 0:
        return
    try:
        import subprocess as _sp
        out = _sp.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV",
                       "/NH"], capture_output=True, timeout=10)
        line = out.stdout.decode("utf-8", "replace").strip().strip('"')
        image = line.split('","')[0].lower() if line else ""
        if "python" not in image:
            try:
                _INNER_PID_FILE.unlink()
            except OSError:
                pass
            return
    except Exception:
        return  # 验不出就不杀（fail-closed）
    try:
        import os as _os
        import signal as _sig
        _os.kill(pid, _sig.SIGTERM)
        print(f"[mineru] reclaimed stale inner mineru-api (PID {pid})",
              file=sys.stderr)
    except Exception as e:
        print(f"[mineru] reclaim stale inner PID {pid} failed: "
              f"{type(e).__name__}", file=sys.stderr)
    try:
        _INNER_PID_FILE.unlink()
    except OSError:
        pass


def _stop_inner_locked():
    """停内服务释显存。调用方须已持有 _INNER_LOCK。"""
    global _inner_base_url
    try:
        _api_server.stop()
    except Exception as e:
        print(f"[mineru] stop inner failed: {type(e).__name__}", file=sys.stderr)
    _inner_base_url = None
    try:
        _INNER_PID_FILE.unlink()
    except OSError:
        pass
    print("[mineru] inner stopped; gpu_mem released", file=sys.stderr)


def _check_idle_unload():
    if _IDle_UNLOAD_SECONDS <= 0:
        return
    # 问题59 B7：在途解析中不卸载——_last_use 只在进入/成功时刷新，长解析
    # （单文件超时 300+30×页数，200 页约 105min）期间必超 300s 阈值；
    # 此前会杀死在途任务致 deferred 空转活锁。_idle_exit_daemon 早已同款守卫。
    if _active_requests > 0:
        return
    if time.time() - _last_use > _IDle_UNLOAD_SECONDS:
        with _INNER_LOCK:
            if time.time() - _last_use > _IDle_UNLOAD_SECONDS and _inner_alive():
                _stop_inner_locked()
                print("[mineru] idle timeout -> GPU released", file=sys.stderr)


def _idle_unload_daemon():
    interval = min(30, max(1, _IDle_UNLOAD_SECONDS))
    while True:
        time.sleep(interval)
        try:
            _check_idle_unload()
        except Exception as e:
            print(f"[mineru] idle check error: {type(e).__name__}", file=sys.stderr)


def _idle_exit_daemon():
    interval = min(30, max(1, _IDLE_EXIT_S))
    while True:
        time.sleep(interval)
        if _IDLE_EXIT_S <= 0 or _inner_alive():
            continue
        if _active_requests > 0:
            continue
        if time.time() - _last_use > _IDLE_EXIT_S:
            print(f"[mineru] idle {_IDLE_EXIT_S}s after unload -> process exit "
                  "(下次需要时会被按需拉起)", file=sys.stderr)
            import os
            os._exit(0)


def _count_pages(pdf_path):
    """快数页数（只读元信息，不渲染）。失败返回 None（由调用方按未知处理）。"""
    try:
        try:
            import pymupdf as fitz  # 新版包名（tool 环境已装）
        except ImportError:
            import fitz  # 旧别名兜底
        doc = fitz.open(pdf_path)
        try:
            return int(doc.page_count)
        finally:
            try:
                doc.close()
            except Exception:
                pass
    except Exception:
        return None


def _multipart_body(pdf_path, fields):
    """手拼 multipart/form-data（只用标准库，不耦合 httpx 版本）。"""
    import os
    boundary = "----mineruR3b%s" % int(time.time() * 1000)
    fname = os.path.basename(pdf_path)
    with open(pdf_path, "rb") as f:
        data = f.read()
    parts = []
    for k, v in fields.items():
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                      % (boundary, k, v)).encode("utf-8"))
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"files\"; "
                  "filename=\"%s\"\r\nContent-Type: application/pdf\r\n\r\n"
                  % (boundary, fname)).encode("utf-8"))
    parts.append(data)
    parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(parts), boundary


def _extract_md(payload):
    """从 /file_parse(JSON) 响应里抠正文。实测形态（fast_api.build_result_dict）：
    按文件名 keyed 的 {pdf_name: {md_content}}；兼容裸 dict / 单元素 list /
    results 列表三种历史形态。"""
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        return None
    md = payload.get("md_content")
    if isinstance(md, str) and md.strip():
        return md
    # {pdf_name: {...}}：取第一个文件项
    for v in payload.values():
        if isinstance(v, dict):
            md = v.get("md_content")
            if isinstance(md, str) and md.strip():
                return md
    # {pdf_name: {...}}：取第一个文件项（含 results  dict 形态）
    results = payload.get("results")
    if isinstance(results, dict):
        for v in results.values():
            if isinstance(v, dict):
                md = v.get("md_content")
                if isinstance(md, str) and md.strip():
                    return md
    if isinstance(results, list) and results and isinstance(results[0], dict):
        md = results[0].get("md_content")
        if isinstance(md, str) and md.strip():
            return md
    return None


def do_parse(pdf_path, timeout_s):
    """解析一份 PDF → (md|None, 短码错误|None)。串行锁内执行，一次一份。"""
    global _last_use
    t0 = time.time()
    pages = _count_pages(pdf_path)
    if pages is not None and pages > _MAX_PAGES:
        return None, (f"too-many-pages: {pages} 页超过上限 {_MAX_PAGES} 页，"
                      "请人工拆分后重建（串行锁下大文件会卡死整轮）")
    if pages is not None and pages <= 0:
        return None, "empty-pdf: 无有效页面"
    base_url = _ensure_inner()
    fields = {
        "backend": "pipeline",
        "parse_method": "auto",
        "lang_list": "ch",
        "formula_enable": "true",
        "table_enable": "true",
        "return_md": "true",
        "response_format_zip": "false",
        "return_middle_json": "false",
        "return_model_output": "false",
        "return_content_list": "false",
        "return_images": "false",
    }
    body, boundary = _multipart_body(pdf_path, fields)
    req = urllib.request.Request(
        base_url.rstrip("/") + "/file_parse", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=%s" % boundary})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as r:
            status = r.status
            raw = r.read()
    except Exception as e:
        return None, f"inner-error: {type(e).__name__}（内服务调用失败）"
    if status == 409:
        return None, "parse-failed: 内服务解析失败（文件损坏或版面异常）"
    if status != 200:
        return None, f"inner-error: HTTP {status}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None, "inner-error: 内服务返回非 JSON"
    md = _extract_md(payload)
    if not md:
        return None, "empty-result: 内服务成功但无正文（图片页无字或全空页）"
    _last_use = time.time()
    return md, None


# ------------- HTTP handler -------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "mineru-server"

    def log_message(self, format, *args):  # noqa: A002
        print(f"[mineru] {self.command} {self.path}", file=sys.stderr)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit=4 * 1024):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") == "/health":
            loaded = _inner_alive()
            self._send(200, {"ok": True, "loaded": loaded,
                             "inner_url": _inner_base_url,
                             "max_pages": _MAX_PAGES,
                             "gpu_mem_gb": round(_gpu_mem(), 2)})
        else:
            self.close_connection = True
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        try:
            body = self._read_body()
        except ValueError:  # 含 JSONDecodeError（它是 ValueError 子类）：请求体非法
            self.close_connection = True
            self._send(400, {"ok": False, "error": "bad request"})
            return
        path = self.path.split("?")[0].rstrip("/")
        if path == "/evict":
            # 双向抢占（checker B1）：bge/WEMM 加载前调这里把 MinerU 请下显存。
            # 问题59 B7：先拿 _API_LOCK 再拿 _INNER_LOCK（与 /parse 持锁顺序一致，
            # 故不死锁）——在途解析走完才停机，不中断当次成果。此前注释声称等待，
            # 实则只持 _INNER_LOCK，do_parse 的长网络等待根本不在该锁内。
            with _API_LOCK:
                with _INNER_LOCK:
                    had = _inner_alive()
                    if had:
                        _stop_inner_locked()
            self._send(200, {"ok": True, "evicted": had})
            return
        if path != "/parse":
            self.close_connection = True
            self._send(404, {"ok": False, "error": "not found"})
            return
        global _active_requests
        pdf_path = body.get("pdf_path")
        if not pdf_path or not Path(pdf_path).is_file():
            self._send(400, {"ok": False, "error": "bad request: pdf_path 不存在"})
            return
        try:
            timeout_s = float(body.get("timeout_s") or 0)
        except (TypeError, ValueError):
            timeout_s = 0.0
        if timeout_s <= 0:
            timeout_s = 600.0
        _active_requests += 1
        try:
            # 串行锁：一次只解一份（8GB 卡叠加即爆；内服务并发上限 3 也用不满也罢）
            with _API_LOCK:
                md, err = do_parse(pdf_path, timeout_s)
        except Exception as e:
            md, err = None, f"server-error: {type(e).__name__}"
        finally:
            _active_requests -= 1
        if md is None:
            self._send(200, {"ok": False, "error": err or "unknown"})
        else:
            self._send(200, {"ok": True, "md": md})


def parse_port_from_url(url: str, default: int = 9102) -> int:
    try:
        from urllib.parse import urlparse
        port = urlparse(url).port
        return port or default
    except Exception:
        return default


def main():
    global _IDle_UNLOAD_SECONDS
    ap = argparse.ArgumentParser()
    default_port = parse_port_from_url(_CFG["mineru_local_url"])
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--unload-after", type=int, default=300)
    ap.add_argument("--idle-exit", type=int, default=1800)
    import gpu_arbiter
    ap.add_argument("--min-vram", type=float, default=gpu_arbiter.MINERU_MIN_VRAM_GB
                    if hasattr(gpu_arbiter, "MINERU_MIN_VRAM_GB") else 4.5)
    ap.add_argument("--vram-wait", type=float, default=300.0)
    ap.add_argument("--max-pages", type=int, default=int(_CFG["mineru_local_max_pages"]))
    args = ap.parse_args()

    global _MIN_VRAM_GB, _VRAM_WAIT_S, _IDLE_EXIT_S, _MAX_PAGES
    _IDle_UNLOAD_SECONDS = args.unload_after
    _MIN_VRAM_GB = args.min_vram
    _VRAM_WAIT_S = args.vram_wait
    _IDLE_EXIT_S = args.idle_exit
    _MAX_PAGES = args.max_pages

    # 模型目录自愈：E:\models\hf 不存在就建（arbiter 侧也会建，双保险；
    # HF_HOME 只作用于本进程环境，不碰全局——C 盘 15GB 现有缓存原样不动）
    try:
        hf_home = Path.home()  # 占位，真正来源见下
        import os
        hh = os.environ.get("HF_HOME") or os.environ.get("HF_HUB_CACHE")
        if hh:
            Path(hh).mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    if _IDle_UNLOAD_SECONDS > 0:
        threading.Thread(target=_idle_unload_daemon, daemon=True,
                         name="idle-unload").start()
    if _IDLE_EXIT_S > 0:
        threading.Thread(target=_idle_exit_daemon, daemon=True,
                         name="idle-exit").start()
    _reclaim_stale_inner()  # 上一个壳若被强杀，认领它残留的内服务
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    srv.daemon_threads = True
    print(f"[mineru] server listening on http://127.0.0.1:{args.port} "
          f"(lazy inner mineru-api, unload_after={_IDle_UNLOAD_SECONDS}s, "
          f"idle_exit={_IDLE_EXIT_S}s, min_vram={_MIN_VRAM_GB}GB, "
          f"max_pages={_MAX_PAGES})",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[mineru] shutting down", file=sys.stderr)
        with _INNER_LOCK:
            try:
                if _inner_alive():
                    _stop_inner_locked()
            except Exception:
                pass
        srv.shutdown()


if __name__ == "__main__":
    main()
