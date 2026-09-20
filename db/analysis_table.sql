CREATE TABLE IF NOT EXISTS analysis_results (
    id SERIAL PRIMARY KEY,
    flow_id TEXT NOT NULL,           -- vd: F1
    analysis_type TEXT NOT NULL,     -- 'pre_merge' hoặc 'post_deploy'
    verdict TEXT NOT NULL,           -- PASS / WARN
    detail TEXT,                     -- kết luận đầy đủ của agent
    runtime_flow TEXT,               -- chuỗi bước runtime (post-deploy)
    created_at TIMESTAMP DEFAULT NOW()
);