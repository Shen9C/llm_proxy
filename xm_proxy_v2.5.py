#!/usr/bin/env python3
"""
小米 MiMo LLM 本地中转代理 v2.2
- 自动缓存并补全 reasoning_content
- 持久化缓存（dbm 文件）
- 按会话 ID + 消息序号精准匹配
- 支持流式 (SSE) 响应，正确结束
- 新增：缓存未命中时的回退策略（strip / error / disable_thinking）
- 适用工具：Trae, Cursor, Roo Code 等

-输入的请求url： http://localhost:8765/v1

"""
import os
print("MIMO_API_KEY:", os.environ.get("MIMO_API_KEY"))
# print("所有环境变量：", dict(os.environ))

import json
import os
import shelve
import hashlib
import threading
import time
import logging
from logging.handlers import RotatingFileHandler
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.request import Request, urlopen, build_opener, ProxyHandler as UrllibProxyHandler
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

# 是否禁用系统代理（直接连接上游）
DISABLE_PROXY = os.environ.get("MIMO_DISABLE_PROXY", "0") == "1"

# 模型映射配置
# 格式: {用户请求的模型名: 实际使用的模型名}
# 如果用户请求的模型名不在映射中，则直接使用用户请求的模型名
MODEL_MAPPING = {
    "mimo-v2.5-pro": "mimo-v2.5-pro",
    "mimo-v2.5": "mimo-v2.5",
    "mimo-v2-flash": "mimo-v2-flash",
    "mimo-v2": "mimo-v2",
    "mimo-v1.5": "mimo-v1.5",
    "mimo-v1": "mimo-v1",
}

# 默认模型（当请求中没有指定模型时使用）
DEFAULT_MODEL = os.environ.get("MIMO_DEFAULT_MODEL", "mimo-v2.5-pro")

# ==================== 持久化缓存 ====================
cache_lock = threading.Lock()

def get_cache():
    return shelve.open(CACHE_FILE, writeback=False)

# ==================== 日志配置 ====================
LOG_FILE = "app.log"
LOG_MAX_SIZE = 10 * 1024 * 1024  # 10MB
LOG_BACKUP_COUNT = 1  # 只保留1个备份文件

# 配置日志格式
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 创建日志处理器
# 控制台处理器
console_handler = logging.StreamHandler()
console_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

# 文件处理器（带轮转）
file_handler = RotatingFileHandler(
    LOG_FILE, 
    maxBytes=LOG_MAX_SIZE, 
    backupCount=LOG_BACKUP_COUNT,
    encoding='utf-8'
)
file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

# 配置根日志记录器
logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    handlers=[console_handler, file_handler]
)

# 获取日志记录器
logger = logging.getLogger("MiMoProxy")

# ==================== 工具函数 ====================
def log_debug(*args, model=None, **kwargs):
    if DEBUG:
        model_prefix = f" - {model}:" if model else ":"
        message = f"{model_prefix} {' '.join(str(arg) for arg in args)}"
        logger.debug(message)

def log_info(*args, model=None, **kwargs):
    model_prefix = f" - {model}:" if model else ":"
    message = f"{model_prefix} {' '.join(str(arg) for arg in args)}"
    logger.info(message)

def log_warn(*args, model=None, **kwargs):
    model_prefix = f" - {model}:" if model else ":"
    message = f"{model_prefix} {' '.join(str(arg) for arg in args)}"
    logger.warning(message)

def log_error(*args, model=None, **kwargs):
    model_prefix = f" - {model}:" if model else ":"
    message = f"{model_prefix} {' '.join(str(arg) for arg in args)}"
    logger.error(message)

def debug_print(*args, model=None, **kwargs):
    if DEBUG:
        model_prefix = f" - {model}:" if model else ":"
        message = f"{model_prefix} {' '.join(str(arg) for arg in args)}"
        logger.debug(message)

def map_model(model_name):
    """
    根据模型映射配置返回实际使用的模型名
    如果用户请求的模型名不在映射中，则直接使用用户请求的模型名
    """
    if not model_name:
        return DEFAULT_MODEL
    
    # 如果模型名在映射中，返回映射后的模型名
    if model_name in MODEL_MAPPING:
        mapped_model = MODEL_MAPPING[model_name]
        log_info(f"模型映射: {model_name} -> {mapped_model}", model=mapped_model)
        return mapped_model
    
    # 如果模型名不在映射中，直接使用用户请求的模型名
    log_info(f"使用用户请求的模型: {model_name}", model=model_name)
    return model_name

def create_proxy_opener():
    """创建一个不使用系统代理的 opener"""
    if DISABLE_PROXY:
        opener = build_opener(UrllibProxyHandler({}))
        log_debug("已创建不使用系统代理的 opener", model=None)
        return opener
    return None

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
        try:
            client_ip = self.client_address[0]
            log_info(f"收到请求 - 客户端: {client_ip}, 路径: {self.path}", model=None)
            
            # 拒绝未设置 API Key 的请求
            if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
                log_error("API Key 未设置，拒绝请求", model=None)
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
            log_debug(f"请求体长度: {content_length} bytes", model=None)
            
            raw_body = self.rfile.read(content_length)
            try:
                req_body = json.loads(raw_body)
            except json.JSONDecodeError as e:
                log_error(f"JSON 解析失败: {e}", model=None)
                self.send_error(400, "Invalid JSON body")
                return

            messages = req_body.get("messages", [])
            if not messages:
                log_error("请求中缺少 messages 数组", model=None)
                self.send_error(400, "Missing messages array")
                return

            session_id = generate_session_id(messages)
            requested_model = req_body.get("model")
            actual_model = map_model(requested_model)
            log_info(f"会话 ID: {session_id}", model=actual_model)
            
            patched_messages, missing = patch_messages(messages, session_id)
            req_body["messages"] = patched_messages

            # 模型自动切换
            if requested_model != actual_model:
                req_body["model"] = actual_model
                log_info(f"模型自动切换: {requested_model} -> {actual_model}", model=actual_model)
            else:
                log_debug(f"使用模型: {actual_model}", model=actual_model)
            
            # 如果缓存缺失且策略为 disable_thinking，则关闭 thinking
            if missing and FALLBACK_STRATEGY == "disable_thinking":
                if "thinking" in req_body:
                    req_body["thinking"] = {"type": "disabled"}
                log_warn("缓存缺失，已禁用 thinking 模式", model=actual_model)

            stream = req_body.get("stream", False)
            log_debug(f"请求类型: {'流式' if stream else '非流式'}", model=actual_model)
            
            target_url = f"{MIMO_BASE_URL}{self.path}"
            log_debug(f"目标地址: {target_url}", model=actual_model)
            
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {MIMO_API_KEY}",
            }
            # 过滤请求头，只传递允许的请求头
            allowed_headers = ["x-request-id", "x-session-id", "x-user-id", "x-conversation-id"]
            for key in allowed_headers:
                if key in self.headers:
                    headers[key] = self.headers[key]
            
            # 记录被过滤的请求头（用于调试）
            filtered_headers = []
            for key in self.headers:
                if key.lower() not in [h.lower() for h in allowed_headers] and key.lower() not in ["content-type", "content-length", "authorization"]:
                    filtered_headers.append(key)
            if filtered_headers:
                log_debug(f"过滤了请求头: {filtered_headers}", model=None)

            if stream:
                self._handle_stream(req_body, target_url, headers, session_id)
            else:
                self._handle_non_stream(req_body, target_url, headers, session_id)
        except Exception as e:
            log_error(f"处理 POST 请求时发生未预期异常: {e}", model=None)
            import traceback
            log_error(f"详细错误信息: {traceback.format_exc()}", model=None)
            try:
                self.send_error(500, f"Internal server error: {str(e)}")
            except:
                pass

    def _handle_non_stream(self, req_body, target_url, headers, session_id):
        model = req_body.get("model", DEFAULT_MODEL)
        log_debug(f"开始非流式请求处理，会话: {session_id}", model=model)
        opener = create_proxy_opener()
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            if opener:
                with opener.open(req, timeout=120) as resp:
                    resp_data = json.loads(resp.read())
            else:
                with urlopen(req, timeout=120) as resp:
                    resp_data = json.loads(resp.read())
            log_debug(f"非流式请求成功，响应大小: {len(json.dumps(resp_data))} bytes", model=model)
        except HTTPError as e:
            err_body = e.read()
            error_msg = err_body.decode('utf-8', errors='replace') if isinstance(err_body, bytes) else str(err_body)
            log_error(f"上游返回错误 {e.code}: {error_msg[:200]}" if len(error_msg) > 200 else f"上游返回错误 {e.code}: {error_msg}", model=model)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)
            return
        except Exception as e:
            log_error(f"连接上游失败: {e}", model=model)
            import traceback
            log_error(f"详细错误信息: {traceback.format_exc()}", model=model)
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            self.send_error(502, error_msg)
            return

        # 缓存 reasoning
        reasoning_count = 0
        for choice in resp_data.get("choices", []):
            reasoning = choice.get("message", {}).get("reasoning_content")
            if reasoning:
                assistant_idx = count_assistant_before(req_body.get("messages", [])) + choice.get("index", 0)
                store_reasoning(session_id, assistant_idx, reasoning)
                reasoning_count += 1
        if reasoning_count > 0:
            log_debug(f"已缓存 {reasoning_count} 条 reasoning", model=model)

        resp_body = json.dumps(resp_data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)
        log_info(f"非流式请求处理完成，会话: {session_id}", model=model)

    def _handle_stream(self, req_body, target_url, headers, session_id):
        model = req_body.get("model", DEFAULT_MODEL)
        log_info(f"开始流式请求处理，会话: {session_id}", model=model)
        opener = create_proxy_opener()
        try:
            req = Request(target_url, data=json.dumps(req_body).encode(),
                          headers=headers, method="POST")
            if opener:
                resp = opener.open(req, timeout=300)
            else:
                resp = urlopen(req, timeout=300)
            log_debug(f"流式连接建立成功", model=model)
        except HTTPError as e:
            err_body = e.read()
            error_msg = err_body.decode('utf-8', errors='replace') if isinstance(err_body, bytes) else str(err_body)
            log_error(f"上游返回错误 {e.code}: {error_msg[:200]}" if len(error_msg) > 200 else f"上游返回错误 {e.code}: {error_msg}", model=model)
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(err_body)
            return
        except Exception as e:
            log_error(f"连接上游失败: {e}", model=model)
            import traceback
            log_error(f"详细错误信息: {traceback.format_exc()}", model=model)
            error_msg = str(e).encode('ascii', 'replace').decode('ascii')
            self.send_error(502, error_msg)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        accumulated_reasoning = ""
        assistant_idx = count_assistant_before(req_body.get("messages", []))
        done = False
        line_count = 0
        try:
            while not done:
                line = resp.readline()
                if not line:
                    log_debug(f"流传输结束，共接收 {line_count} 行", model=model)
                    break
                self.wfile.write(line)
                self.wfile.flush()
                line_count += 1

                line_str = line.decode("utf-8").rstrip("\n").rstrip("\r")
                if line_str.startswith("data:"):
                    data_str = line_str[5:].strip()
                    if data_str == "[DONE]":
                        self.wfile.write(b"\n")   # 补完空行
                        self.wfile.flush()
                        done = True
                        log_debug(f"收到流结束标记 [DONE]", model=model)
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
            log_error(f"流传输异常: {e}", model=model)
            import traceback
            log_error(f"详细错误信息: {traceback.format_exc()}", model=model)
        finally:
            if not done:
                try:
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    log_debug("已发送结束标记", model=model)
                except Exception as flush_e:
                    log_error(f"发送结束标记失败: {flush_e}", model=model)
                    import traceback
                    log_error(f"详细错误信息: {traceback.format_exc()}", model=model)
            resp.close()
            if accumulated_reasoning:
                store_reasoning(session_id, assistant_idx, accumulated_reasoning)
                log_debug(f"流式请求完成，缓存 reasoning 长度: {len(accumulated_reasoning)}", model=model)
            log_info(f"流式请求处理完成，会话: {session_id}", model=model)

    def do_GET(self):
        try:
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
        except Exception as e:
            log_error(f"处理 GET 请求时发生异常: {e}", model=None)
            import traceback
            log_error(f"详细错误信息: {traceback.format_exc()}", model=None)
            try:
                self.send_error(500, f"Internal server error: {str(e)}")
            except:
                pass

    def log_message(self, format, *args):
        if DEBUG:
            logger.debug(f"{format % args}")

def main():
    logger.info("MiMo 代理 v2.5 启动中...")
    logger.info(f"监听端口  : {LISTEN_PORT}")
    logger.info(f"上游地址  : {MIMO_BASE_URL}")
    logger.info(f"缓存文件  : {CACHE_FILE}")
    logger.info(f"回退策略  : {FALLBACK_STRATEGY}")
    logger.info(f"调试模式  : {'开启' if DEBUG else '关闭'}")
    logger.info(f"禁用代理  : {'开启' if DISABLE_PROXY else '关闭'}")
    logger.info(f"默认模型  : {DEFAULT_MODEL}")
    
    if not MIMO_API_KEY or MIMO_API_KEY == "your-api-key-here":
        logger.warning("环境变量 MIMO_API_KEY 未设置！代理将拒绝所有请求。")
        logger.warning("请执行 export MIMO_API_KEY=你的key 后重新启动。")
    
    logger.info(f"请将 Base URL 设置为: http://localhost:{LISTEN_PORT}/v1")
    
    server = HTTPServer(("127.0.0.1", LISTEN_PORT), ProxyHandler)
    try:
        logger.info("代理服务已启动，开始监听请求...")
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号，代理正在关闭...")
        server.server_close()
        logger.info("👋 代理已停止")
    except Exception as e:
        logger.error(f"代理服务异常退出: {e}")
        import traceback
        logger.error(f"详细错误信息: {traceback.format_exc()}")
        logger.error("代理服务已停止，但进程仍在运行...")
        # 不退出进程，让代理继续运行
        try:
            server.server_close()
        except Exception as close_e:
            logger.error(f"关闭服务器时发生错误: {close_e}")
        # 重新抛出异常，以便外部可以捕获
        raise

if __name__ == "__main__":
    main()