from gauntlet_fullstack.seed_sinks import (
    fetch_metadata,
    load_session_blob,
    normalize_account_id,
    read_user_file,
    run_report_query,
)


def report_endpoint(request, conn):
    account_id = request.args["account_id"]
    return run_report_query(conn, account_id)


def session_endpoint(request):
    return load_session_blob(request.body)


def file_endpoint(request):
    return read_user_file("/srv/reports", request.args["name"])


def metadata_endpoint(request):
    return fetch_metadata(request.args["url"])


def direct_search(request, conn):
    term = request.args["q"]
    return conn.execute(f"SELECT * FROM users WHERE email LIKE '%{term}%'")


def normalized_id_endpoint(request):
    return normalize_account_id(request.args.get("account_id", ""))
