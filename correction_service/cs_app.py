import math
import os
from io import BytesIO
from pathlib import Path

import pymupdf
from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

if __package__:
    from . import process_student_answers
    from .correction_data import Base, ExamCorrectionPdf, ProfessorAssignment, QuestionCorrection
else:
    import process_student_answers
    from correction_data import Base, ExamCorrectionPdf, ProfessorAssignment, QuestionCorrection


def process_pdf(contents):
    """Ignore an introductory page and export four answers per resolution sheet."""
    try:
        document = pymupdf.open(stream=contents, filetype="pdf")
    except (RuntimeError, ValueError) as error:
        raise ValueError("The uploaded file is not a readable PDF.") from error

    pages = []
    with document:
        if document.needs_pass:
            raise ValueError("Password-protected PDFs are not supported.")
        if not document.page_count:
            raise ValueError("The PDF contains no pages.")
        # The supplied five-page PDFs begin with a page that is not an answer.
        # Accept either complete four-question sheets or one introductory page
        # followed by complete sheets. Only this in-memory copy is changed.
        if document.page_count > 1 and document.page_count % 4 == 1:
            document.delete_page(0)
        if document.page_count % 4:
            raise ValueError(
                "The PDF must contain four answer pages per sheet, "
                "optionally preceded by one introductory page."
            )
        page_documents = process_student_answers.split_pdf_into_pages(document)
        try:
            process_student_answers.cover_qr_codes_with_gray_squares(page_documents)
            for page_document in page_documents:
                page = page_document[0]
                pixmap = page.get_pixmap(
                    matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csRGB, alpha=False
                )
                png = pixmap.tobytes("png")
                # Rebuild from the masked image so QR content cannot be recovered
                # by removing the original script's PDF overlays.
                with pymupdf.open() as masked:
                    output_page = masked.new_page(width=page.rect.width, height=page.rect.height)
                    output_page.insert_image(output_page.rect, stream=png)
                    pages.append((masked.tobytes(garbage=4, deflate=True), png))
        finally:
            for page_document in page_documents:
                if not page_document.is_closed:
                    page_document.close()
    return pages


def positive_identifier(value, label):
    try:
        identifier = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} must be a positive 64-bit integer.") from error
    if not 0 < identifier <= 2**63 - 1:
        raise ValueError(f"{label} must be a positive 64-bit integer.")
    return identifier


def create_app(database_url=None):
    app = Flask(__name__)
    database_path = Path(__file__).resolve().with_name("corrections.sqlite")
    app.config.update(
        SECRET_KEY=os.environ.get("CORRECTION_SECRET_KEY") or os.urandom(32),
        DATABASE_URL=database_url or os.environ.get(
            "CORRECTION_DATABASE_URL", f"sqlite:///{database_path.as_posix()}"
        ),
        MAX_CONTENT_LENGTH=50 * 1024 * 1024,
    )
    engine = create_engine(
        app.config["DATABASE_URL"],
        connect_args={"check_same_thread": False}
        if app.config["DATABASE_URL"].startswith("sqlite") else {},
    )
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    app.extensions["correction_engine"] = engine
    app.extensions["correction_session_factory"] = Session

    @app.get("/")
    def index():
        with Session() as session:
            pdf_count = session.scalar(select(func.count()).select_from(ExamCorrectionPdf))
            page_count = session.scalar(select(func.count()).select_from(QuestionCorrection))
            corrected_count = session.scalar(
                select(func.count()).select_from(QuestionCorrection)
                .where(QuestionCorrection.evaluated.is_(True))
            )
        return render_template("index.html", pdf_count=pdf_count,
                               page_count=page_count, corrected_count=corrected_count)

    @app.route("/assignments", methods=["GET", "POST"])
    def assignments():
        if request.method == "POST":
            try:
                exam_id = positive_identifier(request.form.get("exam_id"), "Exam identifier")
                professor_id = positive_identifier(request.form.get("professor_id"), "Professor identifier")
                question = positive_identifier(request.form.get("question_number"), "Question number")
                if question not in range(1, 5):
                    raise ValueError("Question number must be between 1 and 4.")
                with Session.begin() as session:
                    assignment = session.scalar(select(ProfessorAssignment).where(
                        ProfessorAssignment.exam_id == exam_id,
                        ProfessorAssignment.question_number == question,
                    ))
                    if assignment is None:
                        session.add(ProfessorAssignment(
                            exam_id=exam_id, question_number=question, professor_id=professor_id
                        ))
                    else:
                        assignment.professor_id = professor_id
            except ValueError as error:
                flash(str(error), "error")
            except IntegrityError:
                flash("This question assignment changed. Please try again.", "error")
            else:
                flash("Professor assignment saved successfully.", "success")
                return redirect(url_for("assignments"))
        with Session() as session:
            rows = session.scalars(select(ProfessorAssignment).order_by(
                ProfessorAssignment.exam_id, ProfessorAssignment.question_number
            )).all()
        return render_template("assignments.html", assignments=rows)

    @app.route("/scans/upload", methods=["GET", "POST"])
    def upload_scan():
        if request.method == "POST":
            try:
                exam_id = positive_identifier(request.form.get("exam_id"), "Exam identifier")
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    raise ValueError("Please select a PDF file.")
                if not upload.filename.lower().endswith(".pdf"):
                    raise ValueError("Only PDF files are accepted.")
                contents = upload.read()
                if not contents:
                    raise ValueError("The uploaded PDF is empty.")
                pages = process_pdf(contents)
                # Store the complete batch only after every page has been processed.
                with Session.begin() as session:
                    source = ExamCorrectionPdf(
                        exam_id=exam_id, filename=secure_filename(upload.filename) or "scan.pdf",
                        pdf_file=contents,
                    )
                    source.corrections = [QuestionCorrection(
                        page_number=index + 1, sheet_number=index // 4 + 1,
                        question_number=index % 4 + 1,
                        page_pdf_file=pdf, page_png_file=png,
                    ) for index, (pdf, png) in enumerate(pages)]
                    session.add(source)
            except (ValueError, RuntimeError) as error:
                flash(str(error), "error")
            else:
                flash(f"PDF uploaded successfully: {len(pages)} page(s) ready for correction.", "success")
                return redirect(url_for("scans"))
        return render_template("uploadScan.html")

    def correction_rows(evaluated=None):
        # Derive assignments at query time, so assignments made after upload apply too.
        query = select(QuestionCorrection, ExamCorrectionPdf, ProfessorAssignment).join(
            ExamCorrectionPdf, QuestionCorrection.exam_correction_pdf_id == ExamCorrectionPdf.id
        ).outerjoin(ProfessorAssignment, (
            (ProfessorAssignment.exam_id == ExamCorrectionPdf.exam_id)
            & (ProfessorAssignment.question_number == QuestionCorrection.question_number)
        )).order_by(ExamCorrectionPdf.id, QuestionCorrection.page_number)
        if evaluated is not None:
            query = query.where(QuestionCorrection.evaluated.is_(evaluated))
        with Session() as session:
            return session.execute(query).all()

    @app.get("/answers")
    def pending_answers():
        return render_template("answers.html", rows=correction_rows(False), corrected=False)

    @app.get("/answers/corrected")
    def corrected_answers():
        return render_template("answers.html", rows=correction_rows(True), corrected=True)

    @app.get("/scans")
    def scans():
        with Session() as session:
            sources = session.scalars(select(ExamCorrectionPdf).order_by(ExamCorrectionPdf.id)).all()
        return render_template("scans.html", sources=sources, rows=correction_rows())

    @app.route("/answers/<int:answer_id>/correct", methods=["GET", "POST"])
    def correct_answer(answer_id):
        with Session.begin() as session:
            answer = session.get(QuestionCorrection, answer_id)
            if answer is None:
                abort(404)
            source = session.get(ExamCorrectionPdf, answer.exam_correction_pdf_id)
            if request.method == "POST":
                try:
                    grade = float(request.form.get("grade", ""))
                    if not math.isfinite(grade) or grade < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    flash("Grade must be a finite, non-negative number.", "error")
                else:
                    answer.grade = grade
                    answer.comment = request.form.get("comment", "").strip()
                    answer.evaluated = True
                    flash("Correction saved successfully.", "success")
                    return redirect(url_for("corrected_answers"))
        return render_template("correctAnswer.html", answer=answer, source=source)

    @app.get("/answers/<int:answer_id>/page.<extension>")
    def answer_page(answer_id, extension):
        if extension not in ("pdf", "png"):
            abort(404)
        with Session() as session:
            answer = session.get(QuestionCorrection, answer_id)
            if answer is None:
                abort(404)
            contents = answer.page_pdf_file if extension == "pdf" else answer.page_png_file
        return send_file(BytesIO(contents), mimetype="application/pdf" if extension == "pdf" else "image/png",
                         download_name=f"answer-{answer_id}.{extension}")

    @app.get("/scans/<int:scan_id>/original.pdf")
    def original_pdf(scan_id):
        with Session() as session:
            source = session.get(ExamCorrectionPdf, scan_id)
            if source is None:
                abort(404)
            contents, filename = source.pdf_file, source.filename
        return send_file(BytesIO(contents), mimetype="application/pdf",
                         download_name=filename, as_attachment=True)

    @app.errorhandler(404)
    def not_found(_):
        return render_template("error.html", message="The requested page or record was not found."), 404

    @app.errorhandler(413)
    def too_large(_):
        return render_template("error.html", message="The upload exceeds the 50 MB limit."), 413

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5004, debug=True)
