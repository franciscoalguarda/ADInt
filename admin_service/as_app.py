import csv
import io
import os
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit
from xml.parsers.expat import ExpatError
from xmlrpc.client import Fault, ProtocolError, ResponseError, SafeTransport, ServerProxy, Transport

from flask import Flask, abort, current_app, redirect, render_template, request, url_for
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

if __package__:
    from .admin_data import Base, Enrollment, Exam
else:
    from admin_data import Base, Enrollment, Exam


MAX_IDENTIFIER = 2**63 - 1


class TimeoutTransport(Transport):
    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


class TimeoutSafeTransport(SafeTransport):
    def __init__(self, timeout):
        super().__init__()
        self.timeout = timeout

    def make_connection(self, host):
        connection = super().make_connection(host)
        connection.timeout = self.timeout
        return connection


def get_error_message(reason, **details):
    messages = {
        "missing_course": "Course is required.",
        "long_course": "Course must be at most 200 characters.",
        "invalid_exam_date": "Enter a valid exam date.",
        "past_exam_date": "Exam date cannot be in the past.",
        "missing_file": "Choose a CSV file to upload.",
        "invalid_csv_extension": "Only CSV files are accepted.",
        "invalid_csv_encoding": "The CSV file must use UTF-8 encoding.",
        "invalid_csv_headers": "CSV must contain a student_id column (student_identifier is also accepted).",
        "duplicate_csv_headers": "CSV headers must be unique.",
        "ambiguous_csv_headers": "Use only one identifier column: student_id or student_identifier.",
        "invalid_csv_row": "Invalid number of CSV fields on line {line_number}.",
        "missing_student_id": "Missing student identifier on CSV line {line_number}.",
        "invalid_student_id": "Invalid student identifier on CSV line {line_number}.",
        "invalid_student_id_range": "Student identifiers must be positive 64-bit integers (CSV line {line_number}).",
        "empty_csv": "The CSV file contains no student identifiers.",
        "invalid_csv": "Invalid CSV format.",
        "enrollment_changed": "One or more students are already enrolled in this exam.",
        "no_students": "Enroll students before ordering the exam sheets.",
        "printing_unavailable": "Could not contact the Printing Service. Please try again.",
        "printing_failed": "The Printing Service could not complete the printing order. Please try again.",
        "printing_rejected": "The Printing Service rejected the printing order.",
        "past_enrollment_date": "Students cannot be enrolled after the exam date.",
        "printing_changed": "Enrollments changed during the PDF update. Use Order printing to update it again.",
    }
    return messages.get(reason, "Could not complete the operation.").format(**details)


def get_exam_or_404(session, exam_id):
    if not 0 < exam_id <= MAX_IDENTIFIER:
        abort(404)
    exam = session.get(Exam, exam_id)
    if exam is None:
        abort(404)
    return exam


def begin_sqlite_write(session):
    if session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def create_app(database_url=None, printing_rpc_url=None):
    """Configure the Flask app, its database, and the Admin Service routes."""
    app = Flask(__name__)

    # Allow deployments and tests to override local defaults without sharing databases.
    database_path = Path(__file__).resolve().parent / "admin.sqlite"
    app.config["DATABASE_URL"] = database_url or os.environ.get(
        "ADMIN_DATABASE_URL", f"sqlite:///{database_path.as_posix()}"
    )
    app.config["PRINTING_RPC_URL"] = printing_rpc_url or os.environ.get(
        "ADMIN_PRINTING_RPC_URL", "http://127.0.0.1:5003"
    )
    app.config["PRINTING_RPC_TIMEOUT"] = 30

    # Create the service's own database and expose a session factory to each request.
    engine = create_engine(
        app.config["DATABASE_URL"],
        connect_args={"check_same_thread": False}
        if app.config["DATABASE_URL"].startswith("sqlite")
        else {},
    )
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    app.extensions["admin_session_factory"] = sessionmaker(
        bind=engine, expire_on_commit=False
    )
    app.extensions["admin_engine"] = engine

    @app.route("/")
    def index():
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exam_count = session.scalar(select(func.count()).select_from(Exam))
            enrollment_count = session.scalar(
                select(func.count()).select_from(Enrollment)
            )
        return render_template(
            "index.html",
            exam_count=exam_count,
            enrollment_count=enrollment_count,
        )

    @app.route("/exams/schedule", methods=["GET", "POST"])
    def schedule_exam():
        error_message = None
        if request.method == "POST":
            course = request.form.get("course", "").strip()
            exam_date_value = request.form.get("date", "").strip()
            try:
                if not course:
                    raise ValueError("missing_course")
                if len(course) > 200:
                    raise ValueError("long_course")
                try:
                    exam_date = date.fromisoformat(exam_date_value)
                except ValueError as error:
                    raise ValueError("invalid_exam_date") from error

                if exam_date < date.today():
                    raise ValueError("past_exam_date")

                session_factory = current_app.extensions["admin_session_factory"]
                with session_factory.begin() as session:
                    session.add(Exam(date=exam_date, course=course))
            except ValueError as error:
                error_message = get_error_message(str(error))
            else:
                return redirect(url_for("exams_success"))

        return render_template("scheduleExam.html", error=error_message)

    @app.route("/exams")
    def list_exams():
        # List exams in chronological order.
        return render_exams()

    def render_exams(error=None, success=None):
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exams = session.scalars(
                select(Exam).order_by(Exam.date, Exam.id)
            ).all()
        return render_template("listExams.html", exams=exams, error=error, success=success,
                               today=date.today())

    @app.get("/exams/success")
    def exams_success():
        return render_exams(success="Exam scheduled successfully.")

    @app.get("/exams/printing/success")
    def printing_order_success():
        return render_exams(success="Printing order sent successfully.")

    @app.route("/exams/<int:exam_id>/enroll", methods=["GET", "POST"])
    def enroll_students(exam_id):
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exam = get_exam_or_404(session, exam_id)

        if exam.date < date.today():
            return render_template(
                "enrollStudents.html", exam=exam,
                error=get_error_message("past_enrollment_date"), enrollment_closed=True,
            ), 409

        error_message = None
        if request.method == "POST":
            upload = request.files.get("file")
            error_details = {}
            try:
                if upload is None or not upload.filename:
                    raise ValueError("missing_file")
                if not upload.filename.lower().endswith(".csv"):
                    raise ValueError("invalid_csv_extension")

                try:
                    contents = upload.read().decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise ValueError("invalid_csv_encoding") from error

                reader = csv.DictReader(io.StringIO(contents), strict=True)
                headers = [
                    header.strip().lower() for header in (reader.fieldnames or [])
                ]
                if len(headers) != len(set(headers)):
                    raise ValueError("duplicate_csv_headers")
                if {"student_id", "student_identifier"}.issubset(headers):
                    raise ValueError("ambiguous_csv_headers")
                identifier_header = next(
                    (
                        header
                        for header in ("student_id", "student_identifier")
                        if header in headers
                    ),
                    None,
                )
                if identifier_header is None:
                    raise ValueError("invalid_csv_headers")

                student_ids = []
                for row in reader:
                    line_number = reader.line_num
                    error_details["line_number"] = line_number
                    if None in row or None in row.values():
                        raise ValueError("invalid_csv_row")
                    normalized_row = {
                        (key or "").strip().lower(): value
                        for key, value in row.items()
                    }
                    raw_id = (normalized_row.get(identifier_header) or "").strip()
                    if not raw_id:
                        if not any((value or "").strip() for value in row.values()):
                            continue
                        raise ValueError("missing_student_id")
                    try:
                        student_id = int(raw_id)
                    except ValueError as error:
                        raise ValueError("invalid_student_id") from error
                    if not 0 < student_id <= MAX_IDENTIFIER:
                        raise ValueError("invalid_student_id_range")
                    student_ids.append(student_id)

                if not student_ids:
                    raise ValueError("empty_csv")
                student_ids = list(dict.fromkeys(student_ids))

                # Filter existing enrollments within the write transaction so
                # overlapping uploads cannot insert the same student twice.
                with session_factory.begin() as session:
                    begin_sqlite_write(session)
                    current_exam = get_exam_or_404(session, exam_id)
                    if current_exam.date < date.today():
                        raise ValueError("past_enrollment_date")
                    existing_ids = set(
                        session.scalars(
                            select(Enrollment.student_id).where(
                                Enrollment.exam_id == exam_id,
                                Enrollment.student_id.in_(student_ids),
                            )
                        ).all()
                    )
                    student_ids = [
                        student_id for student_id in student_ids
                        if student_id not in existing_ids
                    ]
                    session.add_all(
                        Enrollment(exam_id=exam_id, student_id=student_id)
                        for student_id in student_ids
                    )
            except csv.Error:
                error_message = get_error_message("invalid_csv")
            except ValueError as error:
                error_message = get_error_message(str(error), **error_details)
            except IntegrityError:
                error_message = get_error_message("enrollment_changed")
            else:
                return redirect(url_for("enrollments_success", count=len(student_ids)))

        return render_template("enrollStudents.html", exam=exam, error=error_message)

    @app.route("/enrollments")
    def list_enrollments():
        # Show each enrollment alongside the corresponding exam details.
        return render_enrollments()

    def render_enrollments(success=None):
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            rows = session.execute(
                select(Enrollment, Exam)
                .join(Exam, Enrollment.exam_id == Exam.id)
                .order_by(Exam.date, Exam.id, Enrollment.student_id)
            ).all()
        enrollments = [
            {"exam": exam, "student_id": enrollment.student_id}
            for enrollment, exam in rows
        ]
        return render_template("listEnrollments.html", enrollments=enrollments, success=success)

    @app.get("/enrollments/success")
    def enrollments_success():
        count = request.args.get("count", type=int)
        success = "Students enrolled successfully."
        if count == 0:
            success = "No new students were enrolled."
        elif count is not None and count > 0:
            success = f"{count} student(s) enrolled successfully."
        return render_enrollments(success=success)

    @app.post("/exams/<int:exam_id>/order-printing")
    def order_printing(exam_id):
        # Send the exam and enrolled student IDs to the Printing Service via XML-RPC.
        try:
            send_printing_order(exam_id)
        except ValueError as error:
            return render_exams(error=get_error_message(str(error)))
        return redirect(url_for("printing_order_success"))

    def send_printing_order(exam_id):
        session_factory = current_app.extensions["admin_session_factory"]
        for _ in range(3):
            with session_factory() as session:
                exam = get_exam_or_404(session, exam_id)
                exam_data = (exam.id, exam.date.isoformat(), exam.course)
                student_ids = session.scalars(select(Enrollment.student_id).where(
                    Enrollment.exam_id == exam_id
                ).order_by(Enrollment.student_id)).all()
                if not student_ids:
                    raise ValueError("no_students")
            try:
                rpc_url = current_app.config["PRINTING_RPC_URL"]
                transport_type = TimeoutSafeTransport if urlsplit(rpc_url).scheme == "https" else TimeoutTransport
                transport = transport_type(current_app.config["PRINTING_RPC_TIMEOUT"])
                with ServerProxy(rpc_url, transport=transport) as printing:
                    # Decimal strings preserve 64-bit IDs beyond XML-RPC's int32 limit.
                    result = printing.print_exam(
                        str(exam_data[0]), exam_data[1], exam_data[2],
                        [str(student_id) for student_id in student_ids],
                    )
            except (Fault, ResponseError, ExpatError, ValueError, TypeError) as error:
                raise ValueError("printing_failed") from error
            except (OSError, ProtocolError) as error:
                raise ValueError("printing_unavailable") from error
            if result is False:
                raise ValueError("printing_rejected")
            with session_factory() as session:
                current_ids = session.scalars(select(Enrollment.student_id).where(
                    Enrollment.exam_id == exam_id
                ).order_by(Enrollment.student_id)).all()
            if current_ids == student_ids:
                return True
        raise ValueError("printing_changed")

    @app.errorhandler(404)
    def not_found(_):
        return render_template("notFound.html"), 404

    return app


# Keep an app instance available for the development server and WSGI launchers.
app = create_app()


if __name__ == "__main__":
    # Run the Admin Service independently from the other project services.
    app.run(host="127.0.0.1", port=5002, debug=True)
