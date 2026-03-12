import os, time, hmac, hashlib, base64, requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv
import psycopg2
from psycopg2.extras import RealDictCursor

load_dotenv()
app = Flask(__name__)

# ── 설정 ──────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")   # Supabase 연결 URL
SERVICE_ID   = os.getenv("SENS_SERVICE_ID", "")
ACCESS_KEY   = os.getenv("SENS_ACCESS_KEY", "")
SECRET_KEY   = os.getenv("SENS_SECRET_KEY", "")
FROM_NUMBER  = os.getenv("SENS_FROM_NUMBER", "")

# ── DB 연결 ────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    """앱 시작 시 테이블 없으면 자동 생성"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS students (
                    id     SERIAL PRIMARY KEY,
                    name   TEXT NOT NULL,
                    grade  TEXT,
                    parent TEXT,
                    phone  TEXT NOT NULL
                )
            """)
        conn.commit()

# ── SENS 서명 생성 ─────────────────────────────────────────
def make_signature(timestamp):
    uri = f"/sms/v2/services/{SERVICE_ID}/messages"
    msg = bytes(f"POST {uri}\n{timestamp}\n{ACCESS_KEY}", "UTF-8")
    return base64.b64encode(
        hmac.new(bytes(SECRET_KEY, "UTF-8"), msg, digestmod=hashlib.sha256).digest()
    ).decode("UTF-8")

def send_sms(to_phone, content):
    ts  = str(int(time.time() * 1000))
    url = f"https://sens.apigw.ntruss.com/sms/v2/services/{SERVICE_ID}/messages"
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "x-ncp-apigw-timestamp": ts,
        "x-ncp-iam-access-key": ACCESS_KEY,
        "x-ncp-apigw-signature-v2": make_signature(ts),
    }
    body = {
        "type": "SMS" if len(content) <= 90 else "LMS",
        "contentType": "COMM",
        "countryCode": "82",
        "from": FROM_NUMBER.replace("-", ""),
        "content": content,
        "messages": [{"to": to_phone.replace("-", "")}],
    }
    res = requests.post(url, headers=headers, json=body, timeout=10)
    return res.json()

# ── 페이지 라우트 ──────────────────────────────────────────
@app.route("/")
def student_page():
    return render_template("student.html")

@app.route("/admin")
def admin_page():
    return render_template("admin.html")

# ── 학생 API ───────────────────────────────────────────────
@app.route("/api/students", methods=["GET"])
def get_students():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students ORDER BY name")
            rows = cur.fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/students", methods=["POST"])
def add_student():
    d      = request.get_json()
    name   = d.get("name", "").strip()
    grade  = d.get("grade", "").strip()
    parent = d.get("parent", "").strip()
    phone  = d.get("phone", "").replace("-", "").strip()

    if not name or not phone:
        return jsonify({"success": False, "message": "이름과 전화번호는 필수입니다."}), 400
    if len(phone) < 10:
        return jsonify({"success": False, "message": "전화번호를 정확히 입력해주세요."}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO students (name, grade, parent, phone) VALUES (%s,%s,%s,%s)",
                (name, grade, parent, phone)
            )
        conn.commit()
    return jsonify({"success": True, "message": f"{name} 학생이 등록되었습니다."})

@app.route("/api/students/<int:sid>", methods=["PUT"])
def update_student(sid):
    d = request.get_json()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE students SET name=%s, grade=%s, parent=%s, phone=%s WHERE id=%s",
                (d.get("name"), d.get("grade"), d.get("parent"),
                 d.get("phone", "").replace("-", ""), sid)
            )
        conn.commit()
    return jsonify({"success": True})

@app.route("/api/students/<int:sid>", methods=["DELETE"])
def delete_student(sid):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM students WHERE id=%s", (sid,))
        conn.commit()
    return jsonify({"success": True})

# ── 등원 체크인 API ────────────────────────────────────────
@app.route("/api/checkin", methods=["POST"])
def checkin():
    d     = request.get_json()
    last4 = d.get("last4", "").strip()

    if not last4 or len(last4) != 4:
        return jsonify({"success": False, "message": "뒷자리 4자리를 입력해주세요."}), 400

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM students WHERE RIGHT(phone, 4) = %s", (last4,))
            matched = cur.fetchall()

    if not matched:
        return jsonify({"success": False, "message": "일치하는 번호가 없습니다. 선생님께 문의하세요."}), 404

    if not all([SERVICE_ID, ACCESS_KEY, SECRET_KEY, FROM_NUMBER]):
        return jsonify({"success": False, "message": "SENS 설정이 누락되었습니다."}), 500

    now  = time.localtime()
    h, m = now.tm_hour, now.tm_min
    ampm = "오전" if h < 12 else "오후"
    disp = h if h <= 12 else h - 12
    time_str = f"{ampm} {disp}시 {m:02d}분"

    sent_names = []
    for student in matched:
        parent_label = student["parent"] or "학부모"
        msg = (
            f"[{student['name']}] 등원 안내\n"
            f"안녕하세요, {parent_label}님.\n"
            f"{student['name']} 학생이 {time_str}에 학원에 등원하였습니다. 감사합니다."
        )
        try:
            result = send_sms(student["phone"], msg)
            if result.get("statusCode") == "202":
                sent_names.append(student["name"])
        except Exception:
            pass

    if sent_names:
        return jsonify({
            "success": True,
            "message": "부모님께 등원 문자가 발송되었습니다!",
            "student": ", ".join(sent_names),
            "time": time_str,
        })
    return jsonify({"success": False, "message": "문자 발송에 실패했습니다."}), 500

@app.route("/api/config-check")
def config_check():
    db_ok = False
    try:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        db_ok = True
    except Exception:
        pass
    return jsonify({
        "ready": all([SERVICE_ID, ACCESS_KEY, SECRET_KEY, FROM_NUMBER]) and db_ok,
        "db_ok": db_ok,
        "sens_ok": all([SERVICE_ID, ACCESS_KEY, SECRET_KEY, FROM_NUMBER]),
        "from_number_preview": FROM_NUMBER[:4] + "****" if FROM_NUMBER else "",
    })

if __name__ == "__main__":
    init_db()
    app.run(debug=True, host="0.0.0.0", port=5000)
