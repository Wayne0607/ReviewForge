"""综合测试文件：覆盖所有审查维度，验证 ReviewForge 全面审查能力。

=== 安全问题（应该直接确认，不经过校准）===
1. SQL 注入 - 字符串拼接 SQL
2. 命令注入 - os.system / subprocess shell=True
3. 硬编码密钥 - API key / password
4. 路径遍历 - 用户输入作为文件路径
5. 不安全反序列化 - pickle.loads
6. XSS - 危险的 HTML 渲染

=== 性能问题（应该经过校准）===
7. O(n²) 复杂度 - 嵌套循环
8. 不必要的重复计算
9. 阻塞 I/O 在循环中

=== 风格问题（应该经过校准）===
10. 命名不规范 - snake_case 类名
11. 魔法数字
12. 过长函数
13. 宽泛异常捕获
14. 缺少文档字符串
15. 死代码 / 未使用变量

=== 边界问题（可能被校准器过滤）===
16. 参数过多（可能是临时方案）
17. 简化的错误处理（可能是原型）
18. 硬编码配置（可能是开发阶段）
"""

import os
import pickle
import sqlite3
import subprocess
import time


# ===== 安全问题 =====

# 1. SQL 注入
def get_user_by_name(name):
    conn = sqlite3.connect("users.db")
    query = f"SELECT * FROM users WHERE name = '{name}'"
    return conn.execute(query).fetchone()


# 2. 命令注入
def ping_host(host):
    os.system(f"ping -c 1 {host}")
    result = subprocess.run(f"nslookup {host}", shell=True, capture_output=True)
    return result.stdout


# 3. 硬编码密钥
AWS_ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
AWS_SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
DATABASE_URL = "postgresql://admin:password123@localhost:5432/mydb"


# 4. 路径遍历
def read_user_file(filename):
    filepath = "/data/uploads/" + filename
    with open(filepath, "r") as f:
        return f.read()


# 5. 不安全反序列化
def load_session(data):
    return pickle.loads(data)


# 6. XSS 风险
def render_comment(comment):
    return f"<div>{comment}</div>"


# ===== 性能问题 =====

# 7. O(n²) 复杂度
def find_duplicates(items):
    duplicates = []
    for i in range(len(items)):
        for j in range(len(items)):
            if i != j and items[i] == items[j]:
                if items[i] not in duplicates:
                    duplicates.append(items[i])
    return duplicates


# 8. 不必要的重复计算
def process_batch(items):
    results = []
    for item in items:
        # 每次循环都重新计算 len(items)
        if len(items) > 100:
            item = item * 2
        # 每次都创建新连接
        conn = sqlite3.connect("cache.db")
        conn.execute(f"INSERT INTO cache VALUES ('{item}')")
        conn.commit()
        conn.close()
        results.append(item)
    return results


# 9. 阻塞 I/O
def fetch_all_urls(urls):
    results = []
    for url in urls:
        # 同步阻塞请求
        import urllib.request
        response = urllib.request.urlopen(url)
        results.append(response.read())
    return results


# ===== 风格问题 =====

# 10. 命名不规范
class user_data_processor:
    def __init__(self):
        self.UserList = []
        self.temp_data = None

    def ProcessUser(self, user_data):
        self.UserList.append(user_data)

    def get_user_count(self):
        return len(self.UserList)


# 11. 魔法数字
def calculate_discount(price, user_type):
    if user_type == 1:
        return price * 0.95
    elif user_type == 2:
        return price * 0.85
    elif user_type == 3:
        return price * 0.75
    return price


# 12. 过长函数
def complex_processing(data):
    result = []
    for item in data:
        if item.get("type") == "A":
            processed = item["value"] * 2
            if processed > 100:
                processed = 100
            result.append({"type": "A", "value": processed})
        elif item.get("type") == "B":
            processed = item["value"] + 50
            if processed < 0:
                processed = 0
            result.append({"type": "B", "value": processed})
        elif item.get("type") == "C":
            processed = item["value"] ** 0.5
            result.append({"type": "C", "value": processed})
        else:
            result.append({"type": "unknown", "value": item["value"]})
    filtered = [r for r in result if r["value"] > 10]
    sorted_result = sorted(filtered, key=lambda x: x["value"], reverse=True)
    return sorted_result[:100]


# 13. 宽泛异常捕获
def risky_operation():
    try:
        result = 1 / 0
        data = eval("some_code")
        return result
    except Exception:
        return None
    except:
        return "error"


# 14. 缺少文档字符串
def mystery_function(a, b, c):
    x = a + b
    y = x * c
    z = y / (a - b) if a != b else 0
    return {"result": z, "computed": True, "timestamp": time.time()}


# 15. 死代码
def old_parser(data):
    # This function is deprecated
    unused_var = data.get("old_field")
    another_unused = []
    for key in data:
        if key.startswith("old_"):
            another_unused.append(data[key])
    return data  # Returns input unchanged


# ===== 边界问题 =====

# 16. 参数过多
def create_user(name, email, phone, address, city, state, zip_code, country, age, gender):
    return {
        "name": name, "email": email, "phone": phone,
        "address": address, "city": city, "state": state,
        "zip_code": zip_code, "country": country,
        "age": age, "gender": gender,
    }


# 17. 简化的错误处理
def parse_config(path):
    try:
        with open(path) as f:
            return eval(f.read())
    except:
        return {}


# 18. 硬编码配置
DEBUG = True
MAX_RETRIES = 3
TIMEOUT = 30
API_BASE_URL = "http://localhost:8080"
LOG_LEVEL = "DEBUG"
