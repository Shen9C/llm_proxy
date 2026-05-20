#!/usr/bin/env python3
"""
小米 MiMo LLM 本地中转代理 v2.2
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
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ==================== 配置 ====================
LISTEN_PORT = int(os.environ.get("MIMO_PROXY_PORT", 8765))
MIMO_BASE_URL = os.environ.get("MIMO_BASE_URL", "https://api.xiaomimimo.com")
MIMO_API_KEY = os.environ.get("MIMO_API_KEY", "")
CACHE_FILE = os.environ.get("MIMO_CACHE_FILE", "./mimo_cache")
DEBUG = os.environ.get("MIMO_PROXY_DEBUG", "0") == "1"

# 回退策略：当缓存中缺少 reasoning_content 时的处理方式
# "error"            -> 立即返回 400 错误，告知用户需要重置对话
# "strip"            -> 移除 assistant 消息中的 tool_calls，避免 400（可能丢失上下文）
# "disable_thinking" -> 临时关闭 thinking，避免 API 校验 reasoning
FALLBACK_STRATEGY = os.environ.get("MIMO_FALLBACK_STRATEGY", "strip")

# ==================== 持久化缓存 ====================
cache_lock = threading.Lock()

def get_cache():
    return shelve.open(CACHE_FILE, writeback=False)

# ==================== 工具函数 ====================
def debug_print(*args, **kwargs):
    if DEBUG:
        print("[MiMo Proxy]", *args, **kwargs)

def generate_session_id(messages):
    """基于首条用户消息生成稳定的会话 ID"""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = json.dumps(content, sort_keys=True)
            return hashlib.sha256(f"user:{content}".encode()).hexdigest()[:16]
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content", "")
            return hashlib.sha256(f"system:{content}".encode()).hexdigest()[:16]
    raw = json.dumps(messages, sort_keys=True).encode()
    return hashlib.sha256(raw).hexdigest()[:16]

def count_assistant_before(messages):
    return sum(1 for m in messages if m.get("role") == "assistant")

def patch_messages(messages, session_id):
    """
    补全缺失的 reasoning_content，
    若缓存未命中，根据 FALLBACK_STRATEGY 处理。
    返回 (补全后的消息列表, 是否发生缓存缺失)
    """
    with cache_lock:
        db = get_cache()
        try:
            patched = []
            missing = False
            assistant_idx = 0
            for msg in messages:
                role = msg.get("role", "")
                if role == "assistant" and "tool_calls" in msg and not msg.get("reasoning_content"):
                    cache_key = f"{session_id}:{assistant_idx}"
                    cached = db.get(cache_key)
                    if cached:
                        msg = dict(msg)
                        msg["reasoning_content"] = cached
                        debug_print(f"补全 reasoning (key={cache_key})")
                    else:
                        missing = True
                        debug_print(f"缓存缺失 key={cache_key}，策略={FALLBACK_STRATEGY}")
                        if FALLBACK_STRATEGY == "strip":
                            msg = dict(msg)
                            msg.pop("tool_calls", None)
                            debug_print(f"已移除 tool_calls 以绕过校验")
                        elif FALLBACK_STRATEGY == "disable_thinking":
                            # 标记需要禁用 thinking，稍后统一处理
                            pass
                        # error 策略：保持原样，让 MiMo 返回 400
                    assistant_idx += 1
                patched.append(msg)
            return patched, missing
        finally:
            db.close()

def store_reasoning(session_id, assistant_index, reasoning):
    with cache_lock:
        db = get_cache()
        try:
            key = f"{session_id}:{assistant_index}"
            db[key] = reasoning
            debug_print(f"缓存 reasoning (key={key}, len={len(reasoning)})")
        finally:
            db.close()

# ==================== HTTP 请求处理 ====================
class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
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

        content_length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(content_length)
        try:
            req_body = json.loads(raw_body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON body")
            return

        messages = req_body.get("messages", [])
        if not messages:
            self.send_error(400, "Missing messages array")
            return

        session_id = generate_session_id(messages)
        patched_messages, missing = patch_messages(messages, session_id)
        req_body["messages"] = patched_messages

        # 如果缓存缺失且策略为 disable_thinking，则关闭 thinking
        if missing and FALLBACK_STRATEGY == "disable_thinking":
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

        if stream:
            self._handle_stream(req_body, target_url, headers, session_id)
        else:
            self._handle_non_stream(req_body, target_url, headers, session_id)

    def _handle_non_stream(self, req_body, target_url, headers, session_id):
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            with urlopen(req, timeout=120) as resp:
                resp_data = json.loads(resp.read())
        except HTTPError as e:
            err_body = e.read()
            debug_print(f"上游 {e.code}: {err_body}")
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(err_body)
            return
        except Exception as e:
            debug_print(f"连接失败: {e}")
            self.send_error(502, str(e))
            return

        # 缓存 reasoning
        for choice in resp_data.get("choices", []):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning:
                assistant_idx = count_assistant_before(req_body.get("messages", [])) + choice.get("index", 0)
                store_reasoning(session_id, assistant_idx, reasoning)

        resp_body = json.dumps(resp_data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def _handle_stream(self, req_body, target_url, headers, session_id):
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            resp = urlopen(req, timeout=300)
        except HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read())
            return
        except Exception as e:
            self.send_error(502, str(e))
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        accumulated_reasoning = ""
        assistant_idx = count_assistant_before(req_body.get("messages", []))
        done = False
        try:
            while not done:
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
                            chunk = choice.get("delta", {}).get("reasoning_content")
                            if chunk:
                                accumulated_reasoning += chunk
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
            resp.close()
            if accumulated_reasoning:
                store_reasoning(session_id, assistant_idx, accumulated_reasoning)

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            with cache_lock:
                db = get_cache()
                cache_size = len(db)
                db.close()
            health = {
                "status": "ok",
                "cached_entries": cache_size,
                "upstream": MIMO_BASE_URL,
                "fallback_strategy": FALLBACK_STRATEGY,
                "api_key_configured": bool(MIMO_API_KEY and MIMO_API_KEY != "your-api-key-here")
            }
            self.wfile.write(json.dumps(health).encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        if DEBUG:
            super().log_message(format, *args)

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
    print(f"   请将 Base URL 设置为: http://localhost:{LISTEN_PORT}/v1\n")
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 代理已停止")
        server.server_close()

if __name__ == "__main__":
    main()