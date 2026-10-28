import shutil
import sqlite3
import os
import re
import subprocess
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import app as app_module


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "catalog.db"
        shutil.copy2(app_module.BASE_DIR / "catalog.db", self.database)
        self.original_database = app_module.DATABASE
        self.original_upload_dir = app_module.UPLOAD_DIR
        app_module.DATABASE = self.database
        app_module.UPLOAD_DIR = Path(self.temp_dir.name) / "uploads"
        app_module.app.config.update(TESTING=True)
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.DATABASE = self.original_database
        app_module.UPLOAD_DIR = self.original_upload_dir
        self.temp_dir.cleanup()

    def csrf_headers(self):
        response = self.client.get("/")
        token = re.search(r'<meta name="csrf-token" content="([^"]+)"', response.get_data(as_text=True)).group(1)
        return {"X-CSRFToken": token}

    def login_as_user(self, user_id=None, client=None):
        client = client or self.client
        if user_id is None:
            connection = sqlite3.connect(self.database)
            user_id = connection.execute(
                "SELECT id FROM users WHERE role = 'user' ORDER BY id LIMIT 1"
            ).fetchone()[0]
            connection.close()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        return user_id

    def csrf_token(self, path="/", client=None):
        response = (client or self.client).get(path)
        return re.search(
            r'<meta name="csrf-token" content="([^"]+)"',
            response.get_data(as_text=True),
        ).group(1)

    def test_health_and_products(self):
        self.assertEqual(self.client.get("/api/health").status_code, 200)
        response = self.client.get("/api/products")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.get_json(), list)

    def create_order(self):
        product = self.client.get("/api/products").get_json()[0]
        response = self.client.post(
            "/api/orders",
            headers=self.csrf_headers(),
            json={
                "name": "Test Customer",
                "phone": "02012345678",
                "address": "Test address",
                "shipping_fee": 10000,
                "payment": "ໂອນ / QR",
                "items": [{"id": product["id"], "qty": 1}],
            },
        )
        return product, response

    def test_order_creates_items_and_reduces_stock(self):
        connection = sqlite3.connect(self.database)
        orders_before = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        items_before = connection.execute("SELECT COUNT(*) FROM order_items").fetchone()[0]
        payments_before = connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
        connection.close()

        product, response = self.create_order()
        self.assertEqual(response.status_code, 201)
        order = response.get_json()
        self.assertTrue(order["no"].startswith("078-"))

        connection = sqlite3.connect(self.database)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0], orders_before + 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM order_items").fetchone()[0], items_before + 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM payments").fetchone()[0], payments_before + 1)
        self.assertEqual(connection.execute("SELECT stock FROM products WHERE id = ?", (product["id"],)).fetchone()[0], product["stock"] - 1)
        connection.close()

    def test_payment_slip_upload(self):
        _, response = self.create_order()
        order_id = response.get_json()["id"]
        response = self.client.post(
            f"/api/orders/{order_id}/slip",
            headers=self.csrf_headers(),
            data={"slip": (BytesIO(b"test slip"), "receipt.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["filename"].endswith(".png"))

    def test_admin_pages_require_login(self):
        self.assertEqual(self.client.get("/admin/orders").status_code, 302)

    def test_account_profile_and_saved_addresses_are_user_scoped(self):
        user_id = self.login_as_user()
        response = self.client.get("/account")
        self.assertEqual(response.status_code, 200)
        token = self.csrf_token("/account")

        response = self.client.post(
            "/account",
            data={"csrf_token": token, "full_name": "Account Test", "phone": "02012345678"},
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/account/addresses",
            data={
                "csrf_token": token,
                "recipient_name": "Account Test",
                "recipient_phone": "02012345678",
                "address_line": "Test saved address",
                "district": "Test district",
                "province": "Vientiane",
                "is_default": "1",
            },
        )
        self.assertEqual(response.status_code, 302)

        connection = sqlite3.connect(self.database)
        customer_id = connection.execute(
            "SELECT id FROM customers WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        address = connection.execute(
            "SELECT customer_id, address_line, is_default FROM addresses WHERE customer_id = ?",
            (customer_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(address, (customer_id, "Test saved address", 1))

        home = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="savedAddress"', home)
        self.assertIn("Test saved address", home)

        other_client = app_module.app.test_client()
        connection = sqlite3.connect(self.database)
        other_user = connection.execute(
            "SELECT id FROM users WHERE role = 'user' AND id != ? ORDER BY id LIMIT 1",
            (user_id,),
        ).fetchone()[0]
        address_id = address and connection.execute(
            "SELECT id FROM addresses WHERE customer_id = ?", (customer_id,)
        ).fetchone()[0]
        connection.close()
        self.login_as_user(other_user, other_client)
        other_token = self.csrf_token("/account", other_client)
        forbidden = other_client.post(
            f"/account/addresses/{address_id}/delete",
            data={"csrf_token": other_token},
        )
        self.assertEqual(forbidden.status_code, 404)

    def test_order_history_is_scoped_and_includes_real_items(self):
        self.login_as_user()
        product, response = self.create_order()
        self.assertEqual(response.status_code, 201)
        history = self.client.get("/orders")
        self.assertEqual(history.status_code, 200)
        self.assertIn(product["name"], history.get_data(as_text=True))

    def test_account_requires_login(self):
        self.assertEqual(self.client.get("/account").status_code, 302)

    def test_csrf_blocks_state_changing_api_without_token(self):
        response = self.client.post("/api/orders", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("CSRF", response.get_json()["error"])

    def test_slip_upload_rejects_other_browser_session(self):
        _, response = self.create_order()
        order_id = response.get_json()["id"]
        other_client = app_module.app.test_client()
        csrf = re.search(
            r'<meta name="csrf-token" content="([^"]+)"',
            other_client.get("/").get_data(as_text=True),
        ).group(1)
        response = other_client.post(
            f"/api/orders/{order_id}/slip",
            headers={"X-CSRFToken": csrf},
            data={"slip": (BytesIO(b"not-owner"), "receipt.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 403)

    def webhook_event(self, event_type, order_id, event_id="evt_test"):
        return {
            "id": event_id,
            "type": event_type,
            "data": {"object": {
                "id": "cs_test_123",
                "payment_intent": "pi_test_123",
                "metadata": {"order_id": str(order_id)},
            }},
        }

    def post_webhook(self, event):
        with patch.object(app_module.stripe.Webhook, "construct_event", return_value=event):
            return self.client.post(
                "/api/stripe/webhook",
                data=b"signed-payload",
                headers={"Stripe-Signature": "test-signature"},
            )

    def test_webhook_signature_and_duplicate_delivery_are_safe(self):
        old_secret = app_module.STRIPE_WEBHOOK_SECRET
        app_module.STRIPE_WEBHOOK_SECRET = "whsec_test_only"
        try:
            invalid = self.client.post("/api/stripe/webhook", data=b"bad")
            # The real Stripe verifier rejects this before any DB work.
            self.assertEqual(invalid.status_code, 400)
            _, response = self.create_order()
            order_id = response.get_json()["id"]
            event = self.webhook_event("checkout.session.completed", order_id)
            self.assertEqual(self.post_webhook(event).status_code, 200)
            self.assertEqual(self.post_webhook(event).status_code, 200)
            connection = sqlite3.connect(self.database)
            row = connection.execute(
                "SELECT payment_status, order_status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            connection.close()
            self.assertEqual(row, ("paid", "confirmed"))
        finally:
            app_module.STRIPE_WEBHOOK_SECRET = old_secret

    def test_expired_checkout_returns_stock_once(self):
        product, response = self.create_order()
        order_id = response.get_json()["id"]
        event = self.webhook_event("checkout.session.expired", order_id, "evt_expired")
        old_secret = app_module.STRIPE_WEBHOOK_SECRET
        app_module.STRIPE_WEBHOOK_SECRET = "whsec_test_only"
        try:
            self.assertEqual(self.post_webhook(event).status_code, 200)
            self.assertEqual(self.post_webhook(event).status_code, 200)
        finally:
            app_module.STRIPE_WEBHOOK_SECRET = old_secret
        connection = sqlite3.connect(self.database)
        stock, returned = connection.execute(
            "SELECT p.stock, COUNT(*) FROM products p JOIN stock_movements sm ON sm.product_id=p.id "
            "WHERE p.id=? AND sm.order_id=? AND sm.movement_type='return'",
            (product["id"], order_id),
        ).fetchone()
        status = connection.execute(
            "SELECT order_status, payment_status FROM orders WHERE id=?", (order_id,)
        ).fetchone()
        connection.close()
        self.assertEqual(stock, product["stock"])
        self.assertEqual(returned, 1)
        self.assertEqual(status, ("cancelled", "failed"))

    def test_stripe_session_failure_releases_reservation(self):
        product = self.client.get("/api/products").get_json()[0]
        old_key = app_module.STRIPE_SECRET_KEY
        app_module.STRIPE_SECRET_KEY = "sk_test_only"
        try:
            with patch.object(
                app_module.stripe.checkout.Session,
                "create",
                side_effect=app_module.stripe.error.StripeError("test failure"),
            ):
                response = self.client.post(
                    "/api/orders",
                    headers=self.csrf_headers(),
                    json={
                        "name": "Test Customer",
                        "phone": "02012345678",
                        "address": "Test address",
                        "shipping_fee": 10000,
                        "payment": "Stripe Test",
                        "items": [{"id": product["id"], "qty": 1}],
                    },
                )
            self.assertEqual(response.status_code, 502)
        finally:
            app_module.STRIPE_SECRET_KEY = old_key
        connection = sqlite3.connect(self.database)
        stock, status = connection.execute(
            "SELECT p.stock, o.order_status FROM products p JOIN orders o "
            "ON o.id=(SELECT MAX(id) FROM orders) WHERE p.id=?",
            (product["id"],),
        ).fetchone()
        connection.close()
        self.assertEqual(stock, product["stock"])
        self.assertEqual(status, "cancelled")

    def test_login_rate_limit(self):
        self.client.environ_base["REMOTE_ADDR"] = "198.51.100.77"
        response = self.client.get("/login")
        token = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True)).group(1)
        statuses = [
            self.client.post(
                "/login",
                data={"csrf_token": token, "email": "nope@example.test", "password": "wrong"},
            ).status_code
            for _ in range(6)
        ]
        self.assertEqual(statuses[-1], 429)

    def test_production_requires_secret(self):
        environment = os.environ.copy()
        environment["APP_ENV"] = "production"
        environment["FLASK_SECRET_KEY"] = "replace-with-a-long-random-value"
        result = subprocess.run(
            [sys.executable, "-c", "import app"],
            cwd=app_module.BASE_DIR,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FLASK_SECRET_KEY", result.stderr)

    def test_bogus_address_default_does_not_clear_existing_default(self):
        user_id = self.login_as_user()
        token = self.csrf_token("/account")
        self.client.post(
            "/account/addresses",
            data={
                "csrf_token": token,
                "recipient_name": "Default Owner",
                "recipient_phone": "02012345678",
                "address_line": "Primary Address",
                "is_default": "1",
            },
        )
        response = self.client.post(
            "/account/addresses/999999/default",
            data={"csrf_token": token},
        )
        self.assertEqual(response.status_code, 404)
        connection = sqlite3.connect(self.database)
        default_count = connection.execute(
            """
            SELECT COUNT(*) FROM addresses
            WHERE customer_id = (SELECT id FROM customers WHERE user_id = ?)
              AND is_default = 1
            """,
            (user_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(default_count, 1)

    def test_cancelled_order_marks_pending_payment_failed_and_blocks_slip_and_confirm(self):
        _, response = self.create_order()
        order_id = response.get_json()["id"]

        admin_client = app_module.app.test_client()
        connection = sqlite3.connect(self.database)
        admin_id = connection.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.close()
        self.login_as_user(admin_id, admin_client)
        admin_token = self.csrf_token("/", admin_client)

        cancel_res = admin_client.post(
            f"/admin/orders/{order_id}/cancel",
            data={"csrf_token": admin_token},
        )
        self.assertEqual(cancel_res.status_code, 302)

        connection = sqlite3.connect(self.database)
        order_row = connection.execute(
            "SELECT order_status, payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()
        payment_row = connection.execute(
            "SELECT payment_status FROM payments WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        connection.close()
        self.assertEqual(order_row, ("cancelled", "failed"))
        self.assertEqual(payment_row, ("failed",))

        slip_res = self.client.post(
            f"/api/orders/{order_id}/slip",
            headers=self.csrf_headers(),
            data={"slip": (BytesIO(b"slip"), "receipt.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(slip_res.status_code, 400)

        confirm_res = admin_client.post(
            f"/admin/orders/{order_id}/payment",
            data={"csrf_token": admin_token},
        )
        self.assertEqual(confirm_res.status_code, 302)
        connection = sqlite3.connect(self.database)
        after_confirm = connection.execute(
            "SELECT payment_status FROM orders WHERE id = ?",
            (order_id,),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(after_confirm, "failed")

    def test_login_security_session_rotation_and_inactive_user_revocation(self):
        self.client.environ_base["REMOTE_ADDR"] = "198.51.100.88"
        reg_page = self.client.get("/register").get_data(as_text=True)
        reg_token = re.search(r'name="csrf_token" value="([^"]+)"', reg_page).group(1)
        res = self.client.post(
            "/register",
            data={
                "csrf_token": reg_token,
                "full_name": "Sec User",
                "username": "SecUser01",
                "email": "secuser01@example.test",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
        )
        self.assertEqual(res.status_code, 302)

        # Case-insensitive duplicate username must be rejected
        dup_res = self.client.post(
            "/register",
            data={
                "csrf_token": reg_token,
                "full_name": "Sec User Dup",
                "username": "secuser01",
                "email": "other@example.test",
                "password": "StrongPass123!",
                "confirm_password": "StrongPass123!",
            },
        )
        self.assertEqual(dup_res.status_code, 200)
        self.assertIn("ຖືກໃຊ້ແລ້ວ", dup_res.get_data(as_text=True))

        # Login with unsafe next URL must not redirect externally and must rotate CSRF token
        login_page = self.client.get("/login").get_data(as_text=True)
        pre_login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page).group(1)
        login_res = self.client.post(
            "/login?next=//evil.example/phish",
            data={
                "csrf_token": pre_login_token,
                "email": "SECUSER01",
                "password": "StrongPass123!",
            },
        )
        self.assertEqual(login_res.status_code, 302)
        self.assertEqual(login_res.headers["Location"], "/")
        post_login_token = self.csrf_token("/account")
        self.assertNotEqual(pre_login_token, post_login_token)

        # Deactivating user immediately invalidates session on protected routes
        connection = sqlite3.connect(self.database)
        connection.execute(
            "UPDATE users SET is_active = 0 WHERE lower(username) = 'secuser01'"
        )
        connection.commit()
        connection.close()
        self.assertEqual(self.client.get("/account").status_code, 302)

    def test_phone_and_email_registration_and_phone_login(self):
        self.client.environ_base["REMOTE_ADDR"] = "198.51.100.99"
        reg_page = self.client.get("/register").get_data(as_text=True)
        reg_token = re.search(r'name="csrf_token" value="([^"]+)"', reg_page).group(1)

        # Register via Phone only (no username or email required)
        res = self.client.post(
            "/register",
            data={
                "csrf_token": reg_token,
                "register_type": "phone",
                "full_name": "ສົມຊາຍ ວົງດາລາ",
                "phone": "020 5555 9999",
                "password": "PhonePass123!",
            },
        )
        self.assertEqual(res.status_code, 302)

        # Duplicate phone registration (even formatted with +856) must be blocked
        dup_res = self.client.post(
            "/register",
            data={
                "csrf_token": reg_token,
                "register_type": "phone",
                "full_name": "ຄົນອື່ນ",
                "phone": "+856 20 5555 9999",
                "password": "PhonePass123!",
            },
        )
        self.assertEqual(dup_res.status_code, 200)
        self.assertIn("ເບີໂທນີ້ຖືກໃຊ້ສະໝັກແລ້ວ", dup_res.get_data(as_text=True))

        # Login via Phone number (using +856 20 format)
        login_page = self.client.get("/login").get_data(as_text=True)
        login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page).group(1)
        login_res = self.client.post(
            "/login",
            data={
                "csrf_token": login_token,
                "email": "+856 20 5555 9999",
                "password": "PhonePass123!",
                "remember": "1",
            },
        )
        self.assertEqual(login_res.status_code, 302)
        self.assertEqual(login_res.headers["Location"], "/")

        # Checkout form on homepage is immediately pre-filled with name and phone
        home_html = self.client.get("/").get_data(as_text=True)
        self.assertIn("ສົມຊາຍ ວົງດາລາ", home_html)
        self.assertIn("02055559999", home_html)
        self.assertNotIn("@phone.078food.local", home_html)

    def test_admin_dashboard_orders_items_and_stock_movements(self):
        product, order_res = self.create_order()
        self.assertEqual(order_res.status_code, 201)

        admin_client = app_module.app.test_client()
        connection = sqlite3.connect(self.database)
        admin_id = connection.execute(
            "SELECT id FROM users WHERE role = 'admin' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        connection.close()
        self.login_as_user(admin_id, admin_client)

        dash_res = admin_client.get("/admin")
        self.assertEqual(dash_res.status_code, 200)
        self.assertIn(product["name"], dash_res.get_data(as_text=True))

        orders_res = admin_client.get("/admin/orders?status=pending")
        self.assertEqual(orders_res.status_code, 200)
        self.assertIn(product["name"], orders_res.get_data(as_text=True))

        cats_res = admin_client.get("/admin/categories")
        self.assertEqual(cats_res.status_code, 200)
        self.assertIn("ລາຍການ", cats_res.get_data(as_text=True))

        # Editing product stock logs a stock_movement
        admin_token = self.csrf_token("/", admin_client)
        connection = sqlite3.connect(self.database)
        prod_row = connection.execute(
            "SELECT id, category_id, name, price, stock FROM products WHERE id = ?",
            (product["id"],),
        ).fetchone()
        movements_before = connection.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE product_id = ?",
            (product["id"],),
        ).fetchone()[0]
        connection.close()

        edit_res = admin_client.post(
            f"/admin/products/{prod_row[0]}/edit",
            data={
                "csrf_token": admin_token,
                "name": prod_row[2],
                "category_id": prod_row[1],
                "price": prod_row[3],
                "stock": prod_row[4] + 10,
                "description": "Updated stock",
            },
        )
        self.assertEqual(edit_res.status_code, 302)

        connection = sqlite3.connect(self.database)
        movements_after = connection.execute(
            "SELECT COUNT(*) FROM stock_movements WHERE product_id = ?",
            (product["id"],),
        ).fetchone()[0]
        connection.close()
        self.assertEqual(movements_after, movements_before + 1)

    def test_customer_account_password_change_address_edit_and_order_cancel(self):
        # Register a customer via phone
        reg_page = self.client.get("/register").get_data(as_text=True)
        reg_token = re.search(r'name="csrf_token" value="([^"]+)"', reg_page).group(1)
        self.client.post(
            "/register",
            data={
                "csrf_token": reg_token,
                "register_type": "phone",
                "full_name": "ທ້າວ ສົມຫວັງ",
                "phone": "02099887766",
                "password": "OldPassword123!",
            },
        )
        # Log in
        login_page = self.client.get("/login").get_data(as_text=True)
        login_token = re.search(r'name="csrf_token" value="([^"]+)"', login_page).group(1)
        self.client.post(
            "/login",
            data={
                "csrf_token": login_token,
                "email": "02099887766",
                "password": "OldPassword123!",
            },
        )

        # 1. Change password
        acct_token = self.csrf_token("/account")
        pw_res = self.client.post(
            "/account/password",
            data={
                "csrf_token": acct_token,
                "current_password": "OldPassword123!",
                "new_password": "NewPassword456!",
                "confirm_password": "NewPassword456!",
            },
            follow_redirects=True,
        )
        self.assertEqual(pw_res.status_code, 200)
        self.assertIn("ປ່ຽນລະຫັດຜ່ານສຳເລັດ", pw_res.get_data(as_text=True))

        # 2. Add & Edit saved address
        acct_token = self.csrf_token("/account")
        self.client.post(
            "/account/addresses",
            data={
                "csrf_token": acct_token,
                "recipient_name": "ທ້າວ ສົມຫວັງ",
                "recipient_phone": "02099887766",
                "address_line": "ບ້ານໂພນທັນ",
                "district": "ໄຊເສດຖາ",
                "province": "ນະຄອນຫຼວງວຽງຈັນ",
                "is_default": "1",
            },
        )
        connection = sqlite3.connect(self.database)
        addr_id = connection.execute(
            "SELECT id FROM addresses WHERE recipient_phone = '02099887766' ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
        connection.close()

        acct_token = self.csrf_token("/account")
        edit_addr_res = self.client.post(
            f"/account/addresses/{addr_id}/edit",
            data={
                "csrf_token": acct_token,
                "recipient_name": "ທ້າວ ສົມຫວັງ (ແກ້ໄຂ)",
                "recipient_phone": "02099887766",
                "address_line": "ບ້ານດົງໂດກ ຮ່ອມ 2",
                "district": "ໄຊທານີ",
                "province": "ນະຄອນຫຼວງວຽງຈັນ",
                "postal_code": "01000",
            },
            follow_redirects=True,
        )
        self.assertEqual(edit_addr_res.status_code, 200)
        self.assertIn("ແກ້ໄຂທີ່ຢູ່ສຳເລັດ", edit_addr_res.get_data(as_text=True))
        self.assertIn("ບ້ານດົງໂດກ ຮ່ອມ 2", edit_addr_res.get_data(as_text=True))

        # 3. Create order & self-cancel pending order
        product, order_res = self.create_order()
        self.assertEqual(order_res.status_code, 201)
        order_id = order_res.get_json()["id"]

        connection = sqlite3.connect(self.database)
        stock_after_order = connection.execute(
            "SELECT stock FROM products WHERE id = ?",
            (product["id"],),
        ).fetchone()[0]
        connection.close()

        orders_token = self.csrf_token("/orders")
        cancel_res = self.client.post(
            f"/orders/{order_id}/cancel",
            data={"csrf_token": orders_token},
            follow_redirects=True,
        )
        self.assertEqual(cancel_res.status_code, 200)
        self.assertIn("ຍົກເລີກອໍເດີ ແລະຄືນ Stock ສຳເລັດ", cancel_res.get_data(as_text=True))

        connection = sqlite3.connect(self.database)
        order_status, payment_status, stock_after_cancel = connection.execute(
            "SELECT o.order_status, o.payment_status, p.stock FROM orders o JOIN products p ON p.id = ? WHERE o.id = ?",
            (product["id"], order_id),
        ).fetchone()
        connection.close()
        self.assertEqual(order_status, "cancelled")
        self.assertEqual(payment_status, "failed")
        self.assertEqual(stock_after_cancel, stock_after_order + 1)


if __name__ == "__main__":
    unittest.main()
