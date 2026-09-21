"""Module tích hợp Confluence API để lấy tài liệu thiết kế nghiệp vụ."""

import html
import re
import requests
from requests.auth import HTTPBasicAuth
from bs4 import BeautifulSoup
import config
import html2text

FLOW_PAGE_MAP = {
    "F1": "131083"
}


def get_page_content(page_id: str) -> str:
    """Gọi Confluence Cloud REST API v2 để lấy nội dung trang định dạng storage (HTML/XML).

    Args:
        page_id (str): Mã định danh trang Confluence (ID).

    Returns:
        str: Chuỗi nội dung trang dạng storage, hoặc thông báo lỗi nếu không thành công.
    """
    if not config.CONFLUENCE_BASE_URL or not config.CONFLUENCE_EMAIL or not config.CONFLUENCE_TOKEN:
        return "Lỗi: Chưa cấu hình đầy đủ CONFLUENCE_BASE_URL, CONFLUENCE_EMAIL, hoặc CONFLUENCE_TOKEN."

    url = f"{config.CONFLUENCE_BASE_URL.rstrip('/')}/wiki/api/v2/pages/{page_id}"
    params = {"body-format": "storage"}
    headers = {"Accept": "application/json"}
    auth = HTTPBasicAuth(config.CONFLUENCE_EMAIL, config.CONFLUENCE_TOKEN)

    try:
        response = requests.get(url, params=params, auth=auth, headers=headers, timeout=15)
        if response.status_code == 404:
            return f"Không tìm thấy trang Confluence với page_id: {page_id}"
        response.raise_for_status()

        data = response.json()
        body_storage = data.get("body", {}).get("storage", {}).get("value", "")
        if not body_storage:
            return "Trang Confluence không có nội dung storage."
        return body_storage
    except requests.exceptions.RequestException as e:
        return f"Lỗi khi gọi Confluence API ({url}): {str(e)}"
    except Exception as e:
        return f"Lỗi không xác định khi xử lý nội dung Confluence: {str(e)}"


def get_flow_design(page_id: str) -> str:
    """Lấy trang Confluence và chuyển thành text sạch để làm design cho agent.

    Args:
        page_id: ID trang Confluence (vd F1 = 131083)

    Returns:
        Nội dung trang dạng text (markdown), đã bỏ thẻ HTML.
    """
    html_content = get_page_content(page_id)

    # Chuyển HTML sang markdown/text sạch
    converter = html2text.HTML2Text()
    converter.ignore_links = True       # bỏ link cho gọn
    converter.ignore_images = True      # bỏ ảnh
    converter.body_width = 0            # không tự xuống dòng
    text = converter.handle(html_content)

    return text.strip()


def get_flow_design_compact(page_id: str) -> str:
    """Lấy trang Confluence và trích xuất gọn các phần cần thiết cho agent đối chiếu.

    Trích xuất:
    1. Thông tin flow: Flow Name, Entry Endpoint, Services (từ Page Properties)
    2. Các bước nghiệp vụ: từ bảng mục 2.3 (số. mô tả (service, rule))
    3. Business rules: từ bảng mục 3 (Mã: điều kiện → hành động [severity])
    4. NFR: từ bảng mục 4 (Mã: metric operator threshold unit)

    Args:
        page_id: ID trang Confluence (vd: "131083")

    Returns:
        str: Nội dung text có cấu trúc, gọn gàng hoặc thông báo lỗi nếu có.
    """
    html_content = get_page_content(page_id)
    if not html_content or html_content.startswith("Lỗi") or html_content.startswith("Không tìm thấy"):
        return html_content

    def clean_cell(text: str) -> str:
        if not text:
            return ""
        text = html.unescape(text)
        text = " ".join(text.split())
        return re.sub(r"\s+([,.;:])", r"\1", text)

    soup = BeautifulSoup(html_content, "html.parser")
    sections = []

    # 1. Thông tin flow (từ Page Properties)
    pp_heading = None
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        if "page properties" in h.get_text().lower():
            pp_heading = h
            break

    pp_table = pp_heading.find_next("table") if pp_heading else None
    if not pp_table:
        for tbl in soup.find_all("table"):
            t_text = tbl.get_text()
            if "Flow Name" in t_text or "Flow ID" in t_text:
                pp_table = tbl
                break

    flow_info = {}
    if pp_table:
        for tr in pp_table.find_all("tr"):
            cells = [clean_cell(c.get_text(separator=" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if len(cells) >= 2:
                key = cells[0].strip().lower()
                val = cells[1].strip()
                if "flow name" in key:
                    flow_info["Flow Name"] = val
                elif "entry endpoint" in key:
                    flow_info["Entry Endpoint"] = val
                elif "services" in key:
                    flow_info["Services"] = val

    if flow_info:
        flow_lines = ["## Flow"]
        for k in ["Flow Name", "Entry Endpoint", "Services"]:
            if k in flow_info:
                flow_lines.append(f"- {k}: {flow_info[k]}")
        sections.append("\n".join(flow_lines))

    # 2. Các bước nghiệp vụ (mục 2.3)
    step_heading = None
    for h in soup.find_all(["h1", "h2", "h3", "h4"]):
        t = h.get_text()
        if "2.3" in t or "mô tả chi tiết nghiệp vụ" in t.lower():
            step_heading = h
            break

    step_table = step_heading.find_next("table") if step_heading else None
    if not step_table:
        for tbl in soup.find_all("table"):
            first_row = [clean_cell(c.get_text(separator=" ", strip=True)).lower() for c in tbl.find_all(["th", "td"])[:4]]
            if any("bước" in c for c in first_row) and any("mô tả" in c for c in first_row):
                step_table = tbl
                break

    if step_table:
        step_lines = ["## Các bước"]
        for tr in step_table.find_all("tr"):
            cells = [clean_cell(c.get_text(separator=" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if "bước" in cells[0].lower() and len(cells) > 1 and "mô tả" in cells[1].lower():
                continue
            step_no = cells[0] if len(cells) > 0 else ""
            desc = cells[1] if len(cells) > 1 else ""
            service = cells[2] if len(cells) > 2 else ""
            rule = cells[3] if len(cells) > 3 else ""

            meta_parts = [p for p in [service, rule] if p]
            meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""

            step_clean = step_no.rstrip(".")
            line = f"{step_clean}. {desc}{meta_str}"
            step_lines.append(line)
        if len(step_lines) > 1:
            sections.append("\n".join(step_lines))

    # 3. Business rules (mục 3)
    rule_heading = None
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = h.get_text().lower()
        if "3. business rule" in t or "business rule" in t or "quy tắc nghiệp vụ" in t:
            rule_heading = h
            break

    rule_tables = []
    if rule_heading:
        curr = rule_heading
        while curr:
            curr = curr.find_next()
            if not curr:
                break
            if curr.name == "h2" and curr != rule_heading:
                break
            if curr.name == "table":
                rule_tables.append(curr)

    if not rule_tables:
        for tbl in soup.find_all("table"):
            first_row = [clean_cell(c.get_text(separator=" ", strip=True)).lower() for c in tbl.find_all(["th", "td"])[:4]]
            if any("mã" in c for c in first_row) and any("điều kiện" in c for c in first_row):
                rule_tables.append(tbl)

    rule_lines = ["## Rules"]
    for tbl in rule_tables:
        for tr in tbl.find_all("tr"):
            cells = [clean_cell(c.get_text(separator=" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells or ("mã" in cells[0].lower() and "điều kiện" in "".join(cells).lower()):
                continue
            code = cells[0] if len(cells) > 0 else ""
            cond = cells[1] if len(cells) > 1 else ""
            action = cells[2] if len(cells) > 2 else ""
            severity = cells[3] if len(cells) > 3 else ""

            if not code or not (cond or action):
                continue

            r_line = f"{code}: {cond} → {action}"
            if severity:
                r_line += f" [{severity}]"
            rule_lines.append(r_line)

    if len(rule_lines) > 1:
        sections.append("\n".join(rule_lines))

    # 4. NFR (mục 4)
    nfr_heading = None
    for h in soup.find_all(["h1", "h2", "h3"]):
        t = h.get_text().lower()
        if "4. nfr" in t or "nfr" in t or "phi chức năng" in t:
            nfr_heading = h
            break

    nfr_table = nfr_heading.find_next("table") if nfr_heading else None
    if not nfr_table:
        for tbl in soup.find_all("table"):
            first_row = [clean_cell(c.get_text(separator=" ", strip=True)).lower() for c in tbl.find_all(["th", "td"])[:5]]
            if any("metric" in c for c in first_row) and any("operator" in c or "threshold" in c for c in first_row):
                nfr_table = tbl
                break

    if nfr_table:
        nfr_lines = ["## NFR"]
        for tr in nfr_table.find_all("tr"):
            cells = [clean_cell(c.get_text(separator=" ", strip=True)) for c in tr.find_all(["th", "td"])]
            if not cells or ("mã" in cells[0].lower() and "metric" in "".join(cells).lower()):
                continue
            code = cells[0] if len(cells) > 0 else ""
            metric = cells[1] if len(cells) > 1 else ""
            op = cells[2] if len(cells) > 2 else ""
            threshold = cells[3] if len(cells) > 3 else ""
            unit = cells[4] if len(cells) > 4 else ""

            if not code or not metric:
                continue

            nfr_line = " ".join(f"{code}: {metric} {op} {threshold} {unit}".split())
            nfr_lines.append(nfr_line)

        if len(nfr_lines) > 1:
            sections.append("\n".join(nfr_lines))

    result = "\n\n".join(sections).strip()
    return result if result else get_flow_design(page_id)


def get_design_by_flow(flow_id: str) -> str:
    """Tra cứu page_id theo flow_id và lấy thiết kế từ Confluence.

    Args:
        flow_id (str): Mã định danh luồng (vd: "F1").

    Returns:
        str: Nội dung thiết kế dạng text sạch, hoặc thông báo nếu chưa có tài liệu.
    """
    page_id = FLOW_PAGE_MAP.get(flow_id)
    if not page_id:
        return f"Chưa có tài liệu Confluence cho flow {flow_id}."
    return get_flow_design_compact(page_id)