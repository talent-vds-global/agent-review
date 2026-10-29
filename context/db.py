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
    """Trả kết nối về lại connection pool, luôn ở trạng thái sạch.

    psycopg2 tự mở transaction ở câu lệnh đầu tiên. Nếu câu lệnh lỗi mà không rollback, kết nối
    trả về pool còn kẹt ở trạng thái "transaction aborted" và mọi truy vấn sau dùng lại nó đều
    lỗi; câu SELECT không commit thì để lại "idle in transaction". Rollback ở đây xử lý cả hai
    (sau commit thì rollback không làm gì). Kết nối đã hỏng thì đóng hẳn thay vì trả lại pool.
    """
    if not conn or not _pool:
        return
    broken = bool(conn.closed)
    if not broken:
        try:
            conn.rollback()
        except Exception:
            broken = True
    _pool.putconn(conn, close=broken)

