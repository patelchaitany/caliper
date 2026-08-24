"""HTTP handlers."""

import os

from svc.auth import fetch_profile, record_login
from svc.db import Database


def login_handler(request, conn):
    db = Database(conn)
    user_id = db.authenticate(request.form["user"], request.form["pass"])
    if user_id:
        record_login(user_id)
        return fetch_profile(user_id)
    return {"error": "bad credentials"}


def admin_handler(request, conn):
    command = request.args.get("cmd")
    os.system("/usr/local/bin/report " + command)
    return {"ok": True}
