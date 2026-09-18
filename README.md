# 古籍异文汇校服务

该服务接收卷页转写并保存异文组，并在原有汇校接口之上提供**审校分支**与**合并提案**能力，
让多组地域专家可以从同一卷册 revision 各自开分支并行审阅，互不阻塞。

数据文件默认位于 `data/collation.sqlite3`（题目目录），也可通过 `COLLATION_DB_PATH` 指定；
服务不访问任何外部网络。

```sh
python3 -m pip install -e '.[test]'
python3 -m collation.migrate          # 幂等、可重复执行
uvicorn collation.app:app --host 127.0.0.1 --port 0   # --port 0 动态端口
pytest                                # 真实 HTTP（uvicorn 子进程 + httpx）测试
```

## 数据模型

- 卷册维护单调递增的 `head_revision` 与追加式事件游标 `event_cursor`。
- 每个 revision 保存**异文组全量快照**（候选及其原始异文依据）与决定快照，旧 revision 永不改写，
  因此重启后仍可按旧 revision 还原当时的候选与决定。
- `revisions` 表记录父子 revision 谱系。

## 主要接口

- `POST /actors`：登记馆员与权限（`merge` 为合并/裁决权限）。
- `POST /volumes/{id}/groups`：在 revision 0 落入首版异文组（候选 + 原始依据）。
- `GET /volumes/{id}/revisions/{rev}`：按任意旧 revision 回看候选、依据与决定。
- `GET /volumes/{id}/lineage`、`GET /volumes/{id}/events`、`GET /volumes/{id}/audit`。
- `POST /branches`：从**明确存在的卷册 revision** 建立审校分支。
- `POST /branches/{id}/decisions`：`accept`/`reject`/`defer` 只影响本分支；
  决定时从基线快照复制并长期保留原始异文依据。
- `POST /branches/{id}/merge-proposals`：生成合并预览并创建提案，
  固定**源分支水位**（分支决定游标）与**目标分支水位**（卷册 head_revision）。
  预览逐项分类：
  - `auto_merge`：目标未决定，或两边决定/选定文本/依据一致，可自动合入；
  - `same_content_different_evidence`：选定文本内容相同，但所据异文出处不同，
    两套依据保留，合入不覆盖目标结论；
  - `conflict`：处置不同或选定文本不同，须逐项裁决。
- `POST /merge-proposals/{id}/conflicts/{group_id}`：冲突逐项裁决，
  裁决人必须具备 `merge` 权限，且未参与源/目标任一方的原决定。
- `POST /merge-proposals/{id}/commit`：水位仍匹配且冲突全部裁决后，
  推进唯一一个新 revision；过期预览（目标或源水位已变化）一律拒绝。
- 写接口支持 `Idempotency-Key` 请求头：相同键重试返回首次状态码与响应体。

## 一致性

所有写操作使用 `BEGIN IMMEDIATE` 单事务：决定、谱系（新 revision 与父子链）、
审计事件与卷册事件游标在同一 SQLite 事务落盘；并发合并因此串行化，争用同一目标卷册时
只有一个提案能推进 revision，另一个收到 409 并可在重新预览后合入。
