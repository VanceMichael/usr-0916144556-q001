# 古籍异文汇校服务

该服务接收卷页转写并保存异文组，供馆员查询当前卷册修订。现有版本支持提交、列表查询和 SQLite 迁移，数据文件默认位于 `data/collation.sqlite3`，也可通过 `COLLATION_DB_PATH` 指定。

```sh
python3 -m pip install -e '.[test]'
python3 -m collation.migrate
uvicorn collation.app:app --port 0
pytest
```

接口契约和确定性样例位于 `contracts/` 与 `fixtures/`。服务不访问外部识别系统。
