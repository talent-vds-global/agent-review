"""MCP Server — bọc REST API của Runtime Verifier thành MCP tool.

Runtime Verifier là hệ thống kiểm chứng độc lập, chạy bên ngoài vòng lặp của coding agent (kích
hoạt theo sự kiện, theo lịch hoặc thủ công). MCP tool này mở thêm một cửa để agent hoặc người dùng
chủ động yêu cầu kiểm chứng và đọc kết quả: PASS (đạt), RETRY (có vi phạm cần sửa, kèm danh sách)
hoặc ESCALATE (cần người quyết).

Chạy:
    python mcp_server.py                         # stdio (mặc định, dùng với agent)
    python mcp_server.py --transport sse          # SSE  (dùng với dashboard/web)
    VERIFIER_URL=http://10.0.0.5:8000 python mcp_server.py   # trỏ sang server khác

Cấu hình agent (ví dụ claude_desktop_config.json):
    {
      "mcpServers": {
        "runtime-verifier": {
          "command": "python",
          "args": ["/đường/dẫn/tới/mcp_server.py"],
          "env": { "VERIFIER_URL": "http://localhost:8000" }
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

VERIFIER_URL = os.getenv("VERIFIER_URL", "http://localhost:8000")

# ---------------------------------------------------------------------------
# Khởi tạo MCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(name="runtime-verifier")

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


VERDICT_ICON = {"PASS": "✅", "RETRY": "🔁", "ESCALATE": "🚨"}


def _icon(verdict: str) -> str:
    return VERDICT_ICON.get(str(verdict).upper(), "—")


def _format_verdict(result: dict) -> str:
    """Định dạng kết quả `POST /runtime` thành text dễ đọc cho agent."""
    if "error" in result:
        return f"❌ Lỗi: {result['error']}"

    body = result.get("body") or {}
    code = result.get("status_code", 0)

    if code != 200:
        msg = body.get("message", body.get("error", "Không rõ lỗi"))
        return f"⚠️ Verifier trả HTTP {code}: {msg}"

    verdict = body.get("verdict", "UNKNOWN")
    lines = [
        f"{_icon(verdict)} Verdict: **{verdict}**",
        f"Flow: {body.get('flow_id', '?')} | Trace: {body.get('trace_id', '?')} | "
        f"Spec version: {body.get('spec_version', '?')}",
    ]

    evidence = body.get("evidence") or {}
    summary = evidence.get("summary") or {}
    if summary:
        lines.append(
            f"Đối chiếu: {summary.get('matched', 0)}/{summary.get('total_steps', 0)} bước MATCHED, "
            f"{summary.get('partial', 0)} PARTIAL, {summary.get('missing', 0)} MISSING, "
            f"{summary.get('error_calls', 0)} lời gọi lỗi "
            f"(verdict tất định: {summary.get('verdict', '?')})"
        )
        reasons = summary.get("verdict_reasons") or []
        if reasons:
            lines.append("Lý do / vi phạm:")
            lines.extend(f"  - {r}" for r in reasons)

    nfr_lines = [
        f"  [{n.get('status', '?')}] {n.get('code', '?')}: {n.get('measured')} "
        f"(ngưỡng {n.get('operator', '')} {n.get('threshold', '')} {n.get('unit', '')})".rstrip()
        for n in evidence.get("nfrs") or [] if isinstance(n, dict)
    ]
    if nfr_lines:
        lines.append("NFR:")
        lines.extend(nfr_lines)

    conclusion = body.get("conclusion", "")
    if conclusion:
        short = conclusion[:1500] + ("…" if len(conclusion) > 1500 else "")
        lines.append(f"\nDiễn giải của LLM:\n{short}")

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
    evidence matching) rồi diễn giải bằng LLM, trả verdict PASS / RETRY / ESCALATE.

    Dùng để kiểm tra hệ thống đang chạy có đúng tài liệu thiết kế hay không.

    Args:
        flow_id: Mã luồng nghiệp vụ cần kiểm tra (ví dụ "F1"). Nếu bỏ trống, verifier tự detect
                 từ trace.
        trace_id: ID của trace cụ thể cần kiểm tra. Nếu bỏ trống, verifier lấy trace mới nhất
                  của flow.
        service: Tên service để lọc trace (ví dụ "ewallet-gateway"). Tuỳ chọn.
        operation: Tên operation để lọc trace. Tuỳ chọn.

    Returns:
        Kết quả xác minh gồm verdict (PASS/RETRY/ESCALATE), danh sách vi phạm, đo lường NFR và
        diễn giải.
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
        f"{_icon(verdict)} Flow {flow_id}: **{verdict}**\n"
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
        verdict = f.get("verdict") or "NONE"
        lines.append(f"  {_icon(verdict)} {fid} ({title}): {verdict}")

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
