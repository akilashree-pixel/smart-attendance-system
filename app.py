import streamlit as st
import sqlite3
import qrcode
import io
import csv
import secrets
import hashlib
import smtplib
import socket
import os
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from urllib.parse import urlencode
import extra_streamlit_components as stx

# =========================================================
# 1. APP SETTINGS
# =========================================================

st.set_page_config(
    page_title="Smart Campus Attendance System",
    page_icon="🎓",
    layout="wide"
)

DB_NAME = "attendance.db"
COOKIE_NAME = "smart_campus_login"
QR_VALID_MINUTES = 2
LOGIN_SESSION_DAYS = 14
PASSWORD_ITERATIONS = 310000

cookie_manager = stx.CookieManager()


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def get_connection():
    conn = sqlite3.connect(DB_NAME, timeout=20)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# =========================================================
# 2. AUTOMATIC QR ADDRESS DETECTION
# =========================================================

def get_local_ip():
    """Find the laptop's Wi-Fi/LAN IP address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        sock.close()

    return ip


def get_qr_base_url():
    """
    On Streamlit Cloud, use the public app host.
    Locally, use the laptop's network IP automatically.
    """

    try:
        headers = st.context.headers

        host = (
            headers.get("Host")
            or headers.get("host")
            or ""
        )

        if host and "streamlit.app" in host:
            return "https://" + host

    except Exception:
        pass

    local_ip = get_local_ip()
    port = os.environ.get("STREAMLIT_SERVER_PORT", "8501")

    return f"http://{local_ip}:{port}"


# =========================================================
# 3. DATABASE SETUP AND MIGRATION
# =========================================================

def setup_database():
    conn = get_connection()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS students (
            roll_no TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            year TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roll_no TEXT NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL,
            marked_at TEXT,
            UNIQUE(roll_no, attendance_date),
            FOREIGN KEY(roll_no) REFERENCES students(roll_no)
        )
    """)

    attendance_columns = [
        row[1] for row in cur.execute(
            "PRAGMA table_info(attendance)"
        ).fetchall()
    ]

    if "marked_at" not in attendance_columns:
        cur.execute(
            "ALTER TABLE attendance ADD COLUMN marked_at TEXT"
        )
        cur.execute("""
            UPDATE attendance
            SET marked_at = attendance_date || ' 00:00:00'
            WHERE marked_at IS NULL
        """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE,
            roll_no TEXT UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL,
            email_verified INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            FOREIGN KEY(roll_no) REFERENCES students(roll_no)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS otp_codes (
            email TEXT PRIMARY KEY,
            otp_hash TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS login_sessions (
            token_hash TEXT PRIMARY KEY,
            email TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS qr_sessions (
            token TEXT PRIMARY KEY,
            attendance_date TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS login_attempts (
            email TEXT PRIMARY KEY,
            attempts INTEGER NOT NULL DEFAULT 0,
            locked_until TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_email TEXT NOT NULL,
            action TEXT NOT NULL,
            roll_no TEXT,
            details TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


setup_database()


# =========================================================
# 4. PASSWORD AND TOKEN SECURITY
# =========================================================

def hash_password(password):
    salt = secrets.token_bytes(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        PASSWORD_ITERATIONS
    )

    return salt.hex() + ":" + password_hash.hex()


def verify_password(password, stored_hash):
    try:
        salt_hex, expected_hex = stored_hash.split(":")
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)

        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            PASSWORD_ITERATIONS
        )

        return secrets.compare_digest(actual, expected)

    except Exception:
        return False


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_email(email):
    return (
        "@" in email
        and "." in email.split("@")[-1]
        and len(email) <= 254
    )


# =========================================================
# 5. EMAIL OTP
# =========================================================

def send_email(to_email, subject, body):
    try:
        smtp_email = st.secrets.get("SMTP_EMAIL", "")
        smtp_password = st.secrets.get("SMTP_PASSWORD", "")

        if not smtp_email or not smtp_password:
            return False, (
                "Email settings are missing. Configure "
                ".streamlit/secrets.toml first."
            )

        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = smtp_email
        message["To"] = to_email
        message.set_content(body)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(smtp_email, smtp_password)
            server.send_message(message)

        return True, "Email sent successfully."

    except Exception as error:
        return False, f"Email sending failed: {error}"


def send_otp(email):
    otp = str(secrets.randbelow(900000) + 100000)
    otp_hash = hashlib.sha256(otp.encode()).hexdigest()
    expiry = datetime.now() + timedelta(minutes=5)

    success, message = send_email(
        email,
        "Smart Campus - Email Verification",
        (
            f"Your Smart Campus verification OTP is: {otp}\n\n"
            "This OTP expires in 5 minutes. Do not share it."
        )
    )

    if not success:
        return False, message

    conn = get_connection()

    conn.execute("""
        INSERT OR REPLACE INTO otp_codes
        (email, otp_hash, expires_at, attempts)
        VALUES (?, ?, ?, 0)
    """, (email, otp_hash, expiry.isoformat()))

    conn.commit()
    conn.close()

    return True, "OTP sent to your registered email address."


def verify_otp(email, entered_otp):
    conn = get_connection()

    row = conn.execute("""
        SELECT otp_hash, expires_at, attempts
        FROM otp_codes
        WHERE email = ?
    """, (email,)).fetchone()

    if not row:
        conn.close()
        return False, "Please request a new OTP."

    saved_hash, expiry, attempts = row

    if attempts >= 5:
        conn.execute(
            "DELETE FROM otp_codes WHERE email = ?",
            (email,)
        )
        conn.commit()
        conn.close()
        return False, "Too many incorrect OTP attempts. Request a new OTP."

    if datetime.now() > datetime.fromisoformat(expiry):
        conn.execute(
            "DELETE FROM otp_codes WHERE email = ?",
            (email,)
        )
        conn.commit()
        conn.close()
        return False, "OTP expired. Request a new OTP."

    entered_hash = hashlib.sha256(
        entered_otp.strip().encode()
    ).hexdigest()

    if not secrets.compare_digest(saved_hash, entered_hash):
        conn.execute("""
            UPDATE otp_codes
            SET attempts = attempts + 1
            WHERE email = ?
        """, (email,))
        conn.commit()
        conn.close()
        return False, "Incorrect OTP."

    conn.execute("""
        UPDATE users
        SET email_verified = 1
        WHERE email = ?
    """, (email,))

    conn.execute(
        "DELETE FROM otp_codes WHERE email = ?",
        (email,)
    )

    conn.commit()
    conn.close()

    return True, "Email verified successfully."


# =========================================================
# 6. AUDIT LOG
# =========================================================

def add_audit_log(conn, actor, action, roll_no=None, details=""):
    conn.execute("""
        INSERT INTO audit_logs
        (actor_email, action, roll_no, details, created_at)
        VALUES (?, ?, ?, ?, ?)
    """, (actor, action, roll_no, details, now_text()))


# =========================================================
# 7. USER ACCOUNT FUNCTIONS
# =========================================================

def get_user(email):
    conn = get_connection()

    row = conn.execute("""
        SELECT email, roll_no, role, email_verified, active
        FROM users
        WHERE email = ?
    """, (email,)).fetchone()

    conn.close()
    return row


def create_admin(email, password):
    conn = get_connection()

    try:
        conn.execute("""
            INSERT INTO users
            (email, roll_no, password_hash, role,
             email_verified, active, created_at)
            VALUES (?, NULL, ?, 'Admin', 1, 1, ?)
        """, (email, hash_password(password), now_text()))

        conn.commit()
        return True, "Admin account created."

    except sqlite3.IntegrityError:
        return False, "This email already exists."

    finally:
        conn.close()


def create_staff(email, password, actor):
    conn = get_connection()

    try:
        conn.execute("""
            INSERT INTO users
            (email, roll_no, password_hash, role,
             email_verified, active, created_at)
            VALUES (?, NULL, ?, 'Staff', 1, 1, ?)
        """, (email, hash_password(password), now_text()))

        add_audit_log(
            conn, actor, "Staff account created", None, email
        )

        conn.commit()
        return True, "Staff account created successfully."

    except sqlite3.IntegrityError:
        return False, "This email already has an account."

    finally:
        conn.close()


def create_student(roll_no, name, department, year,
                   email, password, actor):
    conn = get_connection()

    try:
        conn.execute("""
            INSERT INTO students
            (roll_no, name, department, year)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(roll_no) DO UPDATE SET
                name = excluded.name,
                department = excluded.department,
                year = excluded.year
        """, (roll_no, name, department, year))

        conn.execute("""
            INSERT INTO users
            (email, roll_no, password_hash, role,
             email_verified, active, created_at)
            VALUES (?, ?, ?, 'Student', 0, 1, ?)
        """, (email, roll_no, hash_password(password), now_text()))

        add_audit_log(
            conn, actor, "Student account created", roll_no, email
        )

        conn.commit()
        return True, "Student profile and login account created."

    except sqlite3.IntegrityError:
        conn.rollback()
        return False, (
            "This email or Student ID already has a login account. "
            "Check the account list before trying again."
        )

    finally:
        conn.close()


# =========================================================
# 8. LOGIN ATTEMPT PROTECTION
# =========================================================

def login_is_locked(email):
    conn = get_connection()

    row = conn.execute("""
        SELECT locked_until
        FROM login_attempts
        WHERE email = ?
    """, (email,)).fetchone()

    conn.close()

    if not row or not row[0]:
        return False

    return datetime.now() < datetime.fromisoformat(row[0])


def record_failed_login(email):
    conn = get_connection()

    row = conn.execute("""
        SELECT attempts
        FROM login_attempts
        WHERE email = ?
    """, (email,)).fetchone()

    attempts = (row[0] if row else 0) + 1
    locked_until = None

    if attempts >= 5:
        locked_until = (
            datetime.now() + timedelta(minutes=10)
        ).isoformat()

    conn.execute("""
        INSERT INTO login_attempts(email, attempts, locked_until)
        VALUES (?, ?, ?)
        ON CONFLICT(email) DO UPDATE SET
            attempts = excluded.attempts,
            locked_until = excluded.locked_until
    """, (email, attempts, locked_until))

    conn.commit()
    conn.close()


def clear_failed_login(email):
    conn = get_connection()
    conn.execute(
        "DELETE FROM login_attempts WHERE email = ?",
        (email,)
    )
    conn.commit()
    conn.close()


# =========================================================
# 9. PERSISTENT LOGIN SESSION
# =========================================================

def create_login_session(email, remember):
    token = secrets.token_urlsafe(40)

    days = LOGIN_SESSION_DAYS if remember else 1
    expiry = datetime.now() + timedelta(days=days)

    conn = get_connection()

    conn.execute("""
        INSERT INTO login_sessions
        (token_hash, email, expires_at)
        VALUES (?, ?, ?)
    """, (hash_token(token), email, expiry.isoformat()))

    conn.commit()
    conn.close()

    cookie_manager.set(
        cookie=COOKIE_NAME,
        val=token,
        expires_at=expiry
    )

    st.session_state["user_email"] = email


def logout():
    token = cookie_manager.get(cookie=COOKIE_NAME)

    if token:
        conn = get_connection()

        conn.execute("""
            DELETE FROM login_sessions
            WHERE token_hash = ?
        """, (hash_token(token),))

        conn.commit()
        conn.close()

    try:
        cookie_manager.delete(COOKIE_NAME)
    except Exception:
        pass

    st.session_state.pop("user_email", None)
    st.rerun()


def get_logged_in_user():
    email = st.session_state.get("user_email")

    if email:
        user = get_user(email)

        if user and user[4] == 1:
            return user

        st.session_state.pop("user_email", None)

    token = cookie_manager.get(cookie=COOKIE_NAME)

    if not token:
        return None

    conn = get_connection()

    row = conn.execute("""
        SELECT email, expires_at
        FROM login_sessions
        WHERE token_hash = ?
    """, (hash_token(token),)).fetchone()

    if not row:
        conn.close()
        return None

    email, expiry = row

    if datetime.now() > datetime.fromisoformat(expiry):
        conn.execute("""
            DELETE FROM login_sessions
            WHERE token_hash = ?
        """, (hash_token(token),))

        conn.commit()
        conn.close()
        return None

    conn.close()

    user = get_user(email)

    if user and user[4] == 1:
        st.session_state["user_email"] = email
        return user

    return None


# =========================================================
# 10. STUDENT AND ATTENDANCE FUNCTIONS
# =========================================================

def get_students():
    conn = get_connection()

    rows = conn.execute("""
        SELECT roll_no, name, department, year
        FROM students
        ORDER BY roll_no
    """).fetchall()

    conn.close()
    return rows


def get_attendance():
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            a.roll_no,
            s.name,
            s.department,
            a.attendance_date,
            a.status,
            a.marked_at
        FROM attendance a
        JOIN students s ON a.roll_no = s.roll_no
        ORDER BY a.id DESC
    """).fetchall()

    conn.close()
    return rows


def get_bulk_attendance(selected_date):
    conn = get_connection()

    rows = conn.execute("""
        SELECT
            s.roll_no,
            s.name,
            COALESCE(a.status, 'Not Marked')
        FROM students s
        LEFT JOIN attendance a
        ON s.roll_no = a.roll_no
        AND a.attendance_date = ?
        ORDER BY s.roll_no
    """, (selected_date,)).fetchall()

    conn.close()

    return [
        {
            "Roll Number": row[0],
            "Student Name": row[1],
            "Status": row[2]
        }
        for row in rows
    ]


def save_bulk_attendance(rows, selected_date, actor):
    conn = get_connection()

    try:
        for row in rows:
            roll_no = row["Roll Number"]
            status = row["Status"]

            if status == "Not Marked":
                conn.execute("""
                    DELETE FROM attendance
                    WHERE roll_no = ?
                    AND attendance_date = ?
                """, (roll_no, selected_date))

            else:
                conn.execute("""
                    INSERT INTO attendance
                    (roll_no, attendance_date, status, marked_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(roll_no, attendance_date)
                    DO UPDATE SET
                        status = excluded.status,
                        marked_at = excluded.marked_at
                """, (roll_no, selected_date, status, now_text()))

            add_audit_log(
                conn,
                actor,
                "Manual attendance update",
                roll_no,
                f"{selected_date}: {status}"
            )

        conn.commit()
        return True

    except sqlite3.Error as error:
        conn.rollback()
        st.error(f"Database error: {error}")
        return False

    finally:
        conn.close()


# =========================================================
# 11. QR SESSION FUNCTIONS
# =========================================================

def create_qr_session():
    token = secrets.token_urlsafe(32)

    now = datetime.now()
    expiry = now + timedelta(minutes=QR_VALID_MINUTES)

    conn = get_connection()

    conn.execute("""
        UPDATE qr_sessions
        SET active = 0
        WHERE active = 1
    """)

    conn.execute("""
        INSERT INTO qr_sessions
        (token, attendance_date, created_at, expires_at, active)
        VALUES (?, ?, ?, ?, 1)
    """, (
        token,
        date.today().isoformat(),
        now.isoformat(),
        expiry.isoformat()
    ))

    conn.commit()
    conn.close()

    return token, expiry


def mark_qr_attendance(token, logged_email, entered_roll):
    conn = get_connection()

    qr = conn.execute("""
        SELECT attendance_date, expires_at, active
        FROM qr_sessions
        WHERE token = ?
    """, (token,)).fetchone()

    if not qr:
        conn.close()
        return False, "Invalid QR code. Ask staff for a new QR code."

    attendance_date, expiry, active = qr

    if not active:
        conn.close()
        return False, "This QR code has been deactivated."

    if datetime.now() > datetime.fromisoformat(expiry):
        conn.close()
        return False, "QR code expired. Scan a new QR code."

    user = conn.execute("""
        SELECT roll_no, role, email_verified, active
        FROM users
        WHERE email = ?
    """, (logged_email,)).fetchone()

    if not user:
        conn.close()
        return False, "Your login account was not found."

    registered_roll, user_role, verified, account_active = user

    if user_role != "Student" or account_active != 1:
        conn.close()
        return False, "Only active student accounts can mark attendance."

    if verified != 1:
        conn.close()
        return False, "Verify your email before marking attendance."

    if registered_roll != entered_roll:
        conn.close()
        return False, (
            "Email and Student ID do not match. "
            "Attendance has been rejected."
        )

    try:
        conn.execute("""
            INSERT INTO attendance
            (roll_no, attendance_date, status, marked_at)
            VALUES (?, ?, 'Present', ?)
        """, (registered_roll, attendance_date, now_text()))

        add_audit_log(
            conn,
            logged_email,
            "QR attendance marked",
            registered_roll,
            attendance_date
        )

        conn.commit()
        conn.close()

        return True, "Your attendance has been marked successfully!"

    except sqlite3.IntegrityError:
        conn.close()
        return False, "Already Marked: attendance exists for this date."

    except sqlite3.Error as error:
        conn.rollback()
        conn.close()
        return False, f"Database error: {error}"


# =========================================================
# 12. FIRST ADMIN SETUP
# =========================================================

conn = get_connection()

admin_count = conn.execute("""
    SELECT COUNT(*)
    FROM users
    WHERE role = 'Admin'
""").fetchone()[0]

conn.close()

if admin_count == 0:

    st.title("🎓 Smart Campus - Initial Admin Setup")

    st.info("Create the first administrator account.")

    with st.form("initial_admin_form"):

        admin_email = st.text_input(
            "Admin Email"
        ).strip().lower()

        admin_password = st.text_input(
            "Create Password",
            type="password"
        )

        confirm_password = st.text_input(
            "Confirm Password",
            type="password"
        )

        submitted = st.form_submit_button(
            "Create Admin Account"
        )

    if submitted:

        if not valid_email(admin_email):
            st.error("Enter a valid email address.")

        elif len(admin_password) < 10:
            st.error("Password must contain at least 10 characters.")

        elif admin_password != confirm_password:
            st.error("Passwords do not match.")

        else:
            success, message = create_admin(
                admin_email,
                admin_password
            )

            if success:
                st.success(message)
                st.info("Now log in using your Admin details.")
                st.rerun()
            else:
                st.error(message)

    st.stop()


# =========================================================
# 13. LOGIN PAGE
# =========================================================

user = get_logged_in_user()

if not user:

    st.title("🎓 Smart Campus Attendance System")
    st.subheader("Secure Portal Login")

    login_tab, otp_tab = st.tabs([
        "Login",
        "Email OTP Verification"
    ])

    with login_tab:

        with st.form("login_form"):

            login_email = st.text_input(
                "Registered Email"
            ).strip().lower()

            login_password = st.text_input(
                "Password",
                type="password"
            )

            student_id = st.text_input(
                "Student ID (Students only)"
            ).strip().upper()

            remember = st.checkbox(
                "Keep me signed in for 14 days"
            )

            login_submit = st.form_submit_button(
                "Login",
                use_container_width=True
            )

        if login_submit:

            if not valid_email(login_email):
                st.error("Enter a valid email address.")

            elif login_is_locked(login_email):
                st.error(
                    "Too many incorrect attempts. "
                    "Try again after 10 minutes."
                )

            else:

                conn = get_connection()

                account = conn.execute("""
                    SELECT email, roll_no, password_hash,
                           role, email_verified, active
                    FROM users
                    WHERE email = ?
                """, (login_email,)).fetchone()

                conn.close()

                if (
                    not account
                    or account[5] != 1
                    or not verify_password(
                        login_password,
                        account[2]
                    )
                ):
                    record_failed_login(login_email)
                    st.error("Invalid email or password.")

                elif (
                    account[3] == "Student"
                    and account[1] != student_id
                ):
                    record_failed_login(login_email)
                    st.error("Email and Student ID do not match.")

                elif (
                    account[3] == "Student"
                    and account[4] == 0
                ):

                    clear_failed_login(login_email)

                    success, message = send_otp(login_email)

                    if success:
                        st.session_state["otp_email"] = login_email
                        st.success(message)
                        st.info(
                            "Open Email OTP Verification and enter your OTP."
                        )
                    else:
                        st.error(message)

                else:

                    clear_failed_login(login_email)

                    create_login_session(
                        login_email,
                        remember
                    )

                    st.rerun()

    with otp_tab:

        otp_email = st.session_state.get("otp_email", "")

        if not otp_email:

            st.info(
                "Students: enter your email, Student ID and password "
                "in the Login tab first. An OTP will be sent to your email."
            )

        else:

            st.write("OTP sent to:", otp_email)

            with st.form("verify_otp_form"):

                entered_otp = st.text_input(
                    "Enter 6-digit OTP",
                    max_chars=6
                )

                verify_submit = st.form_submit_button(
                    "Verify Email",
                    use_container_width=True
                )

            if verify_submit:

                if not entered_otp.isdigit() or len(entered_otp) != 6:
                    st.error("Enter a valid 6-digit OTP.")

                else:

                    success, message = verify_otp(
                        otp_email,
                        entered_otp
                    )

                    if success:
                        st.success(message)
                        st.session_state.pop("otp_email", None)
                        st.info("Please log in again.")
                        st.rerun()
                    else:
                        st.error(message)

            if st.button("Resend OTP"):

                success, message = send_otp(otp_email)

                if success:
                    st.success(message)
                else:
                    st.error(message)

    st.stop()


# =========================================================
# 14. LOGGED-IN USER
# =========================================================

email, roll_no, role, email_verified, active = user

st.sidebar.title("🎓 Smart Campus")
st.sidebar.write(f"**Role:** {role}")
st.sidebar.caption(email)

if st.sidebar.button("Logout", use_container_width=True):
    logout()


# =========================================================
# 15. QR STUDENT ATTENDANCE PAGE
# =========================================================

qr_token = st.query_params.get("token")

if qr_token:

    if role != "Student":
        st.error("Only students can mark QR attendance.")
        st.stop()

    st.title("📱 Student QR Attendance")
    st.success(f"Logged in as: {email}")

    st.info(
        "Enter your Student ID. It must match the ID "
        "registered with your logged-in email."
    )

    with st.form("qr_attendance_form"):

        entered_id = st.text_input(
            "Enter Student ID",
            placeholder="Example: 25IT009"
        ).strip().upper()

        submit_attendance = st.form_submit_button(
            "✅ Mark My Attendance",
            use_container_width=True
        )

    if submit_attendance:

        if not entered_id:
            st.warning("Please enter your Student ID.")

        else:

            success, message = mark_qr_attendance(
                qr_token,
                email,
                entered_id
            )

            if success:
                st.success(message)
                st.balloons()
            else:
                st.error(message)

    st.divider()
    st.caption("Smart Campus Attendance System")
    st.stop()


# =========================================================
# 16. STUDENT PORTAL
# =========================================================

if role == "Student":

    st.title("👨‍🎓 Student Portal")

    conn = get_connection()

    student = conn.execute("""
        SELECT name, department, year
        FROM students
        WHERE roll_no = ?
    """, (roll_no,)).fetchone()

    records = conn.execute("""
        SELECT attendance_date, status, marked_at
        FROM attendance
        WHERE roll_no = ?
        ORDER BY attendance_date DESC
    """, (roll_no,)).fetchall()

    conn.close()

    if not student:
        st.error("Student profile not found. Contact Admin.")
        st.stop()

    st.subheader(f"Welcome, {student[0]}")

    st.write(f"**Student ID:** {roll_no}")
    st.write(f"**Department:** {student[1]}")
    st.write(f"**Year:** {student[2]}")
    st.write(f"**Email:** {email}")

    total_records = len(records)

    present_count = sum(
        1 for record in records
        if record[1] == "Present"
    )

    percentage = (
        present_count / total_records * 100
        if total_records else 0
    )

    col1, col2, col3 = st.columns(3)

    col1.metric("Total Records", total_records)
    col2.metric("Present", present_count)
    col3.metric("Attendance Percentage", f"{percentage:.1f}%")

    st.divider()
    st.subheader("My Attendance History")

    if records:

        st.table({
            "Date": [r[0] for r in records],
            "Status": [r[1] for r in records],
            "Marked At": [r[2] for r in records]
        })

    else:
        st.info("No attendance records available yet.")

    st.info(
        "To mark attendance, scan the QR code displayed by your faculty."
    )


# =========================================================
# 17. STAFF AND ADMIN PORTALS
# =========================================================

elif role in ("Admin", "Staff"):

    st.title(f"🏫 {role} Portal")

    menu_options = [
        "Dashboard",
        "Student List",
        "Manual Attendance",
        "QR Attendance",
        "Attendance Records"
    ]

    if role == "Admin":
        menu_options += [
            "Create Student Account",
            "Create Staff Account",
            "Audit Log"
        ]

    menu = st.sidebar.radio(
        "Navigation",
        menu_options
    )

    # -----------------------------------------------------
    # DASHBOARD
    # -----------------------------------------------------

    if menu == "Dashboard":

        students = get_students()
        attendance = get_attendance()

        today = date.today().isoformat()

        today_present = [
            row for row in attendance
            if row[3] == today and row[4] == "Present"
        ]

        col1, col2, col3 = st.columns(3)

        col1.metric("Total Students", len(students))
        col2.metric("Today's Present", len(today_present))
        col3.metric("Total Attendance Records", len(attendance))

        st.divider()
        st.subheader("Welcome to Smart Campus")

        st.write("""
        **Available features:**

        - View registered students
        - Generate temporary QR attendance codes
        - Mark and edit manual attendance
        - View attendance records
        - Download CSV reports
        """)

    # -----------------------------------------------------
    # STUDENT LIST
    # -----------------------------------------------------

    elif menu == "Student List":

        st.subheader("👨‍🎓 Registered Students")

        students = get_students()

        if students:

            st.table({
                "Student ID": [s[0] for s in students],
                "Student Name": [s[1] for s in students],
                "Department": [s[2] for s in students],
                "Year": [s[3] for s in students]
            })

        else:
            st.info("No students registered yet.")

    # -----------------------------------------------------
    # CREATE STUDENT ACCOUNT
    # -----------------------------------------------------

    elif menu == "Create Student Account" and role == "Admin":

        st.subheader("📝 Create Student Account")

        st.info(
            "Admin creates the student's profile, email and "
            "temporary password. The student verifies their email "
            "during first login."
        )

        with st.form("create_student_form"):

            new_roll = st.text_input(
                "Student ID / Roll Number"
            ).strip().upper()

            new_name = st.text_input(
                "Student Name"
            ).strip()

            new_email = st.text_input(
                "Student Email"
            ).strip().lower()

            department = st.selectbox(
                "Department",
                [
                    "IT", "CSE", "ECE", "EEE",
                    "MECH", "CIVIL", "AIDS", "Other"
                ]
            )

            year = st.selectbox(
                "Year",
                [
                    "1st Year",
                    "2nd Year",
                    "3rd Year",
                    "4th Year"
                ]
            )

            new_password = st.text_input(
                "Temporary Password",
                type="password"
            )

            create_submit = st.form_submit_button(
                "Create Student Account",
                use_container_width=True
            )

        if create_submit:

            if not all([
                new_roll,
                new_name,
                new_email,
                new_password
            ]):
                st.warning("Please fill in all fields.")

            elif not valid_email(new_email):
                st.error("Enter a valid student email.")

            elif len(new_password) < 10:
                st.error(
                    "Temporary password must have at least 10 characters."
                )

            else:

                success, message = create_student(
                    new_roll,
                    new_name,
                    department,
                    year,
                    new_email,
                    new_password,
                    email
                )

                if success:
                    st.success(message)
                else:
                    st.error(message)

    # -----------------------------------------------------
    # CREATE STAFF ACCOUNT
    # -----------------------------------------------------

    elif menu == "Create Staff Account" and role == "Admin":

        st.subheader("👩‍🏫 Create Staff Account")

        with st.form("create_staff_form"):

            staff_email = st.text_input(
                "Staff Email"
            ).strip().lower()

            staff_password = st.text_input(
                "Temporary Password",
                type="password"
            )

            staff_submit = st.form_submit_button(
                "Create Staff Account",
                use_container_width=True
            )

        if staff_submit:

            if not valid_email(staff_email):
                st.error("Enter a valid staff email.")

            elif len(staff_password) < 10:
                st.error(
                    "Password must have at least 10 characters."
                )

            else:

                success, message = create_staff(
                    staff_email,
                    staff_password,
                    email
                )

                if success:
                    st.success(message)
                else:
                    st.error(message)

    # -----------------------------------------------------
    # MANUAL ATTENDANCE
    # -----------------------------------------------------

    elif menu == "Manual Attendance":

        st.subheader("📋 Staff Bulk Attendance Management")

        selected_date = st.date_input(
            "Select Attendance Date",
            value=date.today(),
            key="staff_attendance_date"
        )

        date_text = selected_date.isoformat()
        rows = get_bulk_attendance(date_text)

        if not rows:

            st.info("No students registered yet.")

        else:

            edited_rows = st.data_editor(
                rows,
                column_config={
                    "Roll Number": st.column_config.TextColumn(
                        "Roll Number",
                        disabled=True
                    ),
                    "Student Name": st.column_config.TextColumn(
                        "Student Name",
                        disabled=True
                    ),
                    "Status": st.column_config.SelectboxColumn(
                        "Attendance Status",
                        options=[
                            "Present",
                            "Absent",
                            "Not Marked"
                        ],
                        required=True
                    )
                },
                disabled=["Roll Number", "Student Name"],
                hide_index=True,
                use_container_width=True,
                num_rows="fixed",
                key=f"attendance_editor_{date_text}"
            )

            present = sum(
                1 for r in edited_rows
                if r["Status"] == "Present"
            )

            absent = sum(
                1 for r in edited_rows
                if r["Status"] == "Absent"
            )

            not_marked = sum(
                1 for r in edited_rows
                if r["Status"] == "Not Marked"
            )

            col1, col2, col3 = st.columns(3)

            col1.metric("Present", present)
            col2.metric("Absent", absent)
            col3.metric("Not Marked", not_marked)

            if st.button(
                "💾 Save All Attendance",
                use_container_width=True
            ):

                success = save_bulk_attendance(
                    edited_rows,
                    date_text,
                    email
                )

                if success:
                    st.success("Attendance saved successfully.")
                    st.rerun()

    # -----------------------------------------------------
    # AUTOMATIC QR ATTENDANCE
    # -----------------------------------------------------

    elif menu == "QR Attendance":

        st.subheader("📱 Smart QR Attendance")

        st.write(
            "Click the button below. The system automatically "
            "creates the QR code. No URL entry is required."
        )

        st.info(
            f"QR code validity: {QR_VALID_MINUTES} minutes."
        )

        if st.button(
            "🚀 Generate QR Code",
            use_container_width=True
        ):

            base_url = get_qr_base_url()
            token, expiry = create_qr_session()

            attendance_link = (
                base_url
                + "/?"
                + urlencode({"token": token})
            )

            qr_image = qrcode.make(attendance_link)

            buffer = io.BytesIO()
            qr_image.save(buffer, format="PNG")

            st.session_state["qr_image"] = buffer.getvalue()
            st.session_state["qr_link"] = attendance_link
            st.session_state["qr_expiry"] = expiry

        if "qr_image" in st.session_state:

            st.divider()
            st.success("QR Code Generated Successfully!")

            st.image(
                st.session_state["qr_image"],
                caption="Scan to open Student Attendance",
                width=300
            )

            st.write(
                "QR expires at:",
                st.session_state["qr_expiry"].strftime(
                    "%d-%m-%Y %I:%M:%S %p"
                )
            )

            st.caption(
                "Students must scan the QR code before it expires."
            )

            st.info(
                "For local testing, connect your phone and laptop "
                "to the same Wi-Fi network."
            )

    # -----------------------------------------------------
    # ATTENDANCE RECORDS
    # -----------------------------------------------------

    elif menu == "Attendance Records":

        st.subheader("📊 Attendance Records")

        attendance = get_attendance()

        if attendance:

            st.dataframe({
                "Student ID": [a[0] for a in attendance],
                "Student Name": [a[1] for a in attendance],
                "Department": [a[2] for a in attendance],
                "Date": [a[3] for a in attendance],
                "Status": [a[4] for a in attendance],
                "Marked At": [a[5] for a in attendance]
            }, use_container_width=True)

            output = io.StringIO()
            writer = csv.writer(output)

            writer.writerow([
                "Student ID",
                "Student Name",
                "Department",
                "Date",
                "Status",
                "Marked At"
            ])

            writer.writerows(attendance)

            st.download_button(
                "⬇️ Download Attendance CSV",
                data=output.getvalue(),
                file_name="attendance_records.csv",
                mime="text/csv"
            )

        else:
            st.info("No attendance records available.")

    # -----------------------------------------------------
    # AUDIT LOG
    # -----------------------------------------------------

    elif menu == "Audit Log" and role == "Admin":

        st.subheader("🔐 Attendance Audit Log")

        conn = get_connection()

        logs = conn.execute("""
            SELECT
                actor_email,
                action,
                roll_no,
                details,
                created_at
            FROM audit_logs
            ORDER BY id DESC
        """).fetchall()

        conn.close()

        if logs:

            st.dataframe({
                "User": [r[0] for r in logs],
                "Action": [r[1] for r in logs],
                "Student ID": [r[2] for r in logs],
                "Details": [r[3] for r in logs],
                "Time": [r[4] for r in logs]
            }, use_container_width=True)

        else:
            st.info("No audit log entries yet.")


# =========================================================
# 18. SIDEBAR FOOTER
# =========================================================

st.sidebar.divider()
st.sidebar.caption("Smart Campus Attendance System")
