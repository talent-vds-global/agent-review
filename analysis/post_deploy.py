"""Một vòng phân tích Luồng 2 (post-deploy) cho một flow — lõi dùng chung của mọi trigger.

`POST /runtime` (bấm nút trên dashboard), lịch chạy hằng ngày, polling và webhook Confluence đều
đi qua `run_post_deploy()`, nên mọi kết quả trong `analysis_results` được tạo theo cùng một cách
và chỉ khác nhau ở cột `triggered_by`.

Thứ tự xử lý: chọn trace -> **nhận diện flow** (`analysis/flow_detect.py`) -> **đối chiếu tất
định** với tài liệu Confluence (`analysis/evidence.py`) -> đưa cả trace lẫn kết quả đối chiếu cho
agent diễn giải -> lưu verdict kèm bảng bằng chứng để dashboard đọc lại.
"""

from analysis.evidence import (
    build_evidence, to_prompt_text, PASS, RETRY, ESCALATE, VERDICT_SEVERITY,
)
from analysis.flow_detect import detect_flow, flow_matches, latest_trace_for_flow
from context.results import save_result, get_recent_flow_results
from sources.runtime import get_runtime_flow, get_latest_trace_id, get_spans, parse_log
from sources.confluence import get_flow_spec, render_spec_text
from agent.core import run_agent
from agent.prompt import PROMPT_POST_DEPLOY

DEFAULT_SERVICE = "ewallet-payment-order"

# Khi đã có bảng đối chiếu, prompt chỉ kèm ngần ấy dòng đầu của danh sách span: bảng đối chiếu
# đã chứa bằng chứng từng bước, gửi đủ cả trace (F1 ~150 span) làm request vượt hạn mức token/phút.
RUNTIME_FLOW_PROMPT_LINES = 30

# Cùng một vi phạm còn nguyên sau ngần ấy lượt liên tiếp (kể cả lượt này) thì RETRY không giải
# quyết được nữa -> ESCALATE cho người.
REPEAT_ESCALATE_RUNS = 3


def split_verdict(conclusion: str) -> str:
    """Tách verdict từ dòng đầu tiên của kết luận agent.

    Thứ tự ưu tiên: ESCALATE > RETRY > PASS. WARN (thang cũ) được hiểu là RETRY.
    Nếu không khớp từ khoá nào → UNKNOWN.
    """
    first_line = (conclusion or "").strip().split("\n")[0].upper()
    if ESCALATE in first_line:
        return ESCALATE
    if RETRY in first_line or "WARN" in first_line:
        return RETRY
    if PASS in first_line:
        return PASS
    return "UNKNOWN"


def _compact_runtime_flow(runtime_flow: str, max_lines: int) -> str:
    """Giữ `max_lines` dòng đầu của danh sách span, ghi rõ số dòng đã lược."""
    lines = (runtime_flow or "").splitlines()
    if len(lines) <= max_lines:
        return runtime_flow
    return "\n".join(lines[:max_lines] + [
        f"... (lược {len(lines) - max_lines} span; bằng chứng từng bước nằm trong phần "
        f"KẾT QUẢ ĐỐI CHIẾU TẤT ĐỊNH bên dưới)"
    ])


def _repeated_violations(flow_id: str, current_keys: list) -> list:
    """Khoá vi phạm có mặt ở cả lượt này lẫn (REPEAT_ESCALATE_RUNS - 1) lượt liền trước."""
    if not current_keys:
        return []
    try:
        previous = get_recent_flow_results(flow_id, REPEAT_ESCALATE_RUNS - 1)
    except Exception as e:
        print(f"[Post-deploy] Không đọc được lịch sử kết quả của {flow_id}: {e}")
        return []
    if len(previous) < REPEAT_ESCALATE_RUNS - 1:
        return []
    repeated = set(current_keys)
    for row in previous:
        repeated &= set((row.get("summary") or {}).get("violation_keys") or [])
    return sorted(repeated)


def run_post_deploy(flow_id: str = "", trace_id: str = None, service: str = None,
                    operation: str = None, raw_log: str = "", triggered_by: str = "manual",
                    trigger_run_id: int = None) -> tuple:
    """Phân tích một trace (hoặc raw log) của một flow và lưu kết quả.

    `flow_id` là tuỳ chọn: bỏ trống thì trace tự khai mình thuộc flow nào. Truyền `flow_id` mà
    không truyền `trace_id` thì hệ thống tìm trace mới nhất **của đúng flow đó**.

    Args:
        triggered_by: nguồn kích hoạt, ghi vào `analysis_results.triggered_by`
            ("manual", "schedule_daily", "confluence_poll", "confluence_webhook").
        trigger_run_id: lượt chạy trong `trigger_runs` mà kết quả này thuộc về (nếu có).

    Returns:
        tuple: (body, http_status) — body đúng như `POST /runtime` vẫn trả.
    """
    flow_id = (flow_id or "").strip()
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
    elif raw_log and raw_log.strip():
        if not flow_id:
            # Raw log không có span để nhận diện flow: đoán bừa "F1" thì đem log đi so với tài
            # liệu của flow khác và verdict vô nghĩa.
            return {
                "status": "error",
                "stage": "post_deploy",
                "message": "Phân tích raw log cần truyền flow_id (log không có span để tự "
                           "nhận diện flow).",
            }, 400
        print("\n[Post-deploy] Đang xử lý raw log...")
        runtime_flow = parse_log(raw_log)
    else:
        where = f"flow {flow_id}" if flow_id else f"service '{service or DEFAULT_SERVICE}'"
        return {
            "status": "error",
            "message": f"Không tìm thấy trace nào của {where} trong Jaeger, "
                       f"và cũng không có dữ liệu log trong body.",
        }, 400

    # Nhận diện flow TRƯỚC khi đọc tài liệu: đối chiếu trace với tài liệu của flow khác thì
    # mọi bước đều "thiếu" và verdict vô nghĩa.
    if spans:
        detection = detect_flow(spans)
        print(f"[Post-deploy] Nhận diện flow: {detection['kind']} {detection['flow_id']} "
              f"— {detection['reason']}")
        if not flow_id:
            flow_id = detection["flow_id"]
            if not flow_id:
                return {
                    "status": "error",
                    "stage": "post_deploy",
                    "trace_id": trace_id,
                    "detection": detection,
                    "message": "Không nhận diện được flow của trace này nên không có tài liệu để "
                               "đối chiếu. Truyền flow_id, hoặc bổ sung cửa vào vào "
                               "mapping/flow-map.yaml.",
                }, 422
        elif not flow_matches(detection, flow_id):
            print(f"[Post-deploy] LƯU Ý: trace được nhận diện là "
                  f"{detection['flow_id'] or detection['kind']} nhưng người gọi yêu cầu {flow_id}.")

    # Tài liệu thiết kế từ Confluence — dạng có cấu trúc để đối chiếu, dạng text để đưa vào prompt.
    # Không đọc được tài liệu thì KHÔNG thay bằng bản dự phòng: phần đối chiếu sẽ ESCALATE.
    spec = get_flow_spec(flow_id)
    design = render_spec_text(spec)
    if spec.get("error") and not spec.get("steps"):
        print(f"[Post-deploy] Không dùng được tài liệu Confluence: {spec['error']}")

    # Phiên bản tài liệu đã dùng: khi trigger là "tài liệu đổi", báo cáo phải chỉ ra được verdict
    # này tính trên phiên bản nào của trang. Lấy cùng lần gọi đọc nội dung trang.
    spec_version = spec.get("version")

    # Đối chiếu tất định TRƯỚC khi gọi LLM
    evidence = build_evidence(spec, spans, trace_id=trace_id or "") if spans else None
    if evidence:
        evidence["detection"] = detection
        evidence["spec_version"] = spec_version
        s = evidence["summary"]
        # Flow khớp qua nhãn phụ (vd F5 là đuôi thông báo trong trace F1) là hợp lệ: segments
        # trong mapping chỉ đối chiếu đúng đoạn đó của trace.
        if detection and not flow_matches(detection, flow_id):
            # Trace không phải của flow được yêu cầu (hoặc không nhận diện được): đối chiếu với
            # tài liệu flow này không có nghĩa, verifier không tự kết luận được.
            s["verdict_reasons"].insert(0, f"Trace được nhận diện là "
                                           f"{detection['flow_id'] or detection['kind']}, "
                                           f"khác flow được yêu cầu {flow_id}")
            s["verdict"] = ESCALATE
        repeated = _repeated_violations(flow_id, s.get("violation_keys") or [])
        if repeated and s["verdict"] == RETRY:
            s["verdict_reasons"].insert(0, f"Vi phạm lặp lại qua {REPEAT_ESCALATE_RUNS} lượt liên "
                                           f"tiếp, RETRY không khắc phục được: {', '.join(repeated)}")
            s["verdict"] = ESCALATE
        print(f"[Post-deploy] Đối chiếu tài liệu: khớp {s['matched']}/{s['total_steps']} bước, "
              f"thiếu {s['missing']}, NFR vi phạm {s['nfr_fail']} -> {s['verdict']}")

    user_input = f"""THIẾT KẾ:
{design}

LUỒNG THỰC TẾ TỪ TRACE:
{_compact_runtime_flow(runtime_flow, RUNTIME_FLOW_PROMPT_LINES) if evidence else runtime_flow}

{to_prompt_text(evidence) if evidence else ''}

Hãy đối chiếu và kết luận."""

    print(f"\n[Post-deploy] Đang gửi cho agent đối chiếu flow {flow_id}...")
    try:
        conclusion = run_agent(system_prompt=PROMPT_POST_DEPLOY, user_input=user_input)
    except Exception as e:
        # LLM chỉ diễn giải; verdict đã có từ phần tất định. LLM lỗi (vượt hạn mức token, rate
        # limit, mất kết nối) thì vẫn lưu verdict tất định thay vì bỏ cả lượt phân tích.
        if not evidence:
            raise
        llm_error = str(e)[:500]
        print(f"[Post-deploy] LLM lỗi, dùng verdict tất định: {llm_error}")
        evidence["llm_error"] = llm_error
        conclusion = (f"{evidence['summary']['verdict']}\n\n"
                      f"_Không có diễn giải AI cho lượt này: {llm_error}_\n\n"
                      f"Verdict lấy từ đối chiếu tất định.")
    verdict = split_verdict(conclusion)

    # Server-side override: agent không được hạ mức khi phần tất định đã thấy vi phạm
    det_verdict = evidence["summary"]["verdict"] if evidence else None
    if det_verdict and VERDICT_SEVERITY.get(det_verdict, 0) > VERDICT_SEVERITY.get(verdict, 0):
        print(f"[Post-deploy] Agent kết luận {verdict} nhưng đối chiếu tất định là "
              f"{det_verdict} -> giữ {det_verdict}.")
        verdict = det_verdict

    result_id = save_result(
        flow_id=flow_id,
        analysis_type="post_deploy",
        verdict=verdict,
        detail=conclusion,
        runtime_flow=runtime_flow,
        trace_id=trace_id or None,
        evidence=evidence,
        triggered_by=triggered_by,
        trigger_run_id=trigger_run_id,
        spec_version=spec_version,
    )

    print("\n========== KẾT LUẬN POST-DEPLOY ==========")
    print(f"Flow: {flow_id} | Verdict: {verdict} | Result ID: {result_id} | Trigger: {triggered_by}")
    print(conclusion)
    print("==========================================\n")

    return {
        "status": "success",
        "stage": "post_deploy",
        "id": result_id,
        "flow_id": flow_id,
        "trace_id": trace_id,
        "detection": detection,
        "verdict": verdict,
        "triggered_by": triggered_by,
        "spec_version": spec_version,
        "conclusion": conclusion,
        "runtime_flow": runtime_flow,
        "evidence": evidence,
    }, 200
