import os
import tempfile
from datetime import date
from io import BytesIO
from pathlib import Path

import pymupdf
import qrcode
from flask_xmlrpcre import Fault, XMLRPCHandler
from flask import (
    Flask,
    abort,
    redirect,
    render_template,
    send_file,
    url_for,
)
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from werkzeug.serving import make_server

if __package__:
    from .printing_data import Base, PrintedExam, ResolutionSheet
else:
    from printing_data import Base, PrintedExam, ResolutionSheet


MAX_IDENTIFIER = 2**63 - 1
DEFAULT_DATABASE_PATH = Path(__file__).resolve().with_name("printing.sqlite")
DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().with_name("Exam-template.pdf")


def parse_identifier(value, label):
    message = f"{label} must be a positive 64-bit integer."
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            value = int(value)
        except ValueError as error:
            raise Fault(1, message) from error
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 0 < value <= MAX_IDENTIFIER
    ):
        raise Fault(1, message)
    return value


def create_database_engine(database_url):
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False}
        if database_url.startswith("sqlite")
        else {},
    )
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
    return engine


def add_qr_code(page, identifier):
    image = qrcode.make(str(identifier))
    image_data = BytesIO()
    image.save(image_data, format="PNG")
    image_bytes = image_data.getvalue()

    margin = 18
    size = min(72, page.rect.width - 2 * margin, page.rect.height - 2 * margin)
    if size <= 0:
        raise ValueError(f"Page {page.number + 1} is too small for a QR code.")
    for x, y in (
        (margin, margin),
        (page.rect.width - margin - size, margin),
        (margin, page.rect.height - margin - size),
        (page.rect.width - margin - size, page.rect.height - margin - size),
    ):
        page.insert_image(
            pymupdf.Rect(x, y, x + size, y + size),
            stream=image_bytes,
        )


def create_exam_pdf(template_path, sheet_ids):
    with pymupdf.open(template_path) as template:
        if template.page_count < 5:
            raise ValueError(
                "The exam template must have an instructions page and four question pages."
            )
        document = pymupdf.open()
        try:
            for sheet_id in sheet_ids:
                first_page = document.page_count
                document.insert_pdf(template, from_page=1, to_page=4)
                for page_number in range(first_page, first_page + 4):
                    add_qr_code(document[page_number], sheet_id)
            return document.tobytes(garbage=4, deflate=True)
        finally:
            document.close()


def write_pdf_copy(output_dir, exam_id, contents):
    output_dir = Path(output_dir)
    output_path = output_dir / f"exam_{exam_id}.pdf"
    temporary_path = None
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            dir=output_dir, suffix=".pdf", delete=False
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(contents)
        temporary_path.replace(output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return output_path


def get_exam_or_404(session, exam_id):
    if not 0 < exam_id <= MAX_IDENTIFIER:
        abort(404)
    exam = session.get(PrintedExam, exam_id)
    if exam is None:
        abort(404)
    return exam


def create_print_exam(output_dir, session_factory, template_path=DEFAULT_TEMPLATE_PATH):
    output_dir = Path(output_dir)
    template_path = Path(template_path)

    def print_exam(exam_id, exam_date, course, student_ids):
        exam_id = parse_identifier(exam_id, "exam_id")

        if not isinstance(exam_date, str):
            raise Fault(1, "exam_date must use the YYYY-MM-DD format.")
        try:
            parsed_date = date.fromisoformat(exam_date)
        except ValueError as error:
            raise Fault(1, "exam_date must use the YYYY-MM-DD format.") from error
        if parsed_date.isoformat() != exam_date:
            raise Fault(1, "exam_date must use the YYYY-MM-DD format.")
        if parsed_date < date.today():
            raise Fault(1, "Resolution sheets cannot be printed after the exam date.")

        if not isinstance(course, str) or not course.strip():
            raise Fault(1, "course is required.")
        course = course.strip()

        if not isinstance(student_ids, list) or not student_ids:
            raise Fault(1, "student_ids must contain at least one student.")
        student_ids = [
            parse_identifier(student_id, "Each student ID")
            for student_id in student_ids
        ]
        if len(student_ids) != len(set(student_ids)):
            raise Fault(1, "student_ids must not contain duplicates.")

        with session_factory.begin() as session:
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            exam = session.get(PrintedExam, exam_id)
            changed = exam is None
            if exam is None:
                exam = PrintedExam(
                    id=exam_id,
                    exam_date=parsed_date,
                    course=course,
                    pdf_file=b"",
                )
                session.add(exam)
            else:
                if exam.exam_date < date.today():
                    raise Fault(1, "Resolution sheets cannot be printed after the exam date.")
                changed = exam.exam_date != parsed_date or exam.course != course
                exam.exam_date = parsed_date
                exam.course = course

            existing_students = {sheet.student_id for sheet in exam.sheets}
            for student_id in student_ids:
                if student_id not in existing_students:
                    exam.sheets.append(ResolutionSheet(student_id=student_id))
                    changed = True
            session.flush()
            if changed or not exam.pdf_file:
                exam.pdf_file = create_exam_pdf(
                    template_path, [sheet.id for sheet in exam.sheets]
                )

            # Publish before releasing the write transaction to preserve the latest copy.
            output_path = write_pdf_copy(output_dir, exam_id, exam.pdf_file)

        return f"Generated PDF: {output_path.resolve()}"

    return print_exam


def create_app(database_url=None, output_dir=None, template_path=DEFAULT_TEMPLATE_PATH):
    app = Flask(__name__)
    database_path = DEFAULT_DATABASE_PATH
    app.config.update(
        DATABASE_URL=database_url or os.environ.get(
            "PRINTING_DATABASE_URL", f"sqlite:///{database_path.as_posix()}"
        ),
        OUTPUT_DIR=output_dir or os.environ.get(
            "PRINTING_OUTPUT_DIR", Path(__file__).resolve().parent / "output"
        ),
        TEMPLATE_PATH=template_path,
    )
    engine = create_database_engine(app.config["DATABASE_URL"])
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    app.extensions["printing_engine"] = engine
    app.extensions["printing_session_factory"] = session_factory
    print_exam = create_print_exam(
        app.config["OUTPUT_DIR"], session_factory, app.config["TEMPLATE_PATH"]
    )

    def handle_print_exam(*args):
        try:
            return print_exam(*args)
        except Fault:
            raise
        except Exception as error:
            app.logger.exception("Printing order failed.")
            raise Fault(-32500, "The printing order could not be completed.") from error

    xmlrpc_handler = XMLRPCHandler("printing_xmlrpc", introspection=False)
    xmlrpc_handler.register_function(handle_print_exam, "print_exam")
    xmlrpc_handler.connect(app, "/RPC2")
    xmlrpc_handler.connect(app, "/", endpoint_name="printing_xmlrpc_root")

    @app.get("/")
    @app.get("/exams")
    def list_exams():
        return render_exams()

    def render_exams(error=None, success=None):
        with session_factory() as session:
            exams = session.scalars(
                select(PrintedExam).order_by(PrintedExam.exam_date, PrintedExam.id)
            ).all()
            rows = [
                {
                    "exam": exam,
                    "sheet_count": len(exam.sheets),
                }
                for exam in exams
            ]
        return render_template("index.html", exams=rows, error=error, success=success)

    @app.get("/exams/<int:exam_id>/pdf")
    def view_exam_pdf(exam_id):
        with session_factory() as session:
            exam = get_exam_or_404(session, exam_id)
            contents = exam.pdf_file
        return send_file(
            BytesIO(contents),
            mimetype="application/pdf",
            download_name=f"exam_{exam_id}.pdf",
        )

    @app.post("/exams/<int:exam_id>/pdf/save")
    def save_exam_pdf(exam_id):
        try:
            with session_factory.begin() as session:
                # Serialize saves with printing orders so an older PDF cannot overwrite a newer one.
                if session.bind.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                exam = get_exam_or_404(session, exam_id)
                write_pdf_copy(app.config["OUTPUT_DIR"], exam_id, exam.pdf_file)
        except OSError:
            app.logger.exception("Could not save PDF to the output folder.")
            return render_exams(error="Could not save the PDF to the output folder. Please try again."), 500
        return redirect(url_for("save_pdf_success"))

    @app.get("/exams/pdf/saved")
    def save_pdf_success():
        return render_exams(success="PDF saved to the output folder.")

    return app


def create_server(host="127.0.0.1", port=5003, output_dir=None, database_url=None):
    app = create_app(database_url=database_url, output_dir=output_dir)
    server = make_server(host, port, app, threaded=True)
    server.app = app
    return server


if __name__ == "__main__":
    host = os.environ.get("PRINTING_HOST", "127.0.0.1")
    port = int(os.environ.get("PRINTING_PORT", "5003"))
    server = create_server(host=host, port=port)
    print(f"Printing Service web and XML-RPC server listening on http://{host}:{port}")
    server.serve_forever()
