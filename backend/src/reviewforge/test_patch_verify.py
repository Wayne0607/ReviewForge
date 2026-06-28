"""验证模式检测 + 动态校准。"""

import os
import pickle
import sqlite3

# 安全: SQL 注入
def get_user(name):
    conn = sqlite3.connect("app.db")
    return conn.execute(f"SELECT * FROM users WHERE name = '{name}'").fetchone()

# 安全: 命令注入
def run_cmd(cmd):
    os.system(cmd)

# 安全: 硬编码密钥
API_KEY = "sk-proj-1234567890abcdef"

# 安全: pickle 反序列化
def load(data):
    return pickle.loads(data)

# 风格: 命名不规范
class my_service:
    def ProcessData(self, x):
        return x * 2

# 风格: 宽泛异常
def risky():
    try:
        return 1 / 0
    except:
        return None
