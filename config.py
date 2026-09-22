import os
from dotenv import load_dotenv

# Tải các biến môi trường từ file .env
load_dotenv()

# Cấu hình API Keys & Token
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY") or os.getenv("GROP", "")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Cấu hình kết nối PostgreSQL
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 5433))
DB_NAME = os.getenv("DB_NAME", "context")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "devpass")

# Cấu hình Model AI
MODEL = os.getenv("MODEL", "llama-3.3-70b-versatile")

# Cấu hình Jaeger Tracing
JAEGER_URL = os.getenv("JAEGER_URL", "http://localhost:16686")

# Cấu hình Confluence
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "")
CONFLUENCE_EMAIL = os.getenv("CONFLUENCE_EMAIL", "")
CONFLUENCE_TOKEN = os.getenv("CONFLUENCE_TOKEN", "")


def _parse_pairs(raw: str) -> dict:
    """Đọc chuỗi cấu hình dạng "khoa=gia_tri,khoa2=gia_tri2" thành dict."""
    result = {}
    for part in (raw or "").split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            if key.strip() and value.strip():
                result[key.strip()] = value.strip()
    return result


# Ánh xạ flow -> page id Confluence, ghi đè mặc định trong sources/confluence.py
# Ví dụ: CONFLUENCE_FLOW_PAGES=F1=131083,F2=131099
CONFLUENCE_FLOW_PAGES = _parse_pairs(os.getenv("CONFLUENCE_FLOW_PAGES", ""))

# Dashboard của database-quality-library (Topic #80) theo từng service.
# Mỗi service có một dashboard riêng; số liệu đọc trực tiếp tại thời điểm người dùng xem.
DB_QUALITY_URLS = _parse_pairs(os.getenv("DB_QUALITY_URLS", "")) or {
    "ewallet-payment-order": "http://localhost:19082",
    "ewallet-payment-business": "http://localhost:19083",
    "ewallet-third-party": "http://localhost:19084",
    "ewallet-notification": "http://localhost:19085",
}

# Thư mục chứa file ánh xạ bước tài liệu -> span runtime (mapping/<flow>.yaml)
MAPPING_DIR = os.getenv("MAPPING_DIR", os.path.join(os.path.dirname(__file__), "mapping"))
