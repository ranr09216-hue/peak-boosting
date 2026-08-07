#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Peak 代练 - 订单服务（纯 Python 标准库：http.server + sqlite3）

本地运行:
    python order_server.py [--port 8000]
    浏览器打开 http://localhost:8000

生产部署 (Render / Railway / VPS):
    - 设置环境变量 PORT（平台自动注入）
    - 设置 ADMIN_PASSWORD（管理后台密码，必填）
    - 启动命令: python order_server.py

管理后台: http://<host>/admin （登录后查看全部订单、修改状态）

API:
    POST /api/orders                        提交订单
    GET  /api/orders?contact=xxx            查询订单（按联系方式）
    POST /api/admin/login                   管理员登录
    GET  /api/admin/me                      检查登录态
    GET  /api/admin/orders                  全部订单（需登录）
    POST /api/admin/orders/<id>/status      修改状态（需登录）
    DELETE /api/admin/orders/<id>           删除订单（需登录）
"""

import json
import os
import random
import secrets
import sqlite3
import string
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
DB_FILE = Path(os.environ.get("DB_PATH", str(BASE_DIR / "orders.db")))
LEGACY_JSON = BASE_DIR / "orders.json"
GAMES = {"王者荣耀", "英雄联盟", "和平精英", "原神", "三角洲行动", "永劫无间"}
STATUS_FLOW = ["待确认", "进行中", "已完成"]
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
DEFAULT_ADMIN_USED = "ADMIN_PASSWORD" not in os.environ
SESSION_TTL = 12 * 60 * 60
_lock = threading.Lock()
_sessions = {}  # token -> expiry ts


# ---------- database ----------

def db_connect():
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_FILE), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_no TEXT NOT NULL UNIQUE,
                game TEXT NOT NULL,
                service TEXT NOT NULL,
                current_rank TEXT NOT NULL,
                target_rank TEXT NOT NULL,
                contact TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '待确认',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()
    ensure_progress_column()
    _import_legacy_json()


def ensure_progress_column():
    with db_connect() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "progress" not in cols:
            conn.execute("ALTER TABLE orders ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            conn.commit()


def _import_legacy_json():
    if not LEGACY_JSON.exists() or not LEGACY_JSON.is_file():
        return
    try:
        legacy = json.loads(LEGACY_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(legacy, list):
        return
    with _lock, db_connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        if count > 0:
            return
        for item in legacy:
            try:
                conn.execute(
                    "INSERT INTO orders (order_no, game, service, current_rank, target_rank, contact, status, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(item.get("order_no")),
                        str(item.get("game")),
                        str(item.get("service")),
                        str(item.get("currentRank", item.get("current_rank", ""))),
                        str(item.get("targetRank", item.get("target_rank", ""))),
                        str(item.get("contact")),
                        str(item.get("status", "待确认")),
                        str(item.get("created_at", "")),
                    ),
                )
            except sqlite3.IntegrityError:
                continue
        conn.commit()


def row_to_order(row):
    return {
        "id": row["id"],
        "order_no": row["order_no"],
        "game": row["game"],
        "service": row["service"],
        "currentRank": row["current_rank"],
        "targetRank": row["target_rank"],
        "contact": row["contact"],
        "status": row["status"],
        "progress": row["progress"] if "progress" in row.keys() else 0,
        "created_at": row["created_at"],
    }


def list_orders(contact=None):
    with db_connect() as conn:
        if contact:
            rows = conn.execute(
                "SELECT * FROM orders WHERE lower(contact) LIKE ? ORDER BY id DESC",
                ("%" + contact.lower() + "%",),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM orders ORDER BY id DESC").fetchall()
    return [row_to_order(r) for r in rows]


def create_order(payload):
    order_no = _new_order_no()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _lock, db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO orders (order_no, game, service, current_rank, target_rank, contact, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '待确认', ?)",
            (
                order_no,
                str(payload["game"]),
                str(payload["service"]),
                str(payload["currentRank"]),
                str(payload["targetRank"]),
                str(payload["contact"]),
                now,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (cur.lastrowid,)).fetchone()
    return row_to_order(row)


def update_order_status(order_id, status, progress=None):
    if status not in STATUS_FLOW:
        return None, "无效状态"
    if progress is not None:
        try:
            progress = max(0, min(100, int(progress)))
        except (TypeError, ValueError):
            return None, "进度必须是 0-100 的数字"
    with _lock, db_connect() as conn:
        if progress is not None:
            cur = conn.execute(
                "UPDATE orders SET status = ?, progress = ? WHERE id = ?",
                (status, progress, order_id),
            )
        else:
            cur = conn.execute(
                "UPDATE orders SET status = ? WHERE id = ?",
                (status, order_id),
            )
        conn.commit()
        if cur.rowcount == 0:
            return None, "订单不存在"
        row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    return row_to_order(row), None


def delete_order(order_id):
    with _lock, db_connect() as conn:
        cur = conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        conn.commit()
    return cur.rowcount > 0


def _new_order_no():
    suffix = "".join(random.choices(string.digits, k=4))
    return f"PK{datetime.now():%Y%m%d}-{suffix}"


def validate_order(payload):
    errors = {}
    game = str(payload.get("game", "")).strip()
    service = str(payload.get("service", "")).strip()
    current_rank = str(payload.get("currentRank", "")).strip()
    target_rank = str(payload.get("targetRank", "")).strip()
    contact = str(payload.get("contact", "")).strip()
    if game not in GAMES:
        errors["game"] = "请选择游戏"
    if not service:
        errors["service"] = "请选择服务"
    if not current_rank:
        errors["currentRank"] = "请填写当前段位"
    if not target_rank:
        errors["targetRank"] = "请填写目标段位"
    if len(contact) < 2:
        errors["contact"] = "请填写有效的联系方式"
    return errors


# ---------- admin sessions ----------

def create_session():
    token = secrets.token_urlsafe(24)
    _sessions[token] = time.time() + SESSION_TTL
    return token


def session_valid(token):
    if not token:
        return False
    exp = _sessions.get(token)
    if exp is None:
        return False
    if time.time() > exp:
        _sessions.pop(token, None)
        return False
    return True


# ---------- http handler ----------

STATIC_SUFFIXES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "PeakOrderServer/2.0"

    # ---------- helpers ----------
    def _send_json(self, obj, status=200, extra_headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 1024 * 1024:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _admin_token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        cookies = self.headers.get("Cookie", "")
        for part in cookies.split(";"):
            k, _, v = part.strip().partition("=")
            if k == "peak_admin":
                return v
        return None

    def _require_admin(self):
        if not session_valid(self._admin_token()):
            self._send_json({"ok": False, "error": "未登录或登录已过期"}, 401)
            return False
        return True

    # ---------- routes ----------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if path == "/api/orders":
            query = parse_qs(parsed.query)
            contact = unquote(query.get("contact", [""])[0]).strip()
            self._send_json({"ok": True, "orders": list_orders(contact or None)})
            return

        if path == "/api/orders/latest":
            query = parse_qs(parsed.query)
            contact = unquote(query.get("contact", [""])[0]).strip()
            with db_connect() as conn:
                if contact:
                    row = conn.execute(
                        "SELECT * FROM orders WHERE lower(contact) LIKE ? ORDER BY id DESC LIMIT 1",
                        ("%" + contact.lower() + "%",),
                    ).fetchone()
                else:
                    row = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 1").fetchone()
            self._send_json({"ok": True, "order": row_to_order(row) if row else None})
            return

        if path == "/api/admin/me":
            if self._require_admin():
                self._send_json({"ok": True, "admin": True})
            return

        if path == "/api/admin/orders":
            if self._require_admin():
                self._send_json({"ok": True, "orders": list_orders()})
            return

        # 静态文件（限制后缀，且不暴露数据库/脚本）
        if path in ("/", "/index.html"):
            rel = "game-boosting.html"
        elif path in ("/admin", "/admin/"):
            rel = "admin.html"
        else:
            rel = path.lstrip("/")
        if not rel:
            rel = "game-boosting.html"
        target = (BASE_DIR / rel).resolve()
        suffix = target.suffix.lower()
        if (
            not target.is_relative_to(BASE_DIR)
            or not target.is_file()
            or suffix not in STATIC_SUFFIXES
        ):
            self._send_json({"ok": False, "error": "Not Found"}, 404)
            return
        try:
            body = target.read_bytes()
        except OSError:
            self._send_json({"ok": False, "error": "Not Found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", STATIC_SUFFIXES[suffix])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/orders":
            payload = self._read_json()
            errors = validate_order(payload)
            if errors:
                self._send_json({"ok": False, "error": "请检查表单", "fields": errors}, 400)
                return
            order = create_order(payload)
            self._send_json({"ok": True, "order": order}, 201)
            return

        if path == "/api/admin/login":
            payload = self._read_json()
            password = str(payload.get("password", ""))
            if not secrets.compare_digest(password, ADMIN_PASSWORD):
                self._send_json({"ok": False, "error": "密码错误"}, 401)
                return
            token = create_session()
            self._send_json(
                {"ok": True, "token": token},
                extra_headers={
                    "Set-Cookie": (
                        f"peak_admin={token}; HttpOnly; SameSite=Lax; Path=/; "
                        f"Max-Age={SESSION_TTL}"
                    )
                },
            )
            return

        # /api/admin/orders/<id>/status
        parts = [p for p in path.split("/") if p]
        if len(parts) == 5 and parts[:3] == ["api", "admin", "orders"] and parts[4] == "status":
            if not self._require_admin():
                return
            try:
                order_id = int(parts[3])
            except ValueError:
                self._send_json({"ok": False, "error": "无效订单 ID"}, 400)
                return
            payload = self._read_json()
            order, err = update_order_status(
                order_id,
                str(payload.get("status", "")).strip(),
                payload.get("progress"),
            )
            if err:
                self._send_json({"ok": False, "error": err}, 400)
                return
            self._send_json({"ok": True, "order": order})
            return

        self._send_json({"ok": False, "error": "Not Found"}, 404)

    def do_DELETE(self):
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        if len(parts) == 4 and parts[:3] == ["api", "admin", "orders"]:
            if not self._require_admin():
                return
            try:
                order_id = int(parts[3])
            except ValueError:
                self._send_json({"ok": False, "error": "无效订单 ID"}, 400)
                return
            if delete_order(order_id):
                self._send_json({"ok": True})
            else:
                self._send_json({"ok": False, "error": "订单不存在"}, 404)
            return
        self._send_json({"ok": False, "error": "Not Found"}, 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    port_env = os.environ.get("PORT")
    if port_env and port_env.isdigit():
        port = int(port_env)
        host = os.environ.get("HOST", "0.0.0.0")
    else:
        port = 8000
        if "--port" in sys.argv:
            try:
                port = int(sys.argv[sys.argv.index("--port") + 1])
            except (ValueError, IndexError):
                pass
        host = os.environ.get("HOST", "127.0.0.1")

    init_db()
    if DEFAULT_ADMIN_USED:
        print("警告: 未设置 ADMIN_PASSWORD，管理后台使用默认密码 admin123", file=sys.stderr)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    print(f"Peak 代练服务已启动: http://localhost:{port}")
    print(f"管理后台: http://localhost:{port}/admin")
    print(f"数据库: {DB_FILE}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
