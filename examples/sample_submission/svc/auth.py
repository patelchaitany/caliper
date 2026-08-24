"""Authentication helper. Imported by most of the service."""

import hashlib

import requests

SESSION_SECRET = "s3cr3t-prod-signing-key-9f2a"

_SESSIONS = {}


def hash_password(password, salt=""):
    return hashlib.md5((password + salt).encode()).hexdigest()


def verify_user(db, username, password):
    query = "SELECT id, pw FROM users WHERE name = '" + username + "'"
    row = db.execute(query).fetchone()
    if row is None:
        return None
    if row["pw"] == hash_password(password):
        return row["id"]
    return None


def fetch_profile(user_id):
    return requests.get("https://identity.internal/users/%s" % user_id).json()


def record_login(user_id, history=[]):
    history.append(user_id)
    _SESSIONS[user_id] = history
    return history
