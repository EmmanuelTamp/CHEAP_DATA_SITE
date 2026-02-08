import os
import requests
import secrets
from flask import Flask, render_template, request, redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

# Ensure instance folder exists
os.makedirs(app.instance_path, exist_ok=True)

# Put DB inside /instance (recommended for Flask)
db_path = os.path.join(app.instance_path, "database.db")
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:///" + db_path
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

from flask_migrate import Migrate
migrate = Migrate(app, db)


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    fullname = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)


class Order(db.Model):
    __tablename__ = "orders"  # avoid reserved keyword issues in other DBs

    id = db.Column(db.Integer, primary_key=True)
    phone = db.Column(db.String(20), nullable=False)
    network = db.Column(db.String(20), nullable=False)
    plan = db.Column(db.String(50), nullable=False)
    status = db.Column(db.String(20), default="pending", nullable=False)

    paystack_reference = db.Column(db.String(100), unique=True, nullable=True)
    amount = db.Column(db.Integer, nullable=True)  # store in pesewas (GHS * 100)


@app.route("/")
def home():
    return render_template(
        "index.html",
        active="dashboard",
        wallet_balance="0.88"
    )


OFFERS = [
    {"gb": "1GB", "price": 5.28},
    {"gb": "2GB", "price": 10.44},
    {"gb": "3GB", "price": 15.36},
    {"gb": "4GB", "price": 20.88},
    {"gb": "5GB", "price": 25.56},
    {"gb": "6GB", "price": 30.72},
    {"gb": "8GB", "price": 40.80},
    {"gb": "10GB", "price": 47.49},
    {"gb": "15GB", "price": 68.42},
    {"gb": "20GB", "price": 90.50},
    {"gb": "25GB", "price": 112.70},
    {"gb": "30GB", "price": 135.70},
    {"gb": "40GB", "price": 181.13},
    {"gb": "50GB", "price": 223.10},
    {"gb": "100GB", "price": 431.25},
]

NETWORKS = {
    "mtn": {"label": "MTN", "logo": "img/mtn.png"},
    "telecel": {"label": "TELECEL", "logo": "img/telecel.png"},   # or vodafone.png if you use that
    "airteltigo": {"label": "AIRTELTIGO", "logo": "img/airteltigo.png"},
}


@app.route("/buy")
def buy():
    return render_template(
        "buy.html",
        active="buy",
        wallet_balance="0.88",
        networks=NETWORKS,
        selected=None,
        offers=[]
    )


@app.route("/checkout/<network>/<plan>", methods=["GET", "POST"])
def checkout(network, plan):
    network_key = (network or "").lower().strip()
    if network_key not in NETWORKS:
        abort(404)

    # Find price from OFFERS list
    price = None
    for o in OFFERS:
        if o["gb"] == plan:
            price = o["price"]
            break
    if price is None:
        abort(404)

    regions = [
        "Ahafo", "Ashanti", "Bono", "Bono East", "Central", "Eastern",
        "Greater Accra", "North East", "Northern", "Oti", "Savannah",
        "Upper East", "Upper West", "Volta", "Western", "Western North"
    ]

    if request.method == "POST":
        fullname = (request.form.get("fullname") or "").strip()
        contact = (request.form.get("contact") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        location = (request.form.get("location") or "").strip()  # optional
        recipient_phone = (request.form.get("recipient_phone") or "").strip()

        # Basic safety validation
        if not fullname or not contact or not email or not recipient_phone:
            abort(400, "Please fill all required fields")

        # Save/update user (avoid UNIQUE email error)
        user = User.query.filter_by(email=email).first()
        if user is None:
            user = User(fullname=fullname, phone=contact, email=email)
            db.session.add(user)
        else:
            user.fullname = fullname
            user.phone = contact

        # Create order
        new_order = Order(
            phone=recipient_phone,
            network=NETWORKS[network_key]["label"],
            plan=plan,
            status="pending",
        )

        # Set amount + reference for Paystack
        amount = get_offer_price_pesewas(plan)
        new_order.amount = amount

        reference = f"CDS_{new_order.network}_{plan}_{secrets.token_hex(6)}"
        new_order.paystack_reference = reference

        db.session.add(new_order)

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            raise

        # Initialize transaction on Paystack
        secret_key = os.environ.get("PAYSTACK_SECRET_KEY")
        if not secret_key:
            abort(500, "PAYSTACK_SECRET_KEY not set")

        callback_url = url_for("paystack_callback", _external=True)

        payload = {
            "email": email,
            "amount": amount,
            "currency": "GHS",
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {
                "order_id": new_order.id,
                "network": new_order.network,
                "plan": plan,
                "recipient_phone": recipient_phone,
            },
        }

        headers = {
            "Authorization": f"Bearer {secret_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        r = requests.post(
            "https://api.paystack.co/transaction/initialize",
            json=payload,
            headers=headers,
            timeout=30
        )
        data = r.json()

        if not data.get("status"):
            abort(502, f"Paystack init failed: {data.get('message')}")

        auth_url = data["data"]["authorization_url"]
        return redirect(auth_url)

    # GET request: show the form
    return render_template(
        "checkout.html",
        active="buy",
        wallet_balance="0.88",
        network_key=network_key,
        network_label=NETWORKS[network_key]["label"],
        plan=plan,
        price=price,
        regions=regions
    )

    # Save order
    new_order = Order(
        phone=recipient_phone,
        network=NETWORKS[network_key]["label"],
        plan=plan
    )
    db.session.add(new_order)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    # For now, go to receipt (later you’ll redirect to Paystack)
    return redirect(url_for("order_receipt", order_id=new_order.id))

    # GET request must return the checkout page
    return render_template(
        "checkout.html",
        active="buy",
        wallet_balance="0.88",
        network_key=network_key,
        network_label=NETWORKS[network_key]["label"],
        plan=plan,
        price=price,
        regions=regions
    )

    # Amount in pesewas (GHS * 100)
    amount = get_offer_price_pesewas(plan)
    new_order.amount = amount

    # Create a unique reference for Paystack
    reference = f"CDS_{new_order.network}_{plan}_{secrets.token_hex(6)}"
    new_order.paystack_reference = reference

    db.session.add(new_order)
    db.session.commit()

    # Initialize transaction on Paystack (Redirect API)
    secret_key = os.environ.get("PAYSTACK_SECRET_KEY")
    if not secret_key:
        abort(500, "PAYSTACK_SECRET_KEY not set")

    callback_url = url_for("paystack_callback", _external=True)

    payload = {
        "email": email,
        "amount": amount,
        "currency": "GHS",
        "reference": reference,
        "callback_url": callback_url,
        "metadata": {
            "order_id": new_order.id,
            "network": new_order.network,
            "plan": plan,
            "recipient_phone": recipient_phone,
        },
    }

    headers = {
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    r = requests.post("https://api.paystack.co/transaction/initialize", json=payload, headers=headers, timeout=30)
    data = r.json()

    if not data.get("status"):
        abort(502, f"Paystack init failed: {data.get('message')}")

    auth_url = data["data"]["authorization_url"]
    return redirect(auth_url)
    return redirect(url_for("order_receipt", order_id=new_order.id))
    
@app.route("/paystack/callback")
def paystack_callback():
    reference = request.args.get("reference")
    if not reference:
        abort(400, "Missing reference")

    secret_key = os.environ.get("PAYSTACK_SECRET_KEY")
    if not secret_key:
        abort(500, "PAYSTACK_SECRET_KEY not set")

    headers = {"Authorization": f"Bearer {secret_key}", "Accept": "application/json"}
    r = requests.get(f"https://api.paystack.co/transaction/verify/{reference}", headers=headers, timeout=30)
    data = r.json()

    if not data.get("status"):
        abort(502, f"Paystack verify failed: {data.get('message')}")

    payment_data = data.get("data", {})
    pay_status = payment_data.get("status")  # "success", "failed", etc.

    order = Order.query.filter_by(paystack_reference=reference).first()
    if not order:
        abort(404, "Order not found for this payment reference")

    if pay_status == "success":
        order.status = "paid"
        db.session.commit()
        return redirect(url_for("order_receipt", order_id=order.id))

    order.status = pay_status or "failed"
    db.session.commit()
    return f"Payment not successful. Status: {order.status}"


    return render_template(
        "checkout.html",
        active="buy",
        wallet_balance="0.88",
        network_key=network_key,
        network_label=NETWORKS[network_key]["label"],
        plan=plan,
        price=price,
        regions=regions
    )


def get_offer_price_pesewas(plan: str) -> int:
    # OFFERS is your list like [{"gb":"1GB","price":5.28}, ...]
    for o in OFFERS:
        if o["gb"] == plan:
            return int(round(float(o["price"]) * 100))  # GHS -> pesewas
    return 0


@app.route("/buy/<network>")
def buy_network(network):
    network = (network or "").lower().strip()
    if network not in NETWORKS:
        abort(404)

    return render_template(
        "buy.html",
        active="buy",
        wallet_balance="0.88",
        networks=NETWORKS,
        selected=network,
        offers=OFFERS
    )


@app.route("/order/<int:order_id>")
def order_receipt(order_id: int):
    order = Order.query.get_or_404(order_id)
    return render_template("order_receipt.html", order=order)


if __name__ == "__main__":
    # Create tables if they don't exist yet
    with app.app_context():
        db.create_all()

    app.run(debug=True)
