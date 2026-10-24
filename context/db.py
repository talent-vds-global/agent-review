import psycopg2.pool
from config import DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

_pool = None


def _get_pool():
    global _pool
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,
            host=DB_HOST,
            port=DB_PORT,
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
        )
    return _pool


def get_db_connection():
    """Khởi tạo và trả về kết nối tới cơ sở dữ liệu PostgreSQL từ connection pool."""
    return _get_pool().getconn()


def release_db_connection(conn):
    """Trả kết nối về lại connection pool."""
    if conn and _pool:
        _pool.putconn(conn)

