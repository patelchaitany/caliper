"""Thin database layer used by the API."""

from svc.auth import verify_user


class Database:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql):
        return self.conn.execute(sql)

    def load_orders(self, user_ids):
        orders = []
        for user_id in user_ids:
            rows = self.conn.execute("SELECT * FROM orders WHERE user = %d" % user_id)
            orders.extend(rows)
        return orders

    def authenticate(self, username, password):
        try:
            return verify_user(self, username, password)
        except:
            pass
