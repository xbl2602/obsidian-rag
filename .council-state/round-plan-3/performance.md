# Performance 评审（方案门 第1轮）

## 总体判定：PASS

### 各审查点实测结论
1. 埋点开销可控：update_progress 每次浅拷贝 + json.dumps + os.replace 持锁无 fsync；既有密度已是每文件/每批一次。三埋点中写库相位合并进现有调用零新增写盘；CUDA 降级切回罕见路径无所谓。⚠️ 模型加载前埋点若误放 get_model 入口会退化为每批一次写盘（get_model 被 _encode 每批调一次，靠 _model 早退兜底）。
2. 心跳整体拷贝不被放大：_progress 全标量字段，新增一个 float 仅 JSON 增大 ~30 字节。
3. 判定侧 O(1)：GUI 每 1s 刷新 read_progress + heartbeat_state 纯 dict.get 数值比较，无被掩盖热点。
4. 无 fsync 风暴：埋点均为罕见路径或合并进现有调用。
5. 内存/句柄声称成立：一个 float，无新线程/句柄/常驻对象。

### Blocker 清单
无。

### 非阻塞建议
- S1 index.py:557 "模型加载前"埋点必须放在 get_model() 缓存未命中的实际加载分支内（早退之后），优先 kwarg 合并进既有 update_progress 调用而非新增独立调用。
- S2 index.py:332 progress_start 整 dict 重建点顺手显式清 stall_grace_until（防上一任务 CUDA 降级残留泄漏进下一任务）。
- S3 心跳 tick 不需要任何改动，勿额外读文件或重算。

### 实测位置
index.py:183-191/256-292/295-325/332,344,351/356-416/540-585/650-658,697/1388-1447,1551,1564-1578,1608、gui/app.py:242-253,267,292、gui/store.py:132-150,205-212、config.py:33、grep stall_grace 零命中。
