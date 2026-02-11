import psycopg2

try:
    conn = psycopg2.connect(
        host="127.0.0.1",
        port=5432,
        dbname="bot_game_db",
        user="postgres",
        password="7532159",
        connect_timeout=3,
    )
    cur = conn.cursor()
    cur.execute("SELECT 1;")
    print("OK", cur.fetchone())
    cur.close()
    conn.close()
except Exception as e:
    import traceback
    print("ERROR:", repr(e))
    traceback.print_exc()