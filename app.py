from flask import Flask, request, jsonify
from flask_cors import CORS
import psycopg2.extras

from context.db import get_db_connection
from context.results import save_result
from sources.github import get_pr_diff
from sources.runtime import get_runtime_flow, get_latest_trace_id, parse_log
from sources.confluence import get_design_by_flow
from agent.core import run_agent
from agent.prompt import PROMPT_PRE_MERGE, PROMPT_POST_DEPLOY

app = Flask(__name__)
CORS(app)  # Cho phép dashboard (cổng khác) gọi API


@app.route("/", methods=["GET"])
def index():
    """Endpoint kiểm tra trạng thái hoạt động của hệ thống."""
    return jsonify({
        "service": "Software Quality AI Agent",
        "status": "running",
        "endpoints": {
            "pre_merge": "POST /webhook",
            "post_deploy": "POST /runtime",
            "list_analysis": "GET /api/analysis",
            "flow_analysis": "GET /api/flows/<flow_id>/analysis"
        }
    })


# ========== API cho Dashboard ==========

@app.route("/api/analysis", methods=["GET"])
def list_analysis():
    """Trả danh sách tất cả kết quả phân tích, mới nhất trước."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, flow_id, analysis_type, verdict, created_at
            FROM analysis_results
            ORDER BY created_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route("/api/flows/<flow_id>/analysis", methods=["GET"])
def flow_analysis(flow_id):
    """Trả kết quả phân tích mới nhất của một flow."""
    conn = get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT id, flow_id, analysis_type, verdict, detail, runtime_flow, created_at
            FROM analysis_results
            WHERE flow_id = %s
            ORDER BY created_at DESC
            LIMIT 1
        """, (flow_id,))
        row = cur.fetchone()
        cur.close()

        if row is None:
            return jsonify({"error": f"Không có kết quả phân tích cho flow {flow_id}"}), 404
        return jsonify(dict(row))
    finally:
        conn.close()


# ========== Luồng 1: Pre-merge (GitHub Webhook) ==========

@app.route("/webhook", methods=["POST"])
def github_webhook():
    """Luồng 1: Pre-merge (GitHub Webhook)."""
    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Payload không hợp lệ (không phải JSON)"}), 400

    action = payload.get("action")
    if action not in ["opened", "synchronize", "reopened"]:
        return jsonify({"message": f"Bỏ qua action '{action}'."}), 200

    pull_request = payload.get("pull_request")
    repository = payload.get("repository")
    if not pull_request or not repository:
        return jsonify({"error": "Thiếu dữ liệu pull_request hoặc repository"}), 400

    repo_full_name = repository.get("full_name")
    pr_number = pull_request.get("number")
    pr_title = pull_request.get("title", "")

    print(f"\n[Pre-merge] Đang xử lý PR #{pr_number} ({pr_title}) trên repo {repo_full_name}...")

    pr_diff = get_pr_diff(repo_full_name, pr_number)
    user_input = (
        f"Thông tin Pull Request:\n"
        f"- Repository: {repo_full_name}\n"
        f"- PR Number: #{pr_number}\n"
        f"- Tiêu đề: {pr_title}\n"
        f"- Action: {action}\n\n"
        f"Nội dung Code Diff:\n{pr_diff}"
    )
    conclusion = run_agent(system_prompt=PROMPT_PRE_MERGE, user_input=user_input)

    # Tách verdict từ dòng đầu hoặc kết luận
    first_line = conclusion.strip().split("\n")[0].upper()
    verdict = "WARN" if "WARN" in first_line else ("PASS" if "PASS" in first_line else "UNKNOWN")

    print("\n========== KẾT LUẬN PRE-MERGE ==========")
    print(conclusion)
    print("========================================\n")

    return jsonify({
        "status": "success", "stage": "pre_merge",
        "repo": repo_full_name, "pr_number": pr_number,
        "verdict": verdict,
        "conclusion": conclusion
    }), 200


# ========== Luồng 2: Post-deploy (Runtime Trace / Log Monitoring) ==========

@app.route("/runtime", methods=["POST"])
def runtime_check():
    """Luồng 2: Post-deploy (Runtime Trace / Log Monitoring).

    Nhận trace_id (hoặc tự lấy mới nhất qua get_latest_trace_id), gọi get_runtime_flow,
    chạy agent với PROMPT_POST_DEPLOY + design, lưu kết quả qua save_result, trả JSON.
    """
    payload = request.get_json(silent=True) or {}

    flow_id = payload.get("flow_id", "F1") if isinstance(payload, dict) else "F1"
    trace_id = payload.get("trace_id") if isinstance(payload, dict) else None
    service = payload.get("service", "ewallet-payment-order") if isinstance(payload, dict) else "ewallet-payment-order"
    operation = payload.get("operation") if isinstance(payload, dict) else None

    runtime_flow = ""

    # Nếu không có trace_id, tự động lấy trace mới nhất từ Jaeger
    if not trace_id and service:
        trace_id = get_latest_trace_id(service=service, operation=operation)

    if trace_id:
        print(f"\n[Post-deploy] Đang lấy runtime trace {trace_id} từ Jaeger...")
        runtime_flow = get_runtime_flow(trace_id)
    else:
        # Hỗ trợ fallback: nếu gửi text log trực tiếp thay vì trace
        raw_log = ""
        if isinstance(payload, dict) and "log" in payload:
            raw_log = str(payload["log"])
        else:
            raw_log = request.get_data(as_text=True)

        if raw_log and raw_log.strip():
            print("\n[Post-deploy] Đang xử lý raw log...")
            runtime_flow = parse_log(raw_log)
        else:
            return jsonify({
                "status": "error",
                "message": f"Không tìm thấy trace_id và không có dữ liệu log cho service '{service}'"
            }), 400

    # Lấy tài liệu thiết kế từ Confluence
    design = get_design_by_flow(flow_id)

    user_input = f"""THIẾT KẾ:
{design}

LUỒNG THỰC TẾ TỪ TRACE:
{runtime_flow}

Hãy đối chiếu và kết luận."""

    print(f"\n[Post-deploy] Đang gửi cho agent đối chiếu flow {flow_id}...")
    conclusion = run_agent(system_prompt=PROMPT_POST_DEPLOY, user_input=user_input)

    # Tách verdict từ dòng đầu tiên của kết luận
    first_line = conclusion.strip().split("\n")[0].upper()
    verdict = "WARN" if "WARN" in first_line else ("PASS" if "PASS" in first_line else "UNKNOWN")

    # Lưu kết quả vào DB
    result_id = save_result(
        flow_id=flow_id,
        analysis_type="post_deploy",
        verdict=verdict,
        detail=conclusion,
        runtime_flow=runtime_flow
    )

    print("\n========== KẾT LUẬN POST-DEPLOY ==========")
    print(f"Flow: {flow_id} | Verdict: {verdict} | Result ID: {result_id}")
    print(conclusion)
    print("==========================================\n")

    return jsonify({
        "status": "success",
        "stage": "post_deploy",
        "id": result_id,
        "flow_id": flow_id,
        "trace_id": trace_id,
        "verdict": verdict,
        "conclusion": conclusion,
        "runtime_flow": runtime_flow
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
