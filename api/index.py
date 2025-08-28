# api/index.py
import os
import random
import re
import smtplib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, flash
from supabase import create_client, Client
import cloudinary
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


# Compute paths so templates/static are found regardless of working dir
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(
    __name__,
    template_folder=TEMPLATES_DIR,
    static_folder=STATIC_DIR,
)

# Load secrets from env vars (set these in Vercel, don't hardcode)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
# create_client import remains the same
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app.secret_key = os.getenv("FLASK_SECRET", "dev-secret-change-me")

def send_email_otp(to_email, otp, username):
    sender_email = os.getenv("EMAIL_SENDER")
    sender_password = os.getenv("EMAIL_PASSWORD") 

    subject = "Your Instagram Manager Verification Code"
    body = f"Hello {username},\n\nYour verification code is: {otp}\n\nEnter this on the site to verify your account."

    msg = MIMEText(body, "plain")
    msg["From"] = sender_email
    msg["To"] = to_email
    msg["Subject"] = subject

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, to_email, msg.as_string())


# ---------- Auth ----------
@app.route("/")
def index():
    if "user" not in session:
        return redirect(url_for("login"))
    return render_template("index.html", user=session["user"])

@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
def delete_post(post_id):
    result = supabase.table("posts_db_all").delete().eq("id", post_id).execute()
    return {"status": "success"}  # Only return after delete finishes

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        user = (supabase.table("user_info")
                        .select("*")
                        .eq("user_name", username)
                        .eq("password", password)   # ⚠️ demo only — hash later
                        .execute()
                        .data)

        if user:
            session["user"] = user[0]
            flash("Welcome back!", "success")   # green toast
            return redirect(url_for("index"))
        else:
            flash("Invalid credentials", "error")  # red toast
            return redirect(url_for("login"))      # redirect so toast shows cleanly

    return render_template("login.html")


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"status": "logged_out"})


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()

        # Check duplicate username/email in user_info
        dup = (supabase.table("user_info")
                        .select("user_name")
                        .or_(f"user_name.eq.{data['username']},email_address.eq.{data['email']}")
                        .execute()
                        .data)
        if dup:
            return jsonify({"error": "Username / email already taken"}), 400

        # Check duplicate username/email in otp table too
        dup_otp = (supabase.table("otp")
                        .select("user_name")
                        .or_(f"user_name.eq.{data['username']},email_address.eq.{data['email']}")
                        .execute()
                        .data)
        if dup_otp:
            supabase.table("otp").delete().eq("user_name", data["username"]).execute()

        # Generate OTP
        otp = str(random.randint(100_000, 999_999))

        # Store temporary signup data in otp table
        supabase.table("otp").insert({
            "user_name": data["username"],
            "email_address": data["email"],
            "password": data["password"],
            "plan": data["plan"],
            "otp_generated": otp,
            "created_at": datetime.utcnow().isoformat()
        }).execute()

        # Send email
        send_email_otp(data["email"], otp, data["username"])

        return jsonify({"status": "ok", "username": data["username"]})

    return render_template("signup.html")


@app.route("/verify")
def verify_page():
    return render_template("verify.html")


@app.route("/send_otp", methods=["POST"])
def send_otp():
    data = request.get_json()
    username = data.get("username")
    entered_otp = data.get("otp")

    # Fetch OTP record
    res = supabase.table("otp").select("*").eq("user_name", username).execute()
    if not res.data:
        return jsonify({"status": "error", "error": "User not found"})

    record = res.data[0]
    if str(record["otp_generated"]) != str(entered_otp):
        return jsonify({"status": "error", "error": "Invalid OTP"})

    # Map plan to limits
    plan_limits = {
        "Trial - 7 posts": 7,
        "Standard": 60,
        "Premium": 120
    }

    # Create full account in user_info table
    supabase.table("user_info").insert({
        "user_name": record["user_name"],
        "password": record["password"],
        "email_address": record["email_address"],
        "account_status": "Active",
        "subscription_type": record["plan"],
        "total_token_limit": plan_limits[record["plan"]],
        "tokens_used": 0,
        "is_trial": record["plan"] == "Trial - 7 posts",
    }).execute()

    # Remove from otp table
    supabase.table("otp").delete().eq("user_name", username).execute()

    return jsonify({"status": "verified"})

# ---------- API ----------
def load_cloudinary_config(username: str):
    row = (supabase.table("user_info")
                    .select("cloudinary_cloud_name,cloudinary_api_key,cloudinary_api_secret")
                    .eq("user_name", username)
                    .single()
                    .execute()
                    .data)
    if not row:
        raise ValueError("Cloudinary credentials missing")
    cloudinary.config(
        cloud_name=row["cloudinary_cloud_name"],
        api_key=row["cloudinary_api_key"],
        api_secret=row["cloudinary_api_secret"]
    )


# ---------- JSON endpoints ----------
@app.route("/api/account_status")
def api_account_status():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user = session["user"]
    return jsonify({
        "user_name": user["user_name"],
        "account_status": user.get("account_status"),
        "subscription_type": user.get("subscription_type"),
        "total_token_limit": user.get("total_token_limit"),
        "tokens_used": user.get("tokens_used", 0),
    })


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    username = session["user"]["user_name"]
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        supabase.table("user_info").update({
            "inst_access_token": data["inst_access_token"],
            "ig_user_id": data["ig_user_id"],
            "cloudinary_cloud_name": data["cloudinary_cloud_name"],
            "cloudinary_api_key": data["cloudinary_api_key"],
            "cloudinary_api_secret": data["cloudinary_api_secret"],
        }).eq("user_name", username).execute()
        # refresh session
        session["user"] = (supabase.table("user_info")
                                     .select("*")
                                     .eq("user_name", username)
                                     .execute().data[0])
        return jsonify({"status": "ok"})
    user = session["user"]
    return jsonify({
        "inst_access_token": user.get("inst_access_token", ""),
        "ig_user_id": user.get("ig_user_id", ""),
        "cloudinary_cloud_name": user.get("cloudinary_cloud_name", ""),
        "cloudinary_api_key": user.get("cloudinary_api_key", ""),
        "cloudinary_api_secret": user.get("cloudinary_api_secret", ""),
    })


@app.route("/api/business", methods=["GET", "POST"])
def api_business():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    username = session["user"]["user_name"]
    if request.method == "POST":
        data = request.get_json(silent=True) or request.form.to_dict()
        supabase.table("business_profile").upsert({
            "user_name": username,
            "business_name": data["business_name"],
            "business_introduction": data["business_introduction"],
            "products_services": data["products_services"],
        }).execute()
        return jsonify({"status": "ok"})
    row = (supabase.table("business_profile")
                   .select("*")
                   .eq("user_name", username)
                   .execute().data)
    return jsonify(row[0] if row else {})


@app.route("/api/criteria", methods=["GET", "POST"])
def api_criteria():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    username = session["user"]["user_name"]

    if request.method == "POST":
        data = request.json
        supabase.table("user_info").update({
            "num_of_posts": str(data["num_of_posts"]),
            "frequency": data["frequency"],
            "dontuseuntil": str(data["dontuseuntil"]),
            "posting_hours": data["posting_hours"],
        }).eq("user_name", username).execute()
        return jsonify({"status": "ok"})

    # Always fetch fresh values from DB
    row = (supabase.table("user_info")
           .select("num_of_posts,frequency,dontuseuntil,posting_hours")
           .eq("user_name", username)
           .single()
           .execute()
           .data)

    if not row or all(v in (None, "", "null") for v in row.values()):
        return jsonify({"message": "No criteria set yet"}), 200

    return jsonify({
        "num_of_posts": int(row["num_of_posts"]) if row.get("num_of_posts") not in (None, "", "null") else 1,
        "frequency": row.get("frequency") or "Daily",
        "dontuseuntil": int(row["dontuseuntil"]) if row.get("dontuseuntil") not in (None, "", "null") else 90,
        "posting_hours": row.get("posting_hours") or "11:00, 15:00",
    })


@app.route("/api/analytics", methods=["GET", "POST"])
def api_analytics():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user = session["user"]
    access_token = user.get("inst_access_token")
    ig_user_id = user.get("ig_user_id")
    if not (access_token and ig_user_id):
        return jsonify({"error": "config missing"}), 400

    # profile
    url = f"https://graph.facebook.com/v19.0/{ig_user_id}"
    params = {
        "fields": "username,name,biography,website,profile_picture_url,followers_count,media_count",
        "access_token": access_token
    }
    profile = requests.get(url, params=params).json()

    # recent posts
    posts_url = f"https://graph.facebook.com/v19.0/{ig_user_id}/media"
    posts_params = {
        "fields": "id,caption,media_type,media_url,timestamp,like_count,comments_count",
        "access_token": access_token,
        "limit": 50
    }
    recent_posts = requests.get(posts_url, params=posts_params).json().get("data", [])

    return jsonify({"profile": profile, "recent_posts": recent_posts})


@app.route("/api/insights", methods=["GET", "POST"])
def api_insights():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    user = session["user"]
    access_token = user.get("inst_access_token")
    ig_user_id = user.get("ig_user_id")
    if not (access_token and ig_user_id):
        return jsonify({"error": "config missing"}), 400

    # reach time-series
    url = f"https://graph.facebook.com/v19.0/{ig_user_id}/insights"
    params = {
        "metric": "reach",
        "period": "days_28",
        "metric_type": "time_series",
        "access_token": access_token
    }
    data = requests.get(url, params=params).json().get("data", [])
    reach_series = []
    if data and "values" in data[0]:
        reach_series = [{"date": v["end_time"][:10], "value": v["value"]}
                        for v in data[0]["values"]]

    # aggregate likes/comments
    posts_url = f"https://graph.facebook.com/v19.0/{ig_user_id}/media"
    posts_params = {"fields": "like_count,comments_count", "access_token": access_token, "limit": 100}
    posts = requests.get(posts_url, params=posts_params).json().get("data", [])
    total_likes = sum(p.get("like_count", 0) for p in posts)
    total_comments = sum(p.get("comments_count", 0) for p in posts)

    return jsonify({"reach_series": reach_series,
                    "total_likes": total_likes,
                    "total_comments": total_comments})


@app.route("/api/posts", methods=["GET"])
def api_posts():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    username = session["user"]["user_name"]
    rows = (supabase.table("posts_db_all")
                    .select("*")
                    .eq("user_name", username)
                    .order("scheduled_time", desc=False)
                    .execute()
                    .data)
    events = []
    for r in rows or []:
        events.append({
            "id": r["id"],
            "title": f"Post #{r['id']}",
            "start": r["scheduled_time"].replace(" ", "T") + "Z",
            "display": "block",
            "color": "#28a745" if r["posted"] == "Completed" else "#ffc107",
            "extendedProps": {
                "caption": r.get("caption", ""),
                "image_url": re.sub(r'<.*?>', '', r.get("image_url", "")).strip(),
                "status": r["posted"]
            }
        })
    return jsonify(events)   


@app.route("/api/posts/<int:post_id>", methods=["PUT"])
def api_update_post(post_id):
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True) or request.form.to_dict()
    updates = {}
    if "caption" in data:
        updates["caption"] = data["caption"]
    if "scheduled_time" in data:
        updates["scheduled_time"] = data["scheduled_time"]
    supabase.table("posts_db_all").update(updates).eq("id", post_id).eq("user_name", session["user"]["user_name"]).execute()
    return jsonify({"status": "updated"})


# ---------- Workflows API ----------
@app.route("/api/workflows", methods=["GET", "POST"])
def api_workflows():
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    
    username = session["user"]["user_name"]

    if request.method == "POST":
        data = request.get_json()

        # enforce max 5 conditions
        if len(data.get("conditions", [])) > 5:
            return jsonify({"error": "At most 5 conditions allowed"}), 400

        # enforce single action per condition
        for cond in data.get("conditions", []):
            if "actions" in cond and len(cond["actions"]) > 1:
                return jsonify({"error": "Only 1 action allowed per condition"}), 400

        workflow = {
            "user_name": username,
            "name": data.get("name", "Untitled Workflow"),
            "trigger": data.get("trigger", ""),
            # Ensure conditions/actions are always JSON
            "conditions": data.get("conditions", []),
            "created_at": datetime.utcnow().isoformat()
        }
        supabase.table("workflows").insert(workflow).execute()
        return jsonify({"status": "created"})

    # GET workflows for this user
    rows = (supabase.table("workflows")
                   .select("id, name, trigger, conditions, created_at")
                   .eq("user_name", username)
                   .order("created_at", desc=True)
                   .execute()
                   .data)
    return jsonify(rows)


@app.route("/api/workflows/<int:workflow_id>", methods=["PUT", "DELETE"])
def api_update_workflow(workflow_id):
    if "user" not in session:
        return jsonify({"error": "unauthorized"}), 401
    
    username = session["user"]["user_name"]

    if request.method == "PUT":
        data = request.get_json()

        # enforce limits
        if len(data.get("conditions", [])) > 5:
            return jsonify({"error": "At most 5 conditions allowed"}), 400
        for cond in data.get("conditions", []):
            if "actions" in cond and len(cond["actions"]) > 1:
                return jsonify({"error": "Only 1 action allowed per condition"}), 400

        supabase.table("workflows").update({
            "name": data.get("name", "Untitled Workflow"),
            "trigger": data.get("trigger", ""),
            "conditions": data.get("conditions", [])
        }).eq("id", workflow_id).eq("user_name", username).execute()
        return jsonify({"status": "updated"})

    # DELETE
    supabase.table("workflows").delete().eq("id", workflow_id).eq("user_name", username).execute()
    return jsonify({"status": "deleted"})
