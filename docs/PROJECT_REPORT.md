# Code Review Agent — 项目需求报告

> 版本: v0.3.0 | 作者: wang654987 | 日期: 2026-05

---

## 1. 项目概述

智能代码审查 Agent 系统，一个多阶段 AI 驱动的自动化代码审查工具。当开发者在 GitHub 上提交 Pull Request 时，系统自动触发多阶段分析流水线，将审查意见以结构化评论形式发布到 PR 页面。

**核心目标**：解决传统 AI Code Review 工具的三个痛点——缺乏上下文（只看 diff）、幻觉率高（单一模型误判多）、规则僵化（无法自定义团队规则）。

---

## 2. 系统架构

```
GitHub PR Event (Webhook)
        │
        ▼
┌──────────────────────────────────────────────────┐
│              Pipeline Orchestrator               │
│                                                  │
│  Stage 1   → Semgrep 静态安全扫描                  │
│  Stage 1.5 → 团队规则 DSL 引擎 (YAML 可编程规则)    │
│  Stage 2   → AST 上下文构建器 (调用链追踪)          │
│  Stage 3   → 双模型交叉验证 (DeepSeek + MiMo)       │
│  Stage 4   → 去重聚合 + 置信度评分 + 反馈统计       │
│                                                  │
│  输出 → GitHub PR Review Comments (结构化)         │
└──────────────────────────────────────────────────┘
```

---

## 3. 功能清单

### Phase 1 — 基础管线（已完成）
- [x] GitHub Webhook 接收（HMAC 签名验证）
- [x] Git unified diff 解析器（支持新增/删除/重命名文件）
- [x] LLM 语义审查（litellm 统一接口，支持任意模型）
- [x] 结构化评论自动发布到 GitHub PR
- [x] 多模型/多 provider 兼容（OpenAI / DeepSeek / Anthropic 等）
- [x] 自定义 API endpoint 支持（适配任意 OpenAI 兼容接口）

### Phase 2 — 核心创新（已完成）
- [x] 双模型交叉验证：两模型独立审查 → 交集为高置信度
- [x] 语义模糊匹配算法：Jaccard 标题相似度 + 行号邻近度
- [x] 置信度三级标注：HIGH / MEDIUM / LOW
- [x] AST 上下文构建器（tree-sitter + regex 双引擎）
- [x] 调用链追踪：自动分析变更函数的影响范围
- [x] Semgrep 可编程规则集成
- [x] 管线阶段可插拔（ENABLE_* 环境变量控制）

### Phase 3 — 工程化（已完成）
- [x] 团队规则 DSL 引擎：YAML 定义审查规则
  - forbid_pattern（禁止模式匹配）
  - require_pattern（必须模式匹配）
  - forbid_import（禁止引入依赖）
  - max_function_lines（函数长度限制）
  - naming_convention（命名规范检查）
  - max_new_deps（新增依赖数量限制）
- [x] 反馈闭环系统：SQLite 记录人工 reviewer 的采纳/拒绝
- [x] 历史反馈统计（按分类展示准确率）
- [x] 7 条预设规则模板（.code-review-rules.yaml）

---

## 4. 技术栈

| 层级 | 技术 |
|------|------|
| 语言 | Python 3.12+ |
| Web 框架 | FastAPI + Uvicorn |
| LLM 接口 | litellm（统一 100+ 模型接口） |
| AST 解析 | tree-sitter + regex 回退 |
| 静态分析 | Semgrep |
| 数据存储 | SQLite（反馈数据） |
| HTTP 客户端 | httpx（GitHub API） |
| 配置管理 | pydantic-settings + .env |
| 部署 | Docker / docker-compose |
| 依赖管理 | uv + pyproject.toml |

---

## 5. 核心创新点

### 5.1 多阶段审查流水线
不是简单地将 diff 丢给 LLM，而是经过四个阶段层层递进：静态分析 → 团队规则 → 上下文构建 → 语义审查。每个阶段可独立开关。

### 5.2 双模型交叉验证
两个不同模型（主模型 + 副模型）独立审查同一份代码，结果经语义模糊匹配后取交集。只有两个模型一致提出的问题才标记为 HIGH 置信度，大幅降低 LLM 幻觉导致的误报。

### 5.3 AST 上下文构建器
基于 tree-sitter 解析代码 AST，追踪变更函数的调用链，自动构建"变更影响面图"。LLM 看到的不只是 diff 片段，而是函数被谁调用、会影响哪些模块的完整上下文。

### 5.4 团队规则 DSL
团队在仓库根目录放置 `.code-review-rules.yaml`，定义自定义审查规则。支持 6 种规则类型，覆盖安全、质量、命名、依赖管理等场景。

### 5.5 反馈闭环
记录人工 reviewer 对 AI 审查意见的采纳/拒绝行为，审查报告中展示历史准确率统计，未来可实现基于反馈的自动规则权重调整。

---

## 6. 项目文件结构

```
code-review-agent/
├── app/
│   ├── __init__.py          # 版本号
│   ├── config.py            # 环境变量配置
│   ├── models.py            # Pydantic 数据模型
│   ├── diff_parser.py       # Git unified diff 解析
│   ├── context_builder.py   # AST 上下文构建
│   ├── semgrep_runner.py    # Semgrep 静态分析
│   ├── rules_engine.py      # 团队规则 DSL 引擎
│   ├── reviewer.py          # 管线编排 + 双模型交叉验证
│   ├── github_client.py     # GitHub API 客户端
│   ├── feedback.py          # 反馈闭环系统
│   └── main.py              # FastAPI 应用 + Webhook 端点
├── .code-review-rules.yaml  # 规则模板（供团队复制使用）
├── .env.example             # 环境变量模板
├── pyproject.toml           # 项目元数据 + 依赖
├── Dockerfile
├── docker-compose.yml
└── README.md
```

---

## 7. 快速开始

```bash
# 安装依赖
python -m venv .venv && source .venv/bin/activate
pip install -e .

# 配置
cp .env.example .env  # 编辑填入 LLM_API_KEY 和 GITHUB_TOKEN

# 启动
uvicorn app.main:app --host 0.0.0.0 --port 8000

# 配合 ngrok 暴露公网地址，在 GitHub 仓库配置 Webhook
# Payload URL: https://xxx.ngrok.io/webhook
# Secret: 与 .env 中一致
```

---

## 8. 简历素材速查

- **技术关键词**: Python/FastAPI, litellm, tree-sitter, Semgrep, SQLite, Docker, GitHub Webhook
- **核心指标**: 双模型交叉验证降低误报率, 4 阶段审查流水线, 6 种团队规则类型, 支持 100+ LLM 模型
- **创新标签**: 多阶段流水线、双模型交叉验证、AST 调用链上下文、可编程规则 DSL、反馈闭环学习

---

> 此报告提供完整的项目上下文。将其提供给 AI 助手即可生成简历项目描述。
