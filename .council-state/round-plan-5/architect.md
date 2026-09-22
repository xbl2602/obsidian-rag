## Architect 评审（obsidian-rag 性能审批 · 只审架构）
范围：maker 方案 D1~D7 中架构相关部分；精读 index.py / wemm_indexer.py / library.py /
gpu_arbiter.py / retriever.py / gui/store.py / guiweb/bridge.py+contracts.md。只评不改。

🔴 阻断: 无（本次只读审计未发现影响正确性或明确需求的架构硬伤）

🟡 建议:
S1 tools/check_notes.py:208 — 唯一漏斗旁路：该处 collect_md_files 未传 selection，
  被排除文件会漏进命名规范分析（AGENTS 红线"新增枚举必须穿漏斗"字面违反）。
S2 index.py:1358 + index.py:12 — 漏斗住错模块：collect_md_files（纯读枚举）住在
  写核心 index（含 chromadb 顶层导入），致 gui/store.py:12、graph_data.py:16、
  dedup.py:27、export.py:36 等只读方被迫 import 写核心。建议漏斗下沉 library 或
  独立唯读小模块、index 薄 re-export；行为不变、只动 import。
S3 guiweb/bridge.py:386-426 vs index.py:1388-1407 — 中性尾重复实现：decide_included
  共用了，但 verdict==None 之后的文件名/格式/默认分支两边各写一份（bridge 还多
  一套目录容器跟随逻辑）。建议抽 shared neutral resolver 进 library，两边同调。
S4 retriever.py:11 + wemm_indexer.py:34-36 — 读侧反向依赖写核心：retriever 顶层
  from index 拿 _load_pretrained/CHROMA_DIR/encode_safe，wemm 顶层拿 _terminal_entry/
  collect；import 期值拷贝正是 AGENTS 测试纪律里 retriever.CHROMA_DIR 补丁够不着
  陷阱的根因。建议 loader+目录常量下沉 config/common（行为零变）。
S5 wemm_indexer.py:39-40 vs index.py:118-122 — 终态 reason 字面量第二来源：wemm
  本地重写 "extract-failed"/"empty"，而 index 头注释明示 reason 值是持久化格式、
  必须单源。建议 from index import REASON_*（wemm meta 独立命名空间，改动零风险）。
S6 gpu_arbiter.py:195-267 vs 448-562 — 双服务生命周期 ~70% 复制（5s 早夭探针/
  _wait_health/pid 落盘）：第二份拷贝尚可接受，第三个服务出现前不抽象；
  若再加服务则必须提炼为 service descriptor，否则 god-module。

🟢 可忽略（确认成立、无需改）:
G1 index.py:1745-1767 + wemm_indexer.py:185-192,260-278 — before_serve 回调方向正确：
  控制反转，wemm 永不 import retriever；index 侧仅函数内懒导入 wemm
  （1752/2369/2446），加载期无循环依赖。纯 md 增量零触发亦靠此实现。
G2 library.py:76-162 — 六 helper 决策簇复杂度高但系问题47 用户拍板语义（深度比较/
  同位置打架/子串vs相等双轨），测试已钉住；不属过度设计，不动。
G3 wemm_server.py:137,141 — 直调 from_pretrained 系独立运行时（全局 Python，
  gpu_arbiter 仅标准库约束），无法复用 index._load_pretrained，属正当例外。
G4 gui/store.py:12-21 + 全 gui/guiweb grep — 双 GUI 零侵入行为成立：store 仅调
  kb_stale/collect/load_meta，无 Chroma 写/模型加载；bridge 重检索走函数内懒
  导入（594）。耦合在 import 层（见 S2），不在行为层。

结论: 分级记录（🔴 0 / 🟡 6 / 🟢 4），不设通过/打回（遵编排者裁决）

---
摘要:
- before_serve 方向正确：IoC 解耦，加载期无循环（index 懒导入 wemm 单向破环）。
- decide/collect 名实基本相符：decide 单源成立；漏斗调用方除一处外全传 selection。
- 唯一旁路：tools/check_notes 未传 selection（开发工具，非索引管线）。
- 边界主问题是"住错模块"：漏斗/loader 住写核心，读侧被迫 import index。
- 双 GUI 零侵入行为成立，靠纪律而非构造；中性尾有重复实现，漂移风险。
- gpu_arbiter 双服务复制可接受，未构成过度设计；selection 簇复杂度系拍板语义。
- 🔴 0；不设通过/打回。

发现清单：
- [🟡] tools/check_notes.py:208 — collect 未传 selection，排除文件漏进分析。
- [🟡] index.py:1358 — 漏斗住写核心，读侧被迫 import chromadb 重模块。
- [🟡] guiweb/bridge.py:409 — 中性默认分支与漏斗重复实现，易漂移。
- [🟡] retriever.py:11 — 读侧顶层依赖写核心，致 CHROMA_DIR 快照陷阱。
- [🟡] wemm_indexer.py:39 — reason 字面量第二来源，应复用 index 单源。
- [🟡] gpu_arbiter.py:448 — MinerU/WEMM 拉起逻辑复制，第三服务前需抽象。
- [🟢] wemm_indexer.py:268 — before_serve 在 ensure_server 前恰一次，方向正确。
- [🟢] index.py:1752 — index 懒导入 wemm，加载期无循环依赖。
- [🟢] wemm_server.py:137 — 独立运行时直调 from_pretrained，正当例外。
- [🟢] gui/store.py:12 — 零侵入行为成立（只读调用，无写库）。
