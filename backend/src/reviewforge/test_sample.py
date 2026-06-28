"""Sample file for testing ReviewForge webhook integration.

This file intentionally contains several code quality issues
for the reviewers to detect.
"""

import os
import pickle


def get_user(user_id):
    # SQL injection vulnerability
    query = "SELECT * FROM users WHERE id = " + str(user_id)
    return execute_query(query)


def load_data(data):
    # Insecure deserialization
    return pickle.loads(data)


def read_file(filename):
    # Path traversal
    with open(filename, "r") as f:
        return f.read()


SECRET_KEY = "hardcoded-secret-key-12345"


def process_items(items):
    result = []
    for i in range(len(items)):
        for j in range(len(items)):
            if items[i] == items[j]:
                result.append(items[i])
    return result


class user_manager:
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
# trigger
# retest after async fix
# final test
