"""
本地 mock 静态 HTTP 服务器 —— 自动启动，提供 mock_web 目录下的 HTML
"""
from __future__ import annotations
import http.server
import socketserver
import threading
import socket
from pathlib import Path

from bot.config import MOCK_WEB_DIR, MOCK_SERVER_HOST, MOCK_SERVER_PORT

_server_thread = None


class _Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(MOCK_WEB_DIR), **kwargs)

    def log_message(self, format, *args):
        # 静默，避免刷屏
        pass


class _Server(socketserver.ThreadingTCPServer):
    """多线程 + 允许地址复用：避免单个 keep-alive 连接堵死后续所有请求"""
    allow_reuse_address = True
    daemon_threads = True


def _is_port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) != 0


def ensure_mock_server() -> None:
    """确保 mock 服务器已在本地运行"""
    global _server_thread
    if not _is_port_free(MOCK_SERVER_HOST, MOCK_SERVER_PORT):
        return  # 已在运行

    socketserver.TCPServer.allow_reuse_address = True
    httpd = _Server((MOCK_SERVER_HOST, MOCK_SERVER_PORT), _Handler)

    def _serve():
        httpd.serve_forever()

    _server_thread = threading.Thread(target=_serve, daemon=True)
    _server_thread.start()

    url = f"http://{MOCK_SERVER_HOST}:{MOCK_SERVER_PORT}"
    print(f"\n🖥️  Mock 服务器已启动: {url}  (目录: {MOCK_WEB_DIR})")


if __name__ == "__main__":
    ensure_mock_server()
    print("按 Ctrl+C 退出...")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
