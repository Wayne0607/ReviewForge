"""User HTTP routes (PR-B of the cross-PR demo).

Imports run_query from demo_app.db (the risky primitive added in PR-A) and
feeds untrusted request data straight into it -> cross-PR sql-injection.
"""

from demo_app.db import connect, run_query


def get_users(request):
    conn = connect()
    # VULN: request["q"] is user-controlled and flows into run_query's raw filter
    return run_query(conn, "users", "name = '" + request["q"] + "'")


def count_active(request):
    conn = connect()
    # safe control: fixed literal, no user input
    return run_query(conn, "users", "active = 1")
