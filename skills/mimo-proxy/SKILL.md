---
name: "mimo-proxy"
description: "MiMo LLM 本地中转代理项目规范。当处理缓存系统、流式响应、配置管理、监控诊断、安全规范或性能优化相关任务时调用此 SKILL。"
---

# MiMo LLM 代理 - 项目规范

## 缓存系统规范

### 数据格式
```python
{
    "value": "实际缓存值",
    "timestamp": time.time()
}
```

### 关键操作
- `set()`: 写入时自动添加时间戳
- `get()`: 读取时验证数据格式和过期时间
- `_cleanup_expired()`: 清理时检查 `isinstance(data, dict)`

### 过期策略
- TTL: 默认 3600 秒
- 最大条目: 10000
- 同步间隔: 60 秒

## 流式响应处理

### 数据格式
```
data: {"choices":[{"delta":{"content":"..."}}]}
data: [DONE]
```

### 关键流程
1. 解析 SSE 数据块
2. 累积 `reasoning_content` 和 `content`
3. 检测客户端断开连接
4. 存储 reasoning 到缓存
5. 发送 `[DONE]` 标记

### 异常处理
- 缓存存储失败: 记录日志，不影响响应
- 客户端断开: 立即停止流处理
- 上游超时: 返回 504 错误

## 配置管理

### 环境变量
| 变量 | 默认值 | 说明 |
|------|--------|------|
| MIMO_API_KEY | - | 必需，API 密钥 |
| MIMO_PROXY_PORT | 8765 | 监听端口 |
| MIMO_BASE_URL | https://api.xiaomimimo.com | 上游地址 |
| MIMO_CACHE_FILE | ./mimo_cache | 缓存文件路径 |
| MIMO_PROXY_DEBUG | 0 | 调试模式 |
| MIMO_FALLBACK_STRATEGY | strip | 缓存缺失策略 |

### 策略选项
- `strip`: 移除 tool_calls，使用占位符
- `error`: 返回 400 错误
- `disable_thinking`: 禁用 thinking 模式

## 监控与诊断

### 健康检查
- 端点: `GET /health`
- 返回: 状态、缓存大小、连接池、指标

### 指标收集
- 总请求数、成功/失败数
- 缓存命中率
- 平均响应时间
- 错误类型分布

### 日志规范
- 轮转: 50MB，保留 1 个备份
- 格式: `时间 [级别] 消息`
- 级别: DEBUG, INFO, WARNING, ERROR

## 安全规范

### API Key 保护
- 仅从环境变量读取
- 日志中脱敏显示（前8位 + ****）
- 启动时检查是否设置

### 输入验证
- 必需字段检查: `messages`, `model`
- 消息角色验证: system, user, assistant, tool
- JSON 格式验证

## 性能优化

### 连接池
- 最大连接数: 10
- 连接超时: 30 秒
- 复用策略: LIFO

### 并发控制
- 最大并发: 20
- 使用 `ThreadingMixIn` 实现并发
- 信号量控制资源访问

### 缓存优化
- 内存缓存: OrderedDict (LRU)
- 磁盘缓存: shelve
- 批量同步: 每 60 秒

## 项目结构

```
xm_proxy_v3.0.py  — 核心代理脚本
mimo_common.py    — 公共模块（缓存、配置、指标）
start_xm_proxy_v3.0.bat — 启动脚本
mimo_cache        — 缓存文件
app.log           — 日志文件
```

## 编码规范

### 命名约定
- 文件名: `snake_case.py`
- 类名: `PascalCase`
- 函数/方法: `snake_case`
- 常量: `UPPER_SNAKE_CASE`

### 类型检查
- 使用 `isinstance()` 进行类型验证
- 缓存数据必须包含 `timestamp` 字段
- 异常处理: 捕获具体异常，记录详细日志

### 线程安全
- 使用 `threading.Lock` 保护共享资源
- 使用 `Semaphore` 控制并发数
- 使用 `OrderedDict` 实现线程安全的 LRU
