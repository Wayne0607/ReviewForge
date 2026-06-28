"""测试文件：包含多种代码质量问题，用于验证 ReviewForge 审查能力。"""

import os
import pickle
import subprocess


# 硬编码密钥
API_KEY = "sk-1234567890abcdef"
DATABASE_PASSWORD = "admin123"


def get_user(user_id):
    """SQL 注入风险"""
    query = "SELECT * FROM users WHERE id = " + str(user_id)
    return execute_query(query)


def load_data(data):
    """不安全的反序列化"""
    return pickle.loads(data)


def run_command(user_input):
    """命令注入风险"""
    os.system(user_input)
    subprocess.call(user_input, shell=True)


def read_file(filename):
    """路径遍历风险"""
    with open(filename, "r") as f:
        return f.read()


def process_items(items):
    """O(n²) 性能问题"""
    result = []
    for i in range(len(items)):
        for j in range(len(items)):
            if items[i] == items[j]:
                result.append(items[i])
    return result


class user_manager:
    """命名不规范 + 参数过多"""

    def __init__(self):
        self.users = []

    def add_user(self, name, email, phone, address, city, state, zip_code, country):
        self.users.append({
            "name": name,
            "email": email,
            "phone": phone,
            "address": address,
            "city": city,
            "state": state,
            "zip_code": zip_code,
            "country": country,
        })


def unused_function():
    """死代码"""
    x = 1
    y = 2
    return x + y
