#!/usr/bin/env python3
import os
import sys
import tempfile
from contextlib import closing
from unittest import mock


ROOT = os.path.dirname(os.path.dirname(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app


def main():
    original_db_path = app.DB_PATH
    with tempfile.TemporaryDirectory() as tmpdir:
        app.DB_PATH = os.path.join(tmpdir, "upstream-safety.sqlite3")
        app.init_db()
        with closing(app.db_connect()) as conn:
            conn.execute(
                """
                insert into shops(token, name, link, enabled, interval_seconds, created_at)
                values('SAFE', 'Safety test', 'https://example.test/shop/SAFE', 1, 300, ?)
                """,
                (app.now_text(),),
            )
            app.set_setting(conn, "purchase_check_enabled", "1")
            conn.commit()
            shop = conn.execute("select * from shops where token = 'SAFE'").fetchone()

        products = [
            {
                "goods_key": f"item-{index}",
                "name": f"Item {index}",
                "category": "Default",
                "price": 10 + index,
                "stock": 5,
                "link": f"https://example.test/item/{index}",
            }
            for index in range(3)
        ]
        shop_info = {
            "nickname": "Safety test",
            "link": "https://example.test/shop/SAFE",
            "goods_count": len(products),
        }
        with (
            mock.patch.object(app, "fetch_shop_info", return_value=shop_info),
            mock.patch.object(app, "iter_remote_products", return_value=iter(products)),
            mock.patch.object(
                app,
                "product_is_purchaseable",
                side_effect=AssertionError("regular shop refresh must not fetch product details"),
            ),
        ):
            app.check_shop(shop)

        with closing(app.db_connect()) as conn:
            assert conn.execute("select count(*) from products").fetchone()[0] == 3

        with mock.patch.object(app, "product_is_purchaseable", return_value=(True, None)) as check:
            result = app.validate_next_active_purchase_link()
        assert result["checked"] == 1
        assert check.call_count == 1
        with closing(app.db_connect()) as conn:
            checked = conn.execute(
                "select count(*) from products where last_purchase_check_at is not null"
            ).fetchone()[0]
            assert checked == 1
            conn.execute(
                """
                update products set
                    is_active = 0,
                    inactive_reason = 'unpurchaseable',
                    last_recheck_at = null
                """
            )
            conn.commit()

        recovered_info = {
            "status": 1,
            "name": "Recovered item",
            "price": 10,
            "link": "https://example.test/item/recovered",
            "category": {"name": "Default"},
            "extend": {"stock_count": 5},
        }
        with mock.patch.object(app, "fetch_goods_info", return_value=recovered_info) as fetch:
            result = app.recheck_unpurchaseable_products(force=True, limit=1)
        assert result["checked"] == 1
        assert result["restored"] == 1
        assert fetch.call_count == 1
        with closing(app.db_connect()) as conn:
            assert conn.execute("select count(*) from products where is_active = 1").fetchone()[0] == 1

    app.DB_PATH = original_db_path
    print("upstream_safety_test: ok")


if __name__ == "__main__":
    main()
