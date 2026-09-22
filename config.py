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
