import csv
import io
import os
from datetime import date
from pathlib import Path
from xmlrpc.client import Fault, ProtocolError, ServerProxy

from flask import Flask, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy import create_engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from admin_data import Base, Enrollment, Exam


def create_app(database_url=None, printing_rpc_url=None):
    """Configure the Flask app, its database, and the Admin Service routes."""
    app = Flask(__name__)

    # Allow deployments and tests to override local defaults without sharing databases.
    app.config["SECRET_KEY"] = os.environ.get(
        "ADMIN_SECRET_KEY"
    ) or os.urandom(32)
    database_path = Path(__file__).resolve().parent / "admin.sqlite"
    app.config["DATABASE_URL"] = database_url or os.environ.get(
        "ADMIN_DATABASE_URL", f"sqlite:///{database_path.as_posix()}"
    )
    app.config["PRINTING_RPC_URL"] = printing_rpc_url or os.environ.get(
        "ADMIN_PRINTING_RPC_URL", "http://127.0.0.1:5003"
    )

    # Create the service's own database and expose a session factory to each request.
    engine = create_engine(
        app.config["DATABASE_URL"],
        connect_args={"check_same_thread": False}
        if app.config["DATABASE_URL"].startswith("sqlite")
        else {},
    )
    Base.metadata.create_all(engine)
    app.extensions["admin_session_factory"] = sessionmaker(
        bind=engine, expire_on_commit=False
    )
    app.extensions["admin_engine"] = engine

    @app.route("/")
    def index():
        """Show summary counts for scheduled exams and student enrollments."""
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
        """Display the scheduling form or validate and save a submitted exam."""
        if request.method == "POST":
            course = request.form.get("course", "").strip()
            exam_date_value = request.form.get("date", "").strip()
            try:
                if not course:
                    raise ValueError("Course is required.")
                if len(course) > 200:
                    raise ValueError("Course must be at most 200 characters.")
                try:
                    exam_date = date.fromisoformat(exam_date_value)
                except ValueError as error:
                    raise ValueError("Enter a valid exam date.") from error

                session_factory = current_app.extensions["admin_session_factory"]
                with session_factory.begin() as session:
                    session.add(Exam(date=exam_date, course=course))
            except ValueError as error:
                flash(str(error), "error")
            else:
                flash("Exam scheduled successfully.", "success")
                return redirect(url_for("list_exams"))

        return render_template("scheduleExam.html")

    @app.route("/exams")
    def list_exams():
        """List exams in chronological order."""
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exams = session.scalars(
                select(Exam).order_by(Exam.date, Exam.id)
            ).all()
        return render_template("listExams.html", exams=exams)

    @app.route("/exams/<int:exam_id>/enroll", methods=["GET", "POST"])
    def enroll_students(exam_id):
        """Validate a CSV upload and atomically enroll its students in one exam."""
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exam_exists = session.get(Exam, exam_id) is not None
        if not exam_exists:
            return render_template("notFound.html"), 404

        if request.method == "POST":
            upload = request.files.get("file")
            try:
                if upload is None or not upload.filename:
                    raise ValueError("Choose a CSV file to upload.")

                try:
                    contents = upload.read().decode("utf-8-sig")
                except UnicodeDecodeError as error:
                    raise ValueError("The CSV file must use UTF-8 encoding.") from error

                reader = csv.DictReader(io.StringIO(contents))
                headers = {
                    header.strip().lower() for header in (reader.fieldnames or [])
                }
                identifier_header = next(
                    (
                        header
                        for header in ("student_id", "student_identifier")
                        if header in headers
                    ),
                    None,
                )
                if identifier_header is None:
                    raise ValueError(
                        "CSV must contain a student_id column "
                        "(student_identifier is also accepted)."
                    )

                student_ids = []
                for line_number, row in enumerate(reader, start=2):
                    normalized_row = {
                        (key or "").strip().lower(): value
                        for key, value in row.items()
                    }
                    raw_id = (normalized_row.get(identifier_header) or "").strip()
                    if not raw_id:
                        if not any((value or "").strip() for value in row.values()):
                            continue
                        raise ValueError(
                            f"Missing student identifier on CSV line {line_number}."
                        )
                    try:
                        student_id = int(raw_id)
                    except ValueError as error:
                        raise ValueError(
                            f"Invalid student identifier on CSV line {line_number}."
                        ) from error
                    if student_id <= 0:
                        raise ValueError(
                            f"Student identifiers must be positive "
                            f"(CSV line {line_number})."
                        )
                    student_ids.append(student_id)

                if not student_ids:
                    raise ValueError("The CSV file contains no student identifiers.")
                if len(student_ids) != len(set(student_ids)):
                    raise ValueError("The CSV file contains duplicate student IDs.")

                # Check all existing enrollments before inserting any rows so the
                # complete upload succeeds or fails as one database transaction.
                with session_factory.begin() as session:
                    existing_ids = set(
                        session.scalars(
                            select(Enrollment.student_id).where(
                                Enrollment.exam_id == exam_id,
                                Enrollment.student_id.in_(student_ids),
                            )
                        ).all()
                    )
                    if existing_ids:
                        duplicates = ", ".join(
                            str(student_id) for student_id in sorted(existing_ids)
                        )
                        raise ValueError(
                            f"Students already enrolled in this exam: {duplicates}."
                        )
                    session.add_all(
                        Enrollment(exam_id=exam_id, student_id=student_id)
                        for student_id in student_ids
                    )
            except (ValueError, IntegrityError) as error:
                message = (
                    "One or more students are already enrolled in this exam."
                    if isinstance(error, IntegrityError)
                    else str(error)
                )
                flash(message, "error")
            else:
                flash(
                    f"{len(student_ids)} student(s) enrolled successfully.",
                    "success",
                )
                return redirect(url_for("list_enrollments"))

        with session_factory() as session:
            exam = session.get(Exam, exam_id)
        return render_template("enrollStudents.html", exam=exam)

    @app.route("/enrollments")
    def list_enrollments():
        """Show each enrollment alongside the corresponding exam details."""
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
        return render_template("listEnrollments.html", enrollments=enrollments)

    @app.post("/exams/<int:exam_id>/order-printing")
    def order_printing(exam_id):
        """Send the exam and enrolled student IDs to the Printing Service via XML-RPC."""
        session_factory = current_app.extensions["admin_session_factory"]
        with session_factory() as session:
            exam = session.get(Exam, exam_id)
            if exam is None:
                return render_template("notFound.html"), 404
            exam_data = (exam.id, exam.date.isoformat(), exam.course)
            student_ids = session.scalars(
                select(Enrollment.student_id)
                .where(Enrollment.exam_id == exam_id)
                .order_by(Enrollment.student_id)
            ).all()

        if not student_ids:
            flash("Enroll students before ordering the exam sheets.", "error")
            return redirect(url_for("list_exams"))

        try:
            with ServerProxy(current_app.config["PRINTING_RPC_URL"]) as printing:
                # Keep this argument order aligned with the Printing Service contract.
                result = printing.print_exam(
                    exam_data[0], exam_data[1], exam_data[2], student_ids
                )
        except (Fault, OSError, ProtocolError) as error:
            flash(f"Could not contact the Printing Service: {error}", "error")
            return redirect(url_for("list_exams"))

        if result is False:
            flash("The Printing Service rejected the printing order.", "error")
        else:
            flash(
                str(result) if result is not None else "Printing order sent successfully.",
                "success",
            )
        return redirect(url_for("list_exams"))

    return app


# Keep an app instance available for the development server and WSGI launchers.
app = create_app()


if __name__ == "__main__":
    # Run the Admin Service independently from the other project services.
    app.run(host="127.0.0.1", port=5002, debug=True)
