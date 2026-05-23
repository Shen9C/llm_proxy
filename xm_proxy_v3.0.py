#!/usr/bin/env python3
"""
小米 MiMo LLM 本地中转代理 v2.3
- 自动缓存并补全 reasoning_content
- 持久化缓存（dbm 文件，LRU 淘汰）
- 支持流式 (SSE) 响应
- 缓存未命中回退策略（strip / error / disable_thinking）
- 模型名称自动映射
- 适用工具：Trae, Cursor, Roo Code 等
"""

import json
import os
import time
import http.client
import socket
import hashlib
import threading
import os
from functools import wraps
from contextlib import closing
from urllib.parse import urlparse
from socketserver import ThreadingMixIn
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen, ProxyHandler, build_opener
from urllib.error import HTTPError

os.environ["NO_PROXY"] = "*"
os.environ["http_proxy"] = ""
os.environ["https_proxy"] = ""
opener = build_opener(ProxyHandler({}))

from mimo_common import (
    LISTEN_PORT, MIMO_BASE_URL, MIMO_API_KEY, CACHE_FILE, DEBUG,
    MAX_CONNECTIONS, CONNECTION_TIMEOUT, STREAM_TIMEOUT,
    FALLBACK_STRATEGY, MODEL_MAPPING, FORCE_MODEL_OVERRIDE,
    MAX_CONCURRENT_REQUESTS,
    cache_manager, metrics_collector, config_manager, logger,
    debug_print, patch_messages, store_reasoning, validate_request,
)

request_semaphore = threading.Semaphore(MAX_CONCURRENT_REQUESTS)


# ==================== 请求级缓存（同步版） ====================
class RequestCache:
    def __init__(self, max_size=100, ttl=300):
        self.cache = {}
        self.lock = threading.Lock()
        self.max_size = max_size
        self.ttl = ttl

    def get_request_key(self, req_body, path):
        if "/chat/completions" in path:
            return None
        try:
            cache_data = {'body': req_body, 'path': path}
            return hashlib.sha256(json.dumps(cache_data, sort_keys=True).encode()).hexdigest()
        except Exception:
            return None

    def get(self, request_key):
        if not request_key: return None
        with self.lock:
            entry = self.cache.get(request_key)
            if entry and (time.time() - entry['timestamp'] <= self.ttl):
                return entry['value']
            return None

    def set(self, request_key, response):
        if not request_key: return
        with self.lock:
            if len(self.cache) >= self.max_size:
                try:
                    del self.cache[next(iter(self.cache))]
                except Exception: pass
            self.cache[request_key] = {
                'value': response,
                'timestamp': time.time()
            }

    @property
    def size(self):
        with self.lock:
            return len(self.cache)


request_cache = RequestCache(max_size=1000, ttl=300)


# ==================== 连接池管理（同步版） ====================
class ConnectionPool:
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

    @property
    def stats(self):
        with self.lock:
            pool_details = {}
            total_idle = 0
            for key, pool in self._pools.items():
                pool_details[key] = len(pool)
                total_idle += len(pool)
            return {
                "max_connections": self.max_connections,
                "timeout": self.timeout,
                "pool_count": len(self._pools),
                "idle_connections": total_idle,
                "pools": pool_details
            }


connection_pool = ConnectionPool(MAX_CONNECTIONS, CONNECTION_TIMEOUT)


# ==================== 重试装饰器 ====================
def retry_on_failure(max_retries=3, backoff_factor=2):
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
    return connection_pool.request("POST", url, body=body, headers=headers)


# ==================== HTTP 请求处理 ====================
class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            with request_semaphore:
                content_length = int(self.headers.get("Content-Length", 0))
            raw_body = self.rfile.read(content_length)
        except (ConnectionAbortedError, ConnectionResetError, OSError):
            return

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

        if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            error = {"error": {"message": "MiMo API Key 未设置！请在环境变量中设置 MIMO_API_KEY。", "type": "proxy_config_error"}}
            self.wfile.write(json.dumps(error).encode())
            return

        try:
            req_body = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        try:
            validate_request(req_body)
        except ValueError as e:
            self.send_error(400, str(e))
            return

        original_model = req_body.get("model", "")
        if FORCE_MODEL_OVERRIDE:
            req_body["model"] = FORCE_MODEL_OVERRIDE
            debug_print(f"强制覆盖模型: {original_model} -> {FORCE_MODEL_OVERRIDE}")
        elif original_model in MODEL_MAPPING:
            mapped_model = MODEL_MAPPING[original_model]
            req_body["model"] = mapped_model
            debug_print(f"映射模型名称: {original_model} -> {mapped_model}")

        messages = req_body.get("messages", [])
        patched_messages, missing = patch_messages(messages)
        req_body["messages"] = patched_messages

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

        request_key = None
        if not stream:
            request_key = request_cache.get_request_key(req_body, self.path)
            debug_print(f"生成请求缓存 key: {request_key[:16]}...")
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
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            with opener.open(req, timeout=timeout) as resp:
                status_code = resp.getcode()
                resp_data = json.loads(resp.read())
        except HTTPError as e:
            status_code = e.code
            try:
                resp_data = json.loads(e.read())
            except Exception:
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

        for choice in resp_data.get("choices", []):
            msg = choice.get("message", {})
            reasoning = msg.get("reasoning_content")
            if reasoning:
                messages_prefix = req_body.get("messages", [])
                store_reasoning(messages_prefix, msg, reasoning)

        if request_key:
            request_cache.set(request_key, resp_data)

        resp_body = json.dumps(resp_data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _handle_stream(self, req_body, target_url, headers):
        start_time = time.time()
        timeout = config_manager.get_config("STREAM_TIMEOUT")
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            with closing(opener.open(req, timeout=timeout)) as resp:
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
                    while not done:
                        try:
                            self.connection.setblocking(False)
                            data = self.connection.recv(1, socket.MSG_PEEK)
                            if data == b"":
                                debug_print(f"检测到客户端已断开连接，停止流处理")
                                break
                            self.connection.setblocking(True)
                        except (socket.error, BlockingIOError):
                            self.connection.setblocking(True)

                        line = resp.readline()
                        if not line:
                            break
                        self.wfile.write(line)
                        self.wfile.flush()

                        line_str = line.decode("utf-8").rstrip("\n").rstrip("\r")
                        if line_str.startswith("data:"):
                            data_str = line_str[5:].strip()
                            if data_str == "[DONE]":
                                self.wfile.write(b"\n")
                                self.wfile.flush()
                                done = True
                                break
                            try:
                                data = json.loads(data_str)
                                for choice in data.get("choices", []):
                                    delta = choice.get("delta", {})
                                    chunk_r = delta.get("reasoning_content")
                                    if chunk_r:
                                        accumulated_reasoning += chunk_r
                                    chunk_c = delta.get("content")
                                    if chunk_c:
                                        accumulated_content += chunk_c
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
                    metrics_collector.record_error("stream_read_error")
                finally:
                    if not done:
                        try:
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except Exception:
                            pass
                    try:
                        if accumulated_reasoning:
                            assistant_msg = {"role": "assistant", "content": accumulated_content}
                            if tool_calls_map:
                                sorted_indices = sorted(tool_calls_map.keys())
                                assistant_msg["tool_calls"] = [tool_calls_map[i] for i in sorted_indices]
                            messages_prefix = req_body.get("messages", [])
                            store_reasoning(messages_prefix, assistant_msg, accumulated_reasoning)
                    except Exception as cache_err:
                        debug_print(f"缓存存储失败（不影响响应）: {cache_err}")
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
                "connection_pool": connection_pool.stats,
                "metrics": metrics,
                "cache_hit_rate": f"{metrics.get('cache_hit_rate', 0)*100:.2f}%",
                "avg_response_time": f"{metrics.get('avg_response_time', 0):.3f}s"
            }
            self.wfile.write(json.dumps(health, ensure_ascii=False).encode())
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            metrics = metrics_collector.get_metrics()
            self.wfile.write(json.dumps(metrics).encode())
        elif self.path == "/reset_metrics":
            metrics_collector.reset()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif self.path == "/config":
            config = config_manager.get_config()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(config).encode())
        elif self.path == "/config/reload":
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
    daemon_threads = True


def main():
    if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
        print("⚠️ 环境变量 MIMO_API_KEY 未设置！代理将拒绝所有请求。")
        print("请执行 export MIMO_API_KEY=你的key 后重新启动。")
    print(f"🚀 MiMo 代理 v2.3 启动中...")
    print(f"   监听端口  : {LISTEN_PORT}")
    print(f"   上游地址  : {MIMO_BASE_URL}")
    print(f"   缓存文件  : {CACHE_FILE}")
    print(f"   回退策略  : {FALLBACK_STRATEGY}")
    print(f"   调试模式  : {'开启' if DEBUG else '关闭'}")
    print(f"   连接池    : 最大 {MAX_CONNECTIONS} 连接，超时 {CONNECTION_TIMEOUT}秒")
    print(f"   流式超时  : {STREAM_TIMEOUT}秒")
    print(f"   并发请求  : 已启用 (ThreadingMixIn)")
    print(f"   请将 Base URL 设置为: http://localhost:{LISTEN_PORT}/v1")
    print(f"   WSL 访问地址: http://0.0.0.0:{LISTEN_PORT}/v1")
    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 代理已停止")
        server.server_close()
        connection_pool.close_all()


if __name__ == "__main__":
    main()
