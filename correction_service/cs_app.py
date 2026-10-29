import math
import os
from io import BytesIO
from pathlib import Path

import cv2
import numpy as np
import pymupdf
from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from werkzeug.utils import secure_filename

if __package__:
    from .correction_data import Base, ExamCorrectionPdf, ProfessorAssignment, QuestionCorrection
else:
    from correction_data import Base, ExamCorrectionPdf, ProfessorAssignment, QuestionCorrection


def split_pdf_into_pages(document: pymupdf.Document) -> list[pymupdf.Document]:
	"""Create an independent one-page PDF document for every source page.

	Args:
		document: PDF document whose pages should be separated.

	Returns:
		A list of open PDF documents, each containing one copied page. The
		caller is responsible for closing every returned document.
	"""
	page_documents = []

	for page_number in range(document.page_count):
		page_document = pymupdf.open()
		page_document.insert_pdf(document, from_page=page_number, to_page=page_number)
		page_documents.append(page_document)

	return page_documents


def cover_qr_codes_with_gray_squares(page_documents: list[pymupdf.Document]) -> None:
    """Cover QR codes found across the page and in overlapping image regions."""
    detector = cv2.QRCodeDetector()
    for page_document in page_documents:
        page = page_document[0]
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(2, 2), colorspace=pymupdf.csGRAY, alpha=False
        )
        grayscale = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width
        )
        height, width = grayscale.shape
        tile_width, tile_height = max(1, width // 2), max(1, height // 2)
        regions = [(0, 0, width, height)]
        regions.extend(
            (x, y, tile_width, tile_height)
            for y in sorted({0, (height - tile_height) // 2, height - tile_height})
            for x in sorted({0, (width - tile_width) // 2, width - tile_width})
        )
        boxes = []
        for x, y, region_width, region_height in regions:
            region = grayscale[y:y + region_height, x:x + region_width]
            # Whole-page multi-code detection can miss repeated codes. Single-code
            # detection in overlapping regions also finds codes across tile edges.
            found, points = detector.detectMulti(region)
            if not found or points is None:
                found, points = detector.detect(region)
            if not found or points is None:
                continue
            for qr_points in points.reshape(-1, 4, 2):
                left, right = qr_points[:, 0].min() + x, qr_points[:, 0].max() + x
                top, bottom = qr_points[:, 1].min() + y, qr_points[:, 1].max() + y
                center_x, center_y = (left + right) / 2, (top + bottom) / 2
                if any(old_left <= center_x <= old_right and old_top <= center_y <= old_bottom
                       for old_left, old_top, old_right, old_bottom in boxes):
                    continue
                boxes.append((left, top, right, bottom))

        scale_x, scale_y = width / page.rect.width, height / page.rect.height
        for left, top, right, bottom in boxes:
            side = max(right - left, bottom - top) * 1.1
            center_x, center_y = (left + right) / 2, (top + bottom) / 2
            page.draw_rect(
                pymupdf.Rect(
                    page.rect.x0 + (center_x - side / 2) / scale_x,
                    page.rect.y0 + (center_y - side / 2) / scale_y,
                    page.rect.x0 + (center_x + side / 2) / scale_x,
                    page.rect.y0 + (center_y + side / 2) / scale_y,
                ),
                color=None, fill=(0.5, 0.5, 0.5), overlay=True,
            )


def process_pdf(contents):
    """Ignore an introductory page and export four answers per resolution sheet."""
    try:
        document = pymupdf.open(stream=contents, filetype="pdf")
    except (RuntimeError, ValueError) as error:
        raise ValueError("unreadable_pdf") from error

    pages = []
    with document:
        if document.needs_pass:
            raise ValueError("protected_pdf")
        if not document.page_count:
            raise ValueError("empty_pdf")
        # The supplied five-page PDFs begin with a page that is not an answer.
        # Accept either complete four-question sheets or one introductory page
        # followed by complete sheets. Only this in-memory copy is changed.
        if document.page_count > 1 and document.page_count % 4 == 1:
            document.delete_page(0)
        if document.page_count % 4:
            raise ValueError("invalid_page_count")
        # Align drawing coordinates with the rendered page without changing its appearance.
        for page in document:
            if page.rotation:
                page.remove_rotation()
        page_documents = split_pdf_into_pages(document)
        try:
            cover_qr_codes_with_gray_squares(page_documents)
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


def positive_identifier(value, reason):
    try:
        identifier = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(reason) from error
    if not 0 < identifier <= 2**63 - 1:
        raise ValueError(reason)
    return identifier


def get_record_or_404(session, model, identifier):
    if not 0 < identifier <= 2**63 - 1:
        abort(404)
    record = session.get(model, identifier)
    if record is None:
        abort(404)
    return record


def begin_sqlite_write(session):
    # Serialize assignment changes and grading so their checks remain valid until commit.
    if session.bind.dialect.name == "sqlite":
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")


def get_error_message(reason):
    messages = {
        "invalid_exam_id": "Exam identifier must be a positive 64-bit integer.",
        "invalid_professor_id": "Professor identifier must be a positive 64-bit integer.",
        "invalid_question_number": "Question number must be a positive 64-bit integer.",
        "invalid_question_range": "Question number must be between 1 and 4.",
        "assignment_changed": "This question assignment changed. Please try again.",
        "same_assignment": "This professor is already assigned to this question.",
        "professor_already_assigned": "This professor is already assigned to another question in this exam.",
        "assignment_locked": "The professor cannot be changed because this exam question already has corrected answers.",
        "missing_assignment": "Assign a professor to this exam question before correcting the answer.",
        "missing_pdf": "Please select a PDF file.",
        "invalid_pdf_extension": "Only PDF files are accepted.",
        "empty_upload": "The uploaded PDF is empty.",
        "unreadable_pdf": "The uploaded file is not a readable PDF.",
        "protected_pdf": "Password-protected PDFs are not supported.",
        "empty_pdf": "The PDF contains no pages.",
        "invalid_page_count": (
            "The PDF must contain four answer pages per sheet, "
            "optionally preceded by one introductory page."
        ),
        "pdf_processing_failed": "The PDF could not be processed. Please check the file and try again.",
        "invalid_grade": "Grade must be a finite, non-negative number.",
    }
    return messages.get(reason, "Could not complete the operation.")


def create_app(database_url=None):
    app = Flask(__name__)
    database_path = Path(__file__).resolve().with_name("corrections.sqlite")
    app.config.update(
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

    def get_correction_summary():
        with Session() as session:
            pdf_count = session.scalar(select(func.count()).select_from(ExamCorrectionPdf))
            page_count = session.scalar(select(func.count()).select_from(QuestionCorrection))
            corrected_count = session.scalar(
                select(func.count()).select_from(QuestionCorrection)
                .where(QuestionCorrection.evaluated.is_(True))
            )
        return {
            "pdf_count": pdf_count,
            "page_count": page_count,
            "corrected_count": corrected_count,
            "pending_count": page_count - corrected_count,
            "completion_percent": round(corrected_count * 100 / page_count) if page_count else 0,
        }

    @app.get("/")
    def index():
        return render_template("index.html", **get_correction_summary())

    @app.route("/assignments", methods=["GET", "POST"])
    def assignments():
        error_message = None
        if request.method == "POST":
            try:
                exam_id = positive_identifier(request.form.get("exam_id"), "invalid_exam_id")
                professor_id = positive_identifier(request.form.get("professor_id"), "invalid_professor_id")
                question = positive_identifier(request.form.get("question_number"), "invalid_question_number")
                if question not in range(1, 5):
                    raise ValueError("invalid_question_range")
                with Session.begin() as session:
                    begin_sqlite_write(session)
                    assignment = session.scalar(select(ProfessorAssignment).where(
                        ProfessorAssignment.exam_id == exam_id,
                        ProfessorAssignment.question_number == question,
                    ))
                    if assignment is not None and assignment.professor_id == professor_id:
                        raise ValueError("same_assignment")
                    corrected_answer = session.scalar(select(QuestionCorrection.id).join(
                        ExamCorrectionPdf,
                        QuestionCorrection.exam_correction_pdf_id == ExamCorrectionPdf.id,
                    ).where(
                        ExamCorrectionPdf.exam_id == exam_id,
                        QuestionCorrection.question_number == question,
                        QuestionCorrection.evaluated.is_(True),
                    ).limit(1))
                    if corrected_answer is not None:
                        raise ValueError("assignment_locked")
                    other_assignment = session.scalar(select(ProfessorAssignment.id).where(
                        ProfessorAssignment.exam_id == exam_id,
                        ProfessorAssignment.professor_id == professor_id,
                        ProfessorAssignment.question_number != question,
                    ).limit(1))
                    if other_assignment is not None:
                        raise ValueError("professor_already_assigned")
                    if assignment is None:
                        session.add(ProfessorAssignment(
                            exam_id=exam_id, question_number=question, professor_id=professor_id
                        ))
                    else:
                        assignment.professor_id = professor_id
            except ValueError as error:
                error_message = get_error_message(str(error))
            except IntegrityError:
                error_message = get_error_message("assignment_changed")
            else:
                return redirect(url_for("assignments_success"))
        return render_assignments(error=error_message)

    def render_assignments(error=None, success=None):
        with Session() as session:
            rows = session.scalars(select(ProfessorAssignment).order_by(
                ProfessorAssignment.exam_id, ProfessorAssignment.question_number
            )).all()
        return render_template("assignments.html", assignments=rows, error=error, success=success)

    @app.get("/assignments/success")
    def assignments_success():
        return render_assignments(success="Professor assignment saved successfully.")

    @app.route("/scans/upload", methods=["GET", "POST"])
    def upload_scan():
        error_message = None
        if request.method == "POST":
            try:
                exam_id = positive_identifier(request.form.get("exam_id"), "invalid_exam_id")
                upload = request.files.get("file")
                if upload is None or not upload.filename:
                    raise ValueError("missing_pdf")
                if not upload.filename.lower().endswith(".pdf"):
                    raise ValueError("invalid_pdf_extension")
                contents = upload.read()
                if not contents:
                    raise ValueError("empty_upload")
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
            except ValueError as error:
                error_message = get_error_message(str(error))
            except RuntimeError:
                error_message = get_error_message("pdf_processing_failed")
            else:
                return redirect(url_for("scans_success", count=len(pages)))
        return render_template("uploadScan.html", error=error_message)

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

    @app.get("/answers/corrected/success")
    def corrected_answers_success():
        return render_template(
            "answers.html", rows=correction_rows(True), corrected=True,
            success="Correction saved successfully.",
        )

    @app.get("/scans")
    def scans():
        return render_scans()

    def render_scans(success=None):
        with Session() as session:
            sources = session.scalars(select(ExamCorrectionPdf).order_by(ExamCorrectionPdf.id)).all()
        return render_template("scans.html", sources=sources, rows=correction_rows(), success=success)

    @app.get("/scans/success")
    def scans_success():
        count = request.args.get("count", type=int)
        success = "PDF uploaded successfully."
        if count is not None and count > 0:
            success = f"PDF uploaded successfully: {count} page(s) ready for correction."
        return render_scans(success=success)

    @app.route("/answers/<int:answer_id>/correct", methods=["GET", "POST"])
    def correct_answer(answer_id):
        error_message = None
        with Session.begin() as session:
            if request.method == "POST":
                begin_sqlite_write(session)
            answer = get_record_or_404(session, QuestionCorrection, answer_id)
            source = session.get(ExamCorrectionPdf, answer.exam_correction_pdf_id)
            assignment = session.scalar(select(ProfessorAssignment).where(
                ProfessorAssignment.exam_id == source.exam_id,
                ProfessorAssignment.question_number == answer.question_number,
            ))
            if assignment is None:
                error_message = get_error_message("missing_assignment")
            elif request.method == "POST":
                try:
                    grade = float(request.form.get("grade", ""))
                    if not math.isfinite(grade) or grade < 0:
                        raise ValueError
                except (TypeError, ValueError):
                    error_message = get_error_message("invalid_grade")
                else:
                    answer.grade = grade
                    answer.comment = request.form.get("comment", "").strip()
                    answer.evaluated = True
                    return redirect(url_for("corrected_answers_success"))
        return render_template(
            "correctAnswer.html", answer=answer, source=source,
            assignment=assignment, error=error_message,
        ), 409 if assignment is None else 200

    @app.get("/answers/<int:answer_id>/page.<extension>")
    def answer_page(answer_id, extension):
        if extension not in ("pdf", "png"):
            abort(404)
        with Session() as session:
            answer = get_record_or_404(session, QuestionCorrection, answer_id)
            contents = answer.page_pdf_file if extension == "pdf" else answer.page_png_file
        return send_file(BytesIO(contents), mimetype="application/pdf" if extension == "pdf" else "image/png",
                         download_name=f"answer-{answer_id}.{extension}")

    @app.get("/scans/<int:scan_id>/original.pdf")
    def original_pdf(scan_id):
        with Session() as session:
            source = get_record_or_404(session, ExamCorrectionPdf, scan_id)
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
