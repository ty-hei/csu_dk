# /// script
# requires-python = ">=3.14,<3.15"
# dependencies = [
#   "requests==2.34.2",
#   "pycryptodome==3.23.0",
#   "beautifulsoup4==4.15.0",
# ]
# ///
"""极简本地控制页：uv run simple.py；Ctrl+C 退出，不保存账号或登录会话。"""
from __future__ import annotations

import argparse
import secrets
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import requests

from app.csu.cas import CasIpFrozenError, classify_error
from app.csu.workflow import StatusQueryError, run_once


def page(token: str, message: str = "") -> bytes:
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>本地打卡</title>
<style>
body{{font:16px/1.6 system-ui,sans-serif;background:#f5f6f8;color:#182230;margin:0;padding:24px}}
main{{max-width:430px;margin:8vh auto;background:white;padding:28px;border-radius:16px}}
h1{{font-size:24px;margin-top:0}}p{{color:#526070}}label{{display:block;margin-top:16px}}
input{{box-sizing:border-box;width:100%;padding:11px;border:1px solid #bac3ce;border-radius:8px;font:inherit}}
.actions{{display:flex;gap:10px;margin-top:22px}}button{{flex:1;padding:11px;border:0;border-radius:8px;
font:inherit;cursor:pointer;background:#e9eef5;color:#182230}}button[value=checkin]{{background:#2563eb;color:white}}
output{{display:block;white-space:pre-wrap;background:#eef3fa;padding:14px;border-radius:8px;margin:18px 0}}
small{{display:block;color:#647184;margin-top:20px}}
.option{{display:flex;align-items:flex-start;gap:8px;font-size:14px}}.option input{{width:auto;margin-top:5px}}
</style><main><h1>本地打卡</h1><p>按需运行一次。账号、密码和登录会话不保存到文件。</p>
{f'<output role="status">{escape(message)}</output>' if message else ''}
<form method="post" action="/run" autocomplete="off">
<input type="hidden" name="csrf" value="{escape(token, quote=True)}">
<label for="username">学号</label><input id="username" name="username" required maxlength="80" autocomplete="off">
<label for="password">密码</label><input id="password" name="password" type="password" required maxlength="256"
autocomplete="off">
<label class="option"><input type="checkbox" name="force_logout" value="1">
<span>遇到单处登录时，登出其他 PC 并继续<br>会使其他电脑上的学校登录失效。</span></label>
<div class="actions"><button name="action" value="status">只查询状态</button>
<button name="action" value="checkin">打卡一次</button></div></form>
<small>提交使用楼栋坐标及学校位置校验；仅在符合学校打卡要求时使用。
处理可能需要一分钟，请等待结果。关闭终端或按 Ctrl+C 停止服务。</small></main></html>""".encode()


def error_message(error: Exception) -> str:
    """只展示受控提示，不把学校响应中的凭据或堆栈返回浏览器。"""
    if isinstance(error, StatusQueryError):
        return f"登录已成功，但{error}。"
    if isinstance(error, requests.RequestException):
        return "学校网络请求失败或超时，请稍后再试。"
    if isinstance(error, CasIpFrozenError):
        return "学校已冻结当前 IP，请停止尝试并稍后再试。"
    detail = str(error)
    if "未执行强制登出" in detail:
        return "学校单处登录表单结构发生变化，未执行强制登出，请到学校页面处理。"
    if "单处登录限制尚未解除" in detail:
        return "已尝试登出其他 PC，但学校仍提示单处登录；请到学校页面核实，避免连续重试。"
    if "单处登录" in detail or "其他 PC 端登录" in detail:
        return ("学校提示单处登录：账户已在其他 PC 端登录。"
                "可勾选「登出其他 PC 并继续」后重试；其他电脑的学校登录会失效。")
    kind = classify_error(detail)
    return {
        "badCredentials": "学校提示学号或密码有误，请核对后再试。",
        "inactive": "学校提示账号尚未激活，请先在学校页面处理。",
        "locked": "学校提示账号已锁定，请停止尝试并在学校页面处理。",
        "captcha": "学校要求验证码。可停止服务后用 uv run --with ddddocr simple.py 启动识别支持；识别仍可能失败。",
        "sessionExpired": "学校登录会话已失效，请稍后再试。",
    }.get(kind, "执行未完成，请先在学校页面确认登录和打卡状态。")


class LocalServer(ThreadingHTTPServer):
    daemon_threads = False  # 正在提交时，正常退出会等待请求结束。

    def __init__(self, port: int, runner=run_once):
        super().__init__(("127.0.0.1", port), Handler)
        self.runner = runner
        self.token = secrets.token_urlsafe(32)
        self.action_lock = threading.Lock()
        self.next_attempt = 0.0


class Handler(BaseHTTPRequestHandler):
    server: LocalServer

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *_args):
        pass

    def local_request(self) -> bool:
        port = self.server.server_port
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        origin = self.headers.get("Origin")
        return (
            self.client_address[0] == "127.0.0.1"
            and self.headers.get("Host") in allowed
            and not any(key in self.headers for key in ("Forwarded", "X-Forwarded-For", "X-Real-IP"))
            and (origin is None or origin in {f"http://{host}" for host in allowed})
        )

    def respond(self, message: str = "", status: int = 200):
        body = page(self.server.token, message)
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; "
                         "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.local_request():
            self.respond("仅允许本机直接访问。", 403)
        elif self.path != "/":
            self.respond("页面不存在。", 404)
        else:
            self.respond()

    def do_POST(self):
        if not self.local_request():
            self.respond("仅允许本机直接访问。", 403)
            return
        if self.path != "/run":
            self.respond("页面不存在。", 404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192 or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError
            values = parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True, max_num_fields=5)
            if (set(values) - {"force_logout"} != {"csrf", "username", "password", "action"}
                    or any(len(v) != 1 for v in values.values())
                    or ("force_logout" in values and values["force_logout"] != ["1"])):
                raise ValueError
            force_logout = "force_logout" in values
            token, username, password, action = (values[k][0] for k in ("csrf", "username", "password", "action"))
            username = username.strip()
            if not username or len(username) > 80 or len(password) > 256 or action not in {"status", "checkin"}:
                raise ValueError
        except (ValueError, UnicodeError):
            self.respond("请填写学号和密码，并使用页面按钮操作。", 400)
            return
        if not self.server.action_lock.acquire(blocking=False):
            self.respond("正在处理上一次请求，请等待结果。", 409)
            return
        try:
            if not secrets.compare_digest(token.encode(), self.server.token.encode()):
                self.respond("表单已失效，请使用下方的新表单操作。", 403)
                return
            if time.monotonic() < self.server.next_attempt:
                self.respond("操作过于频繁，请等待 15 秒后再试。", 429)
                return
            # 一次性令牌阻止浏览器刷新、后退重发；密码不会回填进返回页面。
            self.server.token = secrets.token_urlsafe(32)
            try:
                result = self.server.runner(username, password, submit=action == "checkin", force_logout=force_logout)
                message = result.message
            except Exception as error:  # noqa: BLE001 - 本地 UI 边界，不返回学校原始响应
                message = error_message(error)
            finally:
                self.server.next_attempt = time.monotonic() + 15
            self.respond(message)
        finally:
            self.server.action_lock.release()


def main():
    parser = argparse.ArgumentParser(description="极简本地打卡控制页（仅监听本机、不保存账号）")
    parser.add_argument("--port", type=int, default=8765, help="本地端口，默认 8765")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    try:
        with LocalServer(args.port) as server:
            print(f"浏览器打开 http://127.0.0.1:{server.server_port}/ ；按 Ctrl+C 停止。", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                print("\n正在退出；如有操作进行中，将等待其结束。", flush=True)
    except OSError:
        parser.exit(1, "无法启动本地服务，请检查端口占用，或用 --port 8766 更换端口。\n")


if __name__ == "__main__":
    main()
