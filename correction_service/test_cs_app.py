import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Event, current_thread
from unittest.mock import patch

import cv2
import numpy as np
import pymupdf
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

if __package__:
    from . import cs_app as correction_service
    from .correction_data import (
        ExamCorrectionPdf,
        ProfessorAssignment,
        QuestionCorrection,
    )
else:
    import cs_app as correction_service
    from correction_data import ExamCorrectionPdf, ProfessorAssignment, QuestionCorrection


class CorrectionServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "corrections.sqlite"
        self.app = correction_service.create_app(
            f"sqlite:///{database_path.as_posix()}"
        )
        self.app.testing = True
        self.client = self.app.test_client()
        self.session_factory = self.app.extensions["correction_session_factory"]

    def tearDown(self):
        self.app.extensions["correction_engine"].dispose()
        self.temp_dir.cleanup()

    @staticmethod
    def make_pdf(page_count):
        document = pymupdf.open()
        for number in range(1, page_count + 1):
            page = document.new_page()
            page.insert_text((72, 72), f"Answer page {number}")
        contents = document.tobytes()
        document.close()
        return contents

    def upload_pdf(self, contents, filename="scan.pdf"):
        return self.client.post(
            "/scans/upload",
            data={
                "exam_id": "81001",
                "file": (io.BytesIO(contents), filename),
            },
            content_type="multipart/form-data",
        )

    def assign_professor(self, question_number=1, exam_id=81001, professor_id=None):
        response = self.client.post("/assignments", data={
            "exam_id": str(exam_id),
            "professor_id": str(professor_id if professor_id is not None else 82000 + question_number),
            "question_number": str(question_number),
        })
        self.assertEqual(response.status_code, 302)

    def test_required_pages_load(self):
        self.assertIsNone(self.app.secret_key)
        for path in (
            "/",
            "/assignments",
            "/scans/upload",
            "/answers",
            "/answers/corrected",
            "/scans",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertNotIn(b'<p class="error"', response.data)
                self.assertNotIn(b'<p class="success"', response.data)

    def test_home_handles_empty_data_and_links_to_correction_actions(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for count_id in ("pdf-count", "page-count", "corrected-count", "pending-count"):
            self.assertIn(f'id="{count_id}">0</dd>'.encode(), response.data)
        self.assertIn(b'id="completion-percent">0%</span>', response.data)
        self.assertIn(b'<progress value="0" max="1"', response.data)
        self.assertIn(b'id="empty-state"', response.data)
        for path in ("/assignments", "/scans/upload", "/answers", "/answers/corrected", "/scans"):
            self.assertIn(f'href="{path}"'.encode(), response.data)

    def test_home_progress_updates_for_zero_grade_and_completed_corrections(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        for question_number in range(1, 5):
            self.assign_professor(question_number)
        with self.session_factory() as session:
            answer_ids = session.scalars(
                select(QuestionCorrection.id).order_by(QuestionCorrection.id)
            ).all()
        self.assertEqual(self.client.post(
            f"/answers/{answer_ids[0]}/correct", data={"grade": "0"}
        ).status_code, 302)

        response = self.client.get("/")
        self.assertIn(b'id="pdf-count">1</dd>', response.data)
        self.assertIn(b'id="page-count">4</dd>', response.data)
        self.assertIn(b'id="corrected-count">1</dd>', response.data)
        self.assertIn(b'id="pending-count">3</dd>', response.data)
        self.assertIn(b'id="completion-percent">25%</span>', response.data)
        self.assertIn(b'<progress value="1" max="4"', response.data)
        self.assertNotIn(b'id="empty-state"', response.data)

        for answer_id in answer_ids[1:]:
            self.assertEqual(self.client.post(
                f"/answers/{answer_id}/correct", data={"grade": "10"}
            ).status_code, 302)
        response = self.client.get("/")
        self.assertIn(b'id="corrected-count">4</dd>', response.data)
        self.assertIn(b'id="pending-count">0</dd>', response.data)
        self.assertIn(b'id="completion-percent">100%</span>', response.data)
        self.assertIn(b'<progress value="4" max="4"', response.data)
        self.assertIn(b"All available answers have been corrected.", response.data)

    def test_assignment_is_saved_and_question_range_is_validated(self):
        response = self.client.post(
            "/assignments",
            data={
                "exam_id": "81001",
                "professor_id": "82001",
                "question_number": "2",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/assignments/success")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"Professor assignment saved successfully", response.data)
        self.assertIn(b'<form method="post" action="/assignments">', response.data)
        self.assertNotIn(
            b"Professor assignment saved successfully", self.client.get("/assignments").data
        )

        invalid_response = self.client.post(
            "/assignments",
            data={
                "exam_id": "81001",
                "professor_id": "82002",
                "question_number": "5",
            },
        )
        self.assertIn(b"between 1 and 4", invalid_response.data)
        self.assertEqual(invalid_response.status_code, 200)
        self.assertNotIn("Location", invalid_response.headers)
        self.assertNotIn("Set-Cookie", invalid_response.headers)
        self.assertIn(b'value="82002"', invalid_response.data)
        with self.session_factory() as session:
            assignments = session.scalars(select(ProfessorAssignment)).all()
            self.assertEqual(len(assignments), 1)
            self.assertEqual(assignments[0].question_number, 2)

    def test_professor_cannot_be_assigned_to_another_question_in_the_same_exam(self):
        self.assign_professor(question_number=1)
        self.assign_professor(question_number=2)
        for question_number in (3, 2):
            with self.subTest(question_number=question_number):
                response = self.client.post("/assignments", data={
                    "exam_id": "81001", "professor_id": "82001",
                    "question_number": str(question_number),
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"This professor is already assigned to another question in this exam.", response.data)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(b'value="82001"', response.data)
                self.assertIn(f'<option value="{question_number}" selected>'.encode(), response.data)
        with self.session_factory() as session:
            assignments = session.scalars(select(ProfessorAssignment).order_by(
                ProfessorAssignment.question_number
            )).all()
            self.assertEqual([(row.question_number, row.professor_id) for row in assignments],
                             [(1, 82001), (2, 82002)])

    def test_same_professor_can_be_assigned_in_different_exams(self):
        self.assign_professor(question_number=1, professor_id=82001)
        self.assign_professor(question_number=2, exam_id=81002, professor_id=82001)
        with self.session_factory() as session:
            self.assertEqual(len(session.scalars(select(ProfessorAssignment)).all()), 2)

    def test_repeated_assignment_shows_message_without_changing_data(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        self.assign_professor()
        with self.session_factory() as session:
            assignment_id = session.scalar(select(ProfessorAssignment.id))
        for grade in (None, "0", "10"):
            with self.subTest(grade=grade):
                comment = "" if grade is None else f"Correction with grade {grade}"
                if grade is not None:
                    self.assertEqual(self.client.post("/answers/1/correct", data={
                        "grade": grade, "comment": comment,
                    }).status_code, 302)
                response = self.client.post("/assignments", data={
                    "exam_id": "81001", "professor_id": "82001", "question_number": "1",
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"This professor is already assigned to this question.", response.data)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertNotIn(b"Professor assignment saved successfully.", response.data)
                self.assertIn(b'value="81001"', response.data)
                self.assertIn(b'value="82001"', response.data)
                self.assertIn(b'<option value="1" selected>', response.data)
                with self.session_factory() as session:
                    assignments = session.scalars(select(ProfessorAssignment)).all()
                    self.assertEqual(len(assignments), 1)
                    self.assertEqual(assignments[0].id, assignment_id)
                    self.assertEqual(assignments[0].professor_id, 82001)
                    answer = session.get(QuestionCorrection, 1)
                    self.assertEqual(answer.evaluated, grade is not None)
                    self.assertEqual(answer.grade, None if grade is None else float(grade))
                    self.assertEqual(answer.comment, comment)

    def test_professor_can_be_changed_before_any_answer_is_corrected(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(8)).status_code, 302)
        self.assign_professor(professor_id=82001)
        self.assign_professor(professor_id=82005)
        self.assign_professor(question_number=2, professor_id=82001)
        with self.session_factory() as session:
            assignment = session.scalar(select(ProfessorAssignment).where(
                ProfessorAssignment.exam_id == 81001,
                ProfessorAssignment.question_number == 1,
            ))
            self.assertEqual(assignment.professor_id, 82005)
        self.assertIn(b"Professor: 82005.", self.client.get("/answers/1/correct").data)
        self.assertIn(b"Professor: 82005.", self.client.get("/answers/5/correct").data)

    def test_corrected_answer_locks_professor_for_the_entire_exam_question(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(8)).status_code, 302)
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        for question_number, grade in ((1, "0"), (2, "10")):
            with self.subTest(question_number=question_number, grade=grade):
                self.assign_professor(question_number=question_number)
                answer_id = question_number + 8
                self.assertEqual(self.client.post(f"/answers/{answer_id}/correct", data={
                    "grade": grade, "comment": "Original professor correction",
                }).status_code, 302)
                response = self.client.post("/assignments", data={
                    "exam_id": "81001", "question_number": str(question_number),
                    "professor_id": str(82010 + question_number),
                })
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"The professor cannot be changed because this exam question already has corrected answers.", response.data)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                with self.session_factory() as session:
                    assignment = session.scalar(select(ProfessorAssignment).where(
                        ProfessorAssignment.exam_id == 81001,
                        ProfessorAssignment.question_number == question_number,
                    ))
                    self.assertEqual(assignment.professor_id, 82000 + question_number)
                    answer = session.get(QuestionCorrection, answer_id)
                    self.assertEqual(answer.grade, float(grade))
                    self.assertEqual(answer.comment, "Original professor correction")
                    self.assertTrue(answer.evaluated)
                    self.assertFalse(session.get(QuestionCorrection, question_number).evaluated)
        self.assign_professor(question_number=3)
        self.assign_professor(question_number=3, professor_id=82013)
        self.assign_professor(question_number=1, exam_id=81002, professor_id=82001)

    def test_database_rejects_duplicate_professor_assignments(self):
        self.assign_professor()
        with self.assertRaises(IntegrityError):
            with self.session_factory.begin() as session:
                session.add(ProfessorAssignment(exam_id=81001, question_number=2, professor_id=82001))
        self.assign_professor(question_number=2, professor_id=82002)

    def test_grading_and_professor_reassignment_cannot_bypass_each_other(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        self.assign_professor()
        grading_started = Event()
        reassignment_started = Event()
        finish_grading = Event()
        isfinite = correction_service.math.isfinite

        def pause_grading(value):
            grading_started.set()
            if not finish_grading.wait(5):
                raise RuntimeError("Timed out waiting to finish grading")
            return isfinite(value)

        def observe_reassignment(connection, cursor, statement, parameters, context, executemany):
            if current_thread().name.startswith("assignment-race") and statement == "BEGIN IMMEDIATE":
                reassignment_started.set()

        def save_grade():
            with self.app.test_client() as client:
                return client.post("/answers/1/correct", data={"grade": "10"})

        def reassign_professor():
            with self.app.test_client() as client:
                return client.post("/assignments", data={
                    "exam_id": "81001", "question_number": "1", "professor_id": "82002",
                })

        engine = self.app.extensions["correction_engine"]
        event.listen(engine, "before_cursor_execute", observe_reassignment)
        try:
            with patch.object(correction_service.math, "isfinite", side_effect=pause_grading):
                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="assignment-race") as executor:
                    grading = executor.submit(save_grade)
                    try:
                        self.assertTrue(grading_started.wait(5))
                        # Ignore the BEGIN issued by the grading request itself.
                        reassignment_started.clear()
                        reassignment = executor.submit(reassign_professor)
                        self.assertTrue(reassignment_started.wait(5))
                        self.assertFalse(reassignment.done())
                    finally:
                        finish_grading.set()
                    self.assertEqual(grading.result(timeout=5).status_code, 302)
                    response = reassignment.result(timeout=5)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(b"The professor cannot be changed because this exam question already has corrected answers.", response.data)
        finally:
            event.remove(engine, "before_cursor_execute", observe_reassignment)
        with self.session_factory() as session:
            self.assertEqual(session.scalar(select(ProfessorAssignment)).professor_id, 82001)
            self.assertEqual(session.get(QuestionCorrection, 1).grade, 10)

    def test_legacy_database_is_validated_without_rewriting_its_assignments(self):
        database_path = Path(self.temp_dir.name) / "legacy.sqlite"
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("""
                CREATE TABLE professor_assignment (
                    id INTEGER PRIMARY KEY, professor_id INTEGER NOT NULL,
                    exam_id INTEGER NOT NULL, question_number INTEGER NOT NULL,
                    UNIQUE (exam_id, question_number)
                )
            """)
            connection.executemany(
                "INSERT INTO professor_assignment (professor_id, exam_id, question_number) VALUES (?, ?, ?)",
                [(82001, 81001, 1), (82001, 81001, 2)],
            )
            connection.commit()
        legacy_app = correction_service.create_app(f"sqlite:///{database_path.as_posix()}")
        legacy_app.testing = True
        try:
            client = legacy_app.test_client()
            factory = legacy_app.extensions["correction_session_factory"]
            self.assertEqual(client.get("/assignments").status_code, 200)
            with factory() as session:
                self.assertEqual([row.professor_id for row in session.scalars(
                    select(ProfessorAssignment).order_by(ProfessorAssignment.id)
                )], [82001, 82001])
            response = client.post("/assignments", data={
                "exam_id": "81001", "professor_id": "82001", "question_number": "3",
            })
            self.assertIn(b"This professor is already assigned to another question in this exam.", response.data)
            self.assertEqual(client.post("/assignments", data={
                "exam_id": "81001", "professor_id": "82002", "question_number": "2",
            }).status_code, 302)
            self.assertEqual(client.post("/scans/upload", data={
                "exam_id": "81001", "file": (io.BytesIO(self.make_pdf(4)), "scan.pdf"),
            }).status_code, 302)
            self.assertEqual(client.post("/answers/1/correct", data={"grade": "0"}).status_code, 302)
            response = client.post("/assignments", data={
                "exam_id": "81001", "professor_id": "82003", "question_number": "1",
            })
            self.assertIn(b"The professor cannot be changed because this exam question already has corrected answers.", response.data)
        finally:
            legacy_app.extensions["correction_engine"].dispose()

    def test_four_page_scan_is_saved_and_pages_are_available(self):
        response = self.upload_pdf(self.make_pdf(4))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/scans/success?count=4")
        self.assertNotIn("Set-Cookie", response.headers)
        confirmation = self.client.get(response.headers["Location"])
        self.assertEqual(confirmation.status_code, 200)
        self.assertIn(b"PDF uploaded successfully: 4 page(s) ready for correction.", confirmation.data)
        self.assertNotIn("Set-Cookie", confirmation.headers)

        with self.session_factory() as session:
            source = session.get(ExamCorrectionPdf, 1)
            answers = session.scalars(
                select(QuestionCorrection).order_by(QuestionCorrection.page_number)
            ).all()
            self.assertEqual(source.exam_id, 81001)
            self.assertEqual(source.filename, "scan.pdf")
            self.assertEqual(len(answers), 4)
            self.assertEqual(
                [answer.question_number for answer in answers],
                [1, 2, 3, 4],
            )
            answer_id = answers[0].id

        for suffix, mime_type in (
            ("pdf", "application/pdf"),
            ("png", "image/png"),
        ):
            page_response = self.client.get(
                f"/answers/{answer_id}/page.{suffix}"
            )
            self.assertEqual(page_response.status_code, 200)
            self.assertEqual(page_response.mimetype, mime_type)
        self.assertEqual(
            self.client.get("/scans/1/original.pdf").status_code, 200
        )

    def test_rotated_scan_masks_qr_codes_and_preserves_original(self):
        detector = cv2.QRCodeDetector()
        rotations = (0, 90, 180, 270)
        with pymupdf.open() as document:
            for rotation in rotations:
                page = document.new_page(width=595, height=842)
                page.insert_text((200, 400), "Visible answer content")
                for number, (x, y) in enumerate(((18, 18), (505, 18), (18, 752), (505, 752))):
                    qr = cv2.QRCodeEncoder_create().encode(f"sheet-{rotation}-{number}")
                    qr = cv2.copyMakeBorder(qr, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
                    qr = cv2.resize(qr, None, fx=8, fy=8, interpolation=cv2.INTER_NEAREST)
                    encoded, png = cv2.imencode(".png", qr)
                    self.assertTrue(encoded)
                    page.insert_image(pymupdf.Rect(x, y, x + 72, y + 72), stream=png.tobytes())
                page.set_rotation(rotation)
            contents = document.tobytes()

        with pymupdf.open(stream=contents, filetype="pdf") as original:
            original_images = []
            for page in original:
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                    pixmap.height, pixmap.width, 3
                )
                _, decoded, _, _ = detector.detectAndDecodeMulti(image)
                self.assertEqual(len(decoded), 4, "All four source QR codes must be readable")
                original_images.append(image)

        self.assertEqual(self.upload_pdf(contents).status_code, 302)
        self.assertEqual(self.client.get("/scans/1/original.pdf").data, contents)
        with self.session_factory() as session:
            answers = session.scalars(
                select(QuestionCorrection).order_by(QuestionCorrection.page_number)
            ).all()
            self.assertEqual(len(answers), 4)
            for answer, original_image, rotation in zip(answers, original_images, rotations):
                with self.subTest(rotation=rotation):
                    png = cv2.imdecode(np.frombuffer(answer.page_png_file, dtype=np.uint8), cv2.IMREAD_COLOR)
                    with pymupdf.open(stream=answer.page_pdf_file, filetype="pdf") as masked:
                        self.assertEqual(masked[0].rotation, 0)
                        pixmap = masked[0].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                        pdf_image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
                            pixmap.height, pixmap.width, 3
                        )
                    for image in (png, pdf_image):
                        self.assertEqual(image.shape, original_image.shape)
                        _, decoded, points, _ = detector.detectAndDecodeMulti(image)
                        self.assertEqual(len(decoded), 0)
                        self.assertIsNone(points)
                    height, width = original_image.shape[:2]
                    np.testing.assert_array_equal(
                        pdf_image[height // 3:height * 2 // 3, width // 3:width * 2 // 3],
                        original_image[height // 3:height * 2 // 3, width // 3:width * 2 // 3],
                    )

    def test_scan_with_intro_page_is_supported_and_invalid_scan_is_atomic(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(5)).status_code, 302)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(QuestionCorrection.id).order_by(
                    QuestionCorrection.page_number.desc()
                )),
                4,
            )

        response = self.upload_pdf(self.make_pdf(3), "incomplete.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"must contain four answer pages", response.data)
        self.assertNotIn("Location", response.headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b'value="81001"', response.data)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(ExamCorrectionPdf.id).order_by(
                    ExamCorrectionPdf.id.desc()
                )),
                1,
            )
            self.assertEqual(
                session.scalar(select(QuestionCorrection.id).order_by(
                    QuestionCorrection.id.desc()
                )),
                4,
            )

    def test_grade_validation_and_zero_grade_correction(self):
        self.upload_pdf(self.make_pdf(4))
        self.assign_professor()
        with self.session_factory() as session:
            answer_id = session.scalar(
                select(QuestionCorrection.id).order_by(QuestionCorrection.id)
            )

        invalid_response = self.client.post(
            f"/answers/{answer_id}/correct",
            data={"grade": "nan", "comment": "Invalid"},
        )
        self.assertIn(b"finite, non-negative number", invalid_response.data)
        self.assertEqual(invalid_response.status_code, 200)
        self.assertNotIn("Location", invalid_response.headers)
        self.assertNotIn("Set-Cookie", invalid_response.headers)
        self.assertIn(b'value="nan"', invalid_response.data)
        self.assertIn(b">Invalid</textarea>", invalid_response.data)
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, answer_id)
            self.assertFalse(answer.evaluated)
            self.assertIsNone(answer.grade)

        response = self.client.post(
            f"/answers/{answer_id}/correct",
            data={"grade": "0", "comment": "No points"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/answers/corrected/success")
        self.assertIn(b"Correction saved successfully.", response.data)
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"No points", response.data)
        self.assertNotIn(
            b"Correction saved successfully.", self.client.get("/answers/corrected").data
        )
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, answer_id)
            self.assertTrue(answer.evaluated)
            self.assertEqual(answer.grade, 0)
            self.assertEqual(answer.comment, "No points")

    def test_unassigned_answer_is_blocked_until_a_professor_is_assigned(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        path = "/answers/1/correct"
        for method in ("GET", "POST"):
            with self.subTest(method=method):
                response = self.client.open(path, method=method, data={
                    "grade": "10", "comment": "Must not be saved",
                })
                self.assertEqual(response.status_code, 409)
                self.assertIn(b"Assign a professor to this exam question", response.data)
                self.assertIn(b'href="/assignments"', response.data)
                self.assertNotIn(b'name="grade"', response.data)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, 1)
            self.assertFalse(answer.evaluated)
            self.assertIsNone(answer.grade)
            self.assertEqual(answer.comment, "")
        for listing in ("/answers", "/scans"):
            response = self.client.get(listing)
            self.assertNotIn(f'href="{path}"'.encode(), response.data)
            self.assertIn(b">Assign professor</a>", response.data)
        for extension in ("pdf", "png"):
            self.assertEqual(self.client.get(f"/answers/1/page.{extension}").status_code, 200)

        self.assign_professor()
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'name="grade"', response.data)
        self.assertIn(b"Professor: 82001.", response.data)
        for listing in ("/answers", "/scans"):
            self.assertIn(f'href="{path}"'.encode(), self.client.get(listing).data)
        self.assertEqual(self.client.post(path, data={"grade": "10"}).status_code, 302)
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, 1)
            self.assertTrue(answer.evaluated)
            self.assertEqual(answer.grade, 10)

    def test_assignment_must_match_both_exam_and_question(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        self.assign_professor(question_number=2)
        self.assign_professor(question_number=1, exam_id=81002)
        for method in ("GET", "POST"):
            response = self.client.open("/answers/1/correct", method=method, data={"grade": "10"})
            self.assertEqual(response.status_code, 409)
        with self.session_factory() as session:
            self.assertFalse(session.get(QuestionCorrection, 1).evaluated)
        for listing in ("/answers", "/scans"):
            response = self.client.get(listing)
            self.assertNotIn(b'href="/answers/1/correct"', response.data)
            self.assertIn(b'href="/answers/2/correct"', response.data)
        self.assertEqual(self.client.get("/answers/2/correct").status_code, 200)
        self.assertEqual(self.client.post("/answers/2/correct", data={"grade": "10"}).status_code, 302)

    def test_removed_assignment_prevents_editing_an_existing_correction(self):
        self.assertEqual(self.upload_pdf(self.make_pdf(4)).status_code, 302)
        self.assign_professor()
        path = "/answers/1/correct"
        self.assertEqual(self.client.post(path, data={
            "grade": "10", "comment": "Original correction",
        }).status_code, 302)
        self.assertEqual(self.client.get(path).status_code, 200)
        with self.session_factory.begin() as session:
            session.delete(session.scalar(select(ProfessorAssignment)))
        for method in ("GET", "POST"):
            response = self.client.open(path, method=method, data={
                "grade": "20", "comment": "Must not replace the correction",
            })
            self.assertEqual(response.status_code, 409)
            self.assertNotIn(b'name="grade"', response.data)
            self.assertIn(b"Original correction", response.data)
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, 1)
            self.assertTrue(answer.evaluated)
            self.assertEqual(answer.grade, 10)
            self.assertEqual(answer.comment, "Original correction")

    def test_assignment_validation_errors_preserve_the_form(self):
        form = {"exam_id": "81001", "professor_id": "82001", "question_number": "2"}
        cases = (
            ({"exam_id": "invalid"}, b"Exam identifier must be a positive 64-bit integer."),
            ({"professor_id": "0"}, b"Professor identifier must be a positive 64-bit integer."),
            ({"question_number": "invalid"}, b"Question number must be a positive 64-bit integer."),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                response = self.client.post("/assignments", data={**form, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertIn(b'<form method="post" action="/assignments">', response.data)
        with self.session_factory() as session:
            self.assertIsNone(session.scalar(select(ProfessorAssignment.id)))

    def test_assignment_database_conflict_is_rendered_without_a_session(self):
        conflict = IntegrityError("INSERT", {}, RuntimeError("conflict"))
        with patch.object(self.session_factory, "begin", side_effect=conflict):
            response = self.client.post("/assignments", data={
                "exam_id": "81001", "professor_id": "82001", "question_number": "2",
            })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"This question assignment changed. Please try again.", response.data)
        self.assertIn(b'value="82001"', response.data)
        self.assertNotIn("Set-Cookie", response.headers)

    def test_upload_errors_use_predefined_messages_without_saving(self):
        cases = (
            (b"not a PDF", "scan.txt", b"Only PDF files are accepted."),
            (b"", "scan.pdf", b"The uploaded PDF is empty."),
            (b"not a PDF", "scan.pdf", b"The uploaded file is not a readable PDF."),
        )
        for contents, filename, message in cases:
            with self.subTest(filename=filename, contents=contents):
                response = self.upload_pdf(contents, filename)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertIn(b'value="81001"', response.data)

        response = self.client.post("/scans/upload", data={"exam_id": "81001"})
        self.assertIn(b"Please select a PDF file.", response.data)
        with patch.object(correction_service, "process_pdf", side_effect=RuntimeError("internal details")):
            response = self.upload_pdf(b"example", "scan.pdf")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"The PDF could not be processed.", response.data)
        self.assertNotIn(b"internal details", response.data)
        self.assertNotIn("Set-Cookie", response.headers)
        with self.session_factory() as session:
            self.assertIsNone(session.scalar(select(ExamCorrectionPdf.id)))

    def test_success_routes_are_repeatable_and_normal_lists_have_no_message(self):
        cases = (
            ("/assignments/success", b"Professor assignment saved successfully."),
            ("/scans/success?count=4", b"PDF uploaded successfully: 4 page(s) ready for correction."),
            ("/answers/corrected/success", b"Correction saved successfully."),
        )
        for path, message in cases:
            with self.subTest(path=path):
                for _ in range(2):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertIn(message, response.data)
                    self.assertNotIn("Set-Cookie", response.headers)
        for count in ("", "invalid", "0", "-1"):
            response = self.client.get("/scans/success", query_string={"count": count})
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"PDF uploaded successfully.", response.data)
            self.assertNotIn(b"page(s) ready for correction.", response.data)

    def test_missing_records_return_not_found(self):
        self.assertEqual(self.client.get("/answers/999/correct").status_code, 404)
        self.assertEqual(self.client.get("/answers/999/page.pdf").status_code, 404)
        self.assertEqual(self.client.get("/scans/999/original.pdf").status_code, 404)

    def test_out_of_range_record_ids_return_not_found(self):
        for identifier in (0, 2**63, 10**30):
            for path in (
                f"/answers/{identifier}/correct",
                f"/answers/{identifier}/page.pdf",
                f"/answers/{identifier}/page.png",
                f"/scans/{identifier}/original.pdf",
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)
            with self.subTest(identifier=identifier, method="POST"):
                response = self.client.post(f"/answers/{identifier}/correct", data={"grade": "10"})
                self.assertEqual(response.status_code, 404)

    def test_largest_sqlite_record_id_remains_accessible(self):
        identifier = 2**63 - 1
        pdf = self.make_pdf(1)
        with pymupdf.open(stream=pdf, filetype="pdf") as document:
            png = document[0].get_pixmap().tobytes("png")
        with self.session_factory.begin() as session:
            source = ExamCorrectionPdf(id=identifier, exam_id=81001, filename="scan.pdf", pdf_file=pdf)
            source.corrections = [QuestionCorrection(
                id=identifier, page_number=1, sheet_number=1, question_number=1,
                page_pdf_file=pdf, page_png_file=png,
            )]
            session.add(source)
        self.assign_professor()
        for path in (
            f"/answers/{identifier}/correct",
            f"/answers/{identifier}/page.pdf",
            f"/answers/{identifier}/page.png",
            f"/scans/{identifier}/original.pdf",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.post(
            f"/answers/{identifier}/correct", data={"grade": "10"}
        ).status_code, 302)


if __name__ == "__main__":
    unittest.main()
