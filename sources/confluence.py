"""Module tích hợp Confluence API để lấy tài liệu thiết kế nghiệp vụ."""

import requests
from requests.auth import HTTPBasicAuth
import config


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
