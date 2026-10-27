"""MCP Server — bọc REST API của Runtime Verifier thành MCP tool.

Cho phép bất kỳ coding agent nào (Claude Code, Cursor, Cline, …) gọi verify_runtime như một bước
trong vòng lặp harness: agent viết code → deploy → gọi verify_runtime → nhận PASS/WARN → quyết
định sửa hay chuyển bước tiếp.

Chạy:
    python mcp_server.py                         # stdio (mặc định, dùng với agent)
    python mcp_server.py --transport sse          # SSE  (dùng với dashboard/web)
    VERIFIER_URL=http://10.0.0.5:5000 python mcp_server.py   # trỏ sang server khác

Cấu hình agent (ví dụ claude_desktop_config.json):
    {
      "mcpServers": {
        "runtime-verifier": {
          "command": "python",
          "args": ["/đường/dẫn/tới/mcp_server.py"],
          "env": { "VERIFIER_URL": "http://localhost:5000" }
        }
      }
    }
"""

import json
import os
import sys
from typing import Any

import requests
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Cấu hình
# ---------------------------------------------------------------------------

VERIFIER_URL = os.getenv("VERIFIER_URL", "http://localhost:5000")

# ---------------------------------------------------------------------------
# Khởi tạo MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="runtime-verifier",
    version="0.1.0",
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(path: str, payload: dict) -> dict:
    """Gọi REST API của verifier backend."""
    url = f"{VERIFIER_URL}{path}"
    try:
        resp = requests.post(url, json=payload, timeout=120)
        return {"status_code": resp.status_code, "body": resp.json()}
    except requests.exceptions.ConnectionError:
        return {"error": f"Không kết nối được tới verifier tại {url}. Kiểm tra server đã chạy chưa."}
    except requests.exceptions.Timeout:
        return {"error": f"Verifier timeout sau 120s. Pipeline phân tích có thể đang xử lý trace lớn."}
    except Exception as e:
        return {"error": str(e)}


def _get(path: str, params: dict | None = None) -> dict:
    """Gọi GET endpoint của verifier backend."""
    url = f"{VERIFIER_URL}{path}"
    try:
        resp = requests.get(url, params=params, timeout=30)
        return {"status_code": resp.status_code, "body": resp.json()}
    except requests.exceptions.ConnectionError:
        return {"error": f"Không kết nối được tới verifier tại {url}."}
    except Exception as e:
        return {"error": str(e)}


def _format_verdict(result: dict) -> str:
    """Định dạng kết quả verify thành text dễ đọc cho agent."""
    if "error" in result:
        return f"❌ Lỗi: {result['error']}"

    body = result.get("body", {})
    code = result.get("status_code", 0)

    if code != 200:
        msg = body.get("message", body.get("error", "Không rõ lỗi"))
        return f"⚠️ Verifier trả HTTP {code}: {msg}"

    verdict = body.get("verdict", "UNKNOWN")
    flow_id = body.get("flow_id", "?")
    trace_id = body.get("trace_id", "?")
    spec_ver = body.get("spec_version", "?")

    lines = [
        f"{'✅' if verdict == 'PASS' else '🔴'} Verdict: **{verdict}**",
        f"Flow: {flow_id} | Trace: {trace_id} | Spec version: {spec_ver}",
    ]

    # Tóm tắt evidence
    evidence = body.get("evidence", {})
    summary = evidence.get("summary", {})
    if summary:
        det_verdict = summary.get("verdict", "")
        matched = summary.get("matched", 0)
        total = summary.get("total", 0)
        missing = summary.get("missing", 0)
        lines.append(f"Evidence: {matched}/{total} bước MATCHED, {missing} MISSING "
                      f"(deterministic verdict: {det_verdict})")

    # Tóm tắt NFR
    nfr = evidence.get("nfr", {})
    if nfr:
        nfr_lines = []
        for nfr_id, nfr_data in nfr.items():
            if isinstance(nfr_data, dict) and "value" in nfr_data:
                val = nfr_data["value"]
                unit = nfr_data.get("unit", "")
                nfr_lines.append(f"  {nfr_id}: {val}{unit}")
        if nfr_lines:
            lines.append("NFR measurements:")
            lines.extend(nfr_lines)

    # Diễn giải LLM (rút gọn)
    interpretation = body.get("interpretation", "")
    if interpretation:
        # Lấy 500 ký tự đầu
        short = interpretation[:500]
        if len(interpretation) > 500:
            short += "…"
        lines.append(f"\nLLM Interpretation:\n{short}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def verify_runtime(
    flow_id: str = "",
    trace_id: str = "",
    service: str = "",
    operation: str = "",
) -> str:
    """Xác minh hành vi runtime của hệ thống bằng cách đối chiếu trace thực tế với tài liệu thiết kế.

    Công cụ này kéo trace từ Jaeger và spec từ Confluence, chạy đối chiếu tất định (deterministic
    evidence matching) rồi diễn giải bằng LLM, trả verdict PASS hoặc WARN.

    Dùng sau khi deploy để kiểm tra hệ thống chạy đúng thiết kế hay không.

    Args:
        flow_id: Mã luồng nghiệp vụ cần kiểm tra (ví dụ "F1"). Nếu bỏ trống, verifier tự detect
                 từ trace.
        trace_id: ID của trace cụ thể cần kiểm tra. Nếu bỏ trống, verifier lấy trace mới nhất
                  của flow.
        service: Tên service để lọc trace (ví dụ "ewallet-gateway"). Tuỳ chọn.
        operation: Tên operation để lọc trace. Tuỳ chọn.

    Returns:
        Kết quả xác minh gồm verdict (PASS/WARN), bảng evidence, đo lường NFR và diễn giải.
    """
    payload = {}
    if flow_id:
        payload["flow_id"] = flow_id
    if trace_id:
        payload["trace_id"] = trace_id
    if service:
        payload["service"] = service
    if operation:
        payload["operation"] = operation

    result = _post("/runtime", payload)
    return _format_verdict(result)


@mcp.tool()
def list_flows() -> str:
    """Liệt kê các luồng nghiệp vụ đã được khai báo trong hệ thống.

    Trả danh sách flow_id, tên, và trạng thái ánh xạ (có file mapping riêng hay chưa).
    Dùng để biết flow nào có thể verify.

    Returns:
        Danh sách các flow đã khai báo.
    """
    result = _get("/api/flows")
    if "error" in result:
        return f"❌ Lỗi: {result['error']}"

    flows = result.get("body", [])
    if not flows:
        return "Không có flow nào được khai báo."

    lines = ["Các flow nghiệp vụ đã khai báo:\n"]
    for f in flows:
        fid = f.get("flow_id", "?")
        title = f.get("title", "")
        has_mapping = "✅" if f.get("has_mapping") else "—"
        lines.append(f"  {fid}: {title} (mapping: {has_mapping})")
    return "\n".join(lines)


@mcp.tool()
def get_latest_result(flow_id: str) -> str:
    """Lấy kết quả phân tích mới nhất của một flow.

    Args:
        flow_id: Mã luồng nghiệp vụ (ví dụ "F1").

    Returns:
        Kết quả phân tích gần nhất (verdict, thời gian, evidence tóm tắt).
    """
    result = _get(f"/api/flows/{flow_id}/analysis")
    if "error" in result:
        return f"❌ Lỗi: {result['error']}"

    body = result.get("body", {})
    code = result.get("status_code", 0)

    if code == 404:
        return f"Chưa có kết quả phân tích nào cho flow {flow_id}."

    if code != 200:
        return f"⚠️ HTTP {code}: {body}"

    # body có thể là list hoặc dict tuỳ endpoint
    if isinstance(body, list):
        if not body:
            return f"Chưa có kết quả phân tích nào cho flow {flow_id}."
        latest = body[0]
    else:
        latest = body

    verdict = latest.get("verdict", "?")
    created = latest.get("created_at", "?")
    trace_id = latest.get("trace_id", "?")
    triggered_by = latest.get("triggered_by", "?")

    return (
        f"{'✅' if verdict == 'PASS' else '🔴'} Flow {flow_id}: **{verdict}**\n"
        f"Thời gian: {created}\n"
        f"Trace: {trace_id}\n"
        f"Triggered by: {triggered_by}"
    )


@mcp.tool()
def get_system_overview() -> str:
    """Tổng quan chất lượng toàn hệ thống — tất cả flow, verdict mới nhất, xu hướng.

    Returns:
        Bảng tổng quan chất lượng hệ thống.
    """
    result = _get("/api/overview")
    if "error" in result:
        return f"❌ Lỗi: {result['error']}"

    body = result.get("body", {})
    flows = body.get("flows", [])

    if not flows:
        return "Chưa có dữ liệu tổng quan."

    lines = ["📊 Tổng quan chất lượng hệ thống\n"]
    for f in flows:
        fid = f.get("flow_id", "?")
        title = f.get("title", "")
        verdict = f.get("latest_verdict", "—")
        icon = "✅" if verdict == "PASS" else ("🔴" if verdict == "WARN" else "—")
        lines.append(f"  {icon} {fid} ({title}): {verdict}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = "stdio"
    if "--transport" in sys.argv:
        idx = sys.argv.index("--transport")
        if idx + 1 < len(sys.argv):
            transport = sys.argv[idx + 1]

    mcp.run(transport=transport)
