"""wemm_server.py — WEMM 页级看图嵌入的本地 HTTP 服务（用全局 Python 跑）。

WeMM-Embedding 是「看图」的多模态嵌入模型：把 PDF 的一整页图编码成一个向量，
也把一段文字查询编码到同一向量空间，跨模态算余弦相似度 → 页级导航。

为什么独立成服务（而不是塞进项目进程序内调）：
  - 它要怼 5.1GB 进 GPU 显存，与 bge-m3 错峰，独立进程天然隔离；
  - 用「全局 Python」跑，复用本机已装的 torch + transformers + qwen-vl-utils，
    不给项目 .venv 塞任何依赖，也绝不污染项目缓存/索引。

用法：
  全局 Python 启动（非项目 .venv）：
      python wemm_server.py [--port 9101] [--model tencent/WeMM-Embedding-2B]
                            [--dim 512] [--unload-after 0]
  --port          监听端口（默认从 config 的 wemm_url 读，缺省 9101）
  --model         WeMM 模型标识（默认读 config wemm_model）
  --dim           输出向量维度（默认读 config wemm_dim）
  --unload-after  空闲 N 秒后释放 GPU 显存（0=不自动卸载；用于给 bge-m3 让路）

接口（项目进程序内 wemm_indexer / wemm_retriever 调用）：
  GET  /health              → {"ok":true,"model":...,"dim":...,"gpu_mem_gb":...,"loaded":bool}
  POST /embed
       {"type":"image", "content":"<b64>", "dim":512}    → {"ok":true,"embedding":[...]}
       {"type":"text",  "content":"<text>", "dim":512}    → {"ok":true,"embedding":[...]}
       {"type":"text",  "content":["<t1>","<t2>"],"dim":512} → {"ok":true,"embeddings":[[...],...]}
       （dim 省略时用启动时的 --dim；模型不支持该档位则 400）

隐私：页图以 base64 只在内存中传给本机 GPU 进程，绝不出网；任何日志都不含
图片/文本内容，错误只含类型与摘要（对齐项目红线：API Key 不进日志的精神）。
"""
import argparse
import base64
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    import torch
    import torch.nn.functional as F
    from qwen_vl_utils import process_vision_info
    from transformers import AutoModel, AutoProcessor
except Exception as e:  # 启动时给清晰提示，而不是在请求时炸
    print(f"依赖缺失或不可用：{e}\n请用已安装 torch/transformers/qwen-vl-utils 的全局 Python 启动。",
          file=sys.stderr)
    sys.exit(2)

# 尽量读项目 config 的 wemm_*（config.py 纯标准库，全局可 import）；读不到就用默认值
def _read_config_defaults():
    defs = {"wemm_url": "http://127.0.0.1:9101",
            "wemm_model": "tencent/WeMM-Embedding-2B",
            "wemm_dim": 512}
    try:
        import config as cm
        for k in defs:
            v = cm.CFG.get(k)
            if v:
                defs[k] = v
    except Exception:
        pass
    return defs


_CFG = _read_config_defaults()

# ------------- 模型管理（单例加载，串行编码） -------------
_engine = None  # dict(model=, processor=, lock=, device=, loaded_at=)
_ENGINE_LOCK = threading.Lock()
_IDLE_UNLOAD_SECONDS = 0
_last_use = time.time()


def _resolve_model_path(model_id: str) -> str:
    """把 config 里的模型标识解析成可加载路径/ID。

    支持：
      - 本地已缓存的 HF 快照（免二次下载）：从 huggingface 缓存目录查找
        models--tencent--WeMM-Embedding-2B 的最新 snapshot；
      - 否则原样传给 AutoModel.from_pretrained（届时按需下载）。
    """
    if not model_id:
        return model_id
    if Path(model_id).is_dir():
        return model_id
    # HF 缓存：models--owner--name/snapshots/<sha>
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    safe = (model_id.replace("/", "--").replace(":", "--"))
    cand = hf_cache / f"models--{safe}"
    if cand.is_dir():
        snaps = cand / "snapshots"
        if snaps.is_dir():
            s = sorted([p for p in snaps.iterdir() if p.is_dir()])
            if s:
                return str(s[-1])
    return model_id


def _load_engine(model_id: str, dim: int):
    global _engine, _last_use
    with _ENGINE_LOCK:
        if _engine is not None and _engine.get("model_id") == model_id:
            _last_use = time.time()
            return _engine
        # 释放旧的再加载新的 / 首次加载
        if _engine is not None:
            _unload_engine_locked()
        path = _resolve_model_path(model_id)
        t0 = time.time()
        processor = AutoProcessor.from_pretrained(path, trust_remote_code=True)
        model = AutoModel.from_pretrained(path, trust_remote_code=True,
                                          dtype=torch.bfloat16)
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        model = model.to(dev)
        model.eval()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        supported = list(getattr(model.config, "matryoshka_dimensions", None) or [])
        _engine = {"model": model, "processor": processor, "device": dev,
                   "model_id": model_id, "dim": dim if dim else (supported[-1] if supported else 2048),
                   "supported": supported, "loaded_at": time.time()}
        _last_use = time.time()
        print(f"[wemm] model loaded in {time.time()-t0:.1f}s -> {dev}; "
              f"gpu_mem={_gpu_mem():.2f}GB; supported_dims={supported}",
              file=sys.stderr)
        return _engine


def _unload_engine_locked():
    """释放 GPU 显存（给 bge-m3 等让路）。调用方须已持有 _ENGINE_LOCK。"""
    global _engine
    if _engine is not None:
        if _engine["device"] == "cuda" and torch.cuda.is_available():
            torch.cuda.empty_cache()
        _engine = None


def _gpu_mem() -> float:
    if torch.cuda.is_available():
        return torch.cuda.memory_allocated() / 2**30
    return 0.0


def _check_idle_unload():
    """空闲超时自动卸载，释放显存。"""
    if _IDLE_UNLOAD_SECONDS <= 0 or _engine is None:
        return
    if time.time() - _last_use > _IDLE_UNLOAD_SECONDS:
        with _ENGINE_LOCK:
            if time.time() - _last_use > _IDLE_UNLOAD_SECONDS and _engine is not None:
                _unload_engine_locked()
                print("[wemm] idle timeout -> GPU released", file=sys.stderr)


def encode(messages, dim: int, eng):
    """对单条消息（text 或 image）编码 → (1, dim) 归一化向量。"""
    processor, model = eng["processor"], eng["model"]
    prompt = processor.apply_chat_template(messages, tokenize=False,
                                           add_generation_prompt=False)
    images, videos, video_kwargs = process_vision_info(
        messages, image_patch_size=16, return_video_kwargs=True,
        return_video_metadata=True)
    if videos is not None:
        videos, vm = zip(*videos)
        videos = list(videos)
        video_metadata = list(vm)
    else:
        video_metadata = None
    vkwargs: dict = video_kwargs or {}
    inputs = processor(text=prompt, images=images, videos=videos,
                       video_metadata=video_metadata, return_tensors="pt",
                       **vkwargs)
    inputs = inputs.to(eng["device"])
    with torch.inference_mode():
        emb = model.embedding(**inputs).float()
    emb = F.normalize(emb[..., :dim], dim=-1)
    return emb.cpu()


def build_messages(kind: str, content):
    if kind == "image":
        return [{"role": "user",
                 "content": [{"type": "image", "image": content}]}]
    if kind == "text":
        return [{"role": "user",
                 "content": [{"type": "text", "text": content}]}]
    raise ValueError(f"未知类型: {kind}")


# ------------- HTTP handler -------------
class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "wemm-server"

    def log_message(self, format, *args):  # noqa: A002
        # 精简访问日志：只有方法/路径，绝不打印请求体（图片/文本）
        print(f"[wemm] {self.command} {self.path}", file=sys.stderr)

    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self, limit=64 * 1024 * 1024):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8"))

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") == "/health":
            _check_idle_unload()
            with _ENGINE_LOCK:
                loaded = _engine is not None
                ent = _engine or {}
                self._send(200, {"ok": True, "loaded": loaded,
                                 "model": ent.get("model_id") or _CFG["wemm_model"],
                                 "dim": ent.get("dim") or _CFG["wemm_dim"],
                                 "supported_dims": ent.get("supported") or [],
                                 "gpu_mem_gb": round(_gpu_mem(), 2)})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/")
        if path != "/embed":
            self._send(404, {"ok": False, "error": "not found"})
            return
        try:
            body = self._read_body()
            kind = body.get("type")
            content = body.get("content")
            dim = int(body.get("dim") or 0) or _CFG["wemm_dim"]
            if kind not in ("image", "text") or content is None:
                self._send(400, {"ok": False, "error": "type ∈ image|text, content 必填"})
                return
            if kind == "image":
                self._do_embed_image(content, dim)
            else:
                self._do_embed_text(content, dim)
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
        except ValueError as e:
            self._send(400, {"ok": False, "error": "bad request: %s" % str(e)[:200]})
        except Exception as e:
            self._send(500, {"ok": False, "error": "server error: %s" % type(e).__name__})

    def _do_embed_image(self, b64, dim):
        """base64 页图 → 临时文件 → 编码 → 返回向量。图只在内存/临时文件，绝不出网。"""
        import io
        import os
        import tempfile
        try:
            data = base64.b64decode(b64)
        except Exception:
            self._send(400, {"ok": False, "error": "image 不是合法 base64"})
            return
        fd, tmppath = tempfile.mkstemp(suffix=".png")
        with io.open(fd, "wb") as f:
            f.write(data)
        try:
            vec = self._embed(kind="image", content=tmppath, dim=dim)
            self._send(200, {"ok": True, "embedding": vec, "dim": dim})
        finally:
            try:
                os.unlink(tmppath)
            except OSError:
                pass

    def _do_embed_text(self, content, dim):
        if isinstance(content, list):
            vecs = [self._embed(kind="text", content=c, dim=dim) for c in content]
            self._send(200, {"ok": True, "embeddings": vecs, "dim": dim})
        else:
            vec = self._embed(kind="text", content=content, dim=dim)
            self._send(200, {"ok": True, "embedding": vec, "dim": dim})

    def _embed(self, kind, content, dim):
        global _last_use
        _check_idle_unload()
        model_id = _CFG["wemm_model"]
        eng = _load_engine(model_id, dim)
        if eng["supported"] and dim not in eng["supported"]:
            raise ValueError(f"不支持的维度 {dim}，支持 {eng['supported']}")
        _last_use = time.time()
        msgs = build_messages(kind, content)
        with _ENGINE_LOCK:
            vec = encode(msgs, dim, eng)
        return vec.tolist()[0]


def parse_port_from_url(url: str, default: int = 9101) -> int:
    try:
        from urllib.parse import urlparse
        port = urlparse(url).port
        return port or default
    except Exception:
        return default


def main():
    global _IDLE_UNLOAD_SECONDS
    ap = argparse.ArgumentParser()
    default_port = parse_port_from_url(_CFG["wemm_url"])
    ap.add_argument("--port", type=int, default=default_port)
    ap.add_argument("--model", default=_CFG["wemm_model"])
    ap.add_argument("--dim", type=int, default=_CFG["wemm_dim"])
    ap.add_argument("--unload-after", type=int, default=0)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    _CFG["wemm_model"] = args.model
    _CFG["wemm_dim"] = args.dim
    _IDLE_UNLOAD_SECONDS = args.unload_after

    _load_engine(args.model, args.dim)
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    srv.daemon_threads = True
    print(f"[wemm] server listening on http://127.0.0.1:{args.port} "
          f"(model={args.model}, dim={args.dim}, unload_after={_IDLE_UNLOAD_SECONDS}s, "
          f"threads={args.threads})", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("[wemm] shutting down", file=sys.stderr)
        with _ENGINE_LOCK:
            _unload_engine_locked()
        srv.shutdown()


if __name__ == "__main__":
    main()
