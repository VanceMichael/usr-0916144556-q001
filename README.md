# 古籍异文汇校服务

该服务接收卷页转写并保存异文组，供馆员查询当前卷册修订。现支持**审校分支与合并提案**：地域专家可从明确的主线 revision 建立各自分支并行审校，accept / reject / defer 只影响本分支且保留原始异文依据，随后通过合并提案把决定合回主线。数据文件默认位于 `data/collation.sqlite3`，也可通过 `COLLATION_DB_PATH` 指定。

```sh
python3 -m pip install -e '.[test]'
python3 -m collation.migrate      # 可重复执行
python3 -m collation.serve        # 监听 127.0.0.1 动态端口并打印实际端口
pytest
```

接口契约和确定性样例位于 `contracts/` 与 `fixtures/`。服务不访问外部识别系统，也不访问外部网络。

## 审校分支与合并提案

- `POST /volumes/{vid}/groups` · `POST /volumes/{vid}/groups/{gid}/candidates` — 在主线登记异文组与候选（原始依据，决定永不修改它们）；转写提交也会自动沉淀候选。
- `POST /volumes/{vid}/branches` — 建分支，必须显式给出 `base_revision`（主线水位）。
- `POST /volumes/{vid}/branches/{bid}/decisions` — 分支内 `accept` / `reject` / `defer`，只推进本分支水位；支持 `idempotency_key`。
- `GET  /volumes/{vid}/branches/{bid}/state?revision=N` — 按 revision 回看，还原当时的候选与决定（重启后仍有效）。
- `POST /volumes/{vid}/proposals` — 提交合并提案，固定源分支水位与目标分支水位，预览逐项给出 `auto_merge`（可自动合入）、`same_content_different_evidence`（内容相同但依据不同）、`conflict`（决定冲突）。
- `POST /volumes/{vid}/proposals/{pid}/resolutions` — 冲突项逐项裁决；裁决人须具备 merge 权限且未参与原决定。
- `POST /volumes/{vid}/proposals/{pid}/merge` — 执行合并；水位过期即拒绝（409），并发争用同一目标卷册时只有一个推进 revision，`idempotency_key` 重试返回首次结果。
- `POST /volumes/{vid}/permissions` — 授予 merge 权限。
- `GET  /volumes/{vid}/groups/{gid}/lineage` — 谱系：决定链及其跨分支来源。
- `GET  /volumes/{vid}/events?after=N` — 审计事件流与游标。

决定、谱系、审计事件与事件游标在同一 SQLite 事务内落盘；合并写事务以 `BEGIN IMMEDIATE` 互斥，水位不符即整体回滚。
