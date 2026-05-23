#!/usr/bin/env python3
"""
小米 MiMo LLM 代理 - 公共模块
包含同步版 (xm_proxy.py) 和异步版 (xm_proxy3.py) 共享的：
- 配置常量
- CacheManager（带 LRU 淘汰的持久化缓存）
- MetricsCollector（监控指标收集）
- ConfigManager（热更新配置管理）
- 日志初始化（RotatingFileHandler 轮转）
- 工具函数（消息哈希、reasoning 补全、请求验证等）
"""

import json
import os
import shelve
import hashlib
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from collections import OrderedDict

# ==================== 配置常量 ====================
LISTEN_PORT = int(os.environ.get("MIMO_PROXY_PORT", 8765))
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
CACHE_FILE = os.environ.get("MIMO_CACHE_FILE", "./mimo_cache")
DEBUG = os.environ.get("MIMO_PROXY_DEBUG", "0") == "1"

MAX_CONNECTIONS = int(os.environ.get("MIMO_MAX_CONNECTIONS", "10"))
CONNECTION_TIMEOUT = int(os.environ.get("MIMO_CONNECTION_TIMEOUT", "30"))
STREAM_TIMEOUT = int(os.environ.get("MIMO_STREAM_TIMEOUT", "600"))

FALLBACK_STRATEGY = os.environ.get("MIMO_FALLBACK_STRATEGY", "strip")

MODEL_MAPPING = {
    "mimo-v2-flash": os.environ.get("MIMO_TARGET_MODEL", "mimo-v2.5-pro"),
}
FORCE_MODEL_OVERRIDE = os.environ.get("MIMO_FORCE_MODEL", "")

MAX_CONCURRENT_REQUESTS = int(os.environ.get("MIMO_MAX_CONCURRENT", "20"))

# ==================== 配置验证 ====================
VALID_STRATEGIES = {"error", "strip", "disable_thinking"}
VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}

def validate_config():
    if FALLBACK_STRATEGY not in VALID_STRATEGIES:
        raise ValueError(f"Invalid MIMO_FALLBACK_STRATEGY: {FALLBACK_STRATEGY}, must be one of {VALID_STRATEGIES}")
    if LISTEN_PORT < 1 or LISTEN_PORT > 65535:
        raise ValueError(f"Invalid MIMO_PROXY_PORT: {LISTEN_PORT}")
    if MAX_CONNECTIONS < 1:
        raise ValueError(f"Invalid MIMO_MAX_CONNECTIONS: {MAX_CONNECTIONS}")
    if CONNECTION_TIMEOUT < 1:
        raise ValueError(f"Invalid MIMO_CONNECTION_TIMEOUT: {CONNECTION_TIMEOUT}")
    if STREAM_TIMEOUT < 1:
        raise ValueError(f"Invalid MIMO_STREAM_TIMEOUT: {STREAM_TIMEOUT}")

validate_config()

# ==================== CacheManager（LRU 淘汰） ====================
class CacheManager:
    """持久化缓存管理器，内存层使用 OrderedDict 实现 LRU 淘汰"""
    def __init__(self, cache_file, max_size=10000, ttl=3600):
        self.cache_file = cache_file
        self.max_size = max_size
        self.ttl = ttl
        self.lock = threading.Lock()
        self.memory_cache = OrderedDict()
        self.last_sync = 0
        self.sync_interval = 60

    def get(self, key):
        with self.lock:
            if key in self.memory_cache:
                entry = self.memory_cache[key]
                if time.time() - entry['timestamp'] <= self.ttl:
                    self.memory_cache.move_to_end(key)
                    return entry['value']
                else:
                    del self.memory_cache[key]
            db = shelve.open(self.cache_file, writeback=False)
            try:
                data = db.get(key)
                if data:
                    if isinstance(data, dict):
                        if time.time() - data.get('timestamp', 0) > self.ttl:
                            del db[key]
                            return None
                        self.memory_cache[key] = data
                        self.memory_cache.move_to_end(key)
                        return data.get('value')
                    else:
                        del db[key]
                        return None
                return None
            finally:
                db.close()

    def set(self, key, value):
        with self.lock:
            self.memory_cache[key] = {
                'value': value,
                'timestamp': time.time()
            }
            self.memory_cache.move_to_end(key)
            while len(self.memory_cache) > self.max_size:
                self.memory_cache.popitem(last=False)
            if time.time() - self.last_sync > self.sync_interval:
                self._sync_to_disk()
                self.last_sync = time.time()

    def _sync_to_disk(self):
        db = shelve.open(self.cache_file, writeback=False)
        try:
            self._cleanup_expired(db)
            current_time = time.time()
            for key, entry in self.memory_cache.items():
                if current_time - entry['timestamp'] <= self.ttl:
                    db[key] = entry
        finally:
            db.close()

    def _cleanup_expired(self, db):
        current_time = time.time()
        expired_keys = [k for k in list(db.keys())
                        if isinstance(db[k], dict) and 
                           current_time - db[k].get('timestamp', 0) > self.ttl]
        for key in expired_keys:
            del db[key]
        expired_memory = [k for k, v in list(self.memory_cache.items())
                          if current_time - v['timestamp'] > self.ttl]
        for key in expired_memory:
            del self.memory_cache[key]

    @property
    def size(self):
        with self.lock:
            return len(self.memory_cache)


# ==================== MetricsCollector ====================
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
        with self.lock:
            self.metrics['total_requests'] += 1
            if success:
                self.metrics['successful_requests'] += 1
            else:
                self.metrics['failed_requests'] += 1
            if response_time > 0:
                self.metrics['total_response_time'] += response_time
            if endpoint not in self.metrics['request_count_by_endpoint']:
                self.metrics['request_count_by_endpoint'][endpoint] = 0
            self.metrics['request_count_by_endpoint'][endpoint] += 1

    def record_cache_hit(self):
        with self.lock:
            self.metrics['cache_hits'] += 1

    def record_cache_miss(self):
        with self.lock:
            self.metrics['cache_misses'] += 1

    def record_error(self, error_type):
        with self.lock:
            if error_type not in self.metrics['error_count_by_type']:
                self.metrics['error_count_by_type'][error_type] = 0
            self.metrics['error_count_by_type'][error_type] += 1

    def get_metrics(self):
        with self.lock:
            metrics = self.metrics.copy()
            total_cache = metrics['cache_hits'] + metrics['cache_misses']
            metrics['cache_hit_rate'] = metrics['cache_hits'] / total_cache if total_cache > 0 else 0
            if metrics['successful_requests'] > 0:
                metrics['avg_response_time'] = metrics['total_response_time'] / metrics['successful_requests']
            else:
                metrics['avg_response_time'] = 0
            return metrics

    def reset(self):
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


# ==================== ConfigManager ====================
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
            'CONNECTION_TIMEOUT': CONNECTION_TIMEOUT,
            'STREAM_TIMEOUT': STREAM_TIMEOUT
        }

    def update_config(self, key, value):
        with self.lock:
            if key in self.config:
                self.config[key] = value
                return True
            return False

    def get_config(self, key=None):
        with self.lock:
            if key:
                return self.config.get(key)
            return self.config.copy()

    def reload_from_env(self):
        with self.lock:
            self.config['LISTEN_PORT'] = int(os.environ.get("MIMO_PROXY_PORT", 8765))
            self.config['MIMO_BASE_URL'] = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")
            self.config['MIMO_API_KEY'] = os.environ.get("MIMO_API_KEY", "")
            self.config['CACHE_FILE'] = os.environ.get("MIMO_CACHE_FILE", "./mimo_cache")
            self.config['DEBUG'] = os.environ.get("MIMO_PROXY_DEBUG", "0") == "1"
            self.config['FALLBACK_STRATEGY'] = os.environ.get("MIMO_FALLBACK_STRATEGY", "strip")
            self.config['MAX_CONNECTIONS'] = int(os.environ.get("MIMO_MAX_CONNECTIONS", "10"))
            self.config['CONNECTION_TIMEOUT'] = int(os.environ.get("MIMO_CONNECTION_TIMEOUT", "30"))
            self.config['STREAM_TIMEOUT'] = int(os.environ.get("MIMO_STREAM_TIMEOUT", "600"))


# ==================== 日志初始化 ====================
def setup_logger(name, log_file, max_bytes=10*1024*1024, backup_count=5):
    """创建带轮转的 logger"""
    lg = logging.getLogger(name)
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        fh = RotatingFileHandler(log_file, maxBytes=max_bytes,
                                 backupCount=backup_count, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        lg.addHandler(fh)
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
        lg.addHandler(sh)
    return lg


# ==================== 模块级单例 ====================
cache_manager = CacheManager(CACHE_FILE)
metrics_collector = MetricsCollector()
config_manager = ConfigManager()
logger = setup_logger("MiMoProxy", "mimo_proxy.log")


# ==================== 工具函数 ====================
def debug_print(*args, **kwargs):
    message = " ".join(map(str, args))
    if config_manager.get_config("DEBUG"):
        logger.info(message)
    else:
        if "缓存缺失" in message or "错误" in message or "异常" in message:
            logger.warning(message)

def get_stable_message(msg):
    stable = {k: v for k, v in msg.items() if k != "reasoning_content"}
    content = stable.get("content", "")
    if isinstance(content, list):
        stable["content"] = json.dumps(content, sort_keys=True, ensure_ascii=False)
    return stable

def generate_message_hash(messages_prefix, current_msg):
    stable_prefix = [get_stable_message(m) for m in messages_prefix]
    stable_current = get_stable_message(current_msg)
    data = {"prefix": stable_prefix, "current": stable_current}
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]

def get_tool_call_key(tool_calls):
    normalized = []
    for tc in tool_calls:
        entry = {
            "id": tc.get("id", ""),
            "type": tc.get("type", "function"),
            "function": {"name": tc.get("function", {}).get("name", "")}
        }
        normalized.append(entry)
    raw = json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()[:32]

def get_cached_reasoning(cache_key):
    return cache_manager.get(cache_key)

def patch_messages(messages):
    patched = []
    missing = False
    has_tool_calls_in_history = any(
        msg.get("role") == "assistant" and "tool_calls" in msg
        for msg in messages
    )
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "assistant":
            if has_tool_calls_in_history and not msg.get("reasoning_content"):
                cache_key = generate_message_hash(messages[:i], msg)
                cached = cache_manager.get(cache_key)
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
                        msg = dict(msg)
                        msg["reasoning_content"] = "【Proxy】Thinking content restored to maintain tool_call structure..."
                        debug_print(f"缓存缺失，应用智能占位符策略，保留 tool_calls 结构")
                    elif config_manager.get_config("FALLBACK_STRATEGY") == "disable_thinking":
                        pass
            if msg.get("content") is None:
                msg = dict(msg)
                msg["content"] = ""
        patched.append(msg)
    return patched, missing

def store_reasoning(messages_prefix, current_msg, reasoning):
    key = generate_message_hash(messages_prefix, current_msg)
    cache_manager.set(key, reasoning)
    debug_print(f"缓存 reasoning (key={key[:8]}..., len={len(reasoning)})")

def validate_request(req_body):
    required_fields = ['messages', 'model']
    for field in required_fields:
        if field not in req_body:
            raise ValueError(f"Missing required field: {field}")
    if not isinstance(req_body['messages'], list):
        raise ValueError("Messages must be a list")
    valid_roles = ['system', 'user', 'assistant', 'tool']
    for msg in req_body['messages']:
        if msg.get('role') not in valid_roles:
            raise ValueError(f"Invalid message role: {msg.get('role')}")
