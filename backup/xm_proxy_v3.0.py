#!/usr/bin/env python3
"""
小米 MiMo LLM 本地中转代理 v3.0
- 自动缓存并补全 reasoning_content
- 持久化缓存（dbm 文件）
- 按会话 ID + 消息序号精准匹配
- 支持流式 (SSE) 响应，正确结束
- 新增：缓存未命中时的回退策略（strip / error / disable_thinking）
- 适用工具：Trae, Cursor, Roo Code 等
"""

import json
import os
import shelve
import hashlib
import threading
import time
import http.client
import socket
import logging
from functools import wraps
from contextlib import closing
from urllib.parse import urlparse
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError

# ==================== 配置 ====================
LISTEN_PORT = int(os.environ.get("MIMO_PROXY_PORT", 8765))
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
CACHE_FILE = os.environ.get("MIMO_CACHE_FILE", "./mimo_cache")
DEBUG = os.environ.get("MIMO_PROXY_DEBUG", "0") == "1"

# 连接池配置
MAX_CONNECTIONS = int(os.environ.get("MIMO_MAX_CONNECTIONS", "10"))
CONNECTION_TIMEOUT = int(os.environ.get("MIMO_CONNECTION_TIMEOUT", "30"))

# 回退策略：当缓存中缺少 reasoning_content 时的处理方式
# "error"            -> 立即返回 400 错误，告知用户需要重置对话
# "strip"            -> 移除 assistant 消息中的 tool_calls，避免 400（可能丢失上下文）
# "disable_thinking" -> 临时关闭 thinking，避免 API 校验 reasoning
FALLBACK_STRATEGY = os.environ.get("MIMO_FALLBACK_STRATEGY", "strip")

# 请求限流配置
RATE_LIMIT_REQUESTS = int(os.environ.get("MIMO_RATE_LIMIT_REQUESTS", "100"))  # 每分钟最大请求数
RATE_LIMIT_WINDOW = 60  # 限流时间窗口（秒）

# 最大并发请求数限制
MAX_CONCURRENT_REQUESTS = int(os.environ.get("MIMO_MAX_CONCURRENT", "20"))
request_semaphore = threading.Semaphore(MAX_CONCURRENT_REQUESTS)

# ==================== 持久化缓存 ====================
class CacheManager:
    """缓存管理器，带内存缓存层，减少 I/O 开销"""
    def __init__(self, cache_file, max_size=10000, ttl=3600):
        self.cache_file = cache_file
        self.max_size = max_size
        self.ttl = ttl
        self.lock = threading.Lock()
        self.memory_cache = {}  # 内存缓存
        self.last_sync = 0  # 上次同步时间
        self.sync_interval = 60  # 同步间隔（秒）

    def get(self, key):
        with self.lock:
            # 先检查内存缓存
            if key in self.memory_cache:
                entry = self.memory_cache[key]
                if time.time() - entry['timestamp'] <= self.ttl:
                    return entry['value']
                else:
                    del self.memory_cache[key]

            # 再检查磁盘缓存
            db = shelve.open(self.cache_file, writeback=False)
            try:
                data = db.get(key)
                if data:
                    # 检查过期时间
                    if time.time() - data.get('timestamp', 0) > self.ttl:
                        del db[key]
                        return None
                    # 加载到内存缓存
                    self.memory_cache[key] = data
                    return data.get('value')
                return None
            finally:
                db.close()

    def set(self, key, value):
        with self.lock:
            # 更新内存缓存
            self.memory_cache[key] = {
                'value': value,
                'timestamp': time.time()
            }

            # 定期同步到磁盘
            if time.time() - self.last_sync > self.sync_interval:
                self._sync_to_disk()
                self.last_sync = time.time()

    def _sync_to_disk(self):
        """同步内存缓存到磁盘"""
        db = shelve.open(self.cache_file, writeback=False)
        try:
            # 清理过期条目
            self._cleanup_expired(db)
            # 检查大小限制
            if len(db) >= self.max_size:
                self._remove_oldest(db)
            # 同步内存缓存（只同步未过期的条目）
            current_time = time.time()
            for key, entry in self.memory_cache.items():
                if current_time - entry['timestamp'] <= self.ttl:
                    db[key] = entry
        finally:
            db.close()

    def _cleanup_expired(self, db):
        """清理过期的缓存条目"""
        current_time = time.time()
        expired_keys = []
        for key in list(db.keys()):
            data = db[key]
            if current_time - data.get('timestamp', 0) > self.ttl:
                expired_keys.append(key)
        for key in expired_keys:
            del db[key]

        # 同时清理内存缓存中的过期条目
        expired_memory_keys = []
        for key, entry in self.memory_cache.items():
            if current_time - entry['timestamp'] > self.ttl:
                expired_memory_keys.append(key)
        for key in expired_memory_keys:
            del self.memory_cache[key]

    def _remove_oldest(self, db):
        """移除最旧的缓存条目"""
        if not db:
            return
        oldest_key = None
        oldest_time = float('inf')
        for key, data in db.items():
            if data.get('timestamp', 0) < oldest_time:
                oldest_time = data.get('timestamp', 0)
                oldest_key = key
        if oldest_key:
            del db[oldest_key]

    @property
    def size(self):
        with self.lock:
            # 返回内存缓存和磁盘缓存的总大小
            return len(self.memory_cache)

cache_manager = CacheManager(CACHE_FILE, max_size=10000, ttl=3600)


# ==================== 请求级缓存 ====================
class RequestCache:
    """请求级缓存，仅缓存非聊天接口结果"""
    def __init__(self, max_size=100, ttl=300):
        self.cache = {}
        self.lock = threading.Lock()
        self.max_size = max_size
        self.ttl = ttl

    def get_request_key(self, req_body, path):
        """生成请求的唯一key"""
        # 强制不缓存聊天接口，确保流式输出和上下文连续性
        if "/chat/completions" in path:
            return None
        try:
            cache_data = {'body': req_body, 'path': path}
            return hashlib.sha256(json.dumps(cache_data, sort_keys=True).encode()).hexdigest()
        except Exception:
            return None

    def get(self, request_key):
        """获取缓存的响应"""
        if not request_key: return None
        with self.lock:
            entry = self.cache.get(request_key)
            if entry and (time.time() - entry['timestamp'] <= self.ttl):
                return entry['value']
            return None

    def set(self, request_key, response):
        """设置缓存的响应"""
        if not request_key: return
        with self.lock:
            if len(self.cache) >= self.max_size:
                # 简单清理：删除第一个
                try:
                    del self.cache[next(iter(self.cache))]
                except: pass
            self.cache[request_key] = {
                'value': response,
                'timestamp': time.time()
            }

    def _cleanup_expired(self):
        """清理过期的缓存条目"""
        current_time = time.time()
        expired_keys = []
        for key, entry in self.cache.items():
            if current_time - entry['timestamp'] > self.ttl:
                expired_keys.append(key)
        for key in expired_keys:
            del self.cache[key]

    def _remove_oldest(self):
        """移除最旧的缓存条目"""
        if not self.cache:
            return
        oldest_key = None
        oldest_time = float('inf')
        for key, entry in self.cache.items():
            if entry['timestamp'] < oldest_time:
                oldest_time = entry['timestamp']
                oldest_key = key
        if oldest_key:
            del self.cache[oldest_key]

    @property
    def size(self):
        """获取缓存大小"""
        with self.lock:
            return len(self.cache)


request_cache = RequestCache(max_size=1000, ttl=300)

# ==================== 连接池管理 ====================
class ConnectionPool:
    """HTTP/HTTPS 连接池管理，复用底层 TCP 连接"""
    def __init__(self, max_connections=10, timeout=30):
        self.max_connections = max_connections
        self.timeout = timeout
        self.lock = threading.Lock()
        self._pools = {}

    def _get_connection(self, url):
        parsed = urlparse(url)
        scheme = parsed.scheme
        host = parsed.hostname
        port = parsed.port or (443 if scheme == 'https' else 80)
        key = f"{scheme}://{host}:{port}"
        with self.lock:
            if key not in self._pools:
                self._pools[key] = []
            pool = self._pools[key]
            if pool:
                return pool.pop(), True
            if scheme == 'https':
                conn = http.client.HTTPSConnection(host, port, timeout=self.timeout)
            else:
                conn = http.client.HTTPConnection(host, port, timeout=self.timeout)
            return conn, False

    def _return_connection(self, url, conn):
        parsed = urlparse(url)
        key = f"{parsed.scheme}://{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
        with self.lock:
            pool = self._pools.get(key, [])
            if len(pool) < self.max_connections:
                pool.append(conn)
            else:
                conn.close()

    def request(self, method, url, body=None, headers=None):
        """通过连接池发送请求，返回 (data, status)"""
        conn, reused = self._get_connection(url)
        try:
            parsed = urlparse(url)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            status = response.status
            self._return_connection(url, conn)
            return data, status
        except Exception:
            conn.close()
            raise

    def close_all(self):
        with self.lock:
            for pool in self._pools.values():
                for conn in pool:
                    conn.close()
            self._pools.clear()

connection_pool = ConnectionPool(MAX_CONNECTIONS, CONNECTION_TIMEOUT)


# ==================== 监控和统计 ====================
class MetricsCollector:
    """指标收集器"""
    def __init__(self):
        self.lock = threading.Lock()
        self.metrics = {
            'total_requests': 0,
            'cache_hits': 0,
            'cache_misses': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'total_response_time': 0,
            'request_count_by_endpoint': {},
            'error_count_by_type': {}
        }

    def record_request(self, endpoint, success, response_time=0):
        """记录请求"""
        with self.lock:
            self.metrics['total_requests'] += 1
            if success:
                self.metrics['successful_requests'] += 1
            else:
                self.metrics['failed_requests'] += 1

            if response_time > 0:
                self.metrics['total_response_time'] += response_time

            # 按端点统计
            if endpoint not in self.metrics['request_count_by_endpoint']:
                self.metrics['request_count_by_endpoint'][endpoint] = 0
            self.metrics['request_count_by_endpoint'][endpoint] += 1

    def record_cache_hit(self):
        """记录缓存命中"""
        with self.lock:
            self.metrics['cache_hits'] += 1

    def record_cache_miss(self):
        """记录缓存未命中"""
        with self.lock:
            self.metrics['cache_misses'] += 1

    def record_error(self, error_type):
        """记录错误"""
        with self.lock:
            if error_type not in self.metrics['error_count_by_type']:
                self.metrics['error_count_by_type'][error_type] = 0
            self.metrics['error_count_by_type'][error_type] += 1

    def get_metrics(self):
        """获取当前指标"""
        with self.lock:
            metrics = self.metrics.copy()
            # 计算缓存命中率
            total_cache = metrics['cache_hits'] + metrics['cache_misses']
            if total_cache > 0:
                metrics['cache_hit_rate'] = metrics['cache_hits'] / total_cache
            else:
                metrics['cache_hit_rate'] = 0

            # 计算平均响应时间
            if metrics['successful_requests'] > 0:
                metrics['avg_response_time'] = metrics['total_response_time'] / metrics['successful_requests']
            else:
                metrics['avg_response_time'] = 0

            return metrics

    def reset(self):
        """重置指标"""
        with self.lock:
            self.metrics = {
                'total_requests': 0,
                'cache_hits': 0,
                'cache_misses': 0,
                'successful_requests': 0,
                'failed_requests': 0,
                'total_response_time': 0,
                'request_count_by_endpoint': {},
                'error_count_by_type': {}
            }

metrics_collector = MetricsCollector()

# ==================== 配置热更新 ====================
class ConfigManager:
    """配置管理器，支持热更新"""
    def __init__(self):
        self.lock = threading.Lock()
        self.config = {
            'LISTEN_PORT': LISTEN_PORT,
            'MIMO_BASE_URL': MIMO_BASE_URL,
            'MIMO_API_KEY': MIMO_API_KEY,
            'CACHE_FILE': CACHE_FILE,
            'DEBUG': DEBUG,
            'FALLBACK_STRATEGY': FALLBACK_STRATEGY,
            'MAX_CONNECTIONS': MAX_CONNECTIONS,
            'CONNECTION_TIMEOUT': CONNECTION_TIMEOUT
        }

    def update_config(self, key, value):
        """更新配置"""
        with self.lock:
            if key in self.config:
                self.config[key] = value
                debug_print(f"配置更新: {key} = {value}")
                return True
            return False

    def get_config(self, key=None):
        """获取配置"""
        with self.lock:
            if key:
                return self.config.get(key)
            return self.config.copy()

    def reload_from_env(self):
        """从环境变量重新加载配置"""
        with self.lock:
            self.config['LISTEN_PORT'] = int(os.environ.get("MIMO_PROXY_PORT", 8765))
            self.config['MIMO_BASE_URL'] = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")
            self.config['MIMO_API_KEY'] = os.environ.get("MIMO_API_KEY", "")
            self.config['CACHE_FILE'] = os.environ.get("MIMO_CACHE_FILE", "./mimo_cache")
            self.config['DEBUG'] = os.environ.get("MIMO_PROXY_DEBUG", "0") == "1"
            self.config['FALLBACK_STRATEGY'] = os.environ.get("MIMO_FALLBACK_STRATEGY", "strip")
            self.config['MAX_CONNECTIONS'] = int(os.environ.get("MIMO_MAX_CONNECTIONS", "10"))
            self.config['CONNECTION_TIMEOUT'] = int(os.environ.get("MIMO_CONNECTION_TIMEOUT", "30"))
            debug_print("配置已从环境变量重新加载")

config_manager = ConfigManager()

# ==================== 日志配置 ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("mimo_proxy.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("MiMoProxy")

# ==================== 工具函数 ====================
def debug_print(*args, **kwargs):
    message = " ".join(map(str, args))
    if config_manager.get_config("DEBUG"):
        logger.info(message)
    else:
        # 非调试模式下，仅关键信息进日志文件
        if "缓存缺失" in message or "错误" in message or "异常" in message:
            logger.warning(message)

def get_stable_message(msg):
    """返回不包含 reasoning_content 的消息副本，用于生成稳定哈希"""
    stable = {k: v for k, v in msg.items() if k != "reasoning_content"}
    # 对 content 进行标准化处理
    content = stable.get("content", "")
    if isinstance(content, list):
        stable["content"] = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return stable

def generate_message_hash(messages_prefix, current_msg):
    """
    基于消息前缀和当前消息内容生成唯一且稳定的哈希值。
    这种方式比用 (session_id + index) 更健壮，
    能处理消息增删和编辑。
    """
    stable_prefix = [get_stable_message(m) for m in messages_prefix]
    stable_current = get_stable_message(current_msg)

    # 组合前缀和当前消息
    data = {
        "prefix": stable_prefix,
        "current": stable_current
    }
    # 使用 sort_keys 确保 JSON 序列化结果稳定
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]

def get_tool_call_key(tool_calls):
    """为 tool_calls 生成特定的缓存键
    """
    # 对 tool_calls 进行标准化处理
    normalized = []
    for tc in tool_calls:
        # 仅使用 id, type 和 function 名称
        entry = {
            "id": tc.get("id", ""),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc.get("function", {}).get("name", "")
            }
        }
        normalized.append(entry)
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]

def get_cached_reasoning(cache_key):
    """从缓存中获取 reasoning_content"""
    return cache_manager.get(cache_key)

def patch_messages(messages):
    """
    补全缺失的 reasoning_content，
    若缓存未命中，根据 FALLBACK_STRATEGY 处理。
    返回 (补全后的消息列表, 是否发生缓存缺失)
    """
    patched = []
    missing = False

    # 检查历史会话中是否存在工具调用
    has_tool_calls_in_history = any(
        msg.get("role") == "assistant" and "tool_calls" in msg
        for msg in messages
    )

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "assistant":
            # 如果历史会话中存在工具调用，且当前消息缺少 reasoning_content，则尝试补全
            if has_tool_calls_in_history and not msg.get("reasoning_content"):
                # 1. 尝试使用消息上下文哈希键 (最高优先级)
                cache_key = generate_message_hash(messages[:i], msg)
                cached = cache_manager.get(cache_key)

                # 2. 如果失败且包含 tool_calls，尝试使用 tool_calls 特征哈希 (二次匹配)
                if not cached and "tool_calls" in msg:
                    tc_key = get_tool_call_key(msg["tool_calls"])
                    cached = cache_manager.get(tc_key)
                    if cached:
                        debug_print(f"通过 tool_calls 特征哈希匹配成功 (key={tc_key[:8]}...)")

                if cached:
                    msg = dict(msg)
                    msg["reasoning_content"] = cached
                    debug_print(f"补全 reasoning (key={cache_key[:8]}...)")
                    metrics_collector.record_cache_hit()
                else:
                    missing = True
                    debug_print(f"缓存缺失 key={cache_key[:8]}...，策略={config_manager.get_config('FALLBACK_STRATEGY')}")
                    metrics_collector.record_cache_miss()
                    if config_manager.get_config("FALLBACK_STRATEGY") == "strip":
                        # 优化：不再将 tool_calls 转为文本，而是通过补充占位符来保留它们，让 IDE 能处理原始结构
                        msg = dict(msg)
                        msg["reasoning_content"] = "【Proxy】Thinking content restored to maintain tool_call structure..."
                        debug_print(f"缓存缺失，应用智能占位符策略，保留 tool_calls 结构")
                    elif config_manager.get_config("FALLBACK_STRATEGY") == "disable_thinking":
                        # 标记需要禁用 thinking，稍后统一处理
                        pass
                    # error 策略：保持原样，让 MiMo 返回 400

            # 确保 content 不为 None
            if msg.get("content") is None:
                msg = dict(msg)
                msg["content"] = ""

        patched.append(msg)
    return patched, missing

def store_reasoning(messages_prefix, current_msg, reasoning):
    """使用稳定哈希键存储 reasoning_content"""
    key = generate_message_hash(messages_prefix, current_msg)
    cache_manager.set(key, reasoning)
    debug_print(f"缓存 reasoning (key={key[:8]}..., len={len(reasoning)})")

def validate_request(req_body):
    """验证请求的合法性"""
    # 检查必需字段
    required_fields = ['messages', 'model']
    for field in required_fields:
        if field not in req_body:
            raise ValueError(f"Missing required field: {field}")

    # 检查消息格式
    if not isinstance(req_body['messages'], list):
        raise ValueError("Messages must be a list")

    # 检查消息角色
    valid_roles = ['system', 'user', 'assistant', 'tool']
    for msg in req_body['messages']:
        if msg.get('role') not in valid_roles:
            raise ValueError(f"Invalid message role: {msg.get('role')}")

def retry_on_failure(max_retries=3, backoff_factor=2):
    """重试装饰器，仅对连接类异常进行重试"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except (http.client.HTTPException, OSError, TimeoutError) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = backoff_factor ** attempt
                        time.sleep(wait_time)
            raise last_exception
        return wrapper
    return decorator

@retry_on_failure(max_retries=3)
def pooled_request(url, body, headers):
    """带重试的连接池请求，返回 (data, status)"""
    return connection_pool.request("POST", url, body=body, headers=headers)

# ==================== HTTP 请求处理 ====================
class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        with request_semaphore:
            # 读取请求体
            content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)

        # 处理配置更新请求
        if self.path == "/config":
            try:
                config_update = json.loads(raw_body)
                results = {}
                for key, value in config_update.items():
                    success = config_manager.update_config(key, value)
                    results[key] = "updated" if success else "invalid_key"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(results).encode())
                return
            except json.JSONDecodeError:
                self.send_error(400, "Invalid JSON body")
                return

        # 拒绝未设置 API Key 的请求
        if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            error = {
                "error": {
                    "message": "MiMo API Key 未设置！请在环境变量中设置 MIMO_API_KEY。",
                    "type": "proxy_config_error"
                }
            }
            self.wfile.write(json.dumps(error).encode())
            return

        try:
            req_body = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        # 验证请求
        try:
            validate_request(req_body)
        except ValueError as e:
            self.send_error(400, str(e))
            return

        messages = req_body.get("messages", [])
        patched_messages, missing = patch_messages(messages)
        req_body["messages"] = patched_messages

        # 如果缓存缺失且策略为 disable_thinking，则关闭 thinking
        if missing and config_manager.get_config("FALLBACK_STRATEGY") == "disable_thinking":
            if "thinking" in req_body:
                req_body["thinking"] = {"type": "disabled"}
            debug_print("已禁用 thinking 模式以绕过 reasoning 校验")

        stream = req_body.get("stream", False)
        target_url = f"{MIMO_BASE_URL}{self.path}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {MIMO_API_KEY}",
        }
        for key in ("x-request-id", "x-session-id"):
            if key in self.headers:
                headers[key] = self.headers[key]

        # 非流式请求使用请求级缓存
        request_key = None
        if not stream:
            request_key = request_cache.get_request_key(req_body, self.path)
            debug_print(f"生成请求缓存 key: {request_key[:16]}...")
            debug_print(f"请求体: {json.dumps(req_body, sort_keys=True)[:100]}...")
            cached_response = request_cache.get(request_key)
            if cached_response:
                metrics_collector.record_cache_hit()
                debug_print(f"缓存命中: {request_key[:16]}...")
                resp_body = json.dumps(cached_response).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_body)))
                self.end_headers()
                self.wfile.write(resp_body)
                return
            else:
                metrics_collector.record_cache_miss()
                debug_print(f"缓存未命中: {request_key[:16]}...")

        if stream:
            self._handle_stream(req_body, target_url, headers)
        else:
            self._handle_non_stream(req_body, target_url, headers, request_key)

    def _handle_non_stream(self, req_body, target_url, headers, request_key=None):
        start_time = time.time()
        timeout = config_manager.get_config("CONNECTION_TIMEOUT")
        try:
            # 使用 Request 对象设置超时，并在 urlopen 中应用
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            # 显式设置超时
            with urlopen(req, timeout=timeout) as resp:
                status_code = resp.getcode()
                resp_data = json.loads(resp.read())
        except HTTPError as e:
            status_code = e.code
            try:
                resp_data = json.loads(e.read())
            except:
                resp_data = {"error": {"message": str(e)}}
        except (socket.timeout, TimeoutError) as e:
            debug_print(f"请求上游超时 ({timeout}s): {e}")
            metrics_collector.record_error("upstream_timeout")
            self.send_error(504, f"Gateway Timeout: Upstream took too long to respond (> {timeout}s)")
            return
        except Exception as e:
            debug_print(f"连接上游失败: {e}")
            metrics_collector.record_error("connection_failure")
            self.send_error(502, f"Bad Gateway: {str(e)}")
            return

        if status_code != 200:
            debug_print(f"上游返回 {status_code}: {resp_data}")
            metrics_collector.record_error(f"HTTP_{status_code}")
            # 如果是 400 错误，提供更详细的错误信息
            if status_code == 400:
                error_message = "上游 API 拒绝了请求，可能是请求格式不符合要求"
                if isinstance(resp_data, dict) and "error" in resp_data:
                    error_message = resp_data["error"].get("message", error_message)
            else:
                error_message = f"上游返回 {status_code}"
            error_body = json.dumps({"error": {"message": error_message, "type": "upstream_error"}}).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(error_body)
            return

        response_time = time.time() - start_time
        metrics_collector.record_request(self.path, True, response_time)

        # 缓存 reasoning
        for choice in resp_data.get("choices", []):
            msg = choice.get("message", {})
            reasoning = msg.get("reasoning_content")
            if reasoning:
                messages_prefix = req_body.get("messages", [])
                store_reasoning(messages_prefix, msg, reasoning)

        # 缓存整个响应
        debug_print(f"request_key: {request_key}")
        if request_key:
            debug_print(f"存储响应到缓存: {request_key[:16]}...")
            request_cache.set(request_key, resp_data)
            debug_print(f"缓存存储完成，当前大小: {request_cache.size}")
        else:
            debug_print(f"request_key 为 None，不存储缓存")

        resp_body = json.dumps(resp_data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _handle_stream(self, req_body, target_url, headers):
        start_time = time.time()
        timeout = config_manager.get_config("CONNECTION_TIMEOUT")
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            # 流式请求也设置超时，防止建立连接阶段卡死
            # 使用 closing 确保 resp 在任何情况下都能被正确关闭
            with closing(urlopen(req, timeout=timeout)) as resp:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                accumulated_reasoning = ""
                accumulated_content = ""
                tool_calls_map = {}

                done = False
                try:
                    # ========== 流式响应透传优化 ==========
                    while not done:
                        # 检查客户端是否还在连接，避免在上游读取时阻塞导致无法清理资源
                        # 虽然无法完美检测所有断开情况，但可以捕获大多数主动关闭
                        try:
                            # 尝试非阻塞检查
                            self.connection.setblocking(False)
                            data = self.connection.recv(1, socket.MSG_PEEK)
                            if data == b"": # 客户端关闭了连接
                                debug_print(f"检测到客户端已断开连接，停止流处理")
                                break
                            self.connection.setblocking(True)
                        except (socket.error, BlockingIOError):
                            self.connection.setblocking(True)

                        # 使用 readline 而不是 read(chunk_size)，防止 SSE 缓冲阻塞
                        line = resp.readline()
                        if not line:
                            break
                        self.wfile.write(line)
                        self.wfile.flush()

                        line_str = line.decode("utf-8").rstrip("\n").rstrip("\r")
                        if line_str.startswith("data:"):
                            data_str = line_str[5:].strip()
                            if data_str == "[DONE]":
                                self.wfile.write(b"\n")   # 补完空行
                                self.wfile.flush()
                                done = True
                                break
                            try:
                                data = json.loads(data_str)
                                for choice in data.get("choices", []):
                                    delta = choice.get("delta", {})

                                    # 累积 reasoning
                                    chunk_r = delta.get("reasoning_content")
                                    if chunk_r:
                                        accumulated_reasoning += chunk_r

                                    # 累积 content
                                    chunk_c = delta.get("content")
                                    if chunk_c:
                                        accumulated_content += chunk_c

                                    # 累积 tool_calls
                                    tcs = delta.get("tool_calls")
                                    if tcs:
                                        for tc in tcs:
                                            idx = tc.get("index", 0)
                                            if idx not in tool_calls_map:
                                                tool_calls_map[idx] = {"id": "", "type": "function", "function": {"name": "", "arguments": ""}}

                                            if tc.get("id"):
                                                tool_calls_map[idx]["id"] = tc["id"]
                                            if tc.get("function"):
                                                f = tc["function"]
                                                if f.get("name"):
                                                    tool_calls_map[idx]["function"]["name"] += f["name"]
                                                if f.get("arguments"):
                                                    tool_calls_map[idx]["function"]["arguments"] += f["arguments"]
                            except json.JSONDecodeError:
                                pass
                except Exception as e:
                    debug_print(f"流传输异常: {e}")
                finally:
                    if not done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except:
                            pass

                    if accumulated_reasoning:
                        # 构造完整的 assistant 消息用于生成哈希
                        assistant_msg = {
                            "role": "assistant",
                            "content": accumulated_content
                        }
                        if tool_calls_map:
                            # 转换 map 为列表并按 index 排序
                            sorted_indices = sorted(tool_calls_map.keys())
                            assistant_msg["tool_calls"] = [tool_calls_map[i] for i in sorted_indices]

                        messages_prefix = req_body.get("messages", [])
                        store_reasoning(messages_prefix, assistant_msg, accumulated_reasoning)
                    metrics_collector.record_request(self.path, True, time.time() - start_time)

        except HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
            metrics_collector.record_error(f"HTTP_{e.code}_stream")
            return
        except (socket.timeout, TimeoutError) as e:
            debug_print(f"流式连接建立超时 ({timeout}s): {e}")
            metrics_collector.record_error("stream_timeout")
            self.send_error(504, f"Gateway Timeout: Stream connection took too long (> {timeout}s)")
            return
        except Exception as e:
            debug_print(f"流式连接失败: {e}")
            self.send_error(502, f"Bad Gateway: {str(e)}")
            metrics_collector.record_error("stream_connection_failure")
            return

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            metrics = metrics_collector.get_metrics()
            health = {
                "status": "ok",
                "cached_entries": cache_manager.size,
                "request_cached_entries": request_cache.size,
                "upstream": MIMO_BASE_URL,
                "fallback_strategy": config_manager.get_config("FALLBACK_STRATEGY"),
                "api_key_configured": bool(MIMO_API_KEY and MIMO_API_KEY != "your-api-key-here"),
                "metrics": metrics,
                "cache_hit_rate": f"{metrics.get('cache_hit_rate', 0)*100:.2f}%",
                "avg_response_time": f"{metrics.get('avg_response_time', 0):.3f}s"
            }
            self.wfile.write(json.dumps(health, ensure_ascii=False).encode())
        elif self.path == "/metrics":
            # 提供详细的监控指标
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            metrics = metrics_collector.get_metrics()
            self.wfile.write(json.dumps(metrics).encode())
        elif self.path == "/reset_metrics":
            # 重置监控指标
            metrics_collector.reset()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/config":
            # 获取配置
            config = config_manager.get_config()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(config).encode())
        elif self.path == "/config/reload":
            # 从环境变量重新加载配置
            config_manager.reload_from_env()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "config reloaded"}).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        if DEBUG:
            super().log_message(format, *args)

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """支持并发请求的 HTTP 服务器"""
    daemon_threads = True

def main():
    # 启动时警告未设置 API Key
    if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
        print("⚠️ 环境变量 MIMO_API_KEY 未设置！代理将拒绝所有请求。")
        print("请执行 export MIMO_API_KEY=你的key 后重新启动。")
    print(f"🚀 MiMo 代理 v2.2 启动中...")
    print(f"   监听端口  : {LISTEN_PORT}")
    print(f"   上游地址  : {MIMO_BASE_URL}")
    print(f"   缓存文件  : {CACHE_FILE}")
    print(f"   回退策略  : {FALLBACK_STRATEGY}")
    print(f"   调试模式  : {'开启' if DEBUG else '关闭'}")
    print(f"   连接池    : 最大 {MAX_CONNECTIONS} 连接，超时 {CONNECTION_TIMEOUT}秒")
    print(f"   并发请求  : 已启用 (ThreadingMixIn)")
    print(f"   请将 Base URL 设置为: http://localhost:{LISTEN_PORT}/v1\n")
    server = ThreadedHTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 代理已停止")
        server.server_close()
        connection_pool.close_all()

if __name__ == "__main__":
    main()