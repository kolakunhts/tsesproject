import os
import re
import secrets
import sqlite3
import hmac
from datetime import datetime
from urllib.parse import urlsplit
from uuid import uuid4
from pathlib import Path
from functools import wraps
from flask import (
    Flask, jsonify, render_template, request,
    redirect, url_for, flash, abort, send_from_directory, session
)
from flask_login import (
    LoginManager, UserMixin, login_user,
    login_required, logout_user, current_user
)
from werkzeug.security import (
    generate_password_hash, check_password_hash
)
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

try:
    import stripe
except ImportError:  # Stripe is optional until payment testing is enabled.
    stripe = None

BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "catalog.db"
UPLOAD_DIR = BASE_DIR / "uploads" / "payment_slips"
load_dotenv(BASE_DIR / ".env")

app = Flask(__name__)

APP_ENV = os.environ.get("APP_ENV", "development").strip().lower()
IS_PRODUCTION = APP_ENV in {"production", "prod"}
configured_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
if IS_PRODUCTION and (
    not configured_secret
    or configured_secret in {"dev-only-change-this-secret", "replace-with-a-long-random-value"}
):
    raise RuntimeError("FLASK_SECRET_KEY must be set to a strong value in production.")

# Development gets an ephemeral secret; production never has a hard-coded fallback.
app.config["SECRET_KEY"] = configured_secret or secrets.token_urlsafe(32)
app.config["DEBUG"] = APP_ENV == "development" and os.environ.get("FLASK_DEBUG", "0") == "1"
app.config["RATELIMIT_STORAGE_URI"] = os.environ.get(
    "RATELIMIT_STORAGE_URI", "memory://"
)
app.config["RATELIMIT_ENABLED"] = os.environ.get("RATELIMIT_ENABLED", "1") == "1"
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION or os.environ.get("COOKIE_SECURE", "0") == "1"
app.config["REMEMBER_COOKIE_HTTPONLY"] = True
app.config["REMEMBER_COOKIE_SAMESITE"] = "Lax"
app.config["REMEMBER_COOKIE_SECURE"] = app.config["SESSION_COOKIE_SECURE"]
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
STRIPE_CURRENCY = os.environ.get("STRIPE_CURRENCY", "lak").lower()

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "ກະລຸນາ Login ກ່ອນ."
login_manager.login_message_category = "warning"

limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri=app.config["RATELIMIT_STORAGE_URI"],
    enabled=app.config["RATELIMIT_ENABLED"],
)

DUMMY_PASSWORD_HASH = generate_password_hash("078food-dummy-password-timing-guard")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_EMAIL_SUFFIX = "@phone.078food.local"
_MIGRATED_DBS = set()


def normalize_phone(raw):
    """ແປງເບີໂທໃຫ້ເປັນຕົວເລກມາດຕະຖານດຽວກັນ (ເຊັ່ນ +856 20 xxxx xxxx -> 020xxxxxxxx)."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if not digits:
        return ""
    if digits.startswith("856") and len(digits) >= 11:
        digits = "0" + digits[3:]
    elif digits.startswith(("20", "30")) and len(digits) in (9, 10):
        digits = "0" + digits
    return digits


def generate_unique_username(connection, preferred_name, fallback_seed=""):
    """ສ້າງຊື່ຜູ້ໃຊ້ທີ່ບໍ່ຊ້ຳກັນອັດຕະໂນມັດຈາກຊື່ແທ້ ຫຼື ເບີໂທ/ອີເມວ."""
    base = re.sub(r"\s+", " ", (preferred_name or "").strip())[:40]
    if len(base) < 3:
        seed = re.sub(r"[^a-zA-Z0-9_]", "", fallback_seed)[:20] or "user"
        base = f"user_{seed}"
    candidate = base
    suffix_num = 1
    while connection.execute(
        "SELECT 1 FROM users WHERE lower(username) = lower(?)",
        (candidate,)
    ).fetchone() is not None:
        suffix_num += 1
        candidate = f"{base[:42]}_{suffix_num}"
    return candidate


def is_safe_redirect_url(target):
    """ກວດສອບວ່າ URL ສຳລັບ redirect ເປັນ relative path ພາຍໃນເວັບເທົ່ານັ້ນ."""
    if not target or not isinstance(target, str):
        return False
    target = target.strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    if any(ord(ch) < 32 for ch in target):
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc


def rotate_session_on_login(user_row, remember=False):
    """ປ່ຽນ CSRF token ແລະລ້າງ session ເກົ່າເມື່ອ Login ສຳເລັດ ເພື່ອປ້ອງກັນ Session Fixation."""
    guest_orders = [
        int(order_id) for order_id in session.get("guest_order_ids", [])
        if str(order_id).isdigit()
    ]
    session.clear()
    if guest_orders:
        session["guest_order_ids"] = guest_orders[-10:]
    login_user(User(user_row), remember=bool(remember))
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session.modified = True


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.context_processor
def inject_security_helpers():
    return {"csrf_token": csrf_token}


@app.before_request
def validate_csrf():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    # Stripe authenticates this server-to-server request with its signature.
    if request.endpoint == "stripe_webhook":
        return None
    expected = session.get("_csrf_token")
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRFToken")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        if request.path.startswith("/api/") or request.is_json:
            return jsonify({"error": "CSRF validation failed."}), 400
        abort(400, description="CSRF validation failed.")
    return None


def get_db():
    """ເປີດການເຊື່ອມຕໍ່ SQLite."""
    if not DATABASE.is_file():
        raise FileNotFoundError(f"ບໍ່ພົບຖານຂໍ້ມູນ: {DATABASE}")

    connection = sqlite3.connect(DATABASE)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    db_key = str(DATABASE.resolve())
    if db_key not in _MIGRATED_DBS:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(payments)")
        }
        if "slip_path" not in columns:
            connection.execute("ALTER TABLE payments ADD COLUMN slip_path TEXT")
            connection.commit()
        user_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(users)")
        }
        if "phone" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            connection.execute(
                """
                UPDATE users
                SET phone = (
                    SELECT c.phone FROM customers c
                    WHERE c.user_id = users.id AND c.phone IS NOT NULL AND c.phone != ''
                )
                WHERE phone IS NULL
                """
            )
            connection.commit()
        _MIGRATED_DBS.add(db_key)
    return connection


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.is_secure or os.environ.get("FORCE_HTTPS") == "1":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


class User(UserMixin):
    """ເກັບຂໍ້ມູນ User ສຳລັບ Flask-Login."""

    def __init__(self, row):
        keys = row.keys() if hasattr(row, "keys") else []
        raw_email = (row["email"] or "") if "email" in keys else ""
        self.id = row["id"]
        self.username = row["username"]
        self.phone = row["phone"] if "phone" in keys and row["phone"] else None
        self.has_real_email = bool(raw_email and not raw_email.endswith(PHONE_EMAIL_SUFFIX))
        self.real_email = raw_email if self.has_real_email else ""
        self.email = self.real_email or self.phone or ""
        self.role = row["role"]
        self.active = bool(row["is_active"])

    @property
    def is_active(self):
        return self.active


def find_user_by_id(user_id):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT * FROM users WHERE id = ? AND is_active = 1",
            (user_id,)
        ).fetchone()
        return User(row) if row else None
    finally:
        connection.close()


@login_manager.user_loader
def load_user(user_id):
    try:
        return find_user_by_id(user_id)
    except (sqlite3.Error, OSError):
        return None


def check_database():
    """ກວດສອບຖານຂໍ້ມູນແບບອ່ານຢ່າງດຽວ."""
    if not DATABASE.is_file():
        raise FileNotFoundError(f"ບໍ່ພົບຖານຂໍ້ມູນ: {DATABASE}")

    connection = sqlite3.connect(
        f"file:{DATABASE}?mode=ro",
        uri=True
    )
    try:
        tables = connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall()
        return [row[0] for row in tables]
    finally:
        connection.close()


@app.route("/api/health")
def health():
    try:
        return jsonify({
            "status": "ok",
            "message": "078food backend is running",
            "database": DATABASE.name,
            "tables": check_database()
        })
    except (OSError, sqlite3.Error) as error:
        return jsonify({
            "status": "error",
            "message": str(error)
        }), 500


# ---------- Frontend ເດີມ ----------

@app.route("/")
def home():
    customer = None
    addresses = []
    if current_user.is_authenticated:
        connection = get_db()
        try:
            customer = connection.execute(
                "SELECT * FROM customers WHERE user_id = ?",
                (current_user.id,)
            ).fetchone()
            if customer:
                addresses = connection.execute(
                    """
                    SELECT id, recipient_name, recipient_phone, address_line,
                           district, province, postal_code, is_default
                    FROM addresses
                    WHERE customer_id = ?
                    ORDER BY is_default DESC, id DESC
                    """,
                    (customer["id"],)
                ).fetchall()
        finally:
            connection.close()
    return render_template("index.html", customer=customer, addresses=addresses)


# ---------- ສະໝັກບັນຊີ ----------

@app.route("/register", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("home"))

    if request.method == "POST":
        register_type = request.form.get("register_type", "").strip().lower()
        raw_username = request.form.get("username", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = normalize_phone(request.form.get("phone", ""))
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", password)

        if register_type not in {"phone", "email"}:
            register_type = "phone" if (phone and not email) else "email"

        if not full_name or not password:
            flash("ກະລຸນາປ້ອນຊື່ ແລະ ລະຫັດຜ່ານໃຫ້ຄົບ.", "error")
            return render_template("register.html")

        if len(full_name) < 2 or len(full_name) > 100:
            flash("ກະລຸນາປ້ອນຊື່ ແລະ ນາມສະກຸນໃຫ້ຖືກຕ້ອງ (2–100 ຕົວອັກສອນ).", "error")
            return render_template("register.html")

        if raw_username and (len(raw_username) < 3 or len(raw_username) > 50 or " " in raw_username):
            flash("Username ຕ້ອງມີ 3–50 ຕົວອັກສອນ ແລະ ບໍ່ມີຍະຫວ່າງ.", "error")
            return render_template("register.html")

        if register_type == "phone":
            if len(phone) < 8 or len(phone) > 15:
                flash("ກະລຸນາປ້ອນເບີໂທລະສັບໃຫ້ຖືກຕ້ອງ (ຢ່າງໜ້ອຍ 8 ຕົວເລກ).", "error")
                return render_template("register.html")
            email_for_users = f"{phone}{PHONE_EMAIL_SUFFIX}"
            email_for_customer = None
        else:
            if not email or len(email) > 254 or not EMAIL_PATTERN.match(email) or email.endswith(PHONE_EMAIL_SUFFIX):
                flash("ຮູບແບບ Email ບໍ່ຖືກຕ້ອງ.", "error")
                return render_template("register.html")
            email_for_users = email
            email_for_customer = email

        if len(password) < 8 or len(password) > 128:
            flash("ລະຫັດຜ່ານຕ້ອງມີຢ່າງໜ້ອຍ 8 ຕົວ (ສູງສຸດ 128 ຕົວ).", "error")
            return render_template("register.html")

        if password != confirm_password:
            flash("ລະຫັດຜ່ານທັງສອງບໍ່ກົງກັນ.", "error")
            return render_template("register.html")

        connection = get_db()
        try:
            if raw_username:
                dup_user = connection.execute(
                    "SELECT 1 FROM users WHERE lower(username) = lower(?)",
                    (raw_username,)
                ).fetchone()
                if dup_user is not None:
                    flash("Username ຫຼື Email ນີ້ຖືກໃຊ້ແລ້ວ.", "error")
                    return render_template("register.html")

            if register_type == "phone":
                dup_phone = connection.execute(
                    """
                    SELECT 1 FROM users u
                    LEFT JOIN customers c ON c.user_id = u.id
                    WHERE u.phone = ? OR c.phone = ? OR lower(u.email) = lower(?)
                    """,
                    (phone, phone, email_for_users)
                ).fetchone()
                if dup_phone is not None:
                    flash("ເບີໂທນີ້ຖືກໃຊ້ສະໝັກແລ້ວ. ກະລຸນາເຂົ້າສູ່ລະບົບ.", "error")
                    return render_template("register.html")
            else:
                dup_email = connection.execute(
                    "SELECT 1 FROM users WHERE lower(email) = lower(?)",
                    (email_for_users,)
                ).fetchone()
                if dup_email is not None:
                    flash("Email ນີ້ຖືກໃຊ້ສະໝັກແລ້ວ.", "error")
                    return render_template("register.html")

            username = raw_username or generate_unique_username(
                connection, full_name, phone or email_for_users
            )

            # ບໍ່ຮັບ role ຈາກຟອມ: ບັນຊີໃໝ່ເປັນ user ເທົ່ານັ້ນ.
            cursor = connection.execute(
                """
                INSERT INTO users
                    (username, email, phone, password_hash, role, is_active)
                VALUES (?, ?, ?, ?, 'user', 1)
                """,
                (
                    username,
                    email_for_users,
                    phone or None,
                    generate_password_hash(password)
                )
            )
            user_id = cursor.lastrowid

            connection.execute(
                """
                INSERT INTO customers (user_id, full_name, phone, email)
                VALUES (?, ?, ?, ?)
                """,
                (user_id, full_name, phone or None, email_for_customer)
            )
            connection.commit()

            flash("ສະໝັກບັນຊີສຳເລັດ. ກະລຸນາ Login.", "success")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            connection.rollback()
            flash("ຂໍ້ມູນບັນຊີນີ້ຖືກໃຊ້ແລ້ວ.", "error")
        except sqlite3.Error:
            connection.rollback()
            app.logger.exception("Registration database error")
            flash("ມີບັນຫາໃນການສະໝັກ. ກະລຸນາລອງໃໝ່.", "error")
        finally:
            connection.close()

    return render_template("register.html")


# ---------- Customer Login ----------

@app.route("/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def login():
    if current_user.is_authenticated:
        if current_user.role == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("home"))

    if request.method == "POST":
        identifier = (
            request.form.get("identifier")
            or request.form.get("email")
            or ""
        ).strip()
        password = request.form.get("password", "")
        remember = request.form.get("remember") == "1"

        if not identifier or not password or len(identifier) > 254 or len(password) > 128:
            check_password_hash(DUMMY_PASSWORD_HASH, "invalid")
            flash("ເບີໂທ, ອີເມວ ຫຼື ລະຫັດຜ່ານບໍ່ຖືກຕ້ອງ.", "error")
            return render_template("login.html")

        raw_digits = "".join(ch for ch in identifier if ch.isdigit())
        phone_candidate = (
            normalize_phone(identifier)
            if ("@" not in identifier and len(raw_digits) >= 8)
            else ""
        )

        connection = get_db()
        try:
            rows = connection.execute(
                """
                SELECT DISTINCT u.*
                FROM users u
                LEFT JOIN customers c ON c.user_id = u.id
                WHERE lower(u.email) = lower(?)
                   OR lower(u.username) = lower(?)
                   OR (? != '' AND (
                       u.phone = ?
                       OR u.phone = ?
                       OR c.phone = ?
                       OR c.phone = ?
                       OR lower(u.email) = lower(?)
                   ))
                ORDER BY u.is_active DESC, u.id DESC
                """,
                (
                    identifier,
                    identifier,
                    phone_candidate,
                    phone_candidate,
                    raw_digits,
                    phone_candidate,
                    raw_digits,
                    f"{phone_candidate}{PHONE_EMAIL_SUFFIX}" if phone_candidate else "",
                )
            ).fetchall()

            matched_row = None
            if not rows:
                check_password_hash(DUMMY_PASSWORD_HASH, password)
            else:
                for candidate_row in rows:
                    if check_password_hash(candidate_row["password_hash"], password):
                        matched_row = candidate_row
                        break

            if matched_row:
                if matched_row["is_active"] != 1:
                    flash("ບັນຊີນີ້ຖືກລະງັບການໃຊ້ງານ.", "error")
                    return render_template("login.html")

                # Sync guest orders created in this browser session to the user's customer profile
                guest_orders = [
                    int(oid) for oid in session.get("guest_order_ids", [])
                    if str(oid).isdigit()
                ]
                if guest_orders:
                    customer = connection.execute(
                        "SELECT id FROM customers WHERE user_id = ?",
                        (matched_row["id"],)
                    ).fetchone()
                    if customer:
                        placeholders = ",".join("?" for _ in guest_orders)
                        connection.execute(
                            f"UPDATE orders SET customer_id = ? WHERE customer_id IS NULL AND id IN ({placeholders})",
                            [customer["id"], *guest_orders]
                        )
                        connection.commit()

                rotate_session_on_login(matched_row, remember=remember)
                next_page = request.args.get("next")
                if matched_row["role"] == "admin":
                    if is_safe_redirect_url(next_page):
                        return redirect(next_page)
                    return redirect(url_for("admin_dashboard"))
                if is_safe_redirect_url(next_page) and not next_page.startswith("/admin"):
                    return redirect(next_page)
                return redirect(url_for("home"))
        finally:
            connection.close()

        flash("ເບີໂທ, ອີເມວ ຫຼື ລະຫັດຜ່ານບໍ່ຖືກຕ້ອງ.", "error")

    return render_template("login.html")


# ---------- Admin ----------

def admin_required(view_function):
    @wraps(view_function)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("admin_login"))
        if current_user.role != "admin":
            abort(403)
        return view_function(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
@limiter.limit("5 per minute", methods=["POST"])
def admin_login():
    if current_user.is_authenticated and current_user.role == "admin":
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        identifier = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not identifier or not password or len(identifier) > 254 or len(password) > 128:
            check_password_hash(DUMMY_PASSWORD_HASH, "invalid")
            flash("ຂໍ້ມູນ Admin Login ບໍ່ຖືກຕ້ອງ.", "error")
            return render_template("admin/login.html")

        connection = get_db()
        try:
            row = connection.execute(
                """
                SELECT * FROM users
                WHERE (lower(email) = lower(?) OR lower(username) = lower(?))
                """,
                (identifier, identifier)
            ).fetchone()
        finally:
            connection.close()

        target_hash = row["password_hash"] if row else DUMMY_PASSWORD_HASH
        password_ok = check_password_hash(target_hash, password)

        if (
            row
            and row["role"] == "admin"
            and row["is_active"] == 1
            and password_ok
        ):
            rotate_session_on_login(row)
            next_page = request.args.get("next")
            if is_safe_redirect_url(next_page) and next_page.startswith("/admin"):
                return redirect(next_page)
            return redirect(url_for("admin_dashboard"))

        flash("ຂໍ້ມູນ Admin Login ບໍ່ຖືກຕ້ອງ.", "error")

    return render_template("admin/login.html")


@app.route("/admin")
@admin_required
def admin_dashboard():
    connection = get_db()
    try:
        counts = {
            "users": connection.execute(
                "SELECT COUNT(*) FROM users WHERE role = 'user'"
            ).fetchone()[0],
            "products": connection.execute(
                "SELECT COUNT(*) FROM products"
            ).fetchone()[0],
            "orders": connection.execute(
                "SELECT COUNT(*) FROM orders"
            ).fetchone()[0],
            "categories": connection.execute(
                "SELECT COUNT(*) FROM categories"
            ).fetchone()[0],
            "pending_orders": connection.execute(
                "SELECT COUNT(*) FROM orders WHERE order_status = 'pending'"
            ).fetchone()[0],
            "revenue": connection.execute(
                """
                SELECT COALESCE(SUM(total_amount), 0)
                FROM orders
                WHERE order_status != 'cancelled'
                  AND (payment_status = 'paid' OR order_status = 'completed')
                """
            ).fetchone()[0],
            "low_stock": connection.execute(
                "SELECT COUNT(*) FROM products WHERE is_active = 1 AND stock <= 5"
            ).fetchone()[0],
        }
        recent_rows = connection.execute(
            """
            SELECT o.*, p.payment_method
            FROM orders o
            LEFT JOIN payments p ON p.order_id = o.id
            ORDER BY o.id DESC
            LIMIT 5
            """
        ).fetchall()
        recent_orders = []
        for row in recent_rows:
            rec = dict(row)
            rec["items"] = [
                dict(item) for item in connection.execute(
                    "SELECT product_name, quantity FROM order_items WHERE order_id = ? ORDER BY id",
                    (row["id"],)
                ).fetchall()
            ]
            recent_orders.append(rec)

        low_stock_products = connection.execute(
            """
            SELECT id, name, stock, price
            FROM products
            WHERE is_active = 1 AND stock <= 5
            ORDER BY stock ASC, id DESC
            LIMIT 6
            """
        ).fetchall()
    finally:
        connection.close()

    return render_template(
        "admin/dashboard.html",
        counts=counts,
        recent_orders=recent_orders,
        low_stock_products=low_stock_products,
    )


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    session.clear()
    session["_csrf_token"] = secrets.token_urlsafe(32)
    flash("ອອກຈາກລະບົບແລ້ວ.", "success")
    return redirect(url_for("home"))


@app.route("/orders")
@login_required
def my_orders():
    connection = get_db()
    try:
        order_rows = connection.execute(
            """
            SELECT o.*, p.payment_status AS payment_record_status,
                   p.slip_path, p.payment_method
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            LEFT JOIN payments p ON p.order_id = o.id
            WHERE c.user_id = ?
            ORDER BY o.id DESC
            """,
            (current_user.id,)
        ).fetchall()
        orders = []
        for order in order_rows:
            record = dict(order)
            record["items"] = [
                dict(item) for item in connection.execute(
                    """
                    SELECT product_name, quantity, unit_price, subtotal
                    FROM order_items
                    WHERE order_id = ?
                    ORDER BY id
                    """,
                    (order["id"],)
                ).fetchall()
            ]
            orders.append(record)
        return render_template("orders.html", orders=orders)
    finally:
        connection.close()


@app.route("/orders/<int:order_id>/cancel", methods=["POST"])
@login_required
def cancel_my_order(order_id):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            """
            SELECT o.id, o.order_status, o.payment_status
            FROM orders o
            JOIN customers c ON c.id = o.customer_id
            WHERE o.id = ? AND c.user_id = ?
            """,
            (order_id, current_user.id)
        ).fetchone()
        if order is None:
            connection.rollback()
            abort(404)
        if order["order_status"] != "pending" or order["payment_status"] not in {"pending", "unpaid"}:
            connection.rollback()
            flash("ອໍເດີນີ້ບໍ່ສາມາດຍົກເລີກໄດ້ແລ້ວ.", "error")
            return redirect(url_for("my_orders"))

        release_order_stock(connection, order_id, "customer cancelled pending order")
        connection.execute(
            """
            UPDATE orders
            SET order_status = 'cancelled',
                payment_status = 'failed',
                cancelled_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,)
        )
        connection.execute(
            """
            UPDATE payments
            SET payment_status = 'failed',
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND payment_status = 'pending'
            """,
            (order_id,)
        )
        connection.commit()
        flash("ຍົກເລີກອໍເດີ ແລະຄືນ Stock ສຳເລັດ.", "success")
        return redirect(url_for("my_orders"))
    except sqlite3.Error:
        connection.rollback()
        app.logger.exception("Customer order cancel database error")
        flash("ຍົກເລີກອໍເດີບໍ່ສຳເລັດ.", "error")
        return redirect(url_for("my_orders"))
    finally:
        connection.close()


# ---------- Customer Account / Saved Addresses ----------

def get_customer_for_user(connection, user_id):
    return connection.execute(
        "SELECT * FROM customers WHERE user_id = ?",
        (user_id,)
    ).fetchone()


def get_addresses_for_customer(connection, customer_id):
    return connection.execute(
        """
        SELECT id, recipient_name, recipient_phone, address_line,
               district, province, postal_code, is_default
        FROM addresses
        WHERE customer_id = ?
        ORDER BY is_default DESC, id DESC
        """,
        (customer_id,)
    ).fetchall()


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    connection = get_db()
    try:
        customer = get_customer_for_user(connection, current_user.id)
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            new_username = request.form.get("username", "").strip()
            phone = normalize_phone(request.form.get("phone", ""))
            new_email = request.form.get("email", "").strip().lower()
            validation_error = False

            if new_email and (len(new_email) > 254 or not EMAIL_PATTERN.match(new_email) or new_email.endswith(PHONE_EMAIL_SUFFIX)):
                flash("ຮູບແບບ Email ບໍ່ຖືກຕ້ອງ.", "error")
                validation_error = True
            elif new_username and (len(new_username) < 2 or len(new_username) > 50):
                flash("ຊື່ຜູ້ໃຊ້ຕ້ອງມີຄວາມຍາວ 2–50 ຕົວອັກສອນ.", "error")
                validation_error = True

            if not validation_error and (not full_name or len(phone) < 8 or len(phone) > 15):
                flash("ກະລຸນາກວດຊື່ ແລະເບີໂທໃຫ້ຖືກຕ້ອງ.", "error")
            elif not validation_error:
                dup_phone = connection.execute(
                    "SELECT 1 FROM users WHERE phone = ? AND id != ?",
                    (phone, current_user.id)
                ).fetchone()
                dup_email = (
                    connection.execute(
                        "SELECT 1 FROM users WHERE lower(email) = lower(?) AND id != ?",
                        (new_email, current_user.id)
                    ).fetchone()
                    if new_email else None
                )
                dup_username = (
                    connection.execute(
                        "SELECT 1 FROM users WHERE lower(username) = lower(?) AND id != ?",
                        (new_username, current_user.id)
                    ).fetchone()
                    if new_username else None
                )
                if dup_phone is not None:
                    flash("ເບີໂທນີ້ຖືກໃຊ້ໂດຍບັນຊີອື່ນແລ້ວ.", "error")
                elif dup_email is not None:
                    flash("Email ນີ້ຖືກໃຊ້ໂດຍບັນຊີອື່ນແລ້ວ.", "error")
                elif dup_username is not None:
                    flash("ຊື່ຜູ້ໃຊ້ນີ້ຖືກໃຊ້ໂດຍບັນຊີອື່ນແລ້ວ.", "error")
                else:
                    effective_email = new_email or current_user.real_email or None
                    user_email_db = effective_email or f"{phone}{PHONE_EMAIL_SUFFIX}"
                    effective_username = new_username or current_user.username
                    connection.execute(
                        """
                        UPDATE users
                        SET username = ?, phone = ?, email = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (effective_username, phone, user_email_db, current_user.id)
                    )
                    current_user.username = effective_username
                    current_user.phone = phone
                    current_user._raw_email = user_email_db
                    if customer is None:
                        cursor = connection.execute(
                            """
                            INSERT INTO customers (user_id, full_name, phone, email)
                            VALUES (?, ?, ?, ?)
                            """,
                            (current_user.id, full_name, phone, effective_email)
                        )
                        customer_id = cursor.lastrowid
                    else:
                        customer_id = customer["id"]
                        connection.execute(
                            """
                            UPDATE customers
                            SET full_name = ?, phone = ?, email = ?, updated_at = CURRENT_TIMESTAMP
                            WHERE id = ? AND user_id = ?
                            """,
                            (full_name, phone, effective_email, customer_id, current_user.id)
                        )
                    connection.commit()
                    flash("ບັນທຶກຂໍ້ມູນບັນຊີສຳເລັດ.", "success")
                    customer = get_customer_for_user(connection, current_user.id)

        addresses = get_addresses_for_customer(connection, customer["id"]) if customer else []
        order_stats = {"total_orders": 0, "active_orders": 0, "total_spent": 0}
        if customer:
            stats_row = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_orders,
                    COALESCE(SUM(CASE WHEN order_status IN ('pending', 'confirmed', 'processing', 'shipped') THEN 1 ELSE 0 END), 0) AS active_orders,
                    COALESCE(SUM(CASE WHEN order_status != 'cancelled' THEN total_amount ELSE 0 END), 0) AS total_spent
                FROM orders
                WHERE customer_id = ?
                """,
                (customer["id"],)
            ).fetchone()
            if stats_row:
                order_stats = {
                    "total_orders": stats_row["total_orders"],
                    "active_orders": stats_row["active_orders"],
                    "total_spent": stats_row["total_spent"],
                }
        return render_template("account.html", customer=customer, addresses=addresses, order_stats=order_stats)
    finally:
        connection.close()


@app.route("/account/password", methods=["POST"])
@login_required
def change_account_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not current_password or not new_password:
        flash("ກະລຸນາປ້ອນລະຫັດຜ່ານປັດຈຸບັນ ແລະລະຫັດຜ່ານໃໝ່.", "error")
        return redirect(url_for("account"))
    if len(new_password) < 8 or len(new_password) > 128:
        flash("ລະຫັດຜ່ານໃໝ່ຕ້ອງມີ 8–128 ຕົວອັກສອນ.", "error")
        return redirect(url_for("account"))
    if confirm_password and new_password != confirm_password:
        flash("ລະຫັດຜ່ານໃໝ່ ແລະຢືນຢັນລະຫັດຜ່ານບໍ່ກົງກັນ.", "error")
        return redirect(url_for("account"))

    connection = get_db()
    try:
        user_row = connection.execute(
            "SELECT password_hash FROM users WHERE id = ?",
            (current_user.id,)
        ).fetchone()
        if user_row is None or not check_password_hash(user_row["password_hash"], current_password):
            flash("ລະຫັດຜ່ານປັດຈຸບັນບໍ່ຖືກຕ້ອງ.", "error")
            return redirect(url_for("account"))

        connection.execute(
            """
            UPDATE users
            SET password_hash = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (generate_password_hash(new_password), current_user.id)
        )
        connection.commit()
        session["_csrf_token"] = secrets.token_urlsafe(32)
        flash("ປ່ຽນລະຫັດຜ່ານສຳເລັດ.", "success")
        return redirect(url_for("account"))
    finally:
        connection.close()


@app.route("/account/addresses", methods=["POST"])
@login_required
def add_account_address():
    recipient_name = request.form.get("recipient_name", "").strip()
    recipient_phone = normalize_phone(request.form.get("recipient_phone", ""))
    address_line = request.form.get("address_line", "").strip()
    district = request.form.get("district", "").strip()
    province = request.form.get("province", "").strip()
    postal_code = request.form.get("postal_code", "").strip()
    if not recipient_name or len(recipient_phone) < 8 or not address_line:
        flash("ກະລຸນາປ້ອນຊື່, ເບີໂທ ແລະທີ່ຢູ່ໃຫ້ຄົບ.", "error")
        return redirect(url_for("account"))

    connection = get_db()
    try:
        customer = get_customer_for_user(connection, current_user.id)
        if customer is None:
            cursor = connection.execute(
                "INSERT INTO customers (user_id, full_name, phone, email) VALUES (?, ?, ?, ?)",
                (current_user.id, recipient_name, recipient_phone, current_user.email)
            )
            customer_id = cursor.lastrowid
        else:
            customer_id = customer["id"]

        existing_count = connection.execute(
            "SELECT COUNT(*) FROM addresses WHERE customer_id = ?",
            (customer_id,)
        ).fetchone()[0]
        make_default = request.form.get("is_default") == "1" or existing_count == 0
        if make_default:
            connection.execute(
                "UPDATE addresses SET is_default = 0 WHERE customer_id = ?",
                (customer_id,)
            )
        connection.execute(
            """
            INSERT INTO addresses (
                customer_id, recipient_name, recipient_phone, address_line,
                district, province, postal_code, is_default
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_id, recipient_name, recipient_phone, address_line,
                district or None, province or None, postal_code or None,
                1 if make_default else 0,
            )
        )
        connection.commit()
        flash("ເພີ່ມທີ່ຢູ່ສຳເລັດ.", "success")
        return redirect(url_for("account"))
    except sqlite3.Error:
        connection.rollback()
        app.logger.exception("Address save database error")
        flash("ບັນທຶກທີ່ຢູ່ບໍ່ສຳເລັດ.", "error")
        return redirect(url_for("account"))
    finally:
        connection.close()


@app.route("/account/addresses/<int:address_id>/edit", methods=["POST"])
@login_required
def edit_account_address(address_id):
    recipient_name = request.form.get("recipient_name", "").strip()
    recipient_phone = normalize_phone(request.form.get("recipient_phone", ""))
    address_line = request.form.get("address_line", "").strip()
    district = request.form.get("district", "").strip()
    province = request.form.get("province", "").strip()
    postal_code = request.form.get("postal_code", "").strip()
    if not recipient_name or len(recipient_phone) < 8 or not address_line:
        flash("ກະລຸນາປ້ອນຊື່, ເບີໂທ ແລະທີ່ຢູ່ໃຫ້ຄົບ.", "error")
        return redirect(url_for("account"))

    connection = get_db()
    try:
        address = connection.execute(
            """
            SELECT a.id, a.customer_id, a.is_default
            FROM addresses a
            JOIN customers c ON c.id = a.customer_id
            WHERE a.id = ? AND c.user_id = ?
            """,
            (address_id, current_user.id)
        ).fetchone()
        if address is None:
            abort(404)

        make_default = request.form.get("is_default") == "1"
        if make_default:
            connection.execute(
                "UPDATE addresses SET is_default = 0 WHERE customer_id = ?",
                (address["customer_id"],)
            )
            new_is_default = 1
        else:
            new_is_default = address["is_default"]

        connection.execute(
            """
            UPDATE addresses
            SET recipient_name = ?,
                recipient_phone = ?,
                address_line = ?,
                district = ?,
                province = ?,
                postal_code = ?,
                is_default = ?
            WHERE id = ? AND customer_id = ?
            """,
            (
                recipient_name, recipient_phone, address_line,
                district or None, province or None, postal_code or None,
                new_is_default, address_id, address["customer_id"],
            )
        )
        connection.commit()
        flash("ແກ້ໄຂທີ່ຢູ່ສຳເລັດ.", "success")
        return redirect(url_for("account"))
    except sqlite3.Error:
        connection.rollback()
        app.logger.exception("Address edit database error")
        flash("ແກ້ໄຂທີ່ຢູ່ບໍ່ສຳເລັດ.", "error")
        return redirect(url_for("account"))
    finally:
        connection.close()


@app.route("/account/addresses/<int:address_id>/default", methods=["POST"])
@login_required
def set_default_account_address(address_id):
    connection = get_db()
    try:
        address = connection.execute(
            """
            SELECT a.id, a.customer_id
            FROM addresses a
            JOIN customers c ON c.id = a.customer_id
            WHERE a.id = ? AND c.user_id = ?
            """,
            (address_id, current_user.id)
        ).fetchone()
        if address is None:
            abort(404)

        connection.execute(
            """
            UPDATE addresses
            SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END
            WHERE customer_id = ?
            """,
            (address_id, address["customer_id"])
        )
        connection.commit()
        flash("ປ່ຽນທີ່ຢູ່ຫຼັກແລ້ວ.", "success")
        return redirect(url_for("account"))
    finally:
        connection.close()


@app.route("/account/addresses/<int:address_id>/delete", methods=["POST"])
@login_required
def delete_account_address(address_id):
    connection = get_db()
    try:
        address = connection.execute(
            """
            SELECT a.id, a.is_default, a.customer_id
            FROM addresses a
            JOIN customers c ON c.id = a.customer_id
            WHERE a.id = ? AND c.user_id = ?
            """,
            (address_id, current_user.id)
        ).fetchone()
        if address is None:
            abort(404)
        connection.execute("DELETE FROM addresses WHERE id = ?", (address_id,))
        if address["is_default"]:
            connection.execute(
                """
                UPDATE addresses SET is_default = 1
                WHERE id = (
                    SELECT id FROM addresses
                    WHERE customer_id = ? ORDER BY id DESC LIMIT 1
                )
                """,
                (address["customer_id"],)
            )
        connection.commit()
        flash("ລຶບທີ່ຢູ່ແລ້ວ.", "success")
        return redirect(url_for("account"))
    finally:
        connection.close()


# ---------- Admin: Categories Management ----------

@app.route("/admin/categories", methods=["GET", "POST"])
@admin_required
def admin_categories():
    connection = get_db()

    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()

            if not name:
                flash("ກະລຸນາປ້ອນຊື່ໝວດໝູ່.", "error")
            else:
                try:
                    connection.execute(
                        """
                        INSERT INTO categories (name, description)
                        VALUES (?, ?)
                        """,
                        (name, description)
                    )
                    connection.commit()
                    flash("ເພີ່ມໝວດໝູ່ສຳເລັດ.", "success")
                    return redirect(url_for("admin_categories"))

                except sqlite3.IntegrityError:
                    connection.rollback()
                    flash("ຊື່ໝວດໝູ່ນີ້ມີແລ້ວ.", "error")

        categories = connection.execute(
            """
            SELECT c.id, c.name, c.description, c.created_at,
                   COUNT(p.id) AS product_count
            FROM categories c
            LEFT JOIN products p ON p.category_id = c.id
            GROUP BY c.id
            ORDER BY c.id DESC
            """
        ).fetchall()

        return render_template(
            "admin/categories.html",
            categories=categories
        )
    finally:
        connection.close()


@app.route("/admin/categories/<int:category_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_category(category_id):
    connection = get_db()

    try:
        category = connection.execute(
            "SELECT * FROM categories WHERE id = ?",
            (category_id,)
        ).fetchone()

        if category is None:
            abort(404)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()

            if not name:
                flash("ກະລຸນາປ້ອນຊື່ໝວດໝູ່.", "error")
            else:
                try:
                    connection.execute(
                        """
                        UPDATE categories
                        SET name = ?, description = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (name, description, category_id)
                    )
                    connection.commit()
                    flash("ແກ້ໄຂໝວດໝູ່ສຳເລັດ.", "success")
                    return redirect(url_for("admin_categories"))

                except sqlite3.IntegrityError:
                    connection.rollback()
                    flash("ຊື່ໝວດໝູ່ນີ້ມີແລ້ວ.", "error")

            category = connection.execute(
                "SELECT * FROM categories WHERE id = ?",
                (category_id,)
            ).fetchone()

        return render_template(
            "admin/edit_category.html",
            category=category
        )
    finally:
        connection.close()


@app.route("/admin/categories/<int:category_id>/delete", methods=["POST"])
@admin_required
def admin_delete_category(category_id):
    connection = get_db()

    try:
        category = connection.execute(
            "SELECT id FROM categories WHERE id = ?",
            (category_id,)
        ).fetchone()

        if category is None:
            abort(404)

        try:
            connection.execute(
                "DELETE FROM categories WHERE id = ?",
                (category_id,)
            )
            connection.commit()
            flash("ລຶບໝວດໝູ່ສຳເລັດ.", "success")

        except sqlite3.IntegrityError:
            connection.rollback()
            flash(
                "ລຶບບໍ່ໄດ້: ໝວດໝູ່ນີ້ອາດມີສິນຄ້າອ້າງອີງຢູ່.",
                "error"
            )

        return redirect(url_for("admin_categories"))
    finally:
        connection.close()

# ---------- Admin: Products Management ----------

@app.route("/admin/products", methods=["GET", "POST"])
@admin_required
def admin_products():
    connection = get_db()

    try:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            category_id = request.form.get("category_id", "").strip()
            price = request.form.get("price", "").strip()
            stock = request.form.get("stock", "").strip()
            image_url = request.form.get("image_url", "").strip()

            if not all([name, category_id, price, stock]):
                flash("ກະລຸນາປ້ອນຂໍ້ມູນທີ່ຈຳເປັນ.", "error")
            else:
                try:
                    category_id = int(category_id)
                    price = int(price)
                    stock = int(stock)

                    if price < 0 or stock < 0:
                        raise ValueError

                    category = connection.execute(
                        "SELECT id FROM categories WHERE id = ?",
                        (category_id,)
                    ).fetchone()

                    if category is None:
                        flash("ບໍ່ພົບໝວດໝູ່ທີ່ເລືອກ.", "error")
                    else:
                        cursor = connection.execute(
                            """
                            INSERT INTO products
                                (category_id, name, description, price,
                                 stock, image_url, created_by, is_active)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                            """,
                            (
                                category_id, name, description, price,
                                stock, image_url or None, current_user.id
                            )
                        )
                        if stock > 0:
                            connection.execute(
                                """
                                INSERT INTO stock_movements (
                                    product_id, created_by, movement_type,
                                    quantity_change, reason
                                ) VALUES (?, ?, 'restock', ?, 'initial stock')
                                """,
                                (cursor.lastrowid, current_user.id, stock)
                            )
                        connection.commit()
                        flash("ເພີ່ມລາຍການອາຫານສຳເລັດ.", "success")
                        return redirect(url_for("admin_products"))

                except ValueError:
                    flash("ລາຄາ ແລະສະຕັອກຕ້ອງເປັນຈຳນວນເຕັມ 0 ຫຼືຫຼາຍກວ່າ.", "error")
                except sqlite3.IntegrityError:
                    connection.rollback()
                    flash("ບັນທຶກບໍ່ສຳເລັດ. ກະລຸນາກວດຂໍ້ມູນ.", "error")

        categories = connection.execute(
            "SELECT id, name FROM categories ORDER BY name"
        ).fetchall()

        products = connection.execute(
            """
            SELECT p.*, c.name AS category_name
            FROM products p
            JOIN categories c ON c.id = p.category_id
            ORDER BY p.id DESC
            """
        ).fetchall()

        return render_template(
            "admin/products.html",
            products=products,
            categories=categories
        )
    finally:
        connection.close()


@app.route("/admin/products/<int:product_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_product(product_id):
    connection = get_db()

    try:
        product = connection.execute(
            "SELECT * FROM products WHERE id = ?",
            (product_id,)
        ).fetchone()

        if product is None:
            abort(404)

        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            category_id = request.form.get("category_id", "").strip()
            price = request.form.get("price", "").strip()
            stock = request.form.get("stock", "").strip()
            image_url = request.form.get("image_url", "").strip()

            try:
                category_id = int(category_id)
                price = int(price)
                stock = int(stock)

                if not name or price < 0 or stock < 0:
                    raise ValueError

                category = connection.execute(
                    "SELECT id FROM categories WHERE id = ?",
                    (category_id,)
                ).fetchone()

                if category is None:
                    raise ValueError

                stock_diff = stock - int(product["stock"])
                connection.execute(
                    """
                    UPDATE products
                    SET category_id = ?, name = ?, description = ?,
                        price = ?, stock = ?, image_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        category_id, name, description, price, stock,
                        image_url or None, product_id
                    )
                )
                if stock_diff != 0:
                    connection.execute(
                        """
                        INSERT INTO stock_movements (
                            product_id, created_by, movement_type,
                            quantity_change, reason
                        ) VALUES (?, ?, ?, ?, 'admin stock update')
                        """,
                        (
                            product_id,
                            current_user.id,
                            "restock" if stock_diff > 0 else "adjustment",
                            stock_diff,
                        )
                    )
                connection.commit()
                flash("ແກ້ໄຂລາຍການອາຫານສຳເລັດ.", "success")
                return redirect(url_for("admin_products"))

            except (ValueError, TypeError):
                flash("ກະລຸນາກວດຊື່, ໝວດໝູ່, ລາຄາ ແລະສະຕັອກ.", "error")
            except sqlite3.IntegrityError:
                connection.rollback()
                flash("ບັນທຶກບໍ່ສຳເລັດ.", "error")

            product = connection.execute(
                "SELECT * FROM products WHERE id = ?",
                (product_id,)
            ).fetchone()

        categories = connection.execute(
            "SELECT id, name FROM categories ORDER BY name"
        ).fetchall()

        return render_template(
            "admin/edit_product.html",
            product=product,
            categories=categories
        )
    finally:
        connection.close()


@app.route("/admin/products/<int:product_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_product(product_id):
    connection = get_db()

    try:
        product = connection.execute(
            "SELECT id, is_active FROM products WHERE id = ?",
            (product_id,)
        ).fetchone()

        if product is None:
            abort(404)

        new_status = 0 if product["is_active"] else 1

        connection.execute(
            """
            UPDATE products
            SET is_active = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (new_status, product_id)
        )
        connection.commit()

        flash(
            "ປິດການຂາຍແລ້ວ." if new_status == 0 else "ເປີດການຂາຍແລ້ວ.",
            "success"
        )
        return redirect(url_for("admin_products"))
    finally:
        connection.close()


@app.route("/api/products", methods=["GET"])
def api_products():
    connection = get_db()

    try:
        products = connection.execute(
            """
            SELECT
                p.id,
                p.name,
                p.description,
                p.price,
                p.stock,
                p.image_url AS image,
                c.name AS category
            FROM products AS p
            JOIN categories AS c ON c.id = p.category_id
            WHERE p.is_active = 1
            ORDER BY p.id DESC
            """
        ).fetchall()

        return jsonify([
            dict(product) for product in products
        ])

    finally:
        connection.close()


def order_accessible_to_current_request(order_row):
    """Allow the owner/admin, or the browser session that created a guest order."""
    if current_user.is_authenticated and current_user.role == "admin":
        return True
    if current_user.is_authenticated and order_row["user_id"] == current_user.id:
        return True
    guest_order_ids = {int(order_id) for order_id in session.get("guest_order_ids", [])}
    return order_row["id"] in guest_order_ids


def release_order_stock(connection, order_id, reason):
    """Return reserved stock once; caller must hold the transaction."""
    items = connection.execute(
        "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
        (order_id,)
    ).fetchall()
    for item in items:
        already_returned = connection.execute(
            """
            SELECT COALESCE(SUM(quantity_change), 0)
            FROM stock_movements
            WHERE order_id = ? AND product_id = ? AND movement_type = 'return'
            """,
            (order_id, item["product_id"])
        ).fetchone()[0]
        if already_returned == 0:
            connection.execute(
                "UPDATE products SET stock = stock + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item["quantity"], item["product_id"])
            )
            connection.execute(
                """
                INSERT INTO stock_movements (
                    product_id, order_id, created_by, movement_type,
                    quantity_change, reason
                ) VALUES (?, ?, ?, 'return', ?, ?)
                """,
                (
                    item["product_id"], order_id,
                    current_user.id if current_user.is_authenticated else None,
                    item["quantity"], reason,
                )
            )


@app.route("/api/orders", methods=["POST"])
def api_create_order():
    """ສ້າງອໍເດີຈາກກະຕ່າ ແລະຫັກ stock ໃນ transaction ດຽວ."""
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    phone = "".join(character for character in str(payload.get("phone", "")) if character.isdigit())
    address = str(payload.get("address", "")).strip()
    payment_label = str(payload.get("payment", "")).strip()
    items = payload.get("items")

    if "stripe" in payment_label.lower() and (stripe is None or not STRIPE_SECRET_KEY):
        return jsonify({
            "error": "ຍັງບໍ່ໄດ້ຕັ້ງ Stripe Test Key ໃນ .env."
        }), 503

    if not name or len(phone) < 8 or not address:
        return jsonify({"error": "ກະລຸນາປ້ອນຊື່, ເບີໂທ ແລະທີ່ຢູ່ໃຫ້ຄົບ."}), 400
    if not isinstance(items, list) or not items:
        return jsonify({"error": "ກະຕ່າຍັງວ່າງ."}), 400

    try:
        zone_fee = int(payload.get("shipping_fee", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "ຄ່າຈັດສົ່ງບໍ່ຖືກຕ້ອງ."}), 400

    if zone_fee not in (10000, 20000):
        return jsonify({"error": "ເຂດຈັດສົ່ງບໍ່ຖືກຕ້ອງ."}), 400

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        product_rows = {}
        subtotal = 0
        order_items = []

        for raw_item in items:
            try:
                product_id = int(raw_item.get("id"))
                quantity = int(raw_item.get("qty"))
            except (AttributeError, TypeError, ValueError):
                raise ValueError("ລາຍການອາຫານບໍ່ຖືກຕ້ອງ.")

            if product_id in product_rows or quantity <= 0:
                raise ValueError("ລາຍການອາຫານຊ້ຳ ຫຼືຈຳນວນບໍ່ຖືກຕ້ອງ.")

            product = connection.execute(
                """
                SELECT id, name, price, stock
                FROM products
                WHERE id = ? AND is_active = 1
                """,
                (product_id,)
            ).fetchone()

            if product is None:
                raise ValueError("ບໍ່ພົບລາຍການອາຫານ.")
            if product["stock"] < quantity:
                raise ValueError(f"ສິນຄ້າ {product['name']} ມີບໍ່ພຽງພໍ.")

            line_total = product["price"] * quantity
            subtotal += line_total
            product_rows[product_id] = product
            order_items.append({
                "product_id": product_id,
                "product_name": product["name"],
                "unit_price": product["price"],
                "quantity": quantity,
                "subtotal": line_total,
            })

        actual_shipping_fee = 0 if subtotal >= 200000 else zone_fee
        total = subtotal + actual_shipping_fee
        if total <= 0:
            raise ValueError("ຍອດລວມອໍເດີຕ້ອງຫຼາຍກວ່າ 0.")
        invoice_number = (
            f"078-{datetime.now().strftime('%Y%m%d%H%M%S')}-"
            f"{secrets.token_hex(3).upper()}"
        )

        customer_id = None
        if current_user.is_authenticated:
            customer = connection.execute(
                "SELECT id FROM customers WHERE user_id = ?",
                (current_user.id,)
            ).fetchone()
            if customer:
                customer_id = customer["id"]
                connection.execute(
                    "UPDATE customers SET phone = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (phone, customer_id)
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO customers (user_id, full_name, phone, email) VALUES (?, ?, ?, ?)",
                    (current_user.id, name, phone, current_user.email)
                )
                customer_id = cursor.lastrowid

        connection.execute(
            """
            INSERT INTO orders (
                invoice_number, customer_id, order_type, order_status,
                payment_status, subtotal, shipping_fee, total_amount,
                recipient_name, recipient_phone, shipping_address
            ) VALUES (?, ?, 'online', 'pending', 'pending', ?, ?, ?, ?, ?, ?)
            """,
            (
                invoice_number, customer_id, subtotal, actual_shipping_fee,
                total, name, phone, address
            )
        )
        order_id = connection.execute("SELECT last_insert_rowid()").fetchone()[0]

        for item in order_items:
            connection.execute(
                """
                INSERT INTO order_items (
                    order_id, product_id, product_name, unit_price,
                    quantity, subtotal
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id, item["product_id"], item["product_name"],
                    item["unit_price"], item["quantity"], item["subtotal"]
                )
            )
            connection.execute(
                "UPDATE products SET stock = stock - ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (item["quantity"], item["product_id"])
            )
            connection.execute(
                """
                INSERT INTO stock_movements (
                    product_id, order_id, created_by, movement_type,
                    quantity_change, reason
                ) VALUES (?, ?, ?, 'sale', ?, ?)
                """,
                (
                    item["product_id"], order_id,
                    current_user.id if current_user.is_authenticated else None,
                    -item["quantity"], invoice_number
                )
            )

        if "stripe" in payment_label.lower():
            payment_method = "card"
        elif "ສົດ" in payment_label:
            payment_method = "cash"
        else:
            payment_method = "bank_transfer"
        payment_cursor = connection.execute(
            """
            INSERT INTO payments (
                order_id, amount, payment_method, payment_mode, payment_status
            ) VALUES (?, ?, ?, 'test', 'pending')
            """,
            (order_id, total, payment_method)
        )
        connection.commit()

        response = {
            "id": order_id,
            "no": invoice_number,
            "date": datetime.now().strftime("%d/%m/%Y %H:%M"),
            "items": [
                {
                    "name": item["product_name"],
                    "price": item["unit_price"],
                    "qty": item["quantity"],
                }
                for item in order_items
            ],
            "subtotal": subtotal,
            "fee": actual_shipping_fee,
            "total": total,
            "name": name,
            "phone": phone,
            "address": address,
            "zone": "ໃນເມືອງ" if zone_fee == 10000 else "ນອກເມືອງ",
            "payment": payment_label,
            "paymentMethod": payment_method,
            "paymentStatus": "pending",
        }

        if "stripe" in payment_label.lower():
            stripe.api_key = STRIPE_SECRET_KEY
            try:
                checkout_session = stripe.checkout.Session.create(
                    mode="payment",
                    line_items=[
                        {
                            "price_data": {
                                "currency": STRIPE_CURRENCY,
                                "product_data": {"name": item["product_name"]},
                                # LAK is a two-decimal currency in Stripe's API.
                                "unit_amount": item["unit_price"] * 100,
                            },
                            "quantity": item["quantity"],
                        }
                        for item in order_items
                    ] + ([{
                        "price_data": {
                            "currency": STRIPE_CURRENCY,
                            "product_data": {"name": "ຄ່າຈັດສົ່ງ"},
                            "unit_amount": actual_shipping_fee * 100,
                        },
                        "quantity": 1,
                    }] if actual_shipping_fee else []),
                    success_url=url_for(
                        "stripe_success", _external=True
                    ) + "?session_id={CHECKOUT_SESSION_ID}",
                    cancel_url=url_for("home", _external=True),
                    customer_creation="always",
                    phone_number_collection={"enabled": True},
                    metadata={"order_id": str(order_id)},
                )
            except stripe.error.StripeError:
                # The reservation is kept for an abandoned Checkout, but a
                # failed session creation must not leave an unusable order.
                connection.execute("BEGIN IMMEDIATE")
                release_order_stock(connection, order_id, "Stripe Checkout creation failed")
                connection.execute(
                    "UPDATE orders SET order_status = 'cancelled', payment_status = 'failed', "
                    "cancelled_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (order_id,)
                )
                connection.execute(
                    "UPDATE payments SET payment_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (payment_cursor.lastrowid,)
                )
                connection.commit()
                app.logger.error("Stripe Checkout session creation failed")
                return jsonify({"error": "Stripe ບໍ່ສາມາດສ້າງ Checkout ໄດ້."}), 502
            connection.execute(
                "UPDATE payments SET transaction_ref = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (checkout_session.id, payment_cursor.lastrowid)
            )
            connection.commit()
            response["checkout_url"] = checkout_session.url

        if not current_user.is_authenticated:
            guest_order_ids = [int(order_id) for order_id in session.get("guest_order_ids", [])]
            session["guest_order_ids"] = (guest_order_ids + [int(order_id)])[-10:]

        return jsonify(response), 201

    except ValueError as error:
        connection.rollback()
        return jsonify({"error": str(error)}), 400
    except sqlite3.Error:
        connection.rollback()
        app.logger.exception("Create order database error")
        return jsonify({"error": "ບັນທຶກອໍເດີບໍ່ສຳເລັດ."}), 500
    finally:
        connection.close()


@app.route("/payment/success")
def stripe_success():
    return render_template("payment_success.html")


@app.route("/api/orders/<int:order_id>/slip", methods=["POST"])
def upload_payment_slip(order_id):
    """ຮັບສະລິບການໂອນສຳລັບອໍເດີທີ່ລໍຖ້າກວດ."""
    upload = request.files.get("slip")
    if upload is None or not upload.filename:
        return jsonify({"error": "ກະລຸນາເລືອກໄຟລ໌ສະລິບ."}), 400

    filename = secure_filename(upload.filename)
    extension = Path(filename).suffix.lower()
    if extension not in {".jpg", ".jpeg", ".png", ".pdf"}:
        return jsonify({"error": "ຮອງຮັບສະເພາະ JPG, PNG ແລະ PDF."}), 400

    connection = get_db()
    try:
        order = connection.execute(
            """
            SELECT o.id, o.order_status, c.user_id,
                   p.id AS payment_id, p.payment_status
            FROM orders o
            LEFT JOIN customers c ON c.id = o.customer_id
            LEFT JOIN payments p ON p.order_id = o.id
            WHERE o.id = ?
            """,
            (order_id,)
        ).fetchone()
        if order is None or order["payment_id"] is None:
            return jsonify({"error": "ບໍ່ພົບອໍເດີ."}), 404
        if not order_accessible_to_current_request(order):
            return jsonify({"error": "ບໍ່ມີສິດອັບໂຫຼດສະລິບສຳລັບອໍເດີນີ້."}), 403
        if order["order_status"] == "cancelled" or order["payment_status"] != "pending":
            return jsonify({"error": "ອໍເດີນີ້ບໍ່ຢູ່ໃນສະຖານະລໍຖ້າກວດ."}), 400

        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        saved_name = f"order-{order_id}-{uuid4().hex}{extension}"
        upload.save(UPLOAD_DIR / saved_name)
        connection.execute(
            "UPDATE payments SET slip_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (saved_name, order["payment_id"])
        )
        connection.commit()
        return jsonify({"message": "ອັບໂຫຼດສະລິບສຳເລັດ.", "filename": saved_name})
    finally:
        connection.close()


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """ຢືນຢັນການຈ່າຍຈາກ Stripe ດ້ວຍ signature ຂອງ webhook."""
    if stripe is None or not STRIPE_WEBHOOK_SECRET:
        return jsonify({"error": "Stripe webhook ຍັງບໍ່ໄດ້ຕັ້ງຄ່າ."}), 503

    try:
        event = stripe.Webhook.construct_event(
            request.data,
            request.headers.get("Stripe-Signature", ""),
            STRIPE_WEBHOOK_SECRET,
        )
    except (ValueError, stripe.error.SignatureVerificationError):
        return jsonify({"error": "Invalid webhook signature."}), 400

    session = event["data"]["object"]
    order_id = (session.get("metadata") or {}).get("order_id")
    if not order_id:
        return jsonify({"received": True})

    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid order metadata."}), 400

    event_type = event.get("type")
    handled_types = {
        "checkout.session.completed",
        "checkout.session.async_payment_succeeded",
        "checkout.session.async_payment_failed",
        "checkout.session.expired",
    }
    if event_type not in handled_types:
        return jsonify({"received": True})

    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT id, order_status, payment_status FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()
        payment = connection.execute(
            "SELECT id, payment_status, transaction_ref FROM payments WHERE order_id = ?",
            (order_id,)
        ).fetchone()
        if order is None or payment is None:
            connection.rollback()
            return jsonify({"error": "Order not found."}), 404

        transaction_ref = session.get("payment_intent") or session.get("id")
        if event_type in {"checkout.session.completed", "checkout.session.async_payment_succeeded"}:
            # Re-delivered events are safe: a paid order is never charged or
            # stock-adjusted again, and a cancelled order is not resurrected.
            if order["payment_status"] not in {"paid", "refunded"} and order["order_status"] != "cancelled":
                connection.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'paid', order_status = 'confirmed',
                        confirmed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND payment_status NOT IN ('paid', 'refunded')
                    """,
                    (order_id,)
                )
                connection.execute(
                    """
                    UPDATE payments
                    SET payment_status = 'succeeded', paid_at = CURRENT_TIMESTAMP,
                        transaction_ref = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE order_id = ? AND payment_status != 'succeeded'
                    """,
                    (transaction_ref, order_id)
                )
        elif event_type == "checkout.session.async_payment_failed":
            if payment["payment_status"] == "pending":
                connection.execute(
                    "UPDATE payments SET payment_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (payment["id"],)
                )
        elif event_type == "checkout.session.expired":
            # Stripe confirms the Checkout expired before releasing the
            # reservation; this cannot race into a paid state silently.
            if order["payment_status"] == "pending" and order["order_status"] == "pending":
                release_order_stock(connection, order_id, "Stripe Checkout expired")
                connection.execute(
                    """
                    UPDATE orders
                    SET order_status = 'cancelled', payment_status = 'failed',
                        cancelled_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND payment_status = 'pending' AND order_status = 'pending'
                    """,
                    (order_id,)
                )
                connection.execute(
                    "UPDATE payments SET payment_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE id = ? AND payment_status = 'pending'",
                    (payment["id"],)
                )
        connection.commit()
    except (sqlite3.Error, ValueError):
        connection.rollback()
        app.logger.exception("Stripe webhook processing error")
        return jsonify({"error": "Webhook processing failed."}), 500
    finally:
        connection.close()

    return jsonify({"received": True})


# ---------- Admin: Orders Management ----------

@app.route("/admin/orders")
@admin_required
def admin_orders():
    status_filter = request.args.get("status", "").strip()
    allowed_filters = {"pending", "confirmed", "processing", "shipped", "completed", "cancelled"}
    connection = get_db()
    try:
        if status_filter in allowed_filters:
            order_rows = connection.execute(
                """
                SELECT o.*, u.username AS customer_username,
                       p.slip_path, p.payment_method
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                LEFT JOIN users u ON u.id = c.user_id
                LEFT JOIN payments p ON p.order_id = o.id
                WHERE o.order_status = ?
                ORDER BY o.id DESC
                """,
                (status_filter,)
            ).fetchall()
        else:
            status_filter = ""
            order_rows = connection.execute(
                """
                SELECT o.*, u.username AS customer_username,
                       p.slip_path, p.payment_method
                FROM orders o
                LEFT JOIN customers c ON c.id = o.customer_id
                LEFT JOIN users u ON u.id = c.user_id
                LEFT JOIN payments p ON p.order_id = o.id
                ORDER BY o.id DESC
                """
            ).fetchall()

        orders = []
        for row in order_rows:
            record = dict(row)
            record["items"] = [
                dict(item) for item in connection.execute(
                    """
                    SELECT product_name, quantity, unit_price, subtotal
                    FROM order_items
                    WHERE order_id = ?
                    ORDER BY id
                    """,
                    (row["id"],)
                ).fetchall()
            ]
            orders.append(record)
        return render_template("admin/orders.html", orders=orders, status_filter=status_filter)
    finally:
        connection.close()


@app.route("/admin/orders/<int:order_id>/slip")
@admin_required
def admin_view_payment_slip(order_id):
    connection = get_db()
    try:
        row = connection.execute(
            "SELECT slip_path FROM payments WHERE order_id = ?",
            (order_id,)
        ).fetchone()
    finally:
        connection.close()
    if not row or not row["slip_path"]:
        abort(404)
    return send_from_directory(UPLOAD_DIR, row["slip_path"])


@app.route("/admin/orders/<int:order_id>/status", methods=["POST"])
@admin_required
def admin_update_order_status(order_id):
    allowed_statuses = {
        "pending", "confirmed", "processing",
        "shipped", "completed", "cancelled"
    }
    status = request.form.get("status", "").strip()
    if status not in allowed_statuses:
        flash("ສະຖານະອໍເດີບໍ່ຖືກຕ້ອງ.", "error")
        return redirect(url_for("admin_orders"))
    if status == "cancelled":
        flash("ໃຫ້ໃຊ້ປຸ່ມຍົກເລີກ/ຄືນ Stock ເພື່ອປ້ອງກັນການຄືນ Stock ຊ້ຳ.", "error")
        return redirect(url_for("admin_orders"))

    connection = get_db()
    try:
        order = connection.execute(
            "SELECT id, order_status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            abort(404)
        if order["order_status"] == "cancelled":
            flash("ອໍເດີທີ່ຍົກເລີກແລ້ວບໍ່ສາມາດປ່ຽນສະຖານະຄືນໄດ້.", "error")
            return redirect(url_for("admin_orders"))

        connection.execute(
            """
            UPDATE orders
            SET order_status = ?,
                confirmed_at = CASE WHEN ? = 'confirmed' THEN CURRENT_TIMESTAMP ELSE confirmed_at END,
                cancelled_at = CASE WHEN ? = 'cancelled' THEN CURRENT_TIMESTAMP ELSE cancelled_at END,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (status, status, status, order_id)
        )
        connection.commit()
        flash("ອັບເດດສະຖານະອໍເດີສຳເລັດ.", "success")
        return redirect(url_for("admin_orders"))
    finally:
        connection.close()


@app.route("/admin/orders/<int:order_id>/payment", methods=["POST"])
@admin_required
def admin_confirm_order_payment(order_id):
    connection = get_db()
    try:
        order = connection.execute(
            "SELECT id, order_status FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if order is None:
            abort(404)
        if order["order_status"] == "cancelled":
            flash("ບໍ່ສາມາດຢືນຢັນຮັບເງິນສຳລັບອໍເດີທີ່ຍົກເລີກແລ້ວ.", "error")
            return redirect(url_for("admin_orders"))

        connection.execute(
            """
            UPDATE orders
            SET payment_status = 'paid',
                order_status = CASE WHEN order_status = 'pending' THEN 'confirmed' ELSE order_status END,
                confirmed_at = COALESCE(confirmed_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,)
        )
        connection.execute(
            """
            UPDATE payments
            SET payment_status = 'succeeded',
                paid_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE order_id = ? AND payment_status = 'pending'
            """,
            (order_id,)
        )
        connection.commit()
        flash("ຢືນຢັນຮັບເງິນສຳເລັດ.", "success")
        return redirect(url_for("admin_orders"))
    finally:
        connection.close()


@app.route("/admin/orders/<int:order_id>/cancel", methods=["POST"])
@admin_required
def admin_cancel_order(order_id):
    connection = get_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT id, order_status, payment_status FROM orders WHERE id = ?",
            (order_id,)
        ).fetchone()
        payment = connection.execute(
            "SELECT payment_method, transaction_ref, payment_status FROM payments WHERE order_id = ?",
            (order_id,)
        ).fetchone()
        if order is None:
            abort(404)
        if order["order_status"] in {"cancelled", "completed"}:
            flash("ອໍເດີນີ້ບໍ່ສາມາດຍົກເລີກໄດ້.", "error")
            return redirect(url_for("admin_orders"))

        if payment and payment["payment_method"] == "card" and payment["payment_status"] == "succeeded":
            if stripe is None or not STRIPE_SECRET_KEY or not payment["transaction_ref"]:
                flash("ບໍ່ພົບ Stripe key ຫຼື Payment Intent ສຳລັບຄືນເງິນ.", "error")
                return redirect(url_for("admin_orders"))
            try:
                stripe.api_key = STRIPE_SECRET_KEY
                stripe.Refund.create(payment_intent=payment["transaction_ref"])
            except stripe.error.StripeError:
                connection.rollback()
                app.logger.exception("Stripe refund error")
                flash("Stripe ຄືນເງິນບໍ່ສຳເລັດ.", "error")
                return redirect(url_for("admin_orders"))

        items = connection.execute(
            "SELECT product_id, quantity FROM order_items WHERE order_id = ?",
            (order_id,)
        ).fetchall()
        for item in items:
            already_returned = connection.execute(
                """
                SELECT COALESCE(SUM(quantity_change), 0)
                FROM stock_movements
                WHERE order_id = ? AND product_id = ? AND movement_type = 'return'
                """,
                (order_id, item["product_id"])
            ).fetchone()[0]
            if already_returned == 0:
                connection.execute(
                    "UPDATE products SET stock = stock + ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (item["quantity"], item["product_id"])
                )
                connection.execute(
                    """
                    INSERT INTO stock_movements (
                        product_id, order_id, created_by, movement_type,
                        quantity_change, reason
                    ) VALUES (?, ?, ?, 'return', ?, 'order cancelled')
                    """,
                    (item["product_id"], order_id, current_user.id, item["quantity"])
                )

        connection.execute(
            """
            UPDATE orders
            SET order_status = 'cancelled',
                payment_status = CASE
                    WHEN payment_status = 'paid' THEN 'refunded'
                    WHEN payment_status IN ('pending', 'unpaid') THEN 'failed'
                    ELSE payment_status
                END,
                cancelled_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (order_id,)
        )
        if payment:
            if payment["payment_status"] == "succeeded":
                connection.execute(
                    "UPDATE payments SET payment_status = 'refunded', updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                    (order_id,)
                )
            elif payment["payment_status"] == "pending":
                connection.execute(
                    "UPDATE payments SET payment_status = 'failed', updated_at = CURRENT_TIMESTAMP WHERE order_id = ?",
                    (order_id,)
                )
        connection.commit()
        flash("ຍົກເລີກອໍເດີ ແລະຄືນ Stock ສຳເລັດ.", "success")
        return redirect(url_for("admin_orders"))
    finally:
        connection.close()


# ---------- ສ້າງ Admin ຜ່ານ Terminal ເທົ່ານັ້ນ ----------

@app.cli.command("create-admin")
def create_admin():
    """ສ້າງ Admin ຈາກ Terminal; ບໍ່ມີ Admin Register ສາທາລະນະ."""
    import click

    username = click.prompt("Admin username").strip()
    email = click.prompt("Admin email").strip().lower()
    password = click.prompt(
        "Admin password",
        hide_input=True,
        confirmation_prompt=True
    )

    if not username or not email or len(password) < 8:
        raise click.ClickException(
            "Username/Email ຫ້າມວ່າງ ແລະ password ຕ້ອງມີຢ່າງໜ້ອຍ 8 ຕົວ."
        )

    connection = get_db()
    try:
        existing = connection.execute(
            """
            SELECT 1 FROM users
            WHERE lower(username) = lower(?) OR lower(email) = lower(?)
            """,
            (username, email)
        ).fetchone()
        if existing is not None:
            raise click.ClickException("Username ຫຼື Email ນີ້ມີແລ້ວ.")

        connection.execute(
            """
            INSERT INTO users
                (username, email, password_hash, role, is_active)
            VALUES (?, ?, ?, 'admin', 1)
            """,
            (username, email, generate_password_hash(password))
        )
        connection.commit()
        click.echo("ສ້າງ Admin ສຳເລັດ.")
    except sqlite3.IntegrityError:
        connection.rollback()
        raise click.ClickException("Username ຫຼື Email ນີ້ມີແລ້ວ.")
    finally:
        connection.close()


if __name__ == "__main__":
    app.run(debug=app.config["DEBUG"])
