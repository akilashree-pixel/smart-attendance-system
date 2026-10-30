
import streamlit as st
import sqlite3
import csv
import io
from datetime import date

# =====================================
# PAGE CONFIGURATION
# =====================================

st.set_page_config(
    page_title="Smart Campus Attendance",
    page_icon="🎓",
    layout="wide"
)

DB_NAME = "attendance.db"


# =====================================
# DATABASE CONNECTION
# =====================================

def get_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS students (
            roll_no TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            department TEXT NOT NULL,
            year TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attendance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            roll_no TEXT NOT NULL,
            attendance_date TEXT NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(roll_no, attendance_date),
            FOREIGN KEY(roll_no) REFERENCES students(roll_no)
        )
    """)

    conn.commit()
    conn.close()


create_tables()


# =====================================
# DATABASE FUNCTIONS
# =====================================

def get_students():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT roll_no, name, department, year
        FROM students
        ORDER BY roll_no
    """)

    records = cursor.fetchall()
    conn.close()
    return records


def get_attendance():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT s.roll_no, s.name, s.department,
               a.attendance_date, a.status
        FROM attendance a
        JOIN students s ON s.roll_no = a.roll_no
        ORDER BY a.attendance_date DESC
    """)

    records = cursor.fetchall()
    conn.close()
    return records


# =====================================
# DISPLAY TABLE WITHOUT PANDAS
# =====================================

def show_table(headers, rows):

    header_cols = st.columns(len(headers))

    for i, header in enumerate(headers):
        header_cols[i].markdown(f"**{header}**")

    for row in rows:
        cols = st.columns(len(headers))

        for i, value in enumerate(row):
            cols[i].write(str(value))


# =====================================
# SIDEBAR
# =====================================

st.sidebar.title("🎓 Campus Portal")

menu = st.sidebar.radio(
    "Navigation",
    [
        "Dashboard",
        "Register Student",
        "Student List",
        "Mark Attendance",
        "Attendance Records"
    ]
)

st.sidebar.divider()
st.sidebar.caption("Smart Campus Attendance System")
st.sidebar.caption("Project Prototype - Version 1.0")


# =====================================
# MAIN TITLE
# =====================================

st.title("🎓 Smart Campus Attendance System")

st.write(
    "A simple digital platform to manage "
    "student registration and daily attendance."
)

st.divider()


# =====================================
# DASHBOARD
# =====================================

if menu == "Dashboard":

    st.header("📊 Dashboard")

    students = get_students()
    attendance = get_attendance()

    today = str(date.today())

    total_students = len(students)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT COUNT(*) FROM attendance
        WHERE attendance_date = ?
        AND status = 'Present'
    """, (today,))

    present = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(*) FROM attendance
        WHERE attendance_date = ?
        AND status = 'Absent'
    """, (today,))

    absent = cursor.fetchone()[0]

    conn.close()

    not_marked = total_students - present - absent

    percentage = (
        present / total_students * 100
        if total_students > 0 else 0
    )

    c1, c2, c3, c4 = st.columns(4)

    c1.metric("Total Students", total_students)
    c2.metric("Present Today", present)
    c3.metric("Absent Today", absent)
    c4.metric("Attendance", f"{percentage:.1f}%")

    st.divider()

    st.subheader("Today's Attendance Overview")

    st.write(f"Not marked: {not_marked}")

    st.progress(int(percentage))

    st.write(f"Attendance percentage: {percentage:.1f}%")

    st.subheader("Recent Attendance")

    if attendance:
        show_table(
            ["Roll Number", "Name", "Department",
             "Date", "Status"],
            attendance[:10]
        )
    else:
        st.info("No attendance records available.")


# =====================================
# REGISTER STUDENT
# =====================================

elif menu == "Register Student":

    st.header("➕ Register New Student")

    with st.form("student_form"):

        name = st.text_input("Student Name")

        roll_no = st.text_input("Roll Number")

        department = st.selectbox(
            "Department",
            [
                "Information Technology",
                "Computer Science",
                "Electronics",
                "Electrical",
                "Mechanical",
                "Civil"
            ]
        )

        year = st.selectbox(
            "Academic Year",
            ["1st Year", "2nd Year", "3rd Year", "4th Year"]
        )

        submit = st.form_submit_button(
            "Register Student",
            use_container_width=True
        )

        if submit:

            if not name.strip() or not roll_no.strip():
                st.error("Please enter all required details.")

            else:

                conn = get_connection()
                cursor = conn.cursor()

                try:
                    cursor.execute("""
                        INSERT INTO students
                        (roll_no, name, department, year)
                        VALUES (?, ?, ?, ?)
                    """, (
                        roll_no.strip().upper(),
                        name.strip(),
                        department,
                        year
                    ))

                    conn.commit()
                    st.success("Student registered successfully! 🎉")

                except sqlite3.IntegrityError:
                    st.error("Roll number already exists.")

                finally:
                    conn.close()


# =====================================
# STUDENT LIST
# =====================================

elif menu == "Student List":

    st.header("👨‍🎓 Registered Students")

    students = get_students()

    search = st.text_input(
        "🔍 Search by name or roll number"
    )

    if search:

        students = [
            student for student in students
            if search.lower() in student[0].lower()
            or search.lower() in student[1].lower()
        ]

    st.write(f"Students found: {len(students)}")

    if students:
        show_table(
            ["Roll Number", "Name", "Department", "Year"],
            students
        )
    else:
        st.info("No students found.")


# =====================================
# MARK ATTENDANCE
# =====================================

elif menu == "Mark Attendance":

    st.header("📝 Mark Attendance")

    students = get_students()

    selected_date = st.date_input(
        "Select Attendance Date",
        value=date.today()
    )

    if not students:

        st.warning("Register students before marking attendance.")

    else:

        student_options = {
            f"{s[0]} - {s[1]}": s[0]
            for s in students
        }

        with st.form("attendance_form"):

            selected_student = st.selectbox(
                "Select Student",
                list(student_options.keys())
            )

            status = st.selectbox(
                "Attendance Status",
                ["Present", "Absent"]
            )

            submit = st.form_submit_button(
                "Submit Attendance",
                use_container_width=True
            )

            if submit:

                roll = student_options[selected_student]

                conn = get_connection()
                cursor = conn.cursor()

                try:

                    cursor.execute("""
                        INSERT INTO attendance
                        (roll_no, attendance_date, status)
                        VALUES (?, ?, ?)
                    """, (
                        roll,
                        str(selected_date),
                        status
                    ))

                    conn.commit()

                    st.success(
                        f"Attendance marked as {status} successfully!"
                    )

                except sqlite3.IntegrityError:

                    st.error(
                        "Attendance already marked for this student "
                        "on this date."
                    )

                finally:
                    conn.close()


# =====================================
# ATTENDANCE RECORDS
# =====================================

elif menu == "Attendance Records":

    st.header("📋 Attendance Records")

    attendance = get_attendance()

    filter_date = st.date_input(
        "Select date to view records",
        value=date.today()
    )

    search_roll = st.text_input(
        "Search using student roll number"
    )

    filtered = [
        record for record in attendance
        if record[3] == str(filter_date)
    ]

    if search_roll:
        filtered = [
            record for record in filtered
            if search_roll.lower() in record[0].lower()
        ]

    st.write(f"Records found: {len(filtered)}")

    if filtered:

        show_table(
            ["Roll Number", "Name", "Department",
             "Date", "Status"],
            filtered
        )

        # CSV DOWNLOAD

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Roll Number",
            "Name",
            "Department",
            "Date",
            "Status"
        ])

        writer.writerows(filtered)

        st.download_button(
            "⬇ Download Attendance CSV",
            data=output.getvalue(),
            file_name="attendance_records.csv",
            mime="text/csv"
        )

    else:
        st.info("No attendance records found for this date.")


# =====================================
# FOOTER
# =====================================

st.divider()

st.caption(
    "© 2026 Smart Campus Attendance System | College Project"
)
