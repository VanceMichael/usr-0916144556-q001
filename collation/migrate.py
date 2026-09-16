from .db import migrate

if __name__ == "__main__":
    migrate()
    print("数据库迁移完成")
