from flask import Flask, render_template, request, redirect, url_for, session, flash
import sqlite3
import os
from datetime import datetime, timedelta
import smtplib
import stripe
from email.message import EmailMessage
import qrcode
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")

# -------------------------------------------------
# APP CONFIG
# -------------------------------------------------
app = Flask(__name__)
app.secret_key = "mypark_secret_key"
DATABASE = "database.db"

# -------------------------------------------------
# DATABASE HELPERS
# -------------------------------------------------
def get_db():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT UNIQUE,
        password TEXT,
        reward_points INTEGER DEFAULT 0
    )
    """)

    # Add new columns safely
    try:
        c.execute("ALTER TABLE users ADD COLUMN theme TEXT DEFAULT 'light'")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT 'en'")
    except sqlite3.OperationalError:
        pass

    # other tables unchanged
    c.execute("""
    CREATE TABLE IF NOT EXISTS vehicles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        brand TEXT,
        plate TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS parking_slots (
        slot_id TEXT PRIMARY KEY,
        floor INTEGER,
        status TEXT DEFAULT 'available'
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS bookings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        vehicle_id INTEGER,
        slot_id TEXT,
        start_time TEXT,
        end_time TEXT,
        amount REAL,
        qr_path TEXT,
        created_at TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER,
        payment_time TEXT
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        password TEXT
    )
    """)

    conn.commit()
    conn.close()



def init_parking_slots():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM parking_slots")
    if c.fetchone()[0] == 0:
        num = 1
        for floor in range(1, 5):
            for _ in range(100):
                c.execute(
                    "INSERT INTO parking_slots (slot_id, floor) VALUES (?, ?)",
                    (f"P{num}", floor)
                )
                num += 1

    conn.commit()
    conn.close()

# -------------------------------------------------
# QR & EMAIL UTILITIES
# -------------------------------------------------
def generate_qr(data, filename):
    os.makedirs("static/qrcodes", exist_ok=True)
    path = f"static/qrcodes/{filename}"
    img = qrcode.make(data)
    img.save(path)
    return path


def send_email(to_email, subject, body):
    EMAIL = "yourgmail@gmail.com"
    APP_PASSWORD = "your_app_password"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = EMAIL
    msg["To"] = to_email
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(EMAIL, APP_PASSWORD)
            server.send_message(msg)
        print("✅ Email sent to:", to_email)
    except Exception as e:
        print("⚠️ Email notification failed:", e)


def t(en, ms):
    return ms if session.get("language") == "ms" else en
app.jinja_env.globals.update(t=t)

# =========================================================
# AI HELPER FUNCTIONS (SAFE – READ ONLY)
# =========================================================

def get_peak_hour_data():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT strftime('%H', start_time) AS hour, COUNT(*) as count
        FROM bookings
        GROUP BY hour
        ORDER BY count DESC
    """)
    rows = c.fetchall()
    conn.close()

    if not rows:
        return None

    start_hour = int(rows[0][0])
    end_hour = (start_hour + 2) % 24

    return f"{start_hour:02d}:00 – {end_hour:02d}:00"


def get_occupancy_data():
    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT COUNT(*) FROM parking_slots")
    total_slots = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(*)
        FROM bookings
        WHERE start_time <= datetime('now', '+1 hour')
          AND end_time >= datetime('now')
    """)
    predicted = c.fetchone()[0]

    conn.close()

    if total_slots == 0:
        return None

    percent = int((predicted / total_slots) * 100)

    if percent < 40:
        level = "Low"
    elif percent < 70:
        level = "Medium"
    else:
        level = "High"

    return percent, level
def ai_recommend_slot(user_id):
    conn = get_db()
    c = conn.cursor()

    # Get current time
    now = datetime.now().isoformat()

    # Get available slots (not actively booked now)
    c.execute("""
        SELECT ps.slot_id, ps.floor,
               COUNT(b.id) AS total_usage
        FROM parking_slots ps
        LEFT JOIN bookings b ON ps.slot_id = b.slot_id
        WHERE ps.slot_id NOT IN (
            SELECT slot_id
            FROM bookings
            WHERE start_time <= ?
              AND end_time >= ?
        )
        GROUP BY ps.slot_id
        ORDER BY total_usage ASC, ps.floor ASC
        LIMIT 1
    """, (now, now))

    result = c.fetchone()
    conn.close()

    if result:
        return {
            "slot_id": result["slot_id"],
            "floor": result["floor"],
            "reason": "Least congested and currently available slot"
        }

    return {
        "slot_id": None,
        "floor": None,
        "reason": "No suitable slot found"
    }

# -------------------------------------------------
# ROUTES – AUTH
# -------------------------------------------------
@app.route("/")
def welcome():
    return render_template("welcome.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    error = None
    show_login_link = False

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        password = request.form["password"]
        confirm = request.form["confirm_password"]

        # ❌ Passwords do not match
        if password != confirm:
            error = "Passwords do not match."
            return render_template(
                "register.html",
                error=error,
                show_login_link=False
            )

        conn = get_db()
        c = conn.cursor()

        try:
            c.execute(
                "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
                (name, email, password)
            )
            conn.commit()
            conn.close()

            # ✅ New email → redirect to login
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            conn.close()
            error = "Email already registered."
            show_login_link = True

    return render_template(
        "register.html",
        error=error,
        show_login_link=show_login_link
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (request.form["email"], request.form["password"])
        )
        user = c.fetchone()
        conn.close()

        if user:
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["theme"] = user["theme"]      # ✅ ADD
            session["language"] = user["language"]  # ✅ ADD
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid user"

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("welcome"))

# -------------------------------------------------
# USER DASHBOARD
# -------------------------------------------------
@app.route("/dashboard")
def dashboard():
    if "user_id" not in session:
        return redirect(url_for("login"))

    user_id = session["user_id"]
    conn = get_db()
    c = conn.cursor()

    # 1️⃣ Reward points
    c.execute("SELECT reward_points FROM users WHERE id=?", (user_id,))
    points = c.fetchone()["reward_points"]

    # 2️⃣ Active parking (current time within booking)
    now = datetime.now().isoformat()
    c.execute("""
        SELECT * FROM bookings
        WHERE user_id=?
          AND start_time <= ?
          AND end_time >= ?
        ORDER BY end_time ASC
        LIMIT 1
    """, (user_id, now, now))
    active_parking = c.fetchone()

    # 3️⃣ Vehicle count
    c.execute("SELECT COUNT(*) FROM vehicles WHERE user_id=?", (user_id,))
    vehicle_count = c.fetchone()[0]

    # 4️⃣ Recent activity (latest 5 bookings)
    c.execute("""
        SELECT slot_id, created_at
        FROM bookings
        WHERE user_id=?
        ORDER BY created_at DESC
        LIMIT 5
    """, (user_id,))
    recent_activity = c.fetchall()

    conn.close()

    return render_template(
        "dashboard.html",
        name=session["user_name"],
        points=points,
        active_parking=active_parking,
        vehicle_count=vehicle_count,
        recent_activity=recent_activity
    )


def ai_parking_insight(user_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT COUNT(*) FROM bookings
        WHERE user_id=? AND created_at >= datetime('now','-7 days')
    """, (user_id,))
    weekly = c.fetchone()[0]

    conn.close()

    if weekly >= 3:
        return "You park frequently. Consider longer bookings to save time."
    elif weekly == 0:
        return "No recent parking activity. Need help booking?"
    else:
        return "Your parking usage is normal this week."

# -------------------------------------------------
# VEHICLE
# -------------------------------------------------
@app.route("/vehicle", methods=["GET", "POST"])
def vehicle():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    c = conn.cursor()

    if request.method == "POST":
        c.execute(
            "INSERT INTO vehicles (user_id, brand, plate) VALUES (?, ?, ?)",
            (session["user_id"], request.form["brand"], request.form["plate"])
        )
        conn.commit()

    c.execute("SELECT * FROM vehicles WHERE user_id=?", (session["user_id"],))
    vehicles = c.fetchall()
    conn.close()

    return render_template("vehicle.html", vehicles=vehicles)

# -------------------------------------------------
# PARKING
# -------------------------------------------------
@app.route("/parking")
def parking():
    if "user_id" not in session:
        return redirect(url_for("login"))

    floor = int(request.args.get("floor", 1))
    now = datetime.now()

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT slot_id FROM parking_slots WHERE floor=?", (floor,))
    slots_raw = c.fetchall()

    slots = []

    for s in slots_raw:
        slot_id = s["slot_id"]

        # Check if slot has ANY booking overlapping NOW
        c.execute("""
            SELECT id, user_id, start_time, end_time
            FROM bookings
            WHERE slot_id=?
              AND start_time <= ?
              AND end_time > ?
            LIMIT 1
        """, (slot_id, now.isoformat(), now.isoformat()))
        active_booking = c.fetchone()

        if active_booking:
            # 🔴 ACTIVE (RED for everyone)
            slots.append({
                "slot_id": slot_id,
                "status": "booked",
                "color": "red",
                "booking_id": active_booking["id"],
                "booked_by": active_booking["user_id"],
                "start_time": active_booking["start_time"],
                "end_time": active_booking["end_time"]
            })
            continue

        # Check FUTURE booking by current user
        c.execute("""
            SELECT id, start_time, end_time
            FROM bookings
            WHERE slot_id=?
              AND user_id=?
              AND start_time > ?
            ORDER BY start_time ASC
            LIMIT 1
        """, (slot_id, session["user_id"], now.isoformat()))
        future_booking = c.fetchone()

        if future_booking:
            # 🟡 FUTURE (YELLOW for current user only)
            slots.append({
                "slot_id": slot_id,
                "status": "future",
                "color": "yellow",
                "booking_id": future_booking["id"],
                "start_time": future_booking["start_time"],
                "end_time": future_booking["end_time"]
            })
        else:
            # 🟢 AVAILABLE
            slots.append({
                "slot_id": slot_id,
                "status": "available",
                "color": "green"
            })

    # User vehicles
    c.execute("SELECT * FROM vehicles WHERE user_id=?", (session["user_id"],))
    vehicles = c.fetchall()

    conn.close()

    return render_template(
        "parking.html",
        slots=slots,
        vehicles=vehicles,
        floor=floor,
        current_user=session["user_id"]
    )
# =========================================================
# AI FEATURE 1: SMART SLOT RECOMMENDATION
# =========================================================
@app.route("/api/ai/recommend-slot")
def ai_recommend_slot():
    if "user_id" not in session:
        return {"slot_id": None, "reason": "Not logged in"}

    from datetime import datetime

    user_id = session["user_id"]
    now = datetime.now().isoformat()

    conn = get_db()
    c = conn.cursor()

    # 1️⃣ Find available slots (no active booking)
    c.execute("""
        SELECT ps.slot_id, ps.floor
        FROM parking_slots ps
        WHERE ps.slot_id NOT IN (
            SELECT slot_id
            FROM bookings
            WHERE start_time <= ?
              AND end_time >= ?
        )
    """, (now, now))
    available_slots = c.fetchall()

    if not available_slots:
        conn.close()
        return {"slot_id": None, "reason": "No available slots"}

    # 2️⃣ Find least-used slot historically
    c.execute("""
        SELECT ps.slot_id, ps.floor, COUNT(b.id) AS usage
        FROM parking_slots ps
        LEFT JOIN bookings b ON ps.slot_id = b.slot_id
        GROUP BY ps.slot_id
        ORDER BY usage ASC
        LIMIT 1
    """)
    best_slot = c.fetchone()
    conn.close()

    lang = session.get("language", "en")

    reason = (
    "Slot kurang digunakan dan tersedia sekarang"
    if lang == "ms"
    else "Least-used and currently available slot"
     )

    return {
    "slot_id": best_slot["slot_id"],
    "floor": best_slot["floor"],
    "reason": reason
    }
    

# =========================================================
# AI FEATURE 2: PEAK HOUR ANALYSIS
# =========================================================
@app.route("/api/ai/peak-hours", methods=["GET"])
def ai_peak_hours():
    peak = get_peak_hour_data()
    if not peak:
        return {"message": "No booking data available"}, 404

    return {"peak_hour": peak}
# =========================================================
# AI FEATURE 3: OCCUPANCY PREDICTION
# =========================================================
@app.route("/api/ai/occupancy-prediction", methods=["GET"])
def ai_occupancy_prediction():
    data = get_occupancy_data()
    if not data:
        return {"message": "No data available"}, 404

    percent, level = data

    return {
        "predicted_occupancy": percent,
        "occupancy_level": level,
        "time_window": "Next 1 hour"
    }
# =========================================================
# AI FEATURE 4: HELP DESK CHATBOT
# =========================================================
@app.route("/api/ai/chatbot", methods=["POST"])
def ai_chatbot():
    msg = request.json.get("message", "").lower().strip()

    if any(w in msg for w in ["hi", "hello", "hey"]):
        return {"reply": "Hello! 👋 I’m the MyPark Assistant. How can I help you today?"}

    if any(w in msg for w in ["recommend", "suggest", "slot"]):
        return {"reply": "I recommend choosing an available slot on a lower floor to reduce congestion."}

    if "parking" in msg:
        return {"reply": "Green = available, Red = booked, Yellow = your future booking."}

    if any(w in msg for w in ["peak", "busy", "crowded"]):
        return {"reply": "Peak hours usually occur during morning and late afternoon."}

    if "extend" in msg:
        return {"reply": "You can extend parking by clicking your booked slot before it ends."}

    if any(w in msg for w in ["payment", "failed"]):
        return {"reply": "Please retry payment or contact admin if the issue persists."}

    return {
        "reply": "I can help with parking guidance, availability, payments, or extensions."
    }
@app.route("/api/ai/recommend-slot")
def ai_recommend_slot_api():
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401

    suggestion = ai_recommend_slot(session["user_id"])
    return suggestion

@app.route("/api/ai/help", methods=["POST"])
def ai_help():
    user_msg = request.json.get("message", "").lower()
    lang = session.get("language", "en")

    def reply(en, ms):
        return ms if lang == "ms" else en

    if "book" in user_msg or "reserve" in user_msg or "tempah" in user_msg:
        response = reply(
            "You can book a parking slot by selecting a green slot.",
            "Anda boleh menempah slot parkir dengan memilih slot berwarna hijau."
        )

    elif "extend" in user_msg or "lanjut" in user_msg:
        response = reply(
            "Click your red booked slot to extend parking duration.",
            "Klik slot merah yang telah ditempah untuk melanjutkan tempoh parkir."
        )

    elif "payment" in user_msg or "pay" in user_msg or "bayar" in user_msg:
        response = reply(
            "Payments are handled securely via Stripe.",
            "Pembayaran dikendalikan dengan selamat melalui Stripe."
        )

    elif "points" in user_msg or "reward" in user_msg or "mata" in user_msg:
        response = reply(
            "You earn 1 point for every RM1 spent. 10 points equal RM1 discount.",
            "Anda mendapat 1 mata bagi setiap RM1 dibelanjakan. 10 mata bersamaan RM1 diskaun."
        )

    elif "cancel" in user_msg or "batal" in user_msg:
        response = reply(
            "Please contact admin for cancellation assistance.",
            "Sila hubungi pihak pentadbir untuk bantuan pembatalan."
        )

    else:
        response = reply(
            "I'm here to help with parking, booking, payments, and rewards.",
            "Saya di sini untuk membantu berkaitan parkir, tempahan, pembayaran dan ganjaran."
        )

    return {"reply": response}


# -------------------------------------------------
# BOOK
# -------------------------------------------------
@app.route("/book", methods=["POST"])
def book():
    if "user_id" not in session:
        return redirect(url_for("login"))

    start = datetime.strptime(request.form["start"], "%Y-%m-%dT%H:%M")
    end = datetime.strptime(request.form["end"], "%Y-%m-%dT%H:%M")
    amount = ((end - start).total_seconds() / 1800) * 2.5

    conn = get_db()
    c = conn.cursor()

    # 🔴 OVERLAP CHECK (PASTE HERE)
    c.execute("""
        SELECT 1 FROM bookings
        WHERE slot_id=?
          AND NOT (
            end_time <= ? OR start_time >= ?
          )
    """, (
        request.form["slot_id"],
        start.isoformat(),
        end.isoformat()
    ))

    if c.fetchone():
        conn.close()
        flash("This slot is already booked during the selected time.", "error")
        return redirect(url_for("parking"))

    # ✅ INSERT BOOKING (ONLY IF NO CONFLICT)
    c.execute("""
        INSERT INTO bookings (user_id, vehicle_id, slot_id, start_time, end_time, amount, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        session["user_id"],
        request.form["vehicle_id"],
        request.form["slot_id"],
        start.isoformat(),
        end.isoformat(),
        amount,
        datetime.now().isoformat()
    ))

    booking_id = c.lastrowid

    conn.commit()
    conn.close()

    return redirect(url_for("payment", booking_id=booking_id, action="book"))


# -------------------------------------------------
# EXTEND
# -------------------------------------------------
@app.route("/extend", methods=["POST"])
def extend():
    booking_id = request.form["booking_id"]
    extra_slots = int(request.form["extra_slots"])

    conn = get_db()
    c = conn.cursor()

    c.execute("SELECT end_time FROM bookings WHERE id=?", (booking_id,))
    old_end = datetime.fromisoformat(c.fetchone()["end_time"])

    new_end = old_end + timedelta(minutes=extra_slots * 30)
    extra_amount = extra_slots * 2.5

    c.execute("""
        UPDATE bookings
        SET end_time=?, amount=amount+?
        WHERE id=?
    """, (new_end.isoformat(), extra_amount, booking_id))

    # Email notification (EXTEND)
    c.execute("""
        SELECT u.email, u.name, v.brand, v.plate,
               b.slot_id, b.start_time, b.end_time
        FROM users u
        JOIN bookings b ON u.id=b.user_id
        JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.id=?
    """, (booking_id,))
    data = c.fetchone()

    conn.commit()
    conn.close()

    send_email(
        data["email"],
        "MyPark Parking Extension Notification",
        f"""Hello {data['name']}!

Your parking has been successfully EXTENDED.

Car: {data['brand']} - {data['plate']}
Parking Slot: {data['slot_id']}
Period:
Start: {data['start_time']}
End: {data['end_time']}

Thank you for using MyPark.
"""
    )
    return redirect(url_for("payment", booking_id=booking_id, action="reserve"))


# -------------------------------------------------
# PAYMENT
# -------------------------------------------------
@app.route("/payment/<int:booking_id>")
def payment(booking_id):
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT b.*, v.brand, v.plate
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.id=?
    """, (booking_id,))
    booking = c.fetchone()

    c.execute("SELECT reward_points FROM users WHERE id=?", (session["user_id"],))
    points = c.fetchone()["reward_points"]

    conn.close()
    action = request.args.get("action", "book")
    return render_template("payment.html", booking=booking, points=points, action=action)



@app.route("/confirm_payment", methods=["POST"])
def confirm_payment():
    booking_id = request.form["booking_id"]
    action = request.form.get("action", "book")

    # Reward points user wants to use
    use_points = request.form.get("use_points", "0")
    use_points = int(use_points) if use_points.isdigit() else 0

    conn = get_db()
    c = conn.cursor()

    # Fetch booking & user points
    c.execute("""
        SELECT b.amount, b.user_id, u.reward_points
        FROM bookings b
        JOIN users u ON b.user_id = u.id
        WHERE b.id=?
    """, (booking_id,))
    row = c.fetchone()

    if not row:
        conn.close()
        flash("Invalid booking.", "error")
        return redirect(url_for("parking"))

    amount = row["amount"]
    user_id = row["user_id"]
    current_points = row["reward_points"]

    # 🔒 SAFETY CHECK
    if use_points > current_points:
        use_points = 0

    # 🔒 Only multiples of 10 allowed
    use_points = (use_points // 10) * 10

    # 💸 Calculate discount
    discount_rm = use_points // 10  # 10 pts = RM1
    final_amount = max(amount - discount_rm, 0)

    # 🎯 Calculate earned points (RM 1 = 1 point)
    earned_points = int(final_amount)

    # 🔄 Update user reward points
    new_points = current_points - use_points + earned_points

    c.execute("""
        UPDATE users
        SET reward_points=?
        WHERE id=?
    """, (new_points, user_id))

    # 🧾 Record payment
    c.execute("""
        INSERT INTO payments (booking_id, payment_time)
        VALUES (?, ?)
    """, (booking_id, datetime.now().isoformat()))

    # 🔳 Generate QR
    qr_path = generate_qr(
        f"MyPark Booking ID: {booking_id}",
        f"booking_{booking_id}.png"
    )

    c.execute("""
        UPDATE bookings
        SET qr_path=?
        WHERE id=?
    """, (qr_path, booking_id))

    conn.commit()
    conn.close()

    # 🔔 Correct notification
    if action == "reserve":
        flash("Your parking slot has been reserved!", "success")
    else:
        flash("Your parking slot has been booked!", "success")

    return redirect(url_for("parking"))


@app.route("/create-checkout-session/<int:booking_id>")
def create_checkout_session(booking_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    use_points = request.args.get("use_points", 0, type=int)

    conn = get_db()
    c = conn.cursor()

    # Get booking
    c.execute("""
        SELECT amount, user_id
        FROM bookings
        WHERE id=?
    """, (booking_id,))
    booking = c.fetchone()

    if not booking:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("dashboard"))

    amount = float(booking["amount"])

    # 🎯 REWARD REDEMPTION LOGIC
    redeem_rm = use_points // 10  # 10 pts = RM 1
    redeem_rm = min(redeem_rm, int(amount))  # cannot exceed amount

    final_amount = round(amount - redeem_rm, 2)

    # Save redeemed points temporarily in session
    session["redeem_points"] = redeem_rm * 10
    session["final_amount"] = final_amount

    conn.close()

    checkout_session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        line_items=[{
            "price_data": {
                "currency": "myr",
                "product_data": {
                    "name": "MyPark Parking Payment"
                },
                "unit_amount": int(final_amount * 100),  # cents
            },
            "quantity": 1,
        }],
        mode="payment",
        success_url=url_for(
            "payment_success",
            booking_id=booking_id,
            _external=True
        ),
        cancel_url=url_for(
            "payment_cancel",
            booking_id=booking_id,
            _external=True
        )
    )

    return redirect(checkout_session.url)

@app.route("/payment-success/<int:booking_id>")
def payment_success(booking_id):
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    c = conn.cursor()

    # 1️⃣ Get booking details
    c.execute("""
        SELECT user_id, amount
        FROM bookings
        WHERE id=?
    """, (booking_id,))
    booking = c.fetchone()

    if not booking:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("dashboard"))

    user_id = booking["user_id"]
    original_amount = float(booking["amount"])

    # 2️⃣ Get redemption info from session
    redeemed_points = session.pop("redeem_points", 0)
    final_amount = session.pop("final_amount", original_amount)

    # 3️⃣ Insert Stripe-confirmed payment
    c.execute("""
        INSERT INTO payments (booking_id, payment_time)
        VALUES (?, ?)
    """, (booking_id, datetime.now().isoformat()))

    # 4️⃣ 🎯 Reward points logic
    # RM 1 = 1 point (based on FINAL amount paid)
    earned_points = int(final_amount)

    c.execute("""
        UPDATE users
        SET reward_points = reward_points - ? + ?
        WHERE id=?
    """, (redeemed_points, earned_points, user_id))

    # 5️⃣ Generate QR code for parking history
    qr_data = f"MyPark Booking ID: {booking_id}"
    qr_filename = f"booking_{booking_id}.png"
    qr_path = generate_qr(qr_data, qr_filename)

    c.execute("""
        UPDATE bookings
        SET qr_path=?
        WHERE id=?
    """, (qr_path, booking_id))

    conn.commit()
    conn.close()

    # 6️⃣ Success message
    if redeemed_points > 0:
        flash(
            f"Payment successful! RM {original_amount - final_amount:.2f} redeemed, "
            f"{earned_points} points earned 🎉",
            "success"
        )
    else:
        flash(
            f"Payment successful! {earned_points} reward points earned 🎉",
            "success"
        )

    return redirect(url_for("parking"))



@app.route("/payment-cancel/<int:booking_id>")
def payment_cancel(booking_id):
    flash("Payment was cancelled. No charges were made.", "error")
    return redirect(url_for("parking"))


# -------------------------------------------------
# HISTORY
# -------------------------------------------------
@app.route("/history")
def history():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        SELECT b.*, v.brand, v.plate
        FROM bookings b JOIN vehicles v ON b.vehicle_id=v.id
        WHERE b.user_id=?
        ORDER BY b.created_at DESC
    """, (session["user_id"],))

    history = c.fetchall()
    conn.close()
    return render_template("history.html", history=history)

@app.route("/settings", methods=["GET", "POST"])
def settings():
    if "user_id" not in session:
        return redirect(url_for("login"))

    conn = get_db()
    c = conn.cursor()

    if request.method == "POST":
        # ---------- CHANGE PASSWORD ----------
        if request.form.get("new_password"):
            new_pwd = request.form["new_password"]
            confirm_pwd = request.form["confirm_password"]

            if new_pwd == confirm_pwd:
                c.execute(
                    "UPDATE users SET password=? WHERE id=?",
                    (new_pwd, session["user_id"])
                )
                flash("Password updated successfully!", "success")
            else:
                flash("Passwords do not match!", "error")

        # ---------- THEME ----------
        theme = request.form.get("theme")
        if theme:
            c.execute(
                "UPDATE users SET theme=? WHERE id=?",
                (theme, session["user_id"])
            )
            session["theme"] = theme

        # ---------- LANGUAGE ----------
        language = request.form.get("language")
        if language:
            c.execute(
                "UPDATE users SET language=? WHERE id=?",
                (language, session["user_id"])
            )
            session["language"] = language

        conn.commit()

    c.execute("SELECT theme, language FROM users WHERE id=?", (session["user_id"],))
    prefs = c.fetchone()
    conn.close()

    return render_template("settings.html", prefs=prefs)

# -------------------------------------------------
# ADMIN
# -------------------------------------------------
@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    error = None

    if request.method == "POST":
        username = request.form.get("username")
        password = request.form.get("password")

        conn = get_db()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM admins WHERE username=? AND password=?",
            (username, password)
        )
        admin = c.fetchone()
        conn.close()

        if admin:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        else:
            error = "Invalid admin credentials"

    return render_template("admin_login.html", error=error)



@app.route("/admin/dashboard")
def admin_dashboard():
    if not session.get("admin"):
        return redirect(url_for("admin_login"))

    from datetime import datetime
    now = datetime.now().isoformat()

    conn = get_db()
    c = conn.cursor()

    # -------------------------
    # USERS
    # -------------------------
    c.execute("SELECT COUNT(*) FROM users")
    total_users = c.fetchone()[0]

    # -------------------------
    # ACTIVE BOOKINGS (TIME-BASED)
    # -------------------------
    c.execute("""
        SELECT COUNT(*)
        FROM bookings
        WHERE start_time <= ?
          AND end_time >= ?
    """, (now, now))
    active_bookings = c.fetchone()[0]

    # -------------------------
    # SLOT ANALYTICS (CORRECT)
    # -------------------------
    c.execute("SELECT COUNT(*) FROM parking_slots")
    total_slots = c.fetchone()[0]

    c.execute("""
        SELECT COUNT(DISTINCT slot_id)
        FROM bookings
        WHERE start_time <= ?
          AND end_time >= ?
    """, (now, now))
    booked_slots = c.fetchone()[0]

    available_slots = total_slots - booked_slots

    # -------------------------
    # TOTAL INCOME (PAID ONLY)
    # -------------------------
    c.execute("""
        SELECT SUM(b.amount)
        FROM bookings b
        JOIN payments p ON b.id = p.booking_id
    """)
    total_income = round(c.fetchone()[0] or 0, 2)

    # -------------------------
    # DAILY ANALYTICS
    # -------------------------
    c.execute("""
        SELECT substr(created_at, 1, 10), COUNT(*)
        FROM bookings
        GROUP BY substr(created_at, 1, 10)
        ORDER BY substr(created_at, 1, 10)
    """)
    daily = c.fetchall()
    daily_labels = [d[0] for d in daily]
    daily_counts = [d[1] for d in daily]

    # -------------------------
    # MONTHLY ANALYTICS
    # -------------------------
    c.execute("""
        SELECT substr(created_at, 1, 7), COUNT(*)
        FROM bookings
        GROUP BY substr(created_at, 1, 7)
        ORDER BY substr(created_at, 1, 7)
    """)
    monthly = c.fetchall()
    monthly_labels = [m[0] for m in monthly]
    monthly_counts = [m[1] for m in monthly]

    # -------------------------
    # HOURLY USAGE
    # -------------------------
    c.execute("""
        SELECT substr(start_time, 12, 2), COUNT(*)
        FROM bookings
        GROUP BY substr(start_time, 12, 2)
        ORDER BY substr(start_time, 12, 2)
    """)
    hourly = c.fetchall()
    hourly_labels = [h[0] + ":00" for h in hourly]
    hourly_counts = [h[1] for h in hourly]

    # -------------------------
    # PAYMENTS TABLE
    # -------------------------
    c.execute("""
        SELECT 
            p.booking_id,
            u.name,
            b.slot_id,
            b.amount,
            p.payment_time
        FROM payments p
        JOIN bookings b ON p.booking_id = b.id
        JOIN users u ON b.user_id = u.id
        ORDER BY p.payment_time DESC
    """)
    payments = c.fetchall()

    # -------------------------
    # USER REWARDS
    # -------------------------
    c.execute("SELECT name, email, reward_points FROM users")
    users_rewards = c.fetchall()

    # -------------------------
    # AI INSIGHTS
    # -------------------------
    c.execute("""
        SELECT ps.floor, COUNT(*) AS bookings
        FROM bookings b
        JOIN parking_slots ps ON b.slot_id = ps.slot_id
        GROUP BY ps.floor
        ORDER BY bookings DESC
    """)
    ai_floor_usage = c.fetchall()

    c.execute("""
        SELECT slot_id, COUNT(*) AS usage
        FROM bookings
        GROUP BY slot_id
        ORDER BY usage ASC
        LIMIT 5
    """)
    ai_underused_slots = c.fetchall()

    conn.close()

    return render_template(
        "admin_dashboard.html",
        total_users=total_users,
        active_bookings=active_bookings,
        available_slots=available_slots,
        booked_slots=booked_slots,
        total_income=total_income,
        daily_labels=daily_labels,
        daily_counts=daily_counts,
        monthly_labels=monthly_labels,
        monthly_counts=monthly_counts,
        hourly_labels=hourly_labels,
        hourly_counts=hourly_counts,
        payments=payments,
        users_rewards=users_rewards,
        ai_floor_usage=ai_floor_usage,
        ai_underused_slots=ai_underused_slots
    )






@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


# -------------------------------------------------
# MAIN
# -------------------------------------------------
if __name__ == "__main__":
    init_db()
    init_parking_slots()
    app.run(host="0.0.0.0", port=5000)









