# Code Review Agent

AI 驱动的多阶段代码审查系统，通过 GitHub Webhook 自动审查 PR 并发布评论。

## 快速开始

### 1. 环境准备

```bash
# Python 3.12+
python3 --version

# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# 安装依赖
pip install -e ".[dev]"
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env，填入你的 GITHUB_TOKEN 和 LLM_API_KEY
```

### 3. 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 4. 配置 GitHub Webhook

1. 进入你的 GitHub 仓库 → Settings → Webhooks → Add webhook
2. Payload URL: `https://your-domain.com/webhook`（本地测试可用 ngrok）
3. Content type: `application/json`
4. Secret: 与 `.env` 中 `WEBHOOK_SECRET` 一致
5. Events: 勾选 "Pull requests"

## 项目结构

```
code-review-agent/
├── app/
│   ├── __init__.py        # 版本号
│   ├── config.py          # 配置管理（环境变量）
│   ├── models.py          # Pydantic 数据模型
│   ├── diff_parser.py     # Git unified diff 解析器
│   ├── reviewer.py        # LLM 审查核心逻辑
│   ├── github_client.py   # GitHub API 客户端
│   └── main.py            # FastAPI 应用 + webhook 端点
├── .env.example           # 环境变量模板
├── pyproject.toml         # 项目元数据和依赖
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## Phase 路线图

- [x] **Phase 1 (MVP)**: Webhook → Diff 解析 → 单模型审查 → 评论发布
- [ ] **Phase 2**: 上下文构建（AST + 调用链）、双模型交叉验证、Semgrep 集成
- [ ] **Phase 3**: 多语言支持、Dashboard、团队规则 DSL、反馈闭环
