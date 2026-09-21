"""Module tích hợp Confluence API để lấy tài liệu thiết kế nghiệp vụ."""

import requests
from requests.auth import HTTPBasicAuth
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
    return get_flow_design(page_id)