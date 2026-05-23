# Codex Agent Instructions

> Version: 1.2 | Last updated: 2026-05-18

## Project Context
小米 MiMo LLM 本地中转代理项目，项目结构：
- `xm_proxy_v3.0.py` — 核心代理脚本，包含 HTTP 代理服务器、模型映射、日志系统、缓存系统
- `mimo_common.py` — 公共模块（缓存、配置、指标）
- `start_xm_proxy_v3.0.bat` — 交互式启动脚本，支持环境变量读取、超时自动选择
- `app.log` — 日志文件（自动轮转，最大 50MB，只保留 1 个备份文件）
- `mimo_cache` — 缓存文件（基于 shelve 的持久化缓存）
- `skills/` — SKILLS 目录，存放项目规范和开发指南

## Core Work Principles
1. Think before acting. Read existing files before writing code.（行动前先思考，写代码前先阅读现有文件。）
2. Prefer editing over rewriting whole files.（优先编辑而不是重写整个文件。）
3. Avoid re-reading unchanged files; re-read after edits to verify correctness.（避免重复读取未修改的文件，修改后可重新读取验证。）
4. Test your code before declaring done.（在宣布完成前测试你的代码。）

## Output Quality Standards
1. Be concise in output but thorough in reasoning.（输出要简洁，但推理要彻底。）
2. No sycophantic openers or closing fluff.（不要有奉承的开场白或结束语。）
3. Keep solutions simple and direct.（保持解决方案简单直接。）

## Behavior Rules
1. **Language**: Respond in the language of the user's latest message.（使用用户最近消息的语言回复。）
2. **Destructive actions**: For deleting files or large-scale refactoring, ask for user confirmation first.（删除文件或大规模重构前，先询问用户确认。）
3. **Scope**: Focus on the project domain (MiMo LLM proxy); do not introduce unrelated dependencies.（专注于项目领域，不引入无关依赖。）

## Project-Specific Rules
1. **日志系统**: 使用 Python logging 模块，配置日志轮转，限制大小为 50MB，只保留 1 个备份文件
2. **模型映射**: 支持小米官网的多个模型（mimo-v2.5-pro、mimo-v2-flash、mimo-v2.5 等），实现自动切换
3. **环境变量**: 从系统环境变量读取 API Key，支持 MIMO_API_KEY、MIMO_PROXY_PORT、MIMO_BASE_URL 等
4. **缓存系统**: 使用 shelve 实现持久化缓存，按会话 ID + 消息序号精准匹配
5. **代理配置**: 支持禁用系统代理，直接连接上游服务器

## Overrides
1. User instructions always override this file.（用户指令始终覆盖此文件。）
2. When the user requests a specific skill or template, apply it instead of the defaults.（当用户请求特定技能时，优先使用该技能。）

## SKILLS
### mimo-proxy
MiMo LLM 本地中转代理项目规范。当处理缓存系统、流式响应、配置管理、监控诊断、安全规范或性能优化相关任务时调用此 SKILL。详见 `skills/mimo-proxy/SKILL.md`。

### TRAE-code-review
用于执行代码审查任务。适用于审查合并请求、代码差异，并提供关于代码质量、正确性和最佳实践的结构化反馈。

### TRAE-debugger
用于调试需要收集运行时证据的复杂问题。它会启动一个调试服务器通过 HTTP 收集日志，然后遵循科学的调试流程（假设 → 插桩 → 复现 → 分析 → 修复 → 验证）。适用于仅通过静态代码分析无法诊断的 Bug。在用户主动要求运行时调试、或经过多轮对话仍无法通过静态分析解决问题时触发。

### TRAE-generate-mini-app
当用户意图涉及小程序、Taro、微信小程序、跨端小程序等任何包含小程序的意图时，用于生成基于 Taro 框架的高质量、可运行的多端小程序代码。

### skill-creator
MANDATORY tool for creating SKILLs - MUST be invoked IMMEDIATELY when user wants to create/add any skill