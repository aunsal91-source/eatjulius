"""JULIUS order-ahead backend.

Customers order and pay at eatjulius.com/order, pick a 10-minute collection
slot, and each slot takes at most SLOT_CAPACITY online orders so the walk-in
queue is never slowed down. Paid orders appear on the kitchen screen
(/kitchen) KITCHEN_LEAD_MINUTES before their slot starts.
"""
import json
import os
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg2
import psycopg2.extras
import stripe
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
KITCHEN_PIN = os.environ.get("KITCHEN_PIN", "")
PORT = int(os.environ.get("PORT", "5190"))
DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

SLOT_MINUTES = 10
SLOT_CAPACITY = int(os.environ.get("SLOT_CAPACITY", "4"))
# Earliest slot a customer can pick: now + this. Leaves time to pay before
# the order drops into the kitchen 3 minutes ahead of the slot.
MIN_LEAD_MINUTES = int(os.environ.get("MIN_LEAD_MINUTES", "10"))
KITCHEN_LEAD_MINUTES = int(os.environ.get("KITCHEN_LEAD_MINUTES", "3"))
# A started-but-unpaid checkout holds its slot place this long.
HOLD_MINUTES = int(os.environ.get("HOLD_MINUTES", "10"))
MAX_ITEMS_PER_ORDER = int(os.environ.get("MAX_ITEMS_PER_ORDER", "6"))

TZ = ZoneInfo("Europe/London")
# weekday() -> (open, close); Mon–Fri 11:00–21:00, Sat–Sun 11:00–18:00
HOURS = {d: ("11:00", "21:00") for d in range(5)} | {5: ("11:00", "18:00"), 6: ("11:00", "18:00")}

MENU = [
    {"id": "original", "name": "The Original Caesar", "tag": "Flagship · Signature sauce", "price": 1095,
     "desc": "Grilled chicken, romaine, parmesan and our signature Caesar: Parmigiano-Reggiano, anchovy, garlic, Dijon and lemon."},
    {"id": "chipotle", "name": "Smoky Chipotle Caesar", "tag": "The energy kick", "price": 1145,
     "desc": "Grilled chicken, romaine, parmesan, avocado, chipotle crema with lime and cumin."},
    {"id": "goddess", "name": "Green Goddess Caesar", "tag": "The light one", "price": 1145,
     "desc": "Grilled chicken, romaine, parmesan, avocado and herb ranch with basil, chives and tarragon."},
    {"id": "truffle", "name": "Black Truffle Caesar", "tag": "The indulgent one", "price": 1295,
     "desc": "Grilled chicken, romaine, parmesan, roasted mushrooms, black truffle tapenade and garlic aioli."},
]
MENU_BY_ID = {m["id"]: m for m in MENU}

stripe.api_key = STRIPE_SECRET_KEY

BASE_DIR = Path(__file__).resolve().parent
SITE_DIR = BASE_DIR.parent / "public"

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------- database

def get_conn():
    local = "127.0.0.1" in DATABASE_URL or "localhost" in DATABASE_URL
    last_err = None
    for attempt in range(3):
        try:
            return psycopg2.connect(DATABASE_URL, sslmode="disable" if local else "require", connect_timeout=10)
        except psycopg2.OperationalError as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err


def init_db():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id SERIAL PRIMARY KEY,
                token TEXT UNIQUE NOT NULL,
                code TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                slot_start TIMESTAMPTZ NOT NULL,
                customer_name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                items JSONB NOT NULL,
                total_pence INTEGER NOT NULL,
                stripe_session_id TEXT UNIQUE,
                hold_expires_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                paid_at TIMESTAMPTZ,
                ready_at TIMESTAMPTZ,
                collected_at TIMESTAMPTZ
            );
            CREATE INDEX IF NOT EXISTS orders_slot_idx ON orders (slot_start);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


# Orders that take up a place in their slot.
ACTIVE_SQL = "(status IN ('paid','ready','collected') OR (status = 'pending' AND hold_expires_at > now()))"


def get_setting(key, default=None):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
        row = cur.fetchone()
    return row[0] if row else default


def set_setting(key, value):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO settings (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""", (key, value))


def is_paused():
    return get_setting("paused", "0") == "1"


# ---------------------------------------------------------------- slots

def now_utc():
    return datetime.now(timezone.utc)


def day_slots(day):
    """All slot start times (aware, London) for a calendar date."""
    open_s, close_s = HOURS[day.weekday()]
    oh, om = map(int, open_s.split(":"))
    ch, cm = map(int, close_s.split(":"))
    t = datetime(day.year, day.month, day.day, oh, om, tzinfo=TZ)
    end = datetime(day.year, day.month, day.day, ch, cm, tzinfo=TZ)
    out = []
    while t + timedelta(minutes=SLOT_MINUTES) <= end:
        out.append(t)
        t += timedelta(minutes=SLOT_MINUTES)
    return out


def slot_counts(start, end):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(f"""SELECT slot_start, count(*) FROM orders
                        WHERE slot_start >= %s AND slot_start < %s AND {ACTIVE_SQL}
                        GROUP BY slot_start""", (start, end))
        return {row[0]: row[1] for row in cur.fetchall()}


def bookable_day():
    """Today if any slot is still bookable, otherwise the next trading day."""
    earliest = now_utc() + timedelta(minutes=MIN_LEAD_MINUTES)
    today = datetime.now(TZ).date()
    for offset in range(8):
        d = today + timedelta(days=offset)
        slots = [s for s in day_slots(d) if s >= earliest]
        if slots:
            return d, slots
    return today, []


def available_slots():
    day, slots = bookable_day()
    if not slots:
        return day, []
    counts = slot_counts(slots[0], slots[-1] + timedelta(minutes=SLOT_MINUTES))
    return day, [{
        "start": s.isoformat(),
        "label": s.strftime("%H:%M"),
        "left": max(0, SLOT_CAPACITY - counts.get(s, 0)),
    } for s in slots]


def day_label(d):
    today = datetime.now(TZ).date()
    if d == today:
        return "Today"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A %-d %B") if os.name != "nt" else d.strftime("%A %#d %B")


# ---------------------------------------------------------------- orders

def order_public(row):
    slot = row["slot_start"].astimezone(TZ)
    return {
        "token": row["token"],
        "code": row["code"],
        "status": row["status"],
        "name": row["customer_name"],
        "items": row["items"],
        "total": row["total_pence"],
        "slot": slot.strftime("%H:%M"),
        "slotEnd": (slot + timedelta(minutes=SLOT_MINUTES)).strftime("%H:%M"),
        "slotDay": day_label(slot.date()),
        "inKitchen": row["slot_start"] - timedelta(minutes=KITCHEN_LEAD_MINUTES) <= now_utc(),
    }


def get_order(token=None, session_id=None):
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if token:
            cur.execute("SELECT * FROM orders WHERE token = %s", (token,))
        else:
            cur.execute("SELECT * FROM orders WHERE stripe_session_id = %s", (session_id,))
        return cur.fetchone()


def mark_paid(session):
    """Idempotently mark an order paid from a completed Checkout Session."""
    if session.get("payment_status") != "paid":
        return
    order_id = int((session.get("metadata") or {}).get("order_id") or 0)
    details = session.get("customer_details") or {}
    with get_conn() as conn, conn.cursor() as cur:
        # serialise code assignment so two orders never share a number
        cur.execute("SELECT pg_advisory_xact_lock(4242)")
        cur.execute("SELECT status, slot_start FROM orders WHERE id = %s", (order_id,))
        row = cur.fetchone()
        if not row or row[0] not in ("pending", "expired", "cancelled"):
            return
        # Online codes restart each day: O-1, O-2 ... (walk-ins use tickets)
        day_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute("SELECT count(*) FROM orders WHERE code IS NOT NULL AND paid_at >= %s", (day_start,))
        code = f"O-{cur.fetchone()[0] + 1}"
        cur.execute("""UPDATE orders SET status = 'paid', paid_at = now(), code = %s,
                           email = %s, phone = %s
                       WHERE id = %s""",
                    (code, details.get("email"), details.get("phone"), order_id))


# ---------------------------------------------------------------- public API

@app.get("/api/menu")
def api_menu():
    day, slots = available_slots()
    return jsonify({
        "menu": MENU,
        "day": day_label(day),
        "slots": slots,
        "slotMinutes": SLOT_MINUTES,
        "paused": is_paused(),
        "maxItems": MAX_ITEMS_PER_ORDER,
        "capacity": SLOT_CAPACITY,
        "testMode": STRIPE_SECRET_KEY.startswith("sk_test_") or STRIPE_SECRET_KEY.startswith("rk_test_"),
    })


@app.post("/api/checkout")
def api_checkout():
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Payments aren't configured on this server yet."}), 503
    if is_paused():
        return jsonify({"error": "Online ordering is paused right now. Walk-ins welcome at the counter."}), 409

    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:40]
    if not name:
        return jsonify({"error": "Enter a name for the order."}), 400

    items, count, total = [], 0, 0
    for it in body.get("items") or []:
        m = MENU_BY_ID.get(it.get("id"))
        try:
            qty = int(it.get("qty"))
        except (TypeError, ValueError):
            qty = 0
        if not m or qty < 1:
            continue
        items.append({"id": m["id"], "name": m["name"], "qty": qty, "price": m["price"]})
        count += qty
        total += qty * m["price"]
    if not items:
        return jsonify({"error": "Add at least one wrap."}), 400
    if count > MAX_ITEMS_PER_ORDER:
        return jsonify({"error": f"Online orders are up to {MAX_ITEMS_PER_ORDER} wraps. For more, give us a call."}), 400

    try:
        slot = datetime.fromisoformat(body.get("slot") or "")
    except ValueError:
        slot = None
    _, slots = bookable_day()
    if not slot or slot not in slots:
        return jsonify({"error": "That collection time isn't available any more. Pick another."}), 409

    token = secrets.token_urlsafe(12)
    with get_conn() as conn, conn.cursor() as cur:
        # lock this slot so two customers can't both take its last place
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (int(slot.timestamp()),))
        cur.execute(f"SELECT count(*) FROM orders WHERE slot_start = %s AND {ACTIVE_SQL}", (slot,))
        if cur.fetchone()[0] >= SLOT_CAPACITY:
            return jsonify({"error": f"{slot.astimezone(TZ):%H:%M} just filled up. Pick the next slot.", "full": True}), 409
        cur.execute("""INSERT INTO orders (token, slot_start, customer_name, items, total_pence, hold_expires_at)
                       VALUES (%s, %s, %s, %s, %s, now() + %s * interval '1 minute') RETURNING id""",
                    (token, slot, name, json.dumps(items), total, HOLD_MINUTES))
        order_id = cur.fetchone()[0]

    origin = request.headers.get("Origin") or request.host_url.rstrip("/")
    slot_label = slot.astimezone(TZ).strftime("%H:%M")
    try:
        session = stripe.checkout.Session.create(
            mode="payment",
            # no payment_method_types: Stripe shows Apple Pay / Google Pay /
            # cards from the dashboard's payment-method settings
            line_items=[{
                "price_data": {
                    "currency": "gbp",
                    "product_data": {"name": i["name"]},
                    "unit_amount": i["price"],
                },
                "quantity": i["qty"],
            } for i in items],
            phone_number_collection={"enabled": True},
            payment_intent_data={"description": f"JULIUS order for {name}, collect {slot_label}"},
            custom_text={"submit": {"message": f"Collection at the JULIUS kiosk, Reuters Plaza, {slot_label}–"
                                               f"{(slot + timedelta(minutes=SLOT_MINUTES)).astimezone(TZ):%H:%M}."}},
            metadata={"site": "eatjulius", "order_id": str(order_id)},
            expires_at=int(time.time()) + 30 * 60,
            success_url=f"{origin}/order?o={token}",
            cancel_url=f"{origin}/order?cancel={token}",
        )
    except stripe.error.StripeError as e:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order_id,))
        return jsonify({"error": "Couldn't start the payment. Try again in a moment."}), 502

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET stripe_session_id = %s WHERE id = %s", (session.id, order_id))
    return jsonify({"url": session.url})


@app.get("/api/order/<token>")
def api_order(token):
    row = get_order(token=token)
    if not row:
        return jsonify({"error": "Order not found."}), 404
    if row["status"] == "pending" and row["stripe_session_id"] and STRIPE_SECRET_KEY:
        # the customer can land here before the webhook arrives
        session = stripe.checkout.Session.retrieve(row["stripe_session_id"])
        mark_paid(session.to_dict())
        row = get_order(token=token)
    return jsonify(order_public(row))


@app.post("/api/order/<token>/cancel")
def api_order_cancel(token):
    """Customer came back from Stripe without paying: free the slot now."""
    row = get_order(token=token)
    if not row or row["status"] != "pending":
        return jsonify({"ok": True})
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s AND status = 'pending'", (row["id"],))
    if row["stripe_session_id"] and STRIPE_SECRET_KEY:
        try:
            stripe.checkout.Session.expire(row["stripe_session_id"])
        except stripe.error.StripeError:
            pass
    return jsonify({"ok": True})


@app.post("/webhook")
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except (ValueError, stripe.error.SignatureVerificationError):
        return "", 400

    # stripe-python returns StripeObjects here — re-parse the verified payload
    session = json.loads(payload)["data"]["object"]
    if (session.get("metadata") or {}).get("site") != "eatjulius":
        return "", 200

    if event["type"] in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        mark_paid(session)
    elif event["type"] == "checkout.session.expired":
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("UPDATE orders SET status = 'expired' WHERE stripe_session_id = %s AND status = 'pending'",
                        (session.get("id"),))
    return "", 200


# ---------------------------------------------------------------- kitchen

def kitchen_ok():
    pin = request.headers.get("X-Kitchen-Pin", "")
    return bool(KITCHEN_PIN) and secrets.compare_digest(pin, KITCHEN_PIN)


@app.get("/api/kitchen")
def api_kitchen():
    if not kitchen_ok():
        return jsonify({"error": "Wrong PIN."}), 401
    now = now_utc()
    release_before = now + timedelta(minutes=KITCHEN_LEAD_MINUTES)
    today = datetime.now(TZ).date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=TZ)
    with get_conn() as conn, conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""SELECT * FROM orders
                       WHERE status IN ('paid','ready','collected') AND slot_start >= %s
                       ORDER BY slot_start, paid_at""", (day_start - timedelta(hours=1),))
        rows = cur.fetchall()

    def card(r):
        d = order_public(r)
        d["id"] = r["id"]
        d["phone"] = r["phone"]
        return d

    live = [card(r) for r in rows if r["status"] == "paid" and r["slot_start"] <= release_before]
    ready = [card(r) for r in rows if r["status"] == "ready"]
    upcoming = [card(r) for r in rows if r["status"] == "paid" and r["slot_start"] > release_before]
    collected = [card(r) for r in rows if r["status"] == "collected"][-8:]

    _, slots = available_slots()
    return jsonify({
        "live": live, "ready": ready, "upcoming": upcoming, "collected": collected,
        "paused": is_paused(), "capacity": SLOT_CAPACITY,
        "slots": [{"label": s["label"], "taken": SLOT_CAPACITY - s["left"]} for s in slots[:12]],
        "leadMinutes": KITCHEN_LEAD_MINUTES,
    })


@app.post("/api/kitchen/orders/<int:order_id>")
def api_kitchen_status(order_id):
    if not kitchen_ok():
        return jsonify({"error": "Wrong PIN."}), 401
    status = (request.get_json(silent=True) or {}).get("status")
    column = {"ready": "ready_at", "collected": "collected_at", "paid": None}.get(status, "bad")
    if column == "bad":
        return jsonify({"error": "Unknown status."}), 400
    with get_conn() as conn, conn.cursor() as cur:
        stamp = f", {column} = now()" if column else ""
        cur.execute(f"""UPDATE orders SET status = %s{stamp}
                        WHERE id = %s AND status IN ('paid','ready','collected')""", (status, order_id))
    return jsonify({"ok": True})


@app.post("/api/kitchen/pause")
def api_kitchen_pause():
    if not kitchen_ok():
        return jsonify({"error": "Wrong PIN."}), 401
    paused = bool((request.get_json(silent=True) or {}).get("paused"))
    set_setting("paused", "1" if paused else "0")
    return jsonify({"paused": paused})


# ---------------------------------------------------------------- pages

@app.get("/order")
def order_page():
    return send_from_directory(SITE_DIR, "order.html")


@app.get("/kitchen")
def kitchen_page():
    return send_from_directory(SITE_DIR, "kitchen.html")


@app.get("/")
@app.get("/<path:path>")
def static_files(path="index.html"):
    if not (SITE_DIR / path).is_file():
        path = "index.html"
    return send_from_directory(SITE_DIR, path)


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=DEBUG)
