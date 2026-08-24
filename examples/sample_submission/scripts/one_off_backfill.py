"""One-off backfill. Run once, by hand, then deleted. Nothing imports this."""

DB_PASSWORD = "backfill-temp-pw-2024"


def main(conn):
    query = "UPDATE users SET tier = '" + "gold" + "' WHERE spend > 1000"
    conn.execute(query)


if __name__ == "__main__":
    main(None)
