#!/usr/bin/env python3
import os
import sqlite3
import sys
import tempfile
from contextlib import closing


sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def assert_equal(actual, expected, message):
    if actual != expected:
        raise AssertionError(f"{message}: expected {expected!r}, got {actual!r}")


def main():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["DB_PATH"] = os.path.join(tmpdir, "email_digest.sqlite3")
        import app

        sent_messages = []

        def fake_notify(subject, body):
            sent_messages.append((subject, body))
            return True

        app.notify_email = fake_notify
        app.init_db()

        with app.closing(app.db_connect()) as conn:
            app.set_setting(conn, "email_enabled", "1")
            app.set_setting(conn, "email_digest_seconds", "120")
            conn.execute(
                "insert into shops(token, name, link, enabled, interval_seconds, created_at) values(?, ?, ?, 1, 60, ?)",
                ("SHOP1", "店铺一", "http://shop-1", app.now_text()),
            )
            shop1 = conn.execute("select last_insert_rowid()").fetchone()[0]
            conn.execute(
                "insert into shops(token, name, link, enabled, interval_seconds, created_at) values(?, ?, ?, 1, 60, ?)",
                ("SHOP2", "店铺二", "http://shop-2", app.now_text()),
            )
            shop2 = conn.execute("select last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                insert into products(
                    shop_id, goods_key, name, category, price, previous_price,
                    price_delta, stock, previous_stock, last_stock_alert_stock,
                    link, is_active, last_seen_at, created_at
                ) values(?, 'p1', '商品一', '默认', 10, 10, 0, 3, 0, 3, 'http://item-1', 1, ?, ?)
                """,
                (shop1, app.now_text(), app.now_text()),
            )
            product1 = conn.execute("select last_insert_rowid()").fetchone()[0]
            conn.execute(
                """
                insert into products(
                    shop_id, goods_key, name, category, price, previous_price,
                    price_delta, stock, previous_stock, last_stock_alert_stock,
                    link, is_active, last_seen_at, created_at
                ) values(?, 'p2', '商品二', '默认', 20, 20, 0, 8, 9, 8, 'http://item-2', 1, ?, ?)
                """,
                (shop2, app.now_text(), app.now_text()),
            )
            product2 = conn.execute("select last_insert_rowid()").fetchone()[0]
            conn.commit()

            app.record_event(conn, shop1, product1, "restock", "商品一 已补货\n链接：http://item-1", 0, 3, "补货提醒")
            app.record_event(conn, shop2, product2, "stock_drop", "商品二 库存减少 5\n链接：http://item-2", 13, 8, "库存减少提醒")

        with closing(sqlite3.connect(os.environ["DB_PATH"])) as conn:
            assert_equal(conn.execute("select count(*) from events where emailed_at is null").fetchone()[0], 2, "events should be queued")
        assert_equal(sent_messages, [], "record_event should not send immediately")

        result = app.flush_email_digest(force=True)
        assert_equal(result["sent"], True, "digest should send")
        assert_equal(result["count"], 2, "digest should include both events")
        assert_equal(len(sent_messages), 1, "digest should send one email")
        subject, body = sent_messages[0]
        assert "2 条提醒" in subject
        assert "店铺一" in body
        assert "店铺二" in body
        assert "商品一" in body
        assert "商品二" in body

        with closing(sqlite3.connect(os.environ["DB_PATH"])) as conn:
            assert_equal(conn.execute("select count(*) from events where emailed_at is not null").fetchone()[0], 2, "events should be marked emailed")

    print("email_digest_test: ok")


if __name__ == "__main__":
    main()
