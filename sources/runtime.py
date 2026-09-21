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


def _fetch_trace(trace_id: str):
    """Gọi Jaeger API lấy trace data thô (dict), dùng chung cho nhiều hàm.

    Args:
        trace_id (str): Mã trace ID trong hệ thống Jaeger.

    Returns:
        tuple: (trace_dict, error_message). Nếu thành công thì trace_dict là dict
               chứa spans + processes, error_message là None. Nếu lỗi thì trace_dict
               là None, error_message mô tả lỗi.
    """
    url = f"{config.JAEGER_URL}/api/traces/{trace_id}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 404:
            return None, f"Không tìm thấy trace với ID: {trace_id} trong Jaeger."
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        return None, f"Lỗi khi kết nối tới Jaeger ({url}): {str(e)}"

    traces = data.get("data", [])
    if not traces:
        return None, f"Không tìm thấy dữ liệu trace cho trace_id: {trace_id}"

    return traces[0], None


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
    trace, error = _fetch_trace(trace_id)
    if error:
        return error

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


def get_db_quality(trace_id: str) -> list:
    """Trích xuất và gom nhóm thông tin chất lượng database từ các span SQL trong trace.

    Gom các query giống nhau theo (operation, table, statement), tính số lần gọi (call_count),
    tổng thời gian (total_ms), thời gian lớn nhất (max_ms), và phát hiện N+1 (call_count >= 3).

    Args:
        trace_id (str): Mã trace ID trong hệ thống Jaeger.

    Returns:
        list: Danh sách dict mô tả các nhóm query SQL, sắp xếp alert lên đầu,
              rồi theo call_count giảm dần, rồi total_ms giảm dần.
              Trả list rỗng nếu không tìm thấy trace hoặc không có span SQL nào.
    """
    trace, error = _fetch_trace(trace_id)
    if error:
        return []

    spans = trace.get("spans", [])
    groups = {}

    for s in spans:
        # Xây dict key→value từ mảng tags của Jaeger
        tags = {t.get("key"): t.get("value") for t in s.get("tags", [])}

        # Chỉ lấy span có tag db.statement (span SQL)
        raw_statement = tags.get("db.statement")
        if not raw_statement:
            continue

        statement = str(raw_statement).strip()
        table = str(tags.get("db.sql.table") or tags.get("db.table") or "").strip()
        operation = str(tags.get("db.operation") or "").strip().upper()
        if not operation and statement:
            operation = statement.split()[0].upper()

        # Đọc execution_time_ms an toàn (từ tag db.execution_time_ms hoặc duration span tính bằng ms)
        raw_exec = tags.get("db.execution_time_ms")
        if raw_exec is not None:
            try:
                execution_time_ms = float(raw_exec)
            except (ValueError, TypeError):
                execution_time_ms = 0.0
        elif "duration" in s:
            try:
                execution_time_ms = round(float(s["duration"]) / 1000.0, 2)
            except (ValueError, TypeError):
                execution_time_ms = 0.0
        else:
            execution_time_ms = 0.0

        # Đọc quality_flags: tách chuỗi theo dấu phẩy, strip khoảng trắng
        raw_flags = str(tags.get("db.quality_flags", ""))
        if raw_flags.strip():
            span_flags = [f.strip() for f in raw_flags.split(",") if f.strip()]
        else:
            span_flags = []

        # Khóa gom: operation + table + statement
        group_key = (operation, table, statement)
        if group_key not in groups:
            groups[group_key] = {
                "statement": statement,
                "table": table,
                "operation": operation,
                "call_count": 0,
                "total_ms": 0.0,
                "max_ms": 0.0,
                "raw_flags": [],
            }

        g = groups[group_key]
        g["call_count"] += 1
        g["total_ms"] += execution_time_ms
        if execution_time_ms > g["max_ms"] or g["call_count"] == 1:
            g["max_ms"] = execution_time_ms

        for f in span_flags:
            if f != "OK" and f not in g["raw_flags"]:
                g["raw_flags"].append(f)

    results = []
    for g in groups.values():
        call_count = g["call_count"]
        flags = list(g["raw_flags"])

        # Phát hiện N+1: nếu call_count >= 3 cho cùng một query -> thêm cờ N+1_SUSPECT
        if call_count >= 3 and "N+1_SUSPECT" not in flags:
            flags.append("N+1_SUSPECT")

        # Xác định status: có bất kỳ cờ khác OK hoặc call_count >= 3 -> alert
        if flags:
            status = "alert"
        else:
            flags = ["OK"]
            status = "ok"

        results.append({
            "statement": g["statement"],
            "table": g["table"],
            "operation": g["operation"],
            "call_count": call_count,
            "total_ms": round(g["total_ms"], 2),
            "max_ms": round(g["max_ms"], 2),
            "flags": flags,
            "status": status,
        })

    # Sắp xếp: status "alert" lên đầu, rồi theo call_count giảm dần, rồi total_ms giảm dần
    results.sort(key=lambda q: (
        0 if q["status"] == "alert" else 1,
        -q["call_count"],
        -q["total_ms"]
    ))

    return results


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
