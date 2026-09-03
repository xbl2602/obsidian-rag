"""dedup.py — 近似文档去重（MinHash + LSH，文本级，纯标准库）。

目的：找出库内"内容几乎相同"的重复文档（同一课件多份拷贝、docx 及其转出的
PDF、重复的讲义），帮用户清理/合并，避免检索反复命中同一段内容。

与 WEMM 看图页导航无关：去重比较的是**提取出的文字内容**，不产生检索向量，
是只读建议工具——**绝不删除/移动任何文件**（红线：零侵入）。

流水线（用户已确认语义）：
  1. 每份文档 → 正文 → 切成 n-gram 碎片（shingles）
  2. 每个碎片哈希 → 取最小的 k 个哈希值拼成 MinHash 签名（bottom-k sketch，
     依赖 stdlib hashlib，无第三方）
  3. MinHash 签名 → LSH 分桶（切 b 段 × r 行），同桶文档为候选人
  4. 只在桶内两两算 Jaccard 相似度，≥ 阈值者输出为"近似重复组"（连通分量）

正文获取零侵入（红线7）：md/txt 直读源文件；pdf/docx 只读既有提取缓存
（read_cached_markdown），未提取的文件跳过并在报告中计数，绝不后台触发 OCR/云端。

用法：
    .venv\\Scripts\\python dedup.py --library <名|all> [--threshold 0.8]
"""
import argparse
import hashlib
import sys
from pathlib import Path

from index import collect_md_files
from library import effective_config, load_registry, resolve_entries
from extractors import BINARY_EXTS, TEXT_EXTS, read_cached_markdown

DEFAULT_K = 64      # MinHash 签名长度（bottom-k）
DEFAULT_BANDS = 16  # LSH 分多少段
DEFAULT_ROWS = 4    # 每段多少行（bands*rows == k）
DEFAULT_THRESHOLD = 0.8


def _shingles(tokens, n=4):
    """把 token 列表切成 n-gram 碎片（返回去重后的碎片串集合迭代器上的 list）。"""
    if len(tokens) < n:
        return [" ".join(tokens)] if tokens else []
    return [" ".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]


def tokenize(md):
    """正文 → token 列表：按空白切分，去掉纯标点空串，统一小写。"""
    return [t.lower() for t in md.split() if t.strip()]


def _u64(s):
    return int(hashlib.sha1(s.encode("utf-8")).hexdigest()[:16], 16)


def minhash_sketch(tokens, k=DEFAULT_K, n=4):
    """bottom-k MinHash 签名：所有 shingle 哈希值中**最小的 k 个互异值**（有序）。
    要剔除重复哈希：同一 shingle 反复出现不该把 sketch 撑成满 k——sketch 长度
    反映"文档的不同碎片数"；满 k 时两份在 △ 用 /k 估计，未满 k 时用精确交并比。
    两份文档内容越像，其 bottom-k 集合重合越多。
    """
    seen = set()
    for sh in _shingles(tokens, n):
        h = _u64(sh)
        if h in seen:
            continue
        seen.add(h)
        if len(seen) > k:
            seen.discard(max(seen))
    return sorted(seen)


def sketch_jaccard(a, b, k=DEFAULT_K):
    """两个 bottom-k 签名估 Jaccard。

    - 两侧签名都满 k（文档足够长，sketch=各自全集的最小 k 个值）→ 标准 bottom-k
      估计器：以并集第 k 小值 z 为阈值，只数 ≤z 的交集元素除以 k。分子必须按 z
      截断——直接 |A∩B|/k 会把「两边 bottom-k 里但大于 z」的交集元素多算进去，
      对阈值附近的边界对系统性偏高（假阳性重复）。
    - 任一签名未满 k（文档短/碎片少）→ 未满侧 sketch 即其全集，但另一侧只是样本，
      此时 |A∩B|/|A∪B| 是有偏样本估计（对「短文档是长文档子集」类重复偏保守、
      会漏报方向），可接受：宁漏报勿假阳性。
    """
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    if len(sa) == k and len(sb) == k:
        z = sorted(sa | sb)[k - 1]  # 并集的第 k 小值（两 sketch 的并足够覆盖它）
        return sum(1 for x in sa & sb if x <= z) / k
    inter = len(sa & sb)
    union = len(sa | sb)
    if not union:
        return 1.0
    return inter / union


def lsh_bands(sketch, bands=DEFAULT_BANDS, rows=DEFAULT_ROWS):
    """把 MinHash 签名切成 bands×rows 段，返回每个 band 的桶键（set）。"""
    keys = set()
    for b in range(bands):
        seg = tuple(sketch[b * rows:(b + 1) * rows])
        if not seg:
            continue
        key = hashlib.sha1(",".join(map(str, seg)).encode("utf-8")).hexdigest()
        keys.add((b, key))
    return keys


def _read_text(fpath):
    """按类型取正文，零侵入。返回 (text|None, reason|"")。"""
    ext = fpath.suffix.lower().lstrip(".")
    try:
        if ext in ("md", "txt", "markdown"):
            return fpath.read_text(encoding="utf-8", errors="replace"), ""
        if ext in ("pdf", "docx"):
            return read_cached_markdown(str(fpath))
        return None, "unsupported"
    except OSError as e:
        return None, f"read-error:{type(e).__name__}"


def find_duplicates(cfg, threshold=DEFAULT_THRESHOLD, k=DEFAULT_K,
                    bands=DEFAULT_BANDS, rows=DEFAULT_ROWS,
                    progress=None):
    """对单个库做近似去重。返回 (clusters, stats)。

    clusters：list of dict，每个 { "files": [rel...], "links": [(relA, relB, jaccard)...] }，
      按连通分量分组，links 为组内 ≥threshold 的近似重复对。
    stats：{ "scanned": N, "skipped": N, "pairs": N, "groups": N }
    """
    vault = Path(cfg["path"])
    stats = {"scanned": 0, "skipped": 0, "pairs": 0, "groups": 0}
    files = collect_md_files(vault, cfg["exclude_dirs"], cfg["exclude_files"],
                             cfg["exclude_patterns"],
                             sorted(TEXT_EXTS | BINARY_EXTS))

    sketches = {}  # rel -> sketch
    skip_reasons = {}
    for fpath in files:
        rel = str(fpath.relative_to(vault)).replace("\\", "/")
        text, reason = _read_text(fpath)
        if text is None:
            skip_reasons[rel] = reason or "unavailable"
            stats["skipped"] += 1
            continue
        tokens = tokenize(text)
        if len(tokens) < 2:
            skip_reasons[rel] = "too-short"
            stats["skipped"] += 1
            continue
        sketches[rel] = minhash_sketch(tokens, k)
        stats["scanned"] += 1
        if progress:
            progress(f"去重扫描 {stats['scanned']}（{rel}）")

    if len(sketches) < 2:
        return [], stats

    # LSH 分桶 → 候选对
    buckets = {}
    for rel, sk in sketches.items():
        for (b, key) in lsh_bands(sk, bands, rows):
            buckets.setdefault((b, key), []).append(rel)

    seen = set()
    candidates = []
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                a, b = members[i], members[j]
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(key)

    # 桶内精确比对，取 ≥threshold 的边
    edges = []
    for (a, b) in candidates:
        j = sketch_jaccard(sketches[a], sketches[b], k)
        if j >= threshold:
            edges.append((a, b, round(j, 4)))
    stats["pairs"] = len(edges)

    clusters = _clusterize(edges)
    stats["groups"] = len(clusters)
    return clusters, stats


def _clusterize(edges):
    """边集合 → 连通分量（每组 = 一份文档的多个近似重复版本）。"""
    adj = {}
    for (a, b, j) in edges:
        adj.setdefault(a, []).append((b, j))
        adj.setdefault(b, []).append((a, j))
    visited = set()
    clusters = []
    for start in adj:
        if start in visited:
            continue
        stack = [start]
        comp = []
        while stack:
            node = stack.pop()
            if node in visited:
                continue
            visited.add(node)
            comp.append(node)
            stack.extend(nb for nb, _ in adj.get(node, []))
        links = [(a, b, j) for (a, b, j) in edges
                 if (a in comp and b in comp)]
        clusters.append({"files": sorted(comp), "links": links})
    return clusters


def format_report(cfg, clusters, stats, threshold):
    """渲染单库去重报告（server.find_duplicates 与本 CLI 共用的单一实现）。"""
    lines = [f"库「{cfg['name']}」近似重复扫描（阈值 ≥{threshold}）："]
    lines.append(f"  扫描 {stats['scanned']} 份，跳过 {stats['skipped']} 份"
                 f"（未提取/太短/读取失败），近似重复对 {stats['pairs']}，"
                 f"重复组 {stats['groups']}")
    for i, c in enumerate(clusters, 1):
        lines.append(f"  组{i}（{len(c['files'])} 份）:")
        for (a, b, j) in c["links"]:
            lines.append(f"    · {a}  ≈  {b}  （相似度 {j}）")
    if not clusters:
        lines.append("  （未发现近似重复）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="文本级近似文档去重（MinHash+LSH，只读建议）")
    ap.add_argument("--library", default="all")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    args = ap.parse_args()
    try:
        if args.library in ("", "all"):
            entries = load_registry()
        else:
            entries = resolve_entries(args.library, "")
    except ValueError as e:
        print(f"错误：{e}")
        sys.exit(1)
    for e in entries:
        cfg = effective_config(e)
        clusters, stats = find_duplicates(cfg, threshold=args.threshold)
        print(format_report(cfg, clusters, stats, args.threshold))
        print()


if __name__ == "__main__":
    main()
