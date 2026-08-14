# AGENTS.md — 验证 `fix/audit-2026-08-14` 并决定是否合并

> 这份文件是给 **在 Windows 目标机上运行的 AI** 的执行指令。
> 你的任务：把分支 `fix/audit-2026-08-14` 验证一遍，**全部通过才合并到 main**。
> 任何一步不通过 → **停下、不要合并、按「报告模板」向用户汇报**。

---

## 0. 背景（读这段就够）

2026-08-14 做了一次深度审计，找出并修复了 20 个问题（F1–F20）。
完整报告：`docs/2026-08-14-index-quality-audit.md`；变更日志：`TASK_LOG.md` 问题 22。

**修复是在 Linux 虚拟机上做的，用零依赖 stub 沙箱验证的。** 纯逻辑部分（切块、分词、
融合、配置、指纹）已经在那里跑通并有回归测试。但有三类结论**在 Linux 上根本验证不了**，
必须在这台 Windows 机器上确认——**这就是你存在的意义**：

| 编号 | 为什么 Linux 验不了 | 在阶段 |
|---|---|---|
| **F5** | `os.kill(pid,0)` 在 Linux 上是良性探测，在 Windows 上是 `TerminateProcess` | 阶段 2 |
| **F2 实际影响** | 需要读这台机器上真实的 `data/config.json` | 阶段 3 |
| **F15 / 索引质量** | 需要真实 Chroma、真实模型、真实 vault | 阶段 4 |

---

## 1. 硬性约束

- ❌ **不要 force push**，不要 rebase/amend 已推送的提交，不要改写历史。
- ❌ **不要在验证通过前合并**。
- ❌ **不要跳过阶段 0 的备份**——阶段 4 会重建索引，那是破坏性的。
- ❌ 不要修改 `docs/2026-08-14-index-quality-audit.md` 与 `TASK_LOG.md` 的既有内容
  （可以在末尾追加验证结果）。
- ✅ 遇到任何拿不准的判断，**停下来问用户**，不要自己决定。
- ✅ 每个阶段的实际输出都要留在报告里（不要只写"通过"）。

---

## 2. 阶段 0：准备与备份

```powershell
cd <项目根目录>
git fetch origin
git checkout fix/audit-2026-08-14
git pull
.venv\Scripts\pip install -r requirements.txt    # 注意：新增了 jieba
```

**备份现有索引**（阶段 4 会重建，这是唯一的回滚手段）：

```powershell
.venv\Scripts\python export.py
```

记下生成的包路径（`data/export/obsidian-rag-export-*.zip`）。
若要回滚：`.venv\Scripts\python import.py <包路径> --yes`。

> 若 `export.py` 失败，**停止**，报告错误。没有备份就不要往下走。

**判定**：`pip install` 无报错，且 `.venv\Scripts\python -c "import jieba; print(jieba.__version__)"`
能打印版本。

---

## 3. 阶段 1：纯逻辑测试（快，不加载模型）

逐个跑，记录每个的最后一行：

```powershell
.venv\Scripts\python tests\audit_regression_test.py
.venv\Scripts\python tests\library_registry_test.py
.venv\Scripts\python tests\server_singleton_test.py
.venv\Scripts\python tests\test_config_editor.py
.venv\Scripts\python tests\test_gui_store.py
.venv\Scripts\python tests\verify_export_import.py
```

**判定标准**：

| 文件 | 期望 |
|---|---|
| `audit_regression_test.py` | **19/19 通过** |
| `library_registry_test.py` | **14/14 通过**（Linux 沙箱缺 numpy 所以是 13/14，这台机器应该满分） |
| `server_singleton_test.py` | **5/5 通过** |
| `test_config_editor.py` | **0 failures** |
| `test_gui_store.py` | 全通过（需要 flet） |
| `verify_export_import.py` | 全通过 |

> ⚠️ `server_singleton_test.py` 里有一条 `test_pid_alive_self`，它会对**测试进程自己**
> 做存活探测。修复前的代码在 Windows 上跑这条会把测试进程自己杀掉（表现为无输出、
> 无堆栈、直接退出）。**如果它现在能正常打印 PASS，本身就是 F5 修复生效的证据之一。**

**不通过怎么办**：停止，把完整输出贴进报告。不要试图改测试让它变绿。

---

## 4. 阶段 2：F5 —— Windows 上的进程探测（最关键，Linux 验不了）

### 4.1 先证明旧写法确实会杀进程

开两个 PowerShell 窗口。

**窗口 A**：
```powershell
python -c "import os,time; print('PID', os.getpid()); time.sleep(180)"
```
记下打印的 PID。

**窗口 B**（把 `<PID>` 换成上面的数字）：
```powershell
python -c "import os; os.kill(<PID>, 0); print('os.kill 返回了，没有抛异常')"
```

**观察窗口 A**：

- 窗口 A 的进程**消失了** → 确认 `os.kill(pid,0)` 在 Windows 上是终止进程。**F5 成立**。
- 窗口 A 还活着 → **F5 在这台机器上不成立**，停止，报告；修复虽无害但前提要重新审视。
- 窗口 B 抛了 `PermissionError`/`OSError` → 那是另一条坏分支（误判"已死"，会导致双实例）。
  记录下来，同样算 F5 成立。

> 这个测试只影响你自己刚起的那个 sleep 进程，安全。

### 4.2 再证明新写法不杀进程

窗口 A 重新起一个 sleep 进程，记下新 PID。窗口 B：

```powershell
cd <项目根目录>
.venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import index; print('探测结果:', index._pid_alive(<新PID>))"
```

**判定**：必须**同时**满足
1. 打印 `探测结果: True`
2. **窗口 A 的进程仍然活着**

任何一条不满足 → 停止并报告。

### 4.3 单例守卫端到端

```powershell
# 窗口 A
.venv\Scripts\python server.py
# 窗口 B（A 起来之后）
.venv\Scripts\python server.py
```

**判定**：窗口 B 打印「检测到已有 server 实例运行（PID xxx），本实例退出（单例守卫）」并退出，
**而窗口 A 仍在正常运行**。恰好一个存活。

> 这正是用户当初要解决的问题（opencode 拉起双实例吃 4GB）。修复前这里有两种坏结果：
> A 被杀且 B 也退出（一个不剩），或 B 误判 A 已死而两个都跑。

### 4.4 旁证（可选，30 秒）

翻一下 `data\gui_index.log`，找有没有「索引任务结束（退出码 1）」或任务莫名中断、
没有「完成」就结束的记录。有的话，很可能就是 GUI 每秒轮询把索引子进程杀掉的现场。
把找到的行贴进报告。

---

## 5. 阶段 3：F2 —— 读真实配置（最高价值的未知数）

```powershell
type data\config.json | findstr /C:"chunk_char_limit" /C:"rerank_candidates" /C:"fusion_dense_weight" /C:"fusion_bm25_weight" /C:"small_to_big"
```

把**实际数值**记进报告，然后按下表处理：

| 键 | 若发现的值 | 该怎么做 |
|---|---|---|
| `chunk_char_limit` | **1500** | 说明这台机器一直跑的是 v4 大块。**建议改成 600**（与 `small_to_big` 配套）。⚠️ 这是行为变更，**先问用户**再改。 |
| `chunk_char_limit` | 600 | 无需处理 |
| `rerank_candidates` | 10 | 建议改成 50（v5 设计值）。同样先问用户。 |
| `fusion_dense_weight` / `fusion_bm25_weight` | **0.6 / 0.4** | 这两个键**以前是死键、现在真生效了**。保持 0.6/0.4 等于给 dense 加权，排序会变。想与此前完全一致就改成 **1.0 / 1.0**。**先问用户**。 |
| `small_to_big` 等 5 个键 | 本来不存在 | 启动一次程序后应被自动补写进 config.json。确认补写发生了，且**原有键值与注释没被改动**。 |

**验证补写行为**：先备份 `copy data\config.json data\config.json.before`，
随便跑一个读配置的命令（如 `.venv\Scripts\python library.py list`），
再 `fc data\config.json.before data\config.json` 看差异。

**判定**：差异应当**只有新增键**，不应有任何既有键值或注释被改动。

---

## 6. 阶段 4：真实索引重建与检索抽查

### 6.1 全量重建

`META_VERSION` 已从 5 升到 6，切块规则（F8/F9/F17）和嵌入文本（F20）都变了，必须重建。

```powershell
.venv\Scripts\python library.py list          # 记下库名与当前块数
.venv\Scripts\python index.py --library <库名> --full
```

**判定**：
- 正常跑完，打印「完成。Chroma 现有 N 个块」
- 记录**重建前后的块数**和**耗时**。参考：v5 时是 1759 块 / 约 40s（bge-m3）。
  块数会变（切块规则变了），但**不应该差一个数量级**。差太多就停下来报告。
- 全程 GPU 显存不应爆（若出现显存溢出或「100% GPU 但低功耗」的病态，停止并报告）。

### 6.2 检索抽查

```powershell
.venv\Scripts\python tests\eval_retrieval.py
```

再手工抽查几条（用 MCP 或直接调）：

```powershell
.venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); from retriever import hybrid_search; print(hybrid_search('免费证书', top_k=3, with_scores=True))"
```

**逐条确认下面几件事**（这些正是修复的可见效果）：

1. **置信度单调递减**——第一条分最高。若出现「越往下分越高」，F6 没修好,停止。
2. **来源行出现 `[已回填父节全文]`**（至少某些结果上）——F18 生效。
3. **正文里能看到 wikilink 的目标词**（如 `[[燃烧室]]` 应显示为「燃烧室」而不是消失）——F9 生效。
4. **正文开头没有重复的锚点串**（不该出现「A / A笔记 / 航天 / A笔记」这种）——F20 去重生效。
5. 对比 `tests\eval_retrieval.py` 的命中率与问题 21 记录的基线（top1=6/12、top3=9/12）。
   **允许有变化，但明显变差要停下来报告**，并附上具体哪几条退化了。

### 6.3 F15（仅当你有中文名的库）

```powershell
.venv\Scripts\python -c "import sys; sys.path.insert(0,'.'); import library; print(library.collection_for('火箭笔记'))"
```

若注册表里**确实存在**中文名的库：它的 collection 名会变（旧的 `kb_____` → 新的带 hash 的名字），
需要重建该库，**或**用 `library.py config <名> --set collection=<旧名>` 钉住旧名。
**先问用户要哪种**。

当前唯一已知的库 `Obsidian Vault` → `kb_obsidian_vault`，**不受影响**，无需处理。

---

## 7. 阶段 5：判定与合并

### 合并条件（全部满足才能合）

- [ ] 阶段 1 六个测试文件全部达到期望
- [ ] 阶段 2.2 新探测返回 True **且**目标进程存活
- [ ] 阶段 2.3 双实例场景下恰好一个 server 存活
- [ ] 阶段 3 配置补写只新增键、不改既有值与注释
- [ ] 阶段 4.1 重建正常完成，块数无数量级异常
- [ ] 阶段 4.2 五条可见效果逐条确认，检索质量未明显退化
- [ ] 涉及"先问用户"的决策点都已得到用户答复

### 合并

```powershell
git checkout main
git pull
git merge --ff-only fix/audit-2026-08-14
git push
```

若 `--ff-only` 失败（main 有新提交），**不要强推**，改为：

```powershell
git merge fix/audit-2026-08-14
```

解决冲突后再 push；冲突拿不准就停下来问。

合并后在 `TASK_LOG.md` 问题 22 的末尾追加一段「Windows 实机验证结果」，
把阶段 2/3/4 的关键数字写进去（块数、耗时、eval 命中率、os.kill 实测结论），
然后单独提交：`git commit -am "docs: 补充问题22的 Windows 实机验证结果"`。

### 不通过怎么办

1. **不要合并**，分支原样留着。
2. 若阶段 4 已经重建过索引且结果不可接受，用阶段 0 的备份回滚：
   ```powershell
   git checkout main
   .venv\Scripts\python import.py <阶段0的备份包> --yes
   ```
3. 按下面的模板汇报。

---

## 8. 报告模板

```
## 验证报告：fix/audit-2026-08-14

### 阶段 1 纯逻辑测试
audit_regression_test.py    : __/19
library_registry_test.py    : __/14
server_singleton_test.py    : __/5
test_config_editor.py       : __ failures
test_gui_store.py           : ____
verify_export_import.py     : ____

### 阶段 2 F5（Windows 进程探测）
2.1 旧写法 os.kill(pid,0) → 目标进程：[消失 / 存活 / 抛异常____]
2.2 新写法 _pid_alive     → 返回：____，目标进程：[存活 / 消失]
2.3 双 server 实例        → 存活数量：____，窗口B 输出：____
2.4 gui_index.log 异常中断记录：____

### 阶段 3 F2（真实配置）
chunk_char_limit    = ____
rerank_candidates   = ____
fusion_dense_weight = ____ / fusion_bm25_weight = ____
补写行为：[只新增键 / 有既有值被改动：____]
已就配置调整征询用户：[是，结论____ / 否]

### 阶段 4 真实索引
重建：____ 块（重建前 ____ 块），耗时 ____ s
eval_retrieval：top1 __/12，top3 __/12（基线 6/12、9/12）
可见效果：置信度递减[✓/✗] 父节回填[✓/✗] wikilink 保留[✓/✗] 锚点无重复[✓/✗]

### 结论
[ ] 全部通过 → 已合并到 main，commit: ____
[ ] 未通过 → 未合并。失败项：____
```
