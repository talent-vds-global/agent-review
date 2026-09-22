import os

from flask import Flask, request, jsonify
from flask_cors import CORS

import config

from analysis.evidence import build_evidence, to_prompt_text
from analysis.flow_detect import detect_flow, latest_trace_for_flow, list_flows
from context.results import save_result, get_latest_flow_result, list_analysis_results
from sources.github import get_pr_diff
from sources.runtime import (
    get_runtime_flow, get_latest_trace_id, get_spans, get_timeline, parse_log,
)
from sources.confluence import get_flow_spec, render_spec_text, FLOW_PAGE_MAP
from sources.dbquality import get_quality_for_services
from agent.core import run_agent
from agent.prompt import PROMPT_PRE_MERGE, PROMPT_POST_DEPLOY, FALLBACK_DESIGNS

app = Flask(__name__)
CORS(app)  # Cho phép dashboard (cổng khác) gọi API

DEFAULT_SERVICE = "ewallet-payment-order"


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
            "list_flows": "GET /api/flows",
            "flow_analysis": "GET /api/flows/<flow_id>/analysis",
            "flow_evidence": "GET /api/flows/<flow_id>/evidence",
            "trace_flow": "GET /api/traces/<trace_id>/flow",
            "trace_timeline": "GET /api/traces/<trace_id>/timeline",
            "db_quality": "GET /api/db-quality",
        }
    })


def _split_verdict(conclusion: str) -> str:
    """Tách verdict từ dòng đầu tiên của kết luận agent."""
    first_line = (conclusion or "").strip().split("\n")[0].upper()
    return "WARN" if "WARN" in first_line else ("PASS" if "PASS" in first_line else "UNKNOWN")


# ========== API cho Dashboard ==========

@app.route("/api/analysis", methods=["GET"])
def list_analysis():
    """Trả danh sách tất cả kết quả phân tích, mới nhất trước."""
    return jsonify(list_analysis_results(limit=int(request.args.get("limit", 50))))


@app.route("/api/flows", methods=["GET"])
def flows():
    """Danh sách luồng nghiệp vụ đã khai báo trong `mapping/flow-map.yaml`.

    Cho dashboard đổ danh sách flow mà không phải chờ có kết quả phân tích, và cho biết flow nào
    đã có file ánh xạ riêng (`mapping/<flow>.yaml`) thay vì matcher suy tự động.
    """
    items = list_flows()
    for item in items:
        path = os.path.join(config.MAPPING_DIR, f"{item['flow_id']}.yaml")
        item["has_mapping"] = os.path.exists(path)
        item["page_id"] = FLOW_PAGE_MAP.get(item["flow_id"], "")
    return jsonify(items)


@app.route("/api/traces/<trace_id>/flow", methods=["GET"])
def trace_flow(trace_id):
    """Trace này thuộc luồng nghiệp vụ nào — kèm lý do và nhãn phụ.

    Dùng để kiểm nhanh cơ chế nhận diện mà không phải chạy cả vòng phân tích.
    """
    spans = get_spans(trace_id)
    if not spans:
        return jsonify({"error": f"Không tìm thấy trace {trace_id} trong Jaeger."}), 404
    return jsonify(dict(detect_flow(spans), trace_id=trace_id))


@app.route("/api/flows/<flow_id>/analysis", methods=["GET"])
def flow_analysis(flow_id):
    """Trả kết quả phân tích mới nhất của một flow (kèm bảng đối chiếu đã lưu)."""
    row = get_latest_flow_result(flow_id)
    if row is None:
        return jsonify({"error": f"Không có kết quả phân tích cho flow {flow_id}"}), 404
    return jsonify(row)


@app.route("/api/flows/<flow_id>/evidence", methods=["GET"])
def flow_evidence(flow_id):
    """Dựng lại bảng đối chiếu tài liệu ↔ runtime ngay tại thời điểm gọi.

    Tham số: `trace_id` (mặc định: trace đã lưu của flow, hoặc trace mới nhất),
    `service` (dùng khi phải tự tìm trace mới nhất).

    Khác với `/api/flows/<id>/analysis` (trả bảng đã đóng băng cùng verdict), endpoint này
    đọc lại tài liệu Confluence và trace hiện tại — dùng khi tài liệu vừa được sửa.
    """
    trace_id = request.args.get("trace_id", "")
    if not trace_id:
        row = get_latest_flow_result(flow_id)
        trace_id = (row or {}).get("trace_id") or ""
    if not trace_id:
        # Tìm trace THUỘC ĐÚNG FLOW này, không phải trace mới nhất của một service bất kỳ
        trace_id, _ = latest_trace_for_flow(flow_id)
    if not trace_id and request.args.get("service"):
        trace_id = get_latest_trace_id(request.args["service"])
    if not trace_id:
        return jsonify({"error": f"Không tìm thấy trace nào của flow {flow_id} để đối chiếu."}), 404

    spans = get_spans(trace_id)
    if not spans:
        return jsonify({"error": f"Không lấy được span của trace {trace_id}."}), 404

    spec = get_flow_spec(flow_id)
    result = build_evidence(spec, spans, trace_id=trace_id)
    result["detection"] = detect_flow(spans)
    return jsonify(result)


@app.route("/api/traces/<trace_id>/timeline", methods=["GET"])
def trace_timeline(trace_id):
    """Trả sơ đồ trace đã rút gọn (gộp cặp client/server, cuộn truy vấn DB vào bước cha).

    Tham số `db=1` để kèm chi tiết nhóm truy vấn của từng bước.
    """
    include_db = request.args.get("db", "1") not in ("0", "false", "no")
    timeline = get_timeline(trace_id, include_db=include_db)
    if not timeline:
        return jsonify({"error": f"Không tìm thấy trace {trace_id} trong Jaeger."}), 404
    return jsonify(timeline)


@app.route("/api/db-quality", methods=["GET"])
def db_quality():
    """Số liệu database-quality-library của các service, đọc tại thời điểm gọi (không lưu).

    Tham số `services` là danh sách tên service ngăn cách bằng dấu phẩy;
    bỏ trống thì trả toàn bộ service có cấu hình dashboard.
    """
    raw = request.args.get("services", "")
    services = [s.strip() for s in raw.split(",") if s.strip()]
    return jsonify(get_quality_for_services(services))


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
    verdict = _split_verdict(conclusion)

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

    Thứ tự xử lý: chọn trace -> **nhận diện flow** (`analysis/flow_detect.py`) -> **đối chiếu tất
    định** với tài liệu Confluence (`analysis/evidence.py`) -> đưa cả trace lẫn kết quả đối chiếu
    cho agent diễn giải -> lưu verdict kèm bảng bằng chứng để dashboard đọc lại.

    `flow_id` là tuỳ chọn: bỏ trống thì trace tự khai mình thuộc flow nào. Truyền `flow_id` mà
    không truyền `trace_id` thì hệ thống tìm trace mới nhất **của đúng flow đó**.
    """
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        payload = {}

    flow_id = (payload.get("flow_id") or "").strip()
    trace_id = payload.get("trace_id")
    service = payload.get("service")
    operation = payload.get("operation")

    runtime_flow = ""
    spans = []
    detection = None

    # Chọn trace: ưu tiên trace đích danh -> trace mới nhất của flow -> trace mới nhất của service
    if not trace_id and flow_id:
        trace_id, _ = latest_trace_for_flow(flow_id)
        if trace_id:
            print(f"\n[Post-deploy] Trace mới nhất của flow {flow_id}: {trace_id}")
    if not trace_id and (service or not flow_id):
        trace_id = get_latest_trace_id(service=service or DEFAULT_SERVICE, operation=operation)

    if trace_id:
        print(f"\n[Post-deploy] Đang lấy runtime trace {trace_id} từ Jaeger...")
        runtime_flow = get_runtime_flow(trace_id)
        spans = get_spans(trace_id)
    else:
        # Hỗ trợ fallback: nếu gửi text log trực tiếp thay vì trace
        raw_log = str(payload["log"]) if "log" in payload else request.get_data(as_text=True)
        if raw_log and raw_log.strip():
            print("\n[Post-deploy] Đang xử lý raw log...")
            runtime_flow = parse_log(raw_log)
        else:
            where = (f"flow {flow_id}" if flow_id
                     else f"service '{service or DEFAULT_SERVICE}'")
            return jsonify({
                "status": "error",
                "message": f"Không tìm thấy trace nào của {where} trong Jaeger, "
                           f"và cũng không có dữ liệu log trong body.",
            }), 400

    # Nhận diện flow TRƯỚC khi đọc tài liệu: đối chiếu trace với tài liệu của flow khác thì
    # mọi bước đều "thiếu" và verdict vô nghĩa.
    if spans:
        detection = detect_flow(spans)
        print(f"[Post-deploy] Nhận diện flow: {detection['kind']} {detection['flow_id']} "
              f"— {detection['reason']}")
        if not flow_id:
            flow_id = detection["flow_id"]
            if not flow_id:
                return jsonify({
                    "status": "error",
                    "stage": "post_deploy",
                    "trace_id": trace_id,
                    "detection": detection,
                    "message": "Không nhận diện được flow của trace này nên không có tài liệu để "
                               "đối chiếu. Truyền flow_id, hoặc bổ sung cửa vào vào "
                               "mapping/flow-map.yaml.",
                }), 422
        elif detection["flow_id"] and detection["flow_id"] != flow_id:
            print(f"[Post-deploy] LƯU Ý: trace được nhận diện là {detection['flow_id']} "
                  f"nhưng người gọi yêu cầu {flow_id}.")
    if not flow_id:
        flow_id = "F1"      # chỉ còn đường này khi phân tích raw log, không có span để nhận diện

    # Tài liệu thiết kế từ Confluence — dạng có cấu trúc để đối chiếu, dạng text để đưa vào prompt
    spec = get_flow_spec(flow_id)
    design = render_spec_text(spec)
    if spec.get("error") and not spec.get("steps"):
        print(f"[Post-deploy] Không đọc được tài liệu Confluence: {spec['error']}")
        design = FALLBACK_DESIGNS.get(flow_id, design)

    # Đối chiếu tất định TRƯỚC khi gọi LLM
    evidence = build_evidence(spec, spans, trace_id=trace_id or "") if spans else None
    if evidence:
        evidence["detection"] = detection
        s = evidence["summary"]
        print(f"[Post-deploy] Đối chiếu tài liệu: khớp {s['matched']}/{s['total_steps']} bước, "
              f"thiếu {s['missing']}, NFR vi phạm {s['nfr_fail']} -> {s['verdict']}")

    user_input = f"""THIẾT KẾ:
{design}

LUỒNG THỰC TẾ TỪ TRACE:
{runtime_flow}

{to_prompt_text(evidence) if evidence else ''}

Hãy đối chiếu và kết luận."""

    print(f"\n[Post-deploy] Đang gửi cho agent đối chiếu flow {flow_id}...")
    conclusion = run_agent(system_prompt=PROMPT_POST_DEPLOY, user_input=user_input)
    verdict = _split_verdict(conclusion)

    # Luật verdict áp ở server: agent không được hạ mức khi phần tất định đã thấy vi phạm
    if evidence and evidence["summary"]["verdict"] == "WARN" and verdict == "PASS":
        print("[Post-deploy] Agent kết luận PASS nhưng đối chiếu tất định là WARN -> giữ WARN.")
        verdict = "WARN"

    result_id = save_result(
        flow_id=flow_id,
        analysis_type="post_deploy",
        verdict=verdict,
        detail=conclusion,
        runtime_flow=runtime_flow,
        trace_id=trace_id or None,
        evidence=evidence,
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
        "detection": detection,
        "verdict": verdict,
        "conclusion": conclusion,
        "runtime_flow": runtime_flow,
        "evidence": evidence,
    }), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
