"""测试文件：包含明确问题和边界问题，验证动态校准效果。

明确问题（应该被确认）：
- SQL 注入
- 硬编码密钥
- 命令注入

边界问题（可能被校准器降级）：
- 命名风格（项目惯例可能是 snake_case）
- 复杂度问题（可能被高估）
- 潜在性能问题（可能不是瓶颈）
"""

import os
import sqlite3


# 明确问题：硬编码密钥
SECRET_KEY = "sk-proj-1234567890"
DB_PASSWORD = "root123"


def get_user(user_id):
    """明确问题：SQL 注入"""
    conn = sqlite3.connect("app.db")
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return conn.execute(query).fetchone()


def run_cmd(cmd):
    """明确问题：命令注入"""
    os.system(cmd)


# 边界问题：命名风格（可能项目惯例就是 snake_case）
class user_data_manager:
    def __init__(self):
        self.user_list = []

    def add_user(self, user_name, user_email, user_phone, user_address, user_city, user_state, user_zip):
        """边界问题：参数过多，但可能是临时方案"""
        self.user_list.append({
            "name": user_name,
            "email": user_email,
            "phone": user_phone,
            "address": user_address,
            "city": user_city,
            "state": user_state,
            "zip": user_zip,
        })


def process_data(items):
    """边界问题：O(n²) 但数据量可能很小"""
    result = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j and items[i] == items[j]:
                result.append(items[i])
    return result


def read_config(path):
    """边界问题：路径遍历但可能只接受内部路径"""
    with open(path, "r") as f:
        return f.read()


def connect_db():
    """边界问题：异常处理太宽泛"""
    try:
        return sqlite3.connect("app.db")
    except Exception:
        return None
