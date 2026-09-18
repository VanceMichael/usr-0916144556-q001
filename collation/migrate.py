"""可重复执行的数据库迁移命令：python3 -m collation.migrate"""
from .db import migrate

if __name__ == "__main__":
    migrate()
    print("数据库迁移完成（可重复执行）")
