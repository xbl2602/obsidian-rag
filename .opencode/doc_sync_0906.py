import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
base = r'D:/_STOREROOM/lol/Obsidian Vault/20-Projects/Obsidian RAG/'

# ---------- 3. 操作手册：GUI 章节加 guiweb ----------
p = base + 'Obsidian RAG 操作手册.md'
t = open(p, encoding='utf-8').read()
old = '## 图形控制台（GUI，2026-08-12 多库版）\n\n独立桌面窗口（Flet），**零侵入**'
new = r'''## 图形控制台·新版 guiweb 桌面 App（2026-09-06，当前主力）

pywebview 原生窗口 + WebView2 渲染，主打**全库知识图谱**：全部库的笔记节点力导
布局一屏总览（双链边 + WEMM 页归属边 + 可选语义相似边），图谱页直接向全库提问，
命中节点带置信度荡开轨道动画；另设 检索 / 库 / 索引 / 试验台 / 诊断 / 设置 六视图，
Flet 版全部功能对齐并超出（FEATURE_PARITY.md）。同样**零侵入**：不加载模型索引、
不直写 Chroma。桌面快捷方式「Obsidian RAG」双击即启；或：

```powershell
Start-Process -FilePath "C:\Users\xbl26\projects\obsidian-rag\.venv\Scripts\pythonw.exe" -ArgumentList "guiweb\app.py" -WorkingDirectory "C:\Users\xbl26\projects\obsidian-rag"
# 关闭：直接点窗口 ×（单实例文件锁；无 flet 那种孤儿进程问题）
```

> 注意：用浏览器打开 guiweb/ui/index.html 会进「演示模式」（假数据 + 顶部横幅
> 明示），看到的库/检索结果与真实知识库无关；真数据只在桌面 App 里。
> 设置页改动即保存热读；路径类输入框旁有「浏览…」弹原生文件/文件夹选择窗。

## 图形控制台·旧版 Flet（2026-08-12，备用保留）

独立桌面窗口（Flet），**零侵入**'''
assert old in t
t = t.replace(old, new)
open(p, 'w', encoding='utf-8').write(t)
print('操作手册 done')

# ---------- 4. Roadmap：登记新功能规划 ----------
p = base + 'Obsidian RAG Roadmap.md'
t = open(p, encoding='utf-8').read()
add = '''
## 规划中（2026-09-06）：库内文件/文件夹级勾选建模

- 每库新增勾选面板：文件/文件夹级决定是否进向量库；勾选优先级高于该库全局格式
  开关（用户显式选择 > 格式继承）；下钻/上钻浏览不越库边界
- 排除 = 全流程彻底不碰：BGE / WEMM / MinerU / pymupdf 等一律不触达该文件
- AI Agent 经 MCP 提议勾选变更，硬编码用户确认门禁：未经确认不生效
- 待决：文件显式勾选 vs 父文件夹显式排除的优先级、新发现文件默认态、跟随行为
  开关语义（见设计文档）
'''
t = t.rstrip() + '\n' + add
t = t.replace('updated: 2026-09-03', 'updated: 2026-09-06')
open(p, 'w', encoding='utf-8').write(t)
print('Roadmap done')
