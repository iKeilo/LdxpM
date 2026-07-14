#!/usr/bin/env python3
import gzip
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


CHALLENGE_ARG1 = "3E091BA15043A258FD3613E5C7EB5727D2A33943"
CHALLENGE_COOKIE = "acw_sc__v2=6a55cf5905b75e38cca14c3211748ea9d68b2046"
STATE = {
    "empty": False,
    "broken_detail": False,
    "gzip": True,
    "challenge": True,
    "human_verification": False,
    "request_count": 0,
}
APP_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor())


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeShopHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if STATE["gzip"]:
            body = gzip.compress(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if STATE["gzip"]:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_challenge(self):
        body = (
            f"<html><script>var arg1='{CHALLENGE_ARG1}';"
            "document.cookie='acw_sc__v2=calculated';location.reload();"
            "</script></html>"
        ).encode("utf-8")
        if STATE["gzip"]:
            body = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if STATE["gzip"]:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_human_verification(self):
        body = (
            "<!doctype html><script>window._waf_is_mobile=false;</script>"
            "<div id='captcha'>Human verification required</div>"
        ).encode("utf-8")
        if STATE["gzip"]:
            body = gzip.compress(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if STATE["gzip"]:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        STATE["request_count"] += 1
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        token = payload.get("token") or "TESTSHOP"
        base_url = f"http://127.0.0.1:{FAKE_PORT}"

        if STATE["human_verification"]:
            self.send_human_verification()
            return

        if STATE["challenge"] and CHALLENGE_COOKIE not in (self.headers.get("Cookie") or ""):
            self.send_challenge()
            return

        if self.path == "/shopApi/Shop/info":
            count = 0 if STATE["empty"] else 1
            self.send_json(
                {
                    "code": 1,
                    "msg": "success",
                    "data": {
                        "nickname": "测试空店铺",
                        "link": f"{base_url}/shop/{token}",
                        "goods_type_sort": ["card"],
                        "card_count": count,
                        "article_count": 0,
                        "resource_count": 0,
                        "equity_count": 0,
                        "goods_count": count,
                    },
                }
            )
            return

        if self.path == "/shopApi/Shop/categoryList":
            categories = [] if STATE["empty"] else [{"id": 1, "name": "默认分类", "goods_count": 1}]
            self.send_json({"code": 1, "msg": "success", "data": categories})
            return

        if self.path == "/shopApi/Shop/goodsList":
            items = []
            if not STATE["empty"]:
                items = [
                    {
                        "goods_key": "abc123",
                        "name": "黑盒测试商品",
                        "price": 12.5,
                        "link": f"{base_url}/item/abc123",
                        "category": {"id": 1, "name": "默认分类"},
                        "extend": {"stock_count": 9},
                    }
                ]
            self.send_json({"code": 1, "msg": "success", "data": {"total": len(items), "list": items}})
            return

        if self.path == "/shopApi/Shop/goodsInfo":
            if STATE["broken_detail"]:
                self.send_json({"code": 0, "msg": "商品未上架，如有疑问请联系商家", "data": None})
            else:
                self.send_json(
                    {
                        "code": 1,
                        "msg": "success",
                        "data": {
                            "goods_key": payload.get("goods_key"),
                            "status": 1,
                            "name": "黑盒测试商品",
                            "price": 12.5,
                            "link": f"{base_url}/item/abc123",
                            "goods_type": "card",
                            "category": {"id": 1, "name": "默认分类"},
                            "extend": {"stock_count": 9},
                            "user": {"token": token},
                        },
                    }
                )
            return

        self.send_json({"code": 0, "msg": "not found"}, 404)


def request_json(url, payload=None):
    data = None
    method = "GET"
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with APP_OPENER.open(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_for_app(base_url, proc):
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("app exited early")
        try:
            request_json(f"{base_url}/api/summary")
            return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise RuntimeError("app did not start")


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def main():
    global FAKE_PORT

    FAKE_PORT = free_port()
    app_port = free_port()
    fake_server = ThreadingHTTPServer(("127.0.0.1", FAKE_PORT), FakeShopHandler)
    fake_thread = threading.Thread(target=fake_server.serve_forever, daemon=True)
    fake_thread.start()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "blackbox.sqlite3")
        env = os.environ.copy()
        env.update(
            {
                "BASE_URL": f"http://127.0.0.1:{FAKE_PORT}",
                "DEFAULT_SHOP_URL": f"http://127.0.0.1:{FAKE_PORT}/shop/TESTSHOP",
                "DB_PATH": db_path,
                "HOST": "127.0.0.1",
                "PORT": str(app_port),
                "UPSTREAM_MIN_REQUEST_INTERVAL_SECONDS": "0",
                "UPSTREAM_HUMAN_CHECK_COOLDOWN_SECONDS": "60",
            }
        )
        proc = subprocess.Popen(
            [sys.executable, "app.py"],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            base_url = f"http://127.0.0.1:{app_port}"
            wait_for_app(base_url, proc)
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(summary["auth"]["is_admin"], False, "viewer should start unauthenticated")
            assert "smtp_host" not in summary["settings"]
            try:
                request_json(f"{base_url}/api/check", {})
                raise AssertionError("manual check should require admin login")
            except urllib.error.HTTPError as exc:
                assert_equal(exc.code, 403, "manual check without admin")
            request_json(f"{base_url}/api/login", {"username": "admin", "password": "admin"})
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(summary["auth"]["is_admin"], True, "admin should be logged in")
            request_json(f"{base_url}/api/check", {})
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(len(summary["products"]), 1, "product should be imported")
            assert_equal(summary["products"][0]["is_active"], 1, "product should start active")
            assert_equal(summary["shops"][0]["active_product_count"], 1, "shop active count")

            STATE["broken_detail"] = True
            result = request_json(f"{base_url}/api/products/close-unpurchaseable", {})
            assert_equal(result["checked"], 1, "purchase scan should check active product")
            assert_equal(result["closed"], 1, "purchase scan should close broken product")
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(summary["products"][0]["is_active"], 0, "broken detail product should be inactive")
            assert_equal(summary["events"], [], "closed unpurchaseable product should not create mail event")

            STATE["broken_detail"] = False
            request_json(f"{base_url}/api/check", {})
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(summary["products"][0]["is_active"], 0, "normal shop check should keep closed product hidden")
            result = request_json(f"{base_url}/api/products/recheck-unpurchaseable", {})
            assert_equal(result["checked"], 1, "manual recheck should check closed product")
            assert_equal(result["restored"], 1, "manual recheck should restore purchaseable product")
            summary = request_json(f"{base_url}/api/summary")
            assert_equal(summary["products"][0]["is_active"], 1, "product should reactivate when detail works")
            assert_equal(summary["events"][0]["event_type"], "purchaseable", "restore should create purchaseable event")

            STATE["empty"] = True
            request_json(f"{base_url}/api/check", {})
            summary = request_json(f"{base_url}/api/summary")
            product = summary["products"][0]
            assert_equal(product["is_active"], 0, "missing product should be inactive")
            assert_equal(product["stock"], 0, "missing product stock should be zero")
            assert_equal(summary["shops"][0]["active_product_count"], 0, "shop active count after empty")
            assert_equal(summary["shops"][0]["inactive_product_count"], 1, "shop inactive count after empty")
            assert summary["shops"][0]["last_error"] == "店铺当前没有上架商品"
            assert summary["events"][0]["event_type"] == "unlisted"
            assert "已不在店铺上架列表中" in summary["events"][0]["message"]

            STATE["human_verification"] = True
            requests_before = STATE["request_count"]
            request_json(f"{base_url}/api/check", {})
            requests_after_first_check = STATE["request_count"]
            assert requests_after_first_check > requests_before
            summary = request_json(f"{base_url}/api/summary")
            assert "已暂停" in summary["shops"][0]["last_error"]
            assert summary["monitor"]["upstream_blocked_until"]
            request_json(f"{base_url}/api/check", {})
            assert_equal(
                STATE["request_count"],
                requests_after_first_check,
                "circuit breaker should prevent repeated upstream requests",
            )
            print("blackbox_empty_shop: ok")
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
            fake_server.shutdown()


if __name__ == "__main__":
    main()
