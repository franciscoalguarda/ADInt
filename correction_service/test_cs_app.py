import io
import tempfile
import unittest
from pathlib import Path

import pymupdf
from sqlalchemy import select

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

    def test_required_pages_load(self):
        for path in (
            "/",
            "/assignments",
            "/scans/upload",
            "/answers",
            "/answers/corrected",
            "/scans",
        ):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)

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
        self.assertIn(b"Professor assignment saved successfully", response.data)

        invalid_response = self.client.post(
            "/assignments",
            data={
                "exam_id": "81001",
                "professor_id": "82002",
                "question_number": "5",
            },
        )
        self.assertIn(b"between 1 and 4", invalid_response.data)
        with self.session_factory() as session:
            assignments = session.scalars(select(ProfessorAssignment)).all()
            self.assertEqual(len(assignments), 1)
            self.assertEqual(assignments[0].question_number, 2)

    def test_four_page_scan_is_saved_and_pages_are_available(self):
        response = self.upload_pdf(self.make_pdf(4))
        self.assertEqual(response.status_code, 302)

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
        with self.session_factory() as session:
            answer_id = session.scalar(
                select(QuestionCorrection.id).order_by(QuestionCorrection.id)
            )

        invalid_response = self.client.post(
            f"/answers/{answer_id}/correct",
            data={"grade": "nan", "comment": "Invalid"},
        )
        self.assertIn(b"finite, non-negative number", invalid_response.data)
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
        self.assertIn(b"No points", response.data)
        with self.session_factory() as session:
            answer = session.get(QuestionCorrection, answer_id)
            self.assertTrue(answer.evaluated)
            self.assertEqual(answer.grade, 0)
            self.assertEqual(answer.comment, "No points")

    def test_missing_records_return_not_found(self):
        self.assertEqual(self.client.get("/answers/999/correct").status_code, 404)
        self.assertEqual(self.client.get("/answers/999/page.pdf").status_code, 404)
        self.assertEqual(self.client.get("/scans/999/original.pdf").status_code, 404)


if __name__ == "__main__":
    unittest.main()
