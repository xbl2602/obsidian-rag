# RAG 数据包自动导入指引（AI 阅读后自动执行）

> 本文件是给 AI agent（opencode 等）的执行规范。收到数据包后，**先读本文件**，
> 按 §2 顺序自动执行，无需人工逐步操作；任一步失败按 §5 分支处理。

## 0. 任务概述

将 RAG 数据包导入本机，恢复可检索的 Obsidian 知识库索引（**不重新计算嵌入向量**），
并可选启动 MCP server 提供语义检索。包内数据 = 向量库全量 + 源笔记 + 校验清单，
导入后本机即拥有与导出方完全一致的检索能力，同时可作测试靶子数据。

- 包内容：
  - `payload.jsonl.gz` — 全部切块：id / 文本 / 向量 / 元数据（JSON，可用 python 直接读）
  - `vault/` — 源笔记文件（相对路径）
  - `index_meta.json` — 文件指纹（导入后自动同步，避免误判重建）
  - `manifest.json` — 完整性清单：块数、文件数、sha256（导入时校验依据）
- 本包信息：包名 `__PACKAGE__`，库名 `__LIBRARY__`，导出时间 `__EXPORTED_AT__`，
  块数 `__CHUNK_COUNT__`，源文件数 `__FILE_COUNT__`，embedding 维度 `__EMBED_DIM__`。

## 1. 前置检查（全部满足才继续）

1. `python3 --version` 可用（≥3.10；3.14 实测通过）
2. 磁盘剩余 ≥ 2GB（模型缓存：bge-m3 嵌入 ~2.3GB + bge-reranker-v2-m3 重排 ~1.1GB，未下载则另需网络）
3. 包文件存在且可读：`ls -l <包路径>`
4. 网络可达（首次检索需下载嵌入模型；启用重排器还需下载重排模型；已缓存则无需）

## 2. 执行步骤（按序执行）

### 2.1 获取源码
```bash
git clone <仓库地址> obsidian-rag && cd obsidian-rag
# 若已有源码：git pull
```

### 2.2 创建环境并安装依赖
```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
# torch 按平台安装（见 requirements.txt 顶部注释）：
.venv/bin/pip install torch==2.11.0 --index-url https://download.pytorch.org/whl/cu128
```
（Windows 对应：`.venv\Scripts\python.exe -m venv .venv` 后用 `.venv\Scripts\pip install ...`，
torch 用 `pip install torch==2.11.0+cu128 --index-url https://download.pytorch.org/whl/cu128`）

> **多格式索引（v9 起）**：库的 extensions 可含 `pdf,docx`（`python library.py config <名>
> --set extensions=md,pdf,docx`），依赖本文件同目录 requirements.txt 中的
> pymupdf / pymupdf4llm / python-docx。整本均为文字层的 PDF 直接解析；
> **含图片页的 PDF（纯扫描件，或"PPT 文字页+教材扫描图"混装的混合型）整本需要
> 开启 OCR 后端**（问题34 起不再按页占比二分——混合型整本按扫描件路由，宁可诚实
> 跳过也不产出缺图片页的半份内容），见下。

### 扫描件 OCR（MinerU 云端，可选）

1. 到 mineru.net 注册并在「个人中心 → API Token」生成 Key
2. 任选一处配置（两者是同一份 config.json，后保存者生效）：
   - GUI：设置页「扫描件 OCR」组 → `pdf_scan_backend=mineru-cloud` + 粘贴 `mineru_api_key`
   - 手改：编辑 data/config.json 中同名键
3. 无需重建：之前被整本跳过的含图 PDF（扫描件/混合型）会在下一轮索引自动重试
   转正（xsrc 失配机制）
4. 免费额度：extract 接口注册 Token 后 200MB/200 页；超时默认 600s/文件可调
5. 并行与健壮性（2026-09-03 起，问题35/36）：多份文件自动并行送云端
   （`mineru_concurrency`：默认 3；0=最大吞吐——限速闸门自动节流、完成一个补一个；
   1=串行回退）；每分钟提交数滑动窗口限速
   （`mineru_rate_per_minute`，默认 45，官方频控 50/分钟）；临时性失败（网络/429/
   服务异常）自动指数退避重试，429/5xx 在 deadline 内退避续询（不误判失败）；
   进程中断前已上传的任务下一轮自动续接结果
   （`data/extract_cache/mineru_pending.json` 断点簿记），不重复消耗配额；
   Token 失效自动停掉同批剩余任务

### 2.3 导入数据包（核心步骤）
```bash
# --yes 必加：AI 非交互环境，跳过“覆盖确认”询问
.venv/bin/python import.py --yes <包路径>
# 多库：导入到指定库；目标库未注册时加 --create --path <目录> 自动注册
.venv/bin/python import.py --yes --library <库名> --create --path <目录> <包路径>
```
预期输出：解压 → sha256 校验通过 → “导入完成：Chroma <块数> 块，自检通过
（count 一致 + 抽查 embedding 余弦一致）” → 包移入 `data/archive/` → 临时区清理。
（Windows 对应命令：`.venv\Scripts\python.exe import.py --yes <包路径>`）

### 2.4 设置 Vault 路径并验证
```bash
export OBSIDIAN_VAULT="$(pwd)/vault_export"
# 验证索引可加载（不启动服务），输出 count 应与本文件 §0 的块数一致：
.venv/bin/python -c "import chromadb; print(chromadb.PersistentClient(path='data/chroma').get_or_create_collection('obsidian_kb').count())"
```

### 2.5 （可选）启动 MCP server 提供检索
```bash
OBSIDIAN_VAULT="$(pwd)/vault_export" .venv/bin/python server.py
# 或注册到 ~/.config/opencode/opencode.json（MCP stdio 配置）
```

## 3. 验证点（全部满足才算成功）

1. §2.3 输出包含 `自检通过` 且块数与 §0 信息一致
2. §2.4 count 输出与 `__CHUNK_COUNT__` 一致
3. 只读抽查 payload（直接从包读取，不依赖导入产物）：
   ```bash
   .venv/bin/python -c "import zipfile, gzip, json; p=zipfile.ZipFile('<包路径>'); f=gzip.open(p.open('payload.jsonl.gz'),'rt',encoding='utf-8'); [print(json.loads(next(f))['id']) for _ in range(3)]"
   ```
4. （可选）真实检索冒烟：
   ```bash
   OBSIDIAN_VAULT="$(pwd)/vault_export" .venv/bin/python -c "from retriever import hybrid_search; print(hybrid_search('免费证书', top_k=3))"
   ```
   首次调用会下载 bge-m3 嵌入模型（数十秒~数分钟）；默认启用重排器
   （bge-reranker-v2-m3，~1.1GB）时首次检索再加载一次，之后毫秒级。
   返回含 `[来源]` 与正文即成功。

## 4. 只读靶子数据用法（不导入也能用）

数据包是标准 zip（内含 gzip+JSON），任何 Python/7zip 可直接读取，无需导入：
```python
import gzip, json
with gzip.open("payload.jsonl.gz", "rt", encoding="utf-8") as f:
    for i, line in enumerate(f):
        rec = json.loads(line)          # rec["id"], rec["text"], rec["embedding"], rec["meta"]
        if i >= 3: break
        print(rec["id"], rec["text"][:60])
```

## 5. 失败处理（分支决策表）

| 现象 | 判定 | 处理 |
|---|---|---|
| import 报 `sha256 不匹配` 或 `CRC 校验失败` | 包传输损坏 | **不落库**，报告“包损坏，需重新下载/传输”，列出失败文件 |
| import 报 `目标已有 N 块，覆盖需确认` | 未加 `--yes` | 重新执行并带 `--yes`；如需保留旧库先 `mv data/chroma data/chroma.bak` |
| import 报 `目标库未注册` | 库名不在注册表 | 加 `--library <库名> --create --path <目录>` 自动注册后重跑 |
| `pip install` 失败 | 依赖解析问题 | 按 requirements.txt 注释重试 torch 平台命令；再试 `pip install --no-cache-dir` |
| 检索首次调用超时/失败 | 模型未缓存 + 无网络 | 确认网络可达后重试；离线则需单独传输 bge-m3 模型缓存（~2.3GB）与 bge-reranker-v2-m3（~1.1GB）；或把 `data/config.json` 的 `rerank_enabled` 改为 false 关掉重排（纯融合仍可用） |
| `import index` 报 `No module named 'msvcrt'` | 旧代码（未同步跨平台锁修复） | `git pull` 获取最新代码后重试 |
| 磁盘不足 | 空间不够 | 清理后重试；勿删 `~/.cache/huggingface` 下的 bge-m3 |
| import 中途断电/被杀 | 半写状态 | 重跑一次 import 即可（幂等）；必要时 `rm -rf data/chroma data/index_meta.json*` 后重跑 |

## 6. 完成信号（向用户汇报）

- 导入成功：包归档位置 `data/archive/`、Vault 解压位置 `vault_export/`、
  自检块数、如何启动检索（§2.5）
- 导入失败：失败阶段、原因、已按 §5 的处理结果

## 7. 注意事项

- 不要把 `vault_export/` 放在 NFS 挂载盘（fcntl 文件锁在 NFS 上不可靠）
- `--full` 全量重建耗时 ~40s（162 文件/1462 块实测）且需模型，非必要勿跑
- 本包为个人笔记数据，请勿再分发或提交到公开仓库

## 8. 图形控制台（GUI）启动/关闭约定（AI 必须遵守）

**启动**（必须用 pythonw，否则残留 CMD 黑窗）：
```powershell
Start-Process -FilePath "C:\Users\xbl26\projects\obsidian-rag\.venv\Scripts\pythonw.exe" -ArgumentList "gui\app.py" -WorkingDirectory "C:\Users\xbl26\projects\obsidian-rag"
```

**关闭**（必须用 stop.py，禁止 Stop-Process 单杀）：
```powershell
C:\Users\xbl26\projects\obsidian-rag\.venv\Scripts\python.exe gui\stop.py
```

原因：flet 桌面应用是**三层进程**（pythonw 主 → pythonw 子 → flet.exe 渲染窗口）。
单杀 python 会让 flet.exe 成孤儿残留；stop.py 按命令行匹配 python*+flet.exe 全清，
并清理 PID 文件。
