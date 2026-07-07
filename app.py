#!/usr/bin/env python3
import json
import os
import re
import smtplib
import sqlite3
import threading
import urllib.parse
import urllib.request
from contextlib import closing
from datetime import datetime
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


BASE_URL = os.environ.get("BASE_URL", "https://pay.ldxp.cn").rstrip("/")
DB_PATH = os.environ.get("DB_PATH", "ldxp_stock_webapp.sqlite3")
DEFAULT_INTERVAL_SECONDS = 300
DEFAULT_SHOP_URL = os.environ.get("DEFAULT_SHOP_URL", f"{BASE_URL}/shop/WPXSCE1B/")
EMPTY_SHOP_MESSAGE = "店铺当前没有上架商品"
STOCK_DROP_NOTIFY_SECONDS = 30
STOCK_DROP_NOTIFY_THRESHOLD = 5
UNPURCHASEABLE_RECHECK_SECONDS = 600
MONITOR_STATUS = {
    "started_at": None,
    "last_heartbeat_at": None,
    "last_background_check_at": None,
    "last_manual_check_at": None,
    "last_error": None,
    "checks_started": 0,
    "checks_finished": 0,
}
MONITOR_STATUS_LOCK = threading.Lock()
CHECK_LOCK = threading.Lock()


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def update_monitor_status(**kwargs):
    with MONITOR_STATUS_LOCK:
        MONITOR_STATUS.update(kwargs)


def get_monitor_status():
    with MONITOR_STATUS_LOCK:
        return dict(MONITOR_STATUS)


def extract_token(value):
    value = (value or "").strip()
    match = re.search(r"/shop/([^/?#]+)/?", value)
    if match:
        return match.group(1)
    return value.strip("/")


def request_json(path, payload, timeout=25):
    token = payload.get("token", "")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        BASE_URL + path,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Referer": f"{BASE_URL}/shop/{token}/",
            "User-Agent": "Mozilla/5.0 ldxp-stock-webapp/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("code") != 1:
        raise RuntimeError(result.get("msg") or f"{path} failed")
    return result.get("data")


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys = on")
    return conn


def init_db():
    with closing(db_connect()) as conn:
        conn.executescript(
            """
            create table if not exists shops (
                id integer primary key autoincrement,
                token text not null unique,
                name text not null,
                link text not null,
                enabled integer not null default 1,
                interval_seconds integer not null default 300,
                last_checked_at text,
                last_error text,
                created_at text not null
            );

            create table if not exists products (
                id integer primary key autoincrement,
                shop_id integer not null,
                goods_key text not null,
                name text not null,
                category text,
                price real,
                previous_price real,
                price_delta real not null default 0,
                priority integer not null default 0,
                stock integer not null default 0,
                previous_stock integer not null default 0,
                last_stock_alert_stock integer,
                link text not null,
                is_active integer not null default 1,
                missing_since text,
                inactive_reason text,
                last_recheck_at text,
                last_seen_at text not null,
                created_at text not null,
                unique(shop_id, goods_key),
                foreign key(shop_id) references shops(id) on delete cascade
            );

            create table if not exists events (
                id integer primary key autoincrement,
                shop_id integer not null,
                product_id integer,
                event_type text not null,
                message text not null,
                stock_before integer,
                stock_after integer,
                created_at text not null,
                emailed_at text,
                foreign key(shop_id) references shops(id) on delete cascade,
                foreign key(product_id) references products(id) on delete set null
            );

            create table if not exists settings (
                key text primary key,
                value text
            );

            create table if not exists stock_drop_alerts (
                product_id integer primary key,
                shop_id integer not null,
                quantity integer not null default 0,
                from_stock integer not null,
                to_stock integer not null,
                first_seen_at text not null,
                last_seen_at text not null,
                foreign key(shop_id) references shops(id) on delete cascade,
                foreign key(product_id) references products(id) on delete cascade
            );
            """
        )
        ensure_column(conn, "products", "previous_price", "real")
        ensure_column(conn, "products", "price_delta", "real not null default 0")
        ensure_column(conn, "products", "priority", "integer not null default 0")
        ensure_column(conn, "products", "last_stock_alert_stock", "integer")
        ensure_column(conn, "products", "is_active", "integer not null default 1")
        ensure_column(conn, "products", "missing_since", "text")
        ensure_column(conn, "products", "inactive_reason", "text")
        ensure_column(conn, "products", "last_recheck_at", "text")
        conn.execute(
            "update products set last_stock_alert_stock = stock where last_stock_alert_stock is null"
        )
        conn.commit()


def ensure_column(conn, table, column, definition):
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})")}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {definition}")


def setting(conn, key, default=None):
    row = conn.execute("select value from settings where key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        "insert into settings(key, value) values(?, ?) "
        "on conflict(key) do update set value = excluded.value",
        (key, value),
    )


def normalize_smtp_security(port, security):
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 587
    if not security:
        return "ssl" if port == 465 else "starttls"
    if port == 465 and security == "starttls":
        return "ssl"
    return security


def fetch_shop_info(token):
    return request_json("/shopApi/Shop/info", {"token": token})


def fetch_categories(token, goods_type):
    return request_json(
        "/shopApi/Shop/categoryList",
        {"token": token, "goods_type": goods_type},
    )


def fetch_goods_page(token, goods_type, category_id, page, page_size):
    return request_json(
        "/shopApi/Shop/goodsList",
        {
            "token": token,
            "keywords": "",
            "category_id": category_id,
            "goods_type": goods_type,
            "current": page,
            "pageSize": page_size,
        },
    )


def fetch_goods_info(goods_key, token=None):
    payload = {"goods_key": goods_key}
    if token:
        payload["token"] = token
    return request_json("/shopApi/Shop/goodsInfo", payload)


def is_unpurchaseable_error(exc):
    message = str(exc)
    return any(text in message for text in ["商品未上架", "商品不存在", "不存在", "已下架", "下架"])


def shop_list_url(token, category_id=None):
    url = f"{BASE_URL}/shop/{urllib.parse.quote(str(token).strip('/'))}"
    if category_id not in (None, ""):
        url = f"{url}/{urllib.parse.quote(str(category_id))}"
    return url


def product_target_link(token, item, category):
    link = item.get("link") or ""
    goods_key = item.get("goods_key") or ""
    category_id = (item.get("category") or category or {}).get("id")
    if not link and goods_key:
        return f"{BASE_URL}/item/{urllib.parse.quote(str(goods_key))}"
    return link or shop_list_url(token, category_id)


def shop_has_listed_goods(shop_info):
    for key, value in shop_info.items():
        if key.endswith("_count") and key not in {"sell_count"} and safe_int(value) > 0:
            return True
    return safe_int(shop_info.get("goods_count")) > 0


def purchase_check_enabled(conn=None):
    if conn is not None:
        return setting(conn, "purchase_check_enabled", "0") == "1"
    with closing(db_connect()) as local_conn:
        return setting(local_conn, "purchase_check_enabled", "0") == "1"


def product_is_purchaseable(goods_key, token=None):
    try:
        info = fetch_goods_info(goods_key, token)
    except RuntimeError as exc:
        if is_unpurchaseable_error(exc):
            return False, str(exc)
        raise
    if safe_int(info.get("status"), 1) != 1:
        return False, "商品状态不是上架"
    return True, None


def iter_remote_products(token, shop_info):
    goods_types = shop_info.get("goods_type_sort") or ["card", "article", "resource", "equity"]
    for goods_type in goods_types:
        if safe_int(shop_info.get(f"{goods_type}_count", 0)) <= 0:
            continue
        for category in fetch_categories(token, goods_type):
            page = 1
            page_size = 50
            while True:
                data = fetch_goods_page(token, goods_type, category["id"], page, page_size)
                items = data.get("list") or []
                for item in items:
                    extend = item.get("extend") or {}
                    item_category = item.get("category") or category
                    goods_key = item.get("goods_key") or ""
                    if not goods_key:
                        continue
                    yield {
                        "goods_key": goods_key,
                        "name": item.get("name") or "",
                        "category": item_category.get("name") or "",
                        "price": item.get("price") or 0,
                        "stock": safe_int(extend.get("stock_count")),
                        "link": product_target_link(token, item, item_category),
                    }
                if len(items) < page_size:
                    break
                page += 1


def add_shop(shop_url_or_token, interval_seconds=DEFAULT_INTERVAL_SECONDS):
    token = extract_token(shop_url_or_token)
    if not token:
        raise ValueError("请输入店铺链接或 token")
    info = fetch_shop_info(token)
    name = info.get("nickname") or token
    link = info.get("link") or f"{BASE_URL}/shop/{token}"
    interval_seconds = max(int(interval_seconds or DEFAULT_INTERVAL_SECONDS), 60)

    with closing(db_connect()) as conn:
        conn.execute(
            """
            insert into shops(token, name, link, enabled, interval_seconds, created_at)
            values(?, ?, ?, 1, ?, ?)
            on conflict(token) do update set
                name = excluded.name,
                link = excluded.link,
                enabled = 1,
                interval_seconds = excluded.interval_seconds,
                last_error = null
            """,
            (token, name, link, interval_seconds, now_text()),
        )
        conn.commit()

    return token


def notify_email(subject, body):
    with closing(db_connect()) as conn:
        enabled = setting(conn, "email_enabled", "0") == "1"
        smtp_host = setting(conn, "smtp_host", "")
        smtp_port = int(setting(conn, "smtp_port", "587") or "587")
        smtp_user = setting(conn, "smtp_user", "")
        smtp_password = setting(conn, "smtp_password", "")
        mail_from = setting(conn, "mail_from", smtp_user)
        mail_to = setting(conn, "mail_to", "")
        smtp_security = setting(conn, "smtp_security", None)
        if smtp_security is None:
            smtp_security = "starttls" if setting(conn, "smtp_tls", "1") == "1" else "none"
        smtp_security = normalize_smtp_security(smtp_port, smtp_security)

    if not enabled:
        return False
    if not smtp_host or not mail_from or not mail_to:
        raise RuntimeError("邮件通知已开启，但 SMTP 配置不完整")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body)

    smtp_class = smtplib.SMTP_SSL if smtp_security == "ssl" else smtplib.SMTP
    with smtp_class(smtp_host, smtp_port, timeout=20) as smtp:
        if smtp_security == "starttls":
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        if smtp_user:
            smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)
    return True


def record_event(
    conn,
    shop_id,
    product_id,
    event_type,
    message,
    stock_before=None,
    stock_after=None,
    subject="库存提醒",
):
    cursor = conn.execute(
        """
        insert into events(
            shop_id, product_id, event_type, message, stock_before, stock_after, created_at
        ) values(?, ?, ?, ?, ?, ?, ?)
        """,
        (shop_id, product_id, event_type, message, stock_before, stock_after, now_text()),
    )
    event_id = cursor.lastrowid
    conn.commit()

    try:
        sent = notify_email(subject, message)
        if sent:
            conn.execute(
                "update events set emailed_at = ? where id = ?",
                (now_text(), event_id),
            )
            conn.commit()
    except Exception as exc:
        conn.execute(
            "update shops set last_error = ? where id = ?",
            (f"邮件发送失败：{exc}", shop_id),
        )
        conn.commit()


def record_restock_event(conn, shop_id, product_id, product, previous_stock, current_stock):
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 已补货，库存 {previous_stock} -> {current_stock}\n"
        f"分类：{product['category']}\n"
        f"价格：{product['price']}\n"
        f"链接：{product['link']}"
    )
    record_event(
        conn,
        shop_id,
        product_id,
        "restock",
        message,
        previous_stock,
        current_stock,
        f"{priority_prefix}补货提醒",
    )


def record_price_event(conn, shop_id, product_id, product, previous_price, current_price):
    delta = round(float(current_price) - float(previous_price), 2)
    direction = "上涨" if delta > 0 else "降低"
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 价格{direction} {abs(delta):.2f}，"
        f"{float(previous_price):.2f} -> {float(current_price):.2f}\n"
        f"分类：{product['category']}\n"
        f"库存：{product['stock']}\n"
        f"链接：{product['link']}"
    )
    record_event(
        conn,
        shop_id,
        product_id,
        "price_up" if delta > 0 else "price_down",
        message,
        None,
        None,
        f"{priority_prefix}价格{direction}提醒",
    )


def record_stock_drop_event(conn, shop_id, product_id, product, alert_stock, current_stock):
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 库存减少 {alert_stock - current_stock}，"
        f"{alert_stock} -> {current_stock}\n"
        f"分类：{product['category']}\n"
        f"价格：{product['price']}\n"
        f"链接：{product['link']}"
    )
    record_event(
        conn,
        shop_id,
        product_id,
        "stock_drop",
        message,
        alert_stock,
        current_stock,
        f"{priority_prefix}库存减少提醒",
    )


def queue_stock_drop_alert(conn, shop_id, product_id, from_stock, current_stock):
    quantity = from_stock - current_stock
    if quantity <= 0:
        return
    existing = conn.execute(
        "select * from stock_drop_alerts where product_id = ?",
        (product_id,),
    ).fetchone()
    if existing:
        conn.execute(
            """
            update stock_drop_alerts set
                quantity = quantity + ?,
                to_stock = ?,
                last_seen_at = ?
            where product_id = ?
            """,
            (quantity, current_stock, now_text(), product_id),
        )
    else:
        conn.execute(
            """
            insert into stock_drop_alerts(
                product_id, shop_id, quantity, from_stock, to_stock,
                first_seen_at, last_seen_at
            ) values(?, ?, ?, ?, ?, ?, ?)
            """,
            (product_id, shop_id, quantity, from_stock, current_stock, now_text(), now_text()),
        )


def flush_stock_drop_alerts(force=False):
    now = datetime.now()
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            select a.*, p.name, p.category, p.price, p.link, p.priority
            from stock_drop_alerts a
            join products p on p.id = a.product_id
            where a.quantity >= ?
            """,
            (STOCK_DROP_NOTIFY_THRESHOLD,),
        ).fetchall()
        for row in rows:
            first_seen = datetime.strptime(row["first_seen_at"], "%Y-%m-%d %H:%M:%S")
            if not force and (now - first_seen).total_seconds() < STOCK_DROP_NOTIFY_SECONDS:
                continue
            product = {
                "name": row["name"],
                "category": row["category"],
                "price": row["price"],
                "link": row["link"],
                "priority": row["priority"],
            }
            message = (
                f"{'【重点通知】' if row['priority'] else ''}{row['name']} "
                f"过去 {STOCK_DROP_NOTIFY_SECONDS} 秒库存累计减少 {row['quantity']}，"
                f"{row['from_stock']} -> {row['to_stock']}\n"
                f"分类：{row['category']}\n"
                f"价格：{row['price']}\n"
                f"链接：{row['link']}"
            )
            record_event(
                conn,
                row["shop_id"],
                row["product_id"],
                "stock_drop",
                message,
                row["from_stock"],
                row["to_stock"],
                f"{'【重点通知】' if row['priority'] else ''}库存减少提醒",
            )
            conn.execute(
                "delete from stock_drop_alerts where product_id = ?",
                (row["product_id"],),
            )
            conn.commit()


def record_sold_out_event(conn, shop_id, product_id, product, previous_stock):
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 已售罄，库存 {previous_stock} -> 0\n"
        f"分类：{product['category']}\n"
        f"价格：{product['price']}\n"
        f"链接：{product['link']}"
    )
    record_event(
        conn,
        shop_id,
        product_id,
        "sold_out",
        message,
        previous_stock,
        0,
        f"{priority_prefix}售罄提醒",
    )


def record_unlisted_event(conn, shop_id, product_id, product, previous_stock, reason=None):
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 已不在店铺上架列表中，"
        f"库存按 0 处理，原库存 {previous_stock}\n"
        f"分类：{product['category']}\n"
        f"价格：{product['price']}\n"
        f"店铺：{product['shop_link']}"
    )
    if reason:
        message += f"\n原因：{reason}"
    record_event(
        conn,
        shop_id,
        product_id,
        "unlisted",
        message,
        previous_stock,
        0,
        f"{priority_prefix}商品未上架提醒",
    )


def record_purchaseable_event(conn, shop_id, product_id, product):
    priority_prefix = "【重点通知】" if product.get("priority") else ""
    message = (
        f"{priority_prefix}{product['name']} 已恢复可购买，重新回到前台列表\n"
        f"分类：{product['category']}\n"
        f"价格：{product['price']}\n"
        f"库存：{product['stock']}\n"
        f"链接：{product['link']}"
    )
    record_event(
        conn,
        shop_id,
        product_id,
        "purchaseable",
        message,
        None,
        product["stock"],
        f"{priority_prefix}商品恢复可购买提醒",
    )


def deactivate_missing_products(conn, shop_id, seen_keys, checked_at, shop_link):
    if seen_keys:
        placeholders = ",".join("?" for _ in seen_keys)
        rows = conn.execute(
            f"""
            select * from products
            where shop_id = ? and is_active = 1 and goods_key not in ({placeholders})
            """,
            (shop_id, *seen_keys),
        ).fetchall()
    else:
        rows = conn.execute(
            "select * from products where shop_id = ? and is_active = 1",
            (shop_id,),
        ).fetchall()

    for row in rows:
        previous_stock = safe_int(row["stock"])
        conn.execute(
            """
            update products set
                previous_stock = stock,
                stock = 0,
                price_delta = 0,
                is_active = 0,
                missing_since = coalesce(missing_since, ?),
                inactive_reason = coalesce(inactive_reason, 'missing')
            where id = ?
            """,
            (checked_at, row["id"]),
        )
        conn.execute("delete from stock_drop_alerts where product_id = ?", (row["id"],))
        product = {
            "name": row["name"],
            "category": row["category"],
            "price": row["price"],
            "link": row["link"],
            "shop_link": shop_link,
            "priority": row["priority"],
        }
        record_unlisted_event(conn, shop_id, row["id"], product, previous_stock)


def deactivate_product(conn, product_id, checked_at, reason, inactive_reason="unpurchaseable", notify=True):
    row = conn.execute(
        """
        select p.*, s.link as shop_link
        from products p
        join shops s on s.id = p.shop_id
        where p.id = ? and p.is_active = 1
        """,
        (product_id,),
    ).fetchone()
    if not row:
        return False
    previous_stock = safe_int(row["stock"])
    conn.execute(
        """
        update products set
            previous_stock = stock,
            stock = 0,
            price_delta = 0,
            is_active = 0,
            missing_since = coalesce(missing_since, ?),
            inactive_reason = ?,
            last_recheck_at = case when ? = 'unpurchaseable' then ? else last_recheck_at end
        where id = ?
        """,
        (checked_at, inactive_reason, inactive_reason, checked_at, row["id"]),
    )
    conn.execute("delete from stock_drop_alerts where product_id = ?", (row["id"],))
    product = {
        "name": row["name"],
        "category": row["category"],
        "price": row["price"],
        "link": row["link"],
        "shop_link": row["shop_link"],
        "priority": row["priority"],
    }
    if notify:
        record_unlisted_event(conn, row["shop_id"], row["id"], product, previous_stock, reason)
    return True


def close_unpurchaseable_products():
    checked_at = now_text()
    closed = 0
    checked = 0
    errors = []
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            select p.id, p.goods_key, p.name, s.token
            from products p
            join shops s on s.id = p.shop_id
            where p.is_active = 1
            order by p.id
            """
        ).fetchall()

    for row in rows:
        checked += 1
        try:
            ok, reason = product_is_purchaseable(row["goods_key"], row["token"])
        except Exception as exc:
            errors.append(f"{row['goods_key']}: {exc}")
            continue
        if ok:
            continue
        with closing(db_connect()) as conn:
            if deactivate_product(conn, row["id"], checked_at, reason, notify=False):
                closed += 1
            conn.commit()

    return {"checked": checked, "closed": closed, "errors": errors[:20]}


def reactivate_product(conn, row, info, checked_at):
    extend = info.get("extend") or {}
    category = info.get("category") or {}
    current_price = float(info.get("price") or row["price"] or 0)
    previous_price = float(row["price"] or 0)
    current_stock = safe_int(extend.get("stock_count"), safe_int(row["stock"]))
    price_delta = round(current_price - previous_price, 2)
    link = info.get("link") or row["link"]
    category_name = category.get("name") or row["category"] or ""
    conn.execute(
        """
        update products set
            name = ?, category = ?, previous_price = ?, price = ?,
            price_delta = ?, previous_stock = stock, stock = ?,
            last_stock_alert_stock = ?, link = ?, is_active = 1,
            missing_since = null, inactive_reason = null,
            last_recheck_at = ?, last_seen_at = ?
        where id = ?
        """,
        (
            info.get("name") or row["name"],
            category_name,
            previous_price,
            current_price,
            price_delta,
            current_stock,
            current_stock,
            link,
            checked_at,
            checked_at,
            row["id"],
        ),
    )
    product = {
        "name": info.get("name") or row["name"],
        "category": category_name,
        "price": current_price,
        "stock": current_stock,
        "link": link,
        "priority": row["priority"],
    }
    record_purchaseable_event(conn, row["shop_id"], row["id"], product)


def recheck_unpurchaseable_products(force=False):
    now = datetime.now()
    checked_at = now_text()
    checked = 0
    restored = 0
    errors = []
    with closing(db_connect()) as conn:
        rows = conn.execute(
            """
            select p.*, s.token
            from products p
            join shops s on s.id = p.shop_id
            where p.is_active = 0 and p.inactive_reason = 'unpurchaseable'
            order by p.last_recheck_at is not null, p.last_recheck_at, p.id
            """
        ).fetchall()

    for row in rows:
        if not force and row["last_recheck_at"]:
            try:
                last = datetime.strptime(row["last_recheck_at"], "%Y-%m-%d %H:%M:%S")
                if (now - last).total_seconds() < UNPURCHASEABLE_RECHECK_SECONDS:
                    continue
            except ValueError:
                pass
        checked += 1
        try:
            info = fetch_goods_info(row["goods_key"], row["token"])
            if safe_int(info.get("status"), 1) != 1:
                raise RuntimeError("商品状态不是上架")
        except Exception as exc:
            with closing(db_connect()) as conn:
                conn.execute("update products set last_recheck_at = ? where id = ?", (checked_at, row["id"]))
                conn.commit()
            if not is_unpurchaseable_error(exc):
                errors.append(f"{row['goods_key']}: {exc}")
            continue
        with closing(db_connect()) as conn:
            fresh = conn.execute(
                "select p.*, s.token from products p join shops s on s.id = p.shop_id where p.id = ?",
                (row["id"],),
            ).fetchone()
            if fresh and fresh["is_active"] == 0 and fresh["inactive_reason"] == "unpurchaseable":
                reactivate_product(conn, fresh, info, checked_at)
                restored += 1
            conn.commit()

    return {"checked": checked, "restored": restored, "errors": errors[:20]}


def check_shop(shop_row):
    shop_id = shop_row["id"]
    token = shop_row["token"]
    checked_at = now_text()
    shop_info = fetch_shop_info(token)
    shop_name = shop_info.get("nickname") or shop_row["name"]
    shop_link = shop_info.get("link") or shop_row["link"] or shop_list_url(token)
    remote_reports_products = shop_has_listed_goods(shop_info)
    verify_purchase = purchase_check_enabled()

    with closing(db_connect()) as conn:
        conn.execute(
            "update shops set name = ?, link = ? where id = ?",
            (shop_name, shop_link, shop_id),
        )
        conn.commit()

    seen_keys = set()
    for product in iter_remote_products(token, shop_info):
        seen_keys.add(product["goods_key"])
        with closing(db_connect()) as conn:
            existing = conn.execute(
                "select * from products where shop_id = ? and goods_key = ?",
                (shop_id, product["goods_key"]),
            ).fetchone()
            suppressed_unpurchaseable = (
                existing
                and safe_int(existing["is_active"], 1) == 0
                and existing["inactive_reason"] == "unpurchaseable"
            )
            purchaseable = True
            purchase_reason = None
            if verify_purchase and not suppressed_unpurchaseable:
                try:
                    purchaseable, purchase_reason = product_is_purchaseable(product["goods_key"], token)
                except Exception as exc:
                    purchase_reason = str(exc)
            previous_stock = safe_int(existing["stock"]) if existing else 0
            previous_price = float(existing["price"] or 0) if existing else float(product["price"] or 0)
            current_price = float(product["price"] or 0)
            price_delta = round(current_price - previous_price, 2) if existing else 0
            product["priority"] = int(existing["priority"]) if existing else 0
            current_stock = safe_int(product["stock"])
            alert_stock = safe_int(existing["last_stock_alert_stock"]) if existing and existing["last_stock_alert_stock"] is not None else previous_stock
            next_alert_stock = current_stock if (not existing or current_stock > alert_stock) else alert_stock

            if existing:
                if suppressed_unpurchaseable:
                    conn.execute(
                        """
                        update products set
                            name = ?, category = ?, previous_price = price,
                            price = ?, price_delta = 0, link = ?, last_seen_at = ?
                        where id = ?
                        """,
                        (
                            product["name"],
                            product["category"],
                            product["price"],
                            product["link"],
                            checked_at,
                            existing["id"],
                        ),
                    )
                    conn.commit()
                    continue
                conn.execute(
                    """
                    update products set
                        name = ?, category = ?, previous_price = ?, price = ?,
                        price_delta = ?, previous_stock = ?, stock = ?,
                        last_stock_alert_stock = ?, link = ?, is_active = 1,
                        missing_since = null, inactive_reason = null,
                        last_recheck_at = null, last_seen_at = ?
                    where id = ?
                    """,
                    (
                        product["name"],
                        product["category"],
                        previous_price,
                        product["price"],
                        price_delta,
                        previous_stock,
                        current_stock,
                        next_alert_stock,
                        product["link"],
                        checked_at,
                        existing["id"],
                    ),
                )
                product_id = existing["id"]
            else:
                cursor = conn.execute(
                    """
                    insert into products(
                        shop_id, goods_key, name, category, price, previous_price,
                        price_delta, stock, previous_stock, last_stock_alert_stock,
                        link, is_active, missing_since, inactive_reason,
                        last_recheck_at, last_seen_at, created_at
                    ) values(?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, 1, null, null, null, ?, ?)
                    """,
                    (
                        shop_id,
                        product["goods_key"],
                        product["name"],
                        product["category"],
                        product["price"],
                        product["price"],
                        current_stock,
                        current_stock,
                        product["link"],
                        checked_at,
                        checked_at,
                    ),
                )
                product_id = cursor.lastrowid

            conn.commit()
            if not purchaseable:
                with closing(db_connect()) as deactivate_conn:
                    deactivate_product(deactivate_conn, product_id, checked_at, purchase_reason, notify=False)
                    deactivate_conn.commit()
                continue
            if existing and previous_stock <= 0 < current_stock:
                record_restock_event(conn, shop_id, product_id, product, previous_stock, current_stock)
            if existing and previous_stock > 0 and current_stock == 0:
                record_sold_out_event(conn, shop_id, product_id, product, previous_stock)
            if existing and current_stock > 0 and alert_stock - current_stock >= STOCK_DROP_NOTIFY_THRESHOLD:
                queue_stock_drop_alert(conn, shop_id, product_id, alert_stock, current_stock)
                conn.execute(
                    "update products set last_stock_alert_stock = ? where id = ?",
                    (current_stock, product_id),
                )
                conn.commit()
            if existing and price_delta != 0:
                record_price_event(conn, shop_id, product_id, product, previous_price, current_price)

    with closing(db_connect()) as conn:
        if seen_keys or not remote_reports_products:
            deactivate_missing_products(conn, shop_id, seen_keys, checked_at, shop_link)
            last_error = None if seen_keys else EMPTY_SHOP_MESSAGE
        else:
            last_error = "远端显示有商品，但本次列表为空，已跳过下架判定"
        conn.execute(
            "update shops set last_checked_at = ?, last_error = ? where id = ?",
            (checked_at, last_error, shop_id),
        )
        conn.commit()


def check_enabled_shops(force=False, source="background"):
    if not CHECK_LOCK.acquire(blocking=False):
        return False
    try:
        update_monitor_status(checks_started=get_monitor_status()["checks_started"] + 1)
        checked_any = False
        with closing(db_connect()) as conn:
            shops = conn.execute("select * from shops where enabled = 1").fetchall()
        for shop in shops:
            try:
                due = force
                if not due:
                    due = True
                    if shop["last_checked_at"]:
                        last = datetime.strptime(shop["last_checked_at"], "%Y-%m-%d %H:%M:%S")
                        due = (datetime.now() - last).total_seconds() >= shop["interval_seconds"]
                if due:
                    check_shop(shop)
                    checked_any = True
            except Exception as exc:
                update_monitor_status(last_error=str(exc))
                with closing(db_connect()) as conn:
                    conn.execute(
                        "update shops set last_error = ? where id = ?",
                        (str(exc), shop["id"]),
                    )
                    conn.commit()
        flush_stock_drop_alerts(force=False)
        recheck_unpurchaseable_products(force=False)
        finished = get_monitor_status()["checks_finished"] + 1
        payload = {"checks_finished": finished, "last_heartbeat_at": now_text()}
        if checked_any:
            if source == "manual":
                payload["last_manual_check_at"] = now_text()
            else:
                payload["last_background_check_at"] = now_text()
        update_monitor_status(**payload)
        return checked_any
    finally:
        CHECK_LOCK.release()


class MonitorThread(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.stop_event = threading.Event()

    def run(self):
        update_monitor_status(started_at=now_text(), last_heartbeat_at=now_text())
        while not self.stop_event.is_set():
            update_monitor_status(last_heartbeat_at=now_text())
            check_enabled_shops(force=False, source="background")
            self.stop_event.wait(5)


def row_to_dict(row):
    return dict(row) if row else None


def api_summary():
    with closing(db_connect()) as conn:
        shops = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select
                    s.*,
                    coalesce(sum(case when coalesce(p.is_active, 1) = 1 then 1 else 0 end), 0) as active_product_count,
                    coalesce(sum(case when coalesce(p.is_active, 1) = 0 then 1 else 0 end), 0) as inactive_product_count,
                    count(p.id) as product_count
                from shops s
                left join products p on p.shop_id = s.id
                group by s.id
                order by s.id desc
                """
            )
        ]
        products = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select
                    p.*,
                    s.name as shop_name,
                    s.token as shop_token,
                    s.link as shop_link
                from products p
                join shops s on s.id = p.shop_id
                order by p.is_active desc, p.stock asc, p.last_seen_at desc
                """
            )
        ]
        events = [
            row_to_dict(row)
            for row in conn.execute(
                """
                select
                    e.*,
                    s.name as shop_name,
                    s.link as shop_link,
                    p.name as product_name,
                    case
                        when p.id is null or coalesce(p.is_active, 1) = 0 then s.link
                        else p.link
                    end as product_link,
                    coalesce(p.is_active, 0) as product_is_active
                from events e
                join shops s on s.id = e.shop_id
                left join products p on p.id = e.product_id
                order by e.id desc
                limit 50
                """
            )
        ]
        smtp_port = setting(conn, "smtp_port", "587")
        smtp_security = normalize_smtp_security(
            smtp_port,
            setting(
                conn,
                "smtp_security",
                "starttls" if setting(conn, "smtp_tls", "1") == "1" else "none",
            ),
        )
        settings = {
            "email_enabled": setting(conn, "email_enabled", "0"),
            "smtp_host": setting(conn, "smtp_host", ""),
            "smtp_port": smtp_port,
            "smtp_user": setting(conn, "smtp_user", ""),
            "mail_from": setting(conn, "mail_from", ""),
            "mail_to": setting(conn, "mail_to", ""),
            "smtp_security": smtp_security,
            "smtp_tls": setting(conn, "smtp_tls", "1"),
            "purchase_check_enabled": setting(conn, "purchase_check_enabled", "0"),
            "unpurchaseable_recheck_seconds": str(UNPURCHASEABLE_RECHECK_SECONDS),
        }
    return {
        "shops": shops,
        "products": products,
        "events": events,
        "settings": settings,
        "monitor": get_monitor_status(),
    }


def html_page(initial_data):
    initial_json = json.dumps(initial_data, ensure_ascii=False).replace("</", "<\\/")
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>库存监控</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d2433;
      --muted: #6b7280;
      --line: #d8dde6;
      --accent: #0f766e;
      --bad: #b91c1c;
      --good: #047857;
      --warn: #b45309;
      --blue: #0f5f9e;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 24px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    main { width: min(1280px, calc(100% - 32px)); margin: 18px auto 40px; }
    .grid { display: grid; grid-template-columns: 360px 1fr; gap: 16px; align-items: start; }
    section, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    section { padding: 16px; margin-bottom: 16px; }
    h2 { margin: 0 0 12px; font-size: 15px; }
    label { display: block; color: var(--muted); font-size: 12px; margin: 10px 0 6px; }
    input, select {
      width: 100%; height: 36px; border: 1px solid var(--line); border-radius: 6px;
      padding: 0 10px; background: #fff; color: var(--text);
    }
    button {
      height: 36px; border: 1px solid var(--accent); border-radius: 6px; background: var(--accent);
      color: white; padding: 0 12px; cursor: pointer; font-weight: 600; white-space: nowrap;
    }
    button.secondary { background: white; color: var(--accent); }
    button.danger { background: white; color: var(--bad); border-color: #f3b4b4; }
    button:disabled { opacity: .55; cursor: not-allowed; }
    .row { display: flex; gap: 8px; align-items: center; }
    .row > * { flex: 1; }
    .muted { color: var(--muted); }
    .status { min-height: 20px; color: var(--muted); margin-top: 10px; }
    .stats { display: grid; grid-template-columns: repeat(6, 1fr); gap: 10px; margin-bottom: 16px; }
    .stat { padding: 14px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .stat strong { display: block; font-size: 22px; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 10px 12px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }
    th { font-size: 12px; color: var(--muted); font-weight: 700; background: #fafafa; position: sticky; top: 0; z-index: 1; }
    a { color: var(--blue); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .badge { display: inline-flex; align-items: center; min-width: 46px; justify-content: center; height: 24px; padding: 0 8px; border-radius: 999px; font-weight: 700; }
    .badge.zero { color: var(--bad); background: #fee2e2; }
    .badge.good { color: var(--good); background: #d1fae5; }
    .badge.inactive { color: var(--muted); background: #e5e7eb; }
    tr.inactive { background: #fafafa; }
    tr.inactive td { color: var(--muted); }
    .price-up { color: var(--bad); font-weight: 700; }
    .price-down { color: var(--good); font-weight: 700; }
    .price-flat { color: var(--muted); }
    .priority-btn { height: 30px; border-color: var(--line); background: white; color: var(--muted); }
    .priority-btn.active { border-color: var(--warn); background: #fff7ed; color: var(--warn); }
    .toolbar { display: grid; grid-template-columns: minmax(220px, 1fr) 160px 150px 150px 120px 120px; gap: 8px; margin-bottom: 10px; }
    .scroll { max-height: 680px; overflow: auto; }
    .shop-item { display: grid; gap: 8px; padding: 10px 0; border-bottom: 1px solid var(--line); }
    .shop-item:last-child { border-bottom: 0; }
    .shop-actions { display: flex; gap: 8px; }
    .error { color: var(--bad); }
    .event { padding: 12px; border-bottom: 1px solid var(--line); white-space: pre-line; }
    .event:last-child { border-bottom: 0; }
    .event-type { display: inline-block; min-width: 58px; margin-right: 6px; color: var(--muted); }
    @media (max-width: 920px) {
      .grid { grid-template-columns: 1fr; }
      .stats { grid-template-columns: repeat(2, 1fr); }
      .toolbar { grid-template-columns: 1fr; }
      th { position: static; }
      header { position: static; align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <h1>库存监控</h1>
    <div class="row" style="max-width: 560px; width: 100%;">
      <select id="refreshInterval">
        <option value="0">不自动刷新</option>
        <option value="5000">每 5 秒刷新</option>
        <option value="15000" selected>每 15 秒刷新</option>
        <option value="30000">每 30 秒刷新</option>
        <option value="60000">每 1 分钟刷新</option>
      </select>
      <button class="secondary" id="refreshBtn">刷新</button>
      <button id="checkBtn">立即检查</button>
    </div>
  </header>

  <main>
    <div class="stats">
      <div class="stat"><span class="muted">店铺</span><strong id="shopCount">0</strong></div>
      <div class="stat"><span class="muted">商品</span><strong id="productCount">0</strong></div>
      <div class="stat"><span class="muted">有货</span><strong id="inStockCount">0</strong></div>
      <div class="stat"><span class="muted">价格变化</span><strong id="priceChangeCount">0</strong></div>
      <div class="stat"><span class="muted">未上架</span><strong id="inactiveCount">0</strong></div>
      <div class="stat"><span class="muted">后台心跳</span><strong id="monitorHeartbeat" style="font-size:13px;">-</strong></div>
    </div>

    <div class="grid">
      <aside>
        <section>
          <h2>添加店铺</h2>
          <label>店铺链接或 token</label>
          <input id="shopInput" placeholder="https://pay.ldxp.cn/shop/WPXSCE1B/">
          <label>检查间隔，秒</label>
          <input id="intervalInput" type="number" min="60" value="300">
          <div class="row" style="margin-top: 12px;"><button id="addShopBtn">添加并监控</button></div>
          <div class="status" id="addStatus"></div>
        </section>

        <section>
          <h2>邮件通知</h2>
          <label><input id="emailEnabled" type="checkbox" style="width:auto;height:auto;margin-right:6px;">开启邮件通知</label>
          <label>SMTP 服务器</label>
          <input id="smtpHost" placeholder="smtp.example.com">
          <div class="row">
            <div><label>端口</label><input id="smtpPort" type="number" value="587"></div>
            <div><label>加密方式</label><select id="smtpSecurity"><option value="starttls">STARTTLS / 587</option><option value="ssl">SSL / 465</option><option value="none">不加密</option></select></div>
          </div>
          <label>SMTP 用户名</label><input id="smtpUser">
          <label>SMTP 密码或授权码</label><input id="smtpPassword" type="password" autocomplete="new-password">
          <label>发件人</label><input id="mailFrom">
          <label>收件人</label><input id="mailTo">
          <div class="row" style="margin-top: 12px;">
            <button id="saveEmailBtn">保存邮件设置</button>
            <button class="secondary" id="testEmailBtn">测试</button>
          </div>
          <div class="status" id="emailStatus"></div>
        </section>

        <section>
          <h2>店铺</h2>
          <div id="shops"></div>
        </section>
      </aside>

      <div>
        <section>
          <h2>商品</h2>
          <div class="toolbar">
            <input id="filterInput" placeholder="搜索商品、分类、店铺">
            <select id="shopFilter"><option value="all">全部店铺</option></select>
            <select id="stockFilter">
              <option value="active" selected>当前上架</option>
              <option value="all">全部商品</option>
              <option value="zero">只看无货</option>
              <option value="in">只看有货</option>
              <option value="inactive">只看未上架</option>
            </select>
            <select id="priceSort">
              <option value="default">筛选后默认排序</option>
              <option value="asc">筛选后价格低到高</option>
              <option value="desc">筛选后价格高到低</option>
            </select>
            <input id="minPriceInput" type="number" min="0" step="0.01" placeholder="最低价">
            <input id="maxPriceInput" type="number" min="0" step="0.01" placeholder="最高价">
          </div>
          <div class="muted" id="productResultInfo" style="margin-bottom: 10px;">-</div>
          <div class="row" style="margin-bottom: 10px; align-items: center;">
            <label style="margin:0; flex: 0 0 auto;"><input id="purchaseCheckEnabled" type="checkbox" style="width:auto;height:auto;margin-right:6px;">自动关闭不可购买连接</label>
            <button class="secondary" id="savePurchaseCheckBtn" style="flex:0 0 auto;">保存开关</button>
            <button class="danger" id="closeUnpurchaseableBtn" style="flex:0 0 auto;">一键关闭不可购买连接</button>
            <button class="secondary" id="recheckUnpurchaseableBtn" style="flex:0 0 auto;">立即复查已关闭连接</button>
            <span class="status" id="purchaseCheckStatus" style="margin:0;"></span>
          </div>
          <div class="muted" id="purchaseCheckHint" style="margin-bottom: 10px;"></div>
          <div class="scroll panel">
            <table>
              <thead>
                <tr>
                  <th>商品</th><th>店铺</th><th>分类</th><th>价格</th><th>价格变化</th><th>库存</th><th>状态</th><th>监控级别</th><th>更新时间</th>
                </tr>
              </thead>
              <tbody id="products"></tbody>
            </table>
          </div>
        </section>

        <section>
          <h2>事件</h2>
          <div class="panel" id="events"></div>
        </section>
      </div>
    </div>
  </main>

  <script>
    let data = __INITIAL_DATA__;
    let refreshTimer = null;
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      return new Promise((resolve, reject) => {
        if (typeof XMLHttpRequest === "undefined") {
          reject(new Error("当前浏览器不支持自动刷新请求，请手动刷新页面"));
          return;
        }
        const xhr = new XMLHttpRequest();
        xhr.open(options.method || "GET", path, true);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.onload = () => {
          let body = {};
          try { body = JSON.parse(xhr.responseText || "{}"); }
          catch (err) { reject(new Error("接口返回不是 JSON")); return; }
          if (xhr.status < 200 || xhr.status >= 300 || body.error) {
            reject(new Error(body.error || xhr.statusText || "请求失败"));
          } else {
            resolve(body);
          }
        };
        xhr.onerror = () => reject(new Error("网络请求失败"));
        xhr.send(options.body || null);
      });
    }

    async function load() {
      try {
        data = await api("/api/summary");
        render();
      } catch (err) {
        $("events").innerHTML = `<div class="event error">${escapeHtml(err.message)}</div>`;
      }
    }

    function render() {
      const selectedShop = $("shopFilter").value || "all";
      const activeProducts = data.products.filter(p => Number(p.is_active ?? 1) === 1);
      const inactiveProducts = data.products.filter(p => Number(p.is_active ?? 1) === 0);
      $("shopCount").textContent = data.shops.length;
      $("productCount").textContent = activeProducts.length;
      $("inStockCount").textContent = activeProducts.filter(p => p.stock > 0).length;
      $("priceChangeCount").textContent = activeProducts.filter(p => Number(p.price_delta || 0) !== 0).length;
      $("inactiveCount").textContent = inactiveProducts.length;
      $("monitorHeartbeat").textContent = data.monitor?.last_heartbeat_at || "未启动";

      const settings = data.settings || {};
      $("smtpHost").value = settings.smtp_host || "";
      $("smtpPort").value = settings.smtp_port || "587";
      $("smtpUser").value = settings.smtp_user || "";
      $("mailFrom").value = settings.mail_from || "";
      $("mailTo").value = settings.mail_to || "";
      $("smtpSecurity").value = settings.smtp_security || (settings.smtp_tls === "0" ? "none" : "starttls");
      $("emailEnabled").checked = settings.email_enabled === "1";
      $("purchaseCheckEnabled").checked = settings.purchase_check_enabled === "1";
      $("purchaseCheckHint").textContent = `被一键关闭的不可购买连接会在后台约每 ${Math.round(Number(settings.unpurchaseable_recheck_seconds || 600) / 60)} 分钟复查一次；恢复可购买后会重新回到前台并发送恢复邮件。关闭期间不参与邮件提醒。`;

      $("shopFilter").innerHTML = `<option value="all">全部店铺</option>` + data.shops.map(shop =>
        `<option value="${shop.id}">${escapeHtml(shop.name)}</option>`
      ).join("");
      if (["all", ...data.shops.map(s => String(s.id))].includes(selectedShop)) $("shopFilter").value = selectedShop;

      $("shops").innerHTML = data.shops.map(shop => `
        <div class="shop-item">
          <strong><a href="${escapeAttr(shop.link)}" target="_blank" rel="noreferrer">${escapeHtml(shop.name)}</a></strong>
          <span class="muted">${escapeHtml(shop.token)} · ${shop.enabled ? "监控中" : "已停用"} · ${shop.interval_seconds}s</span>
          <span class="muted">上架 ${shop.active_product_count || 0} · 未上架 ${shop.inactive_product_count || 0}</span>
          <span class="muted">上次检查：${escapeHtml(shop.last_checked_at || "尚未检查")}</span>
          ${shop.last_error ? `<span class="error">${escapeHtml(shop.last_error)}</span>` : ""}
          <div class="shop-actions">
            <input data-shop-interval="${shop.id}" type="number" min="60" value="${shop.interval_seconds}" title="后端检查间隔，秒">
            <button class="secondary" data-save-shop-interval="${shop.id}">保存间隔</button>
            <button class="danger" data-delete-shop="${shop.id}">删除店铺</button>
          </div>
        </div>
      `).join("") || `<div class="muted">还没有店铺</div>`;

      document.querySelectorAll("[data-delete-shop]").forEach(btn => {
        btn.onclick = () => deleteShop(btn.getAttribute("data-delete-shop"));
      });
      document.querySelectorAll("[data-save-shop-interval]").forEach(btn => {
        btn.onclick = () => saveShopInterval(btn.getAttribute("data-save-shop-interval"));
      });

      renderProducts();
      renderEvents();
    }

    function renderProducts() {
      const q = $("filterInput").value.trim().toLowerCase();
      const stockFilter = $("stockFilter").value;
      const shopFilter = $("shopFilter").value;
      const priceSort = $("priceSort").value;
      const minPrice = $("minPriceInput").value === "" ? null : Number($("minPriceInput").value);
      const maxPrice = $("maxPriceInput").value === "" ? null : Number($("maxPriceInput").value);
      const rows = data.products.filter(p => {
        const hay = `${p.name} ${p.category} ${p.shop_name} ${p.goods_key}`.toLowerCase();
        const active = Number(p.is_active ?? 1) === 1;
        const price = Number(p.price || 0);
        if (shopFilter !== "all" && String(p.shop_id) !== shopFilter) return false;
        if (q && !hay.includes(q)) return false;
        if (stockFilter === "active" && !active) return false;
        if (stockFilter === "inactive" && active) return false;
        if (stockFilter === "zero" && (!active || p.stock > 0)) return false;
        if (stockFilter === "in" && (!active || p.stock <= 0)) return false;
        if (minPrice !== null && Number.isFinite(minPrice) && price < minPrice) return false;
        if (maxPrice !== null && Number.isFinite(maxPrice) && price > maxPrice) return false;
        return true;
      });
      if (priceSort === "asc") {
        rows.sort((a, b) => Number(a.price || 0) - Number(b.price || 0));
      } else if (priceSort === "desc") {
        rows.sort((a, b) => Number(b.price || 0) - Number(a.price || 0));
      }
      $("productResultInfo").textContent = `当前筛选结果 ${rows.length} 个商品${priceSort === "asc" ? "，已按价格从低到高排序" : priceSort === "desc" ? "，已按价格从高到低排序" : ""}`;

      $("products").innerHTML = rows.map(p => {
        const active = Number(p.is_active ?? 1) === 1;
        const targetLink = active ? p.link : p.shop_link;
        return `
        <tr class="${active ? "" : "inactive"}">
          <td><a href="${escapeAttr(targetLink)}" target="_blank" rel="noreferrer">${escapeHtml(p.name)}</a><br><span class="muted">${escapeHtml(p.goods_key)}</span></td>
          <td>${escapeHtml(p.shop_name)}</td>
          <td>${escapeHtml(p.category || "")}</td>
          <td>${Number(p.price || 0).toFixed(2)}</td>
          <td>${formatPriceDelta(p)}</td>
          <td><span class="badge ${active ? (p.stock > 0 ? "good" : "zero") : "inactive"}">${active ? p.stock : "未上架"}</span></td>
          <td>${active ? "上架" : `未上架${p.missing_since ? " · " + escapeHtml(p.missing_since) : ""}`}</td>
          <td><button class="priority-btn ${p.priority ? "active" : ""}" data-priority-product="${p.id}" data-priority-value="${p.priority ? 0 : 1}">${p.priority ? "重点监控" : "非重点"}</button></td>
          <td>${escapeHtml(p.last_seen_at || "")}</td>
        </tr>
      `}).join("") || `<tr><td colspan="9" class="muted">没有匹配商品</td></tr>`;
      document.querySelectorAll("[data-priority-product]").forEach(btn => {
        btn.onclick = () => togglePriority(btn.getAttribute("data-priority-product"), btn.getAttribute("data-priority-value"));
      });
    }

    function renderEvents() {
      $("events").innerHTML = data.events.map(event => `
        <div class="event">
          <span class="event-type">${eventLabel(event.event_type)}</span><strong>${escapeHtml(event.shop_name)}</strong> ? <span class="muted">${escapeHtml(event.created_at)}</span>
          <br>${formatEventMessage(event.message)}
          ${event.product_link ? `<br><a href="${escapeAttr(event.product_link)}" target="_blank" rel="noreferrer">打开商品</a>` : ""}
        </div>
      `).join("") || `<div class="event muted">还没有事件</div>`;
    }

    function formatPriceDelta(p) {
      const delta = Number(p.price_delta || 0);
      if (delta > 0) return `<span class="price-up">上涨 ${delta.toFixed(2)}</span>`;
      if (delta < 0) return `<span class="price-down">降低 ${Math.abs(delta).toFixed(2)}</span>`;
      return `<span class="price-flat">无变化</span>`;
    }

    function eventLabel(type) {
      if (type === "restock") return "补货";
      if (type === "price_up") return "涨价";
      if (type === "price_down") return "降价";
      if (type === "stock_drop") return "库存减少";
      if (type === "sold_out") return "售罄";
      if (type === "unlisted") return "未上架";
      if (type === "purchaseable") return "恢复";
      return "事件";
    }

    function formatEventMessage(message) {
      return escapeHtml(message).replace("【重点通知】", '<strong class="price-up">【重点通知】</strong>');
    }

    async function deleteShop(id) {
      const shop = data.shops.find(s => String(s.id) === String(id));
      if (!confirm(`确定删除店铺「${shop ? shop.name : id}」吗？该店铺的商品监控会停止。`)) return;
      await api(`/api/shops/${id}/delete`, { method: "POST", body: JSON.stringify({}) });
      await load();
    }

    async function saveShopInterval(id) {
      const input = document.querySelector(`[data-shop-interval="${id}"]`);
      const interval_seconds = Math.max(Number(input?.value || 300), 60);
      await api(`/api/shops/${id}/interval`, { method: "POST", body: JSON.stringify({ interval_seconds }) });
      await load();
    }

    async function togglePriority(id, value) {
      const priority = Number(value) ? 1 : 0;
      const product = data.products.find(p => String(p.id) === String(id));
      if (product) product.priority = priority;
      renderProducts();
      try {
        await api(`/api/products/${id}/priority`, { method: "POST", body: JSON.stringify({ priority }) });
        await load();
      } catch (err) {
        alert(err.message);
        await load();
      }
    }

    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    function escapeAttr(value) { return escapeHtml(value); }

    function updateRefreshTimer() {
      if (refreshTimer) clearInterval(refreshTimer);
      const ms = Number($("refreshInterval").value || 0);
      localStorage.setItem("stockRefreshMs", String(ms));
      if (ms > 0) refreshTimer = setInterval(load, ms);
    }

    $("refreshBtn").onclick = load;
    $("filterInput").oninput = renderProducts;
    $("stockFilter").onchange = renderProducts;
    $("shopFilter").onchange = renderProducts;
    $("priceSort").onchange = renderProducts;
    $("minPriceInput").oninput = renderProducts;
    $("maxPriceInput").oninput = renderProducts;
    $("refreshInterval").onchange = updateRefreshTimer;

    $("savePurchaseCheckBtn").onclick = async () => {
      $("purchaseCheckStatus").textContent = "正在保存...";
      try {
        await api("/api/settings/purchase-check", {
          method: "POST",
          body: JSON.stringify({ enabled: $("purchaseCheckEnabled").checked ? "1" : "0" })
        });
        $("purchaseCheckStatus").textContent = "已保存";
        await load();
      } catch (err) {
        $("purchaseCheckStatus").textContent = err.message;
      }
    };

    $("closeUnpurchaseableBtn").onclick = async () => {
      if (!confirm("确定扫描当前所有上架商品，并关闭点进去显示未上架或不可购买的连接吗？")) return;
      $("closeUnpurchaseableBtn").disabled = true;
      $("purchaseCheckStatus").textContent = "正在扫描...";
      try {
        const result = await api("/api/products/close-unpurchaseable", { method: "POST", body: JSON.stringify({}) });
        $("purchaseCheckStatus").textContent = `已扫描 ${result.checked} 个，关闭 ${result.closed} 个`;
        await load();
      } catch (err) {
        $("purchaseCheckStatus").textContent = err.message;
      } finally {
        $("closeUnpurchaseableBtn").disabled = false;
      }
    };

    $("recheckUnpurchaseableBtn").onclick = async () => {
      $("recheckUnpurchaseableBtn").disabled = true;
      $("purchaseCheckStatus").textContent = "正在复查...";
      try {
        const result = await api("/api/products/recheck-unpurchaseable", { method: "POST", body: JSON.stringify({}) });
        $("purchaseCheckStatus").textContent = `已复查 ${result.checked} 个，恢复 ${result.restored} 个`;
        await load();
      } catch (err) {
        $("purchaseCheckStatus").textContent = err.message;
      } finally {
        $("recheckUnpurchaseableBtn").disabled = false;
      }
    };

    $("addShopBtn").onclick = async () => {
      $("addStatus").textContent = "正在添加...";
      try {
        await api("/api/shops", { method: "POST", body: JSON.stringify({ shop: $("shopInput").value, interval_seconds: $("intervalInput").value }) });
        $("addStatus").textContent = "已添加，正在刷新数据";
        $("shopInput").value = "";
        await api("/api/check", { method: "POST", body: JSON.stringify({}) });
        await load();
      } catch (err) { $("addStatus").textContent = err.message; }
    };

    $("checkBtn").onclick = async () => {
      $("checkBtn").disabled = true;
      try { await api("/api/check", { method: "POST", body: JSON.stringify({}) }); await load(); }
      catch (err) { alert(err.message); }
      finally { $("checkBtn").disabled = false; }
    };

    $("saveEmailBtn").onclick = async () => {
      $("emailStatus").textContent = "正在保存...";
      try { await api("/api/settings/email", { method: "POST", body: JSON.stringify(readEmailForm(false)) }); $("emailStatus").textContent = "已保存"; await load(); }
      catch (err) { $("emailStatus").textContent = err.message; }
    };

    $("testEmailBtn").onclick = async () => {
      $("emailStatus").textContent = "正在发送测试邮件...";
      try { await api("/api/settings/email/test", { method: "POST", body: JSON.stringify(readEmailForm(true)) }); $("emailStatus").textContent = "测试邮件已发送"; }
      catch (err) { $("emailStatus").textContent = err.message; }
    };

    function readEmailForm(enable) {
      return {
        email_enabled: $("emailEnabled").checked || enable ? "1" : "0",
        smtp_host: $("smtpHost").value,
        smtp_port: $("smtpPort").value,
        smtp_user: $("smtpUser").value,
        smtp_password: $("smtpPassword").value,
        mail_from: $("mailFrom").value,
        mail_to: $("mailTo").value,
        smtp_security: $("smtpSecurity").value,
        smtp_tls: $("smtpSecurity").value === "none" ? "0" : "1"
      };
    }

    const savedRefresh = localStorage.getItem("stockRefreshMs");
    if (savedRefresh) $("refreshInterval").value = savedRefresh;
    updateRefreshTimer();
    render();
  </script>
</body>
</html>""".replace("__INITIAL_DATA__", initial_json)


class AppHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            body = html_page(api_summary()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path == "/api/summary":
            self.send_json(api_summary())
            return
        self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        try:
            parsed = urllib.parse.urlparse(self.path)
            body = self.read_json()
            if parsed.path == "/api/shops":
                token = add_shop(body.get("shop"), body.get("interval_seconds", DEFAULT_INTERVAL_SECONDS))
                self.send_json({"ok": True, "token": token})
                return
            match = re.fullmatch(r"/api/shops/(\d+)/delete", parsed.path)
            if match:
                with closing(db_connect()) as conn:
                    conn.execute("delete from shops where id = ?", (int(match.group(1)),))
                    conn.commit()
                self.send_json({"ok": True})
                return
            match = re.fullmatch(r"/api/shops/(\d+)/interval", parsed.path)
            if match:
                interval_seconds = max(int(body.get("interval_seconds") or DEFAULT_INTERVAL_SECONDS), 60)
                with closing(db_connect()) as conn:
                    conn.execute(
                        "update shops set interval_seconds = ? where id = ?",
                        (interval_seconds, int(match.group(1))),
                    )
                    conn.commit()
                self.send_json({"ok": True, "interval_seconds": interval_seconds})
                return
            match = re.fullmatch(r"/api/products/(\d+)/priority", parsed.path)
            if match:
                priority = 1 if body.get("priority") else 0
                with closing(db_connect()) as conn:
                    conn.execute(
                        "update products set priority = ? where id = ?",
                        (priority, int(match.group(1))),
                    )
                    conn.commit()
                self.send_json({"ok": True, "priority": priority})
                return
            if parsed.path == "/api/products/close-unpurchaseable":
                result = close_unpurchaseable_products()
                self.send_json({"ok": True, **result})
                return
            if parsed.path == "/api/products/recheck-unpurchaseable":
                result = recheck_unpurchaseable_products(force=True)
                self.send_json({"ok": True, **result})
                return
            if parsed.path == "/api/check":
                checked_any = check_enabled_shops(force=True, source="manual")
                self.send_json({"ok": True, "checked_any": checked_any})
                return
            if parsed.path == "/api/settings/purchase-check":
                with closing(db_connect()) as conn:
                    enabled = "1" if str(body.get("enabled", "0")) == "1" else "0"
                    set_setting(conn, "purchase_check_enabled", enabled)
                    conn.commit()
                self.send_json({"ok": True, "enabled": enabled})
                return
            if parsed.path == "/api/settings/email":
                self.save_email_settings(body)
                self.send_json({"ok": True})
                return
            if parsed.path == "/api/settings/email/test":
                self.save_email_settings(body)
                notify_email("库存监控测试邮件", f"这是一封测试邮件。\n发送时间：{now_text()}")
                self.send_json({"ok": True})
                return
            self.send_json({"error": "not found"}, 404)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 400)

    def save_email_settings(self, body):
        with closing(db_connect()) as conn:
            security = normalize_smtp_security(body.get("smtp_port", "587"), body.get("smtp_security", "starttls"))
            for key in [
                "email_enabled",
                "smtp_host",
                "smtp_port",
                "smtp_user",
                "mail_from",
                "mail_to",
                "smtp_tls",
            ]:
                set_setting(conn, key, str(body.get(key, "")))
            set_setting(conn, "smtp_security", security)
            set_setting(conn, "smtp_tls", "0" if security == "none" else "1")
            if body.get("smtp_password"):
                set_setting(conn, "smtp_password", str(body.get("smtp_password", "")))
            conn.commit()


def bootstrap_default_shop():
    if not DEFAULT_SHOP_URL:
        return
    with closing(db_connect()) as conn:
        count = conn.execute("select count(*) as count from shops").fetchone()["count"]
    if count == 0:
        add_shop(DEFAULT_SHOP_URL, DEFAULT_INTERVAL_SECONDS)


def run(host="127.0.0.1", port=8765):
    init_db()
    bootstrap_default_shop()
    monitor = MonitorThread()
    monitor.start()
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"库存监控页面：http://{host}:{port}", flush=True)
    print("按 Ctrl+C 停止服务", flush=True)
    try:
        server.serve_forever()
    finally:
        monitor.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8765")),
    )
