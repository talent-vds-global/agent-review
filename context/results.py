"""Module quản lý lưu trữ và truy vấn kết quả phân tích chất lượng của AI Agent."""

import psycopg2.extras
from context.db import get_db_connection


def save_result(
    flow_id: str,
    analysis_type: str,
    verdict: str,
    detail: str,
    runtime_flow: str = None
) -> int:
    """Ghi kết quả đánh giá của Agent vào bảng analysis_results trong database.

    Args:
        flow_id (str): Mã luồng nghiệp vụ (ví dụ: 'F1').
        analysis_type (str): Giai đoạn phân tích ('pre_merge' hoặc 'post_deploy').
        verdict (str): Kết luận tổng quan ('PASS', 'WARN', hoặc 'UNKNOWN').
        detail (str): Nội dung phân tích và giải thích chi tiết của Agent.
        runtime_flow (str, optional): Chuỗi các bước runtime trích xuất từ trace.

    Returns:
        int: ID của bản ghi vừa được tạo trong bảng analysis_results.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO analysis_results (flow_id, analysis_type, verdict, detail, runtime_flow)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
            """, (flow_id, analysis_type, verdict, detail, runtime_flow))
            new_id = cur.fetchone()[0]
            conn.commit()
            print(f"[Database] Đã lưu kết quả phân tích #{new_id} cho flow '{flow_id}' ({verdict}).")
            return new_id
    except Exception as e:
        if conn:
            conn.rollback()
        print(f"[Database] Lỗi khi lưu kết quả phân tích: {e}")
        raise
    finally:
        if conn:
            conn.close()


def get_latest_flow_result(flow_id: str) -> dict:
    """Lấy kết quả phân tích mới nhất của một flow cụ thể.

    Args:
        flow_id (str): Mã luồng nghiệp vụ cần tra cứu.

    Returns:
        dict: Bản ghi kết quả phân tích mới nhất, hoặc None nếu không tồn tại.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, flow_id, analysis_type, verdict, detail, runtime_flow, created_at
                FROM analysis_results
                WHERE flow_id = %s
                ORDER BY created_at DESC
                LIMIT 1;
            """, (flow_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        if conn:
            conn.close()


def list_analysis_results(limit: int = 50) -> list:
    """Lấy danh sách tất cả các kết quả phân tích gần nhất.

    Args:
        limit (int): Số lượng bản ghi tối đa cần lấy.

    Returns:
        list[dict]: Danh sách các kết quả phân tích sắp xếp theo thời gian mới nhất.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT id, flow_id, analysis_type, verdict, created_at
                FROM analysis_results
                ORDER BY created_at DESC
                LIMIT %s;
            """, (limit,))
            rows = cur.fetchall()
            return [dict(r) for r in rows]
    finally:
        if conn:
            conn.close()
