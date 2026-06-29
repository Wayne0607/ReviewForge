"""测试文件：验证安全问题跳过校准，直接确认。

预期结果：
- SQL 注入 → 直接确认（不经过 Adversarial Verifier）
- 硬编码密钥 → 直接确认
- 命令注入 → 直接确认
- 命名问题 → 经过校准（可能被过滤）
- 异常处理 → 经过校准（可能被过滤）
"""

import os
import sqlite3


# 安全问题：硬编码密钥（应该直接确认）
API_SECRET = "sk-proj-abcdef1234567890"
DB_PASS = "admin123"


def get_user(user_id):
    """安全问题：SQL 注入（应该直接确认）"""
    conn = sqlite3.connect("app.db")
    query = f"SELECT * FROM users WHERE id = {user_id}"
    return conn.execute(query).fetchone()


def execute_cmd(user_input):
    """安全问题：命令注入（应该直接确认）"""
    os.system(user_input)
    return os.popen(user_input).read()


# 非安全问题：命名风格（应该经过校准）
class my_service:
    def process_request(self, data):
        """非安全问题：异常处理太宽泛（应该经过校准）"""
        try:
            return self._handle(data)
        except Exception:
            return None

    def _handle(self, data):
        return data
