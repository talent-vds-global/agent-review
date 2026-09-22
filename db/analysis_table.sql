-- Bảng kết quả phân tích của agent. Chạy lại được nhiều lần (idempotent).
CREATE TABLE IF NOT EXISTS analysis_results (
    id SERIAL PRIMARY KEY,
    flow_id TEXT NOT NULL,           -- vd: F1
    analysis_type TEXT NOT NULL,     -- 'pre_merge' hoặc 'post_deploy'
    verdict TEXT NOT NULL,           -- PASS / WARN
    detail TEXT,                     -- kết luận đầy đủ của agent
    runtime_flow TEXT,               -- chuỗi bước runtime (post-deploy)
    trace_id TEXT,                   -- trace đã dùng để phân tích (mở lại được ở tab Sơ đồ trace)
    evidence JSONB,                  -- bảng đối chiếu tài liệu <-> runtime (analysis/evidence.py)
    created_at TIMESTAMP DEFAULT NOW()
);

-- Nâng cấp bảng cũ (đã tạo trước khi có evidence mapping)
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS trace_id TEXT;
ALTER TABLE analysis_results ADD COLUMN IF NOT EXISTS evidence JSONB;

CREATE INDEX IF NOT EXISTS idx_analysis_flow_time
    ON analysis_results (flow_id, created_at DESC);
