import json
import requests
import config

# Từ khóa của span NHIỄU cần bỏ (chi tiết ORM/Hibernate/DB nội bộ)
NOISE_PREFIXES = [
    "Session.",           # Session.merge, Session.persist, Session.find
    "Transaction.commit",
    "SELECT com.",        # query theo entity class
    "SELECT ",            # SELECT paymentdb.xxx
    "INSERT ",
    "UPDATE ",
]

# Tên database đứng một mình cũng là nhiễu
NOISE_EXACT = ["orderdb", "paymentdb", "notifdb", "thirdpartydb"]


def is_noise(operation: str) -> bool:
    """Kiểm tra một operation/span có phải là span kỹ thuật nhiễu hay không."""
    if not operation:
        return False
    if operation in NOISE_EXACT:
        return True
    return any(operation.startswith(p) for p in NOISE_PREFIXES)


def get_runtime_flow(trace_id: str) -> str:
    """Gọi Jaeger API lấy trace, lọc bỏ span nhiễu, sắp theo thời gian, trả chuỗi các bước nghiệp vụ.

    Args:
        trace_id (str): Mã trace ID trong hệ thống Jaeger.

    Returns:
        str: Danh sách đánh số các bước nghiệp vụ thực tế (kèm [LỖI] nếu có).
    """
    url = f"{config.JAEGER_URL}/api/traces/{trace_id}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            return f"Không tìm thấy trace với ID: {trace_id} trong Jaeger."
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        return f"Lỗi khi kết nối tới Jaeger ({url}): {str(e)}"

    traces = data.get("data", [])
    if not traces:
        return f"Không tìm thấy dữ liệu trace cho trace_id: {trace_id}"

    trace = traces[0]
    spans = trace.get("spans", [])
    processes = trace.get("processes", {})

    span_list = []
    for s in spans:
        process_id = s.get("processID", "")
        service = processes.get(process_id, {}).get("serviceName", "unknown")
        op = s.get("operationName", "")
        has_error = any(
            t.get("key") == "error" and t.get("value") is True
            for t in s.get("tags", [])
        )
        span_list.append({
            "service": service,
            "op": op,
            "start": s.get("startTime", 0),
            "duration_ms": round(s.get("duration", 0) / 1000, 1),
            "error": has_error,
        })

    # Sắp theo thời gian bắt đầu
    span_list.sort(key=lambda x: x["start"])

    # Lọc: chỉ giữ span nghiệp vụ
    lines = []
    for sp in span_list:
        if is_noise(sp["op"]):
            continue
        err = " [LỖI]" if sp["error"] else ""
        lines.append(f"[{sp['service']}] {sp['op']} ({sp['duration_ms']}ms){err}")

    if not lines:
        return "Không có bước nghiệp vụ nào sau khi lọc bỏ span nhiễu."

    return "\n".join(f"{i}. {l}" for i, l in enumerate(lines, 1))


def get_latest_trace_id(service: str, operation: str = None) -> str:
    """Lấy trace ID mới nhất từ Jaeger theo service, bỏ qua trace health check.

    Args:
        service (str): Tên service trong Jaeger.
        operation (str, optional): Tên operation cần lọc. Nếu None, lấy nhiều trace
            gần nhất rồi tự bỏ qua health check.

    Returns:
        str: Trace ID mới nhất hợp lệ, hoặc chuỗi rỗng nếu không tìm thấy.
    """
    url = f"{config.JAEGER_URL}/api/traces"
    # Lấy nhiều trace gần nhất (không chỉ 1) để còn lọc bỏ health check
    params = {"service": service, "limit": 20}
    if operation:
        params["operation"] = operation

    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        traces = data.get("data", [])
        if not traces:
            return ""

        # Bỏ qua các trace không phải nghiệp vụ (health check, actuator...)
        for trace in traces:
            spans = trace.get("spans", [])
            # Lấy operation của span gốc (root span - không có parent)
            root_ops = [
                s.get("operationName", "")
                for s in spans
                if not s.get("references")  # span gốc không có reference tới span cha
            ]
            root_op = root_ops[0] if root_ops else ""

            # Bỏ qua health check và các endpoint hạ tầng
            if "actuator" in root_op or "health" in root_op.lower():
                continue

            # Trace có nhiều span (giao dịch thật thường >5 span) mới lấy
            if len(spans) >= 5:
                return trace.get("traceID", "")

        # Nếu không có trace nghiệp vụ nào, trả về trace mới nhất (fallback)
        return traces[0].get("traceID", "")

    except requests.exceptions.RequestException as e:
        print(f"[Jaeger] Lỗi khi truy vấn trace mới nhất: {e}")
        return ""


def parse_log(raw_log: str) -> str:
    """Nhận runtime log/trace thô và trích xuất thông tin hành vi thực tế của ứng dụng.

    Định dạng lại chuỗi log (hỗ trợ cả JSON string và plain text log) thành cấu trúc
    trực quan (thứ tự gọi hàm, luồng chạy, trạng thái) để cung cấp cho Agent phân tích.

    Args:
        raw_log (str): Dữ liệu log hoặc trace thô nhận được từ hệ thống giám sát / application runtime.

    Returns:
        str: Chuỗi văn bản đã chuẩn hóa và định dạng rõ ràng để đưa vào ngữ cảnh Agent.
    """
    if not raw_log or not str(raw_log).strip():
        return "Dữ liệu log trống."

    text = str(raw_log).strip()

    # Thử parse dưới dạng JSON
    try:
        parsed_json = json.loads(text)
        formatted_json = json.dumps(parsed_json, indent=2, ensure_ascii=False)
        return (
            "--- RUNTIME LOG / TRACE (Structured JSON) ---\n"
            f"{formatted_json}\n"
            "--------------------------------------------"
        )
    except Exception:
        pass

    # Trường hợp là text log nhiều dòng
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    formatted_lines = []
    for idx, line in enumerate(lines, start=1):
        formatted_lines.append(f"[{idx:02d}] {line}")

    numbered_log = "\n".join(formatted_lines)
    return (
        "--- RUNTIME LOG / TRACE (Sequential Lines) ---\n"
        f"{numbered_log}\n"
        "----------------------------------------------"
    )
