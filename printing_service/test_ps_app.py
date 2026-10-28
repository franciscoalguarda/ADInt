import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from xmlrpc.client import Fault, ServerProxy

import pymupdf
import qrcode
from sqlalchemy import select

if __package__:
    from .ps_app import create_app, create_server
    from .printing_data import PrintedExam, ResolutionSheet
else:
    from ps_app import create_app, create_server
    from printing_data import PrintedExam, ResolutionSheet


class PrintingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.output_dir = self.root / "pdfs"
        self.database_url = f"sqlite:///{(self.root / 'printing.sqlite').as_posix()}"
        self.app = create_app(database_url=self.database_url, output_dir=self.output_dir)
        self.app.testing = True
        self.client = self.app.test_client()
        self.server = create_server(
            port=0,
            output_dir=self.output_dir,
            database_url=self.database_url,
        )
        self.server_thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.server_thread.start()
        self.proxy = ServerProxy(
            f"http://127.0.0.1:{self.server.server_address[1]}"
        )

    def tearDown(self):
        self.proxy.__exit__(None, None, None)
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.server.app.extensions["printing_engine"].dispose()
        self.app.extensions["printing_engine"].dispose()
        self.temp_dir.cleanup()

    def test_print_exam_creates_four_answer_pages_per_student(self):
        with patch("qrcode.make", wraps=qrcode.make) as qr_factory:
            result = self.proxy.print_exam(
                12, "2026-11-01", "Algorithms", [10, 20]
            )

        pdf_path = self.output_dir / "exam_12.pdf"
        self.assertIn(str(pdf_path.resolve()), result)
        self.assertTrue(pdf_path.is_file())
        pdf_contents = pdf_path.read_bytes()
        self.assertTrue(pdf_contents.startswith(b"%PDF-"))
        with pymupdf.open(stream=pdf_contents, filetype="pdf") as document:
            self.assertEqual(document.page_count, 8)
            self.assertIn("Question 1", document[0].get_text())
            self.assertIn("Question 4", document[7].get_text())

        session_factory = self.app.extensions["printing_session_factory"]
        with session_factory() as session:
            exam = session.get(PrintedExam, 12)
            sheets = session.scalars(
                select(ResolutionSheet)
                .where(ResolutionSheet.exam_id == 12)
                .order_by(ResolutionSheet.id)
            ).all()
            self.assertEqual(exam.exam_date.isoformat(), "2026-11-01")
            self.assertEqual(exam.course, "Algorithms")
            self.assertEqual([sheet.student_id for sheet in sheets], [10, 20])
            self.assertEqual(len({sheet.id for sheet in sheets}), 2)
            self.assertEqual(exam.pdf_file, pdf_contents)
            sheet_ids = [sheet.id for sheet in sheets]

        self.assertEqual(
            [call.args[0] for call in qr_factory.call_args_list],
            [str(sheet_id) for sheet_id in sheet_ids for _ in range(4)],
        )
        with pymupdf.open(stream=pdf_contents, filetype="pdf") as document:
            for page_number in range(document.page_count):
                self.assertEqual(
                    len(document[page_number].get_image_info()),
                    4,
                    f"Expected four QR code images on page {page_number + 1}",
                )

    def test_web_page_lists_exams_and_serves_saved_pdf(self):
        self.proxy.print_exam(12, "2026-11-01", "Algorithms", [10])

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(b"2026-11-01", response.data)
        self.assertIn(b"1", response.data)

        response = self.client.get("/exams/12/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertEqual(self.client.get("/exams/999/pdf").status_code, 404)

    def test_reprinting_replaces_exam_and_never_reuses_sheet_identifiers(self):
        self.proxy.print_exam(12, "2026-11-01", "Algorithms", [10, 20])
        session_factory = self.app.extensions["printing_session_factory"]
        with session_factory() as session:
            old_ids = set(
                session.scalars(
                    select(ResolutionSheet.id).where(ResolutionSheet.exam_id == 12)
                ).all()
            )

        self.proxy.print_exam(12, "2026-11-02", "Databases", [30])

        with session_factory() as session:
            exam = session.get(PrintedExam, 12)
            sheets = session.scalars(
                select(ResolutionSheet).where(ResolutionSheet.exam_id == 12)
            ).all()
            self.assertEqual(exam.course, "Databases")
            self.assertEqual(exam.exam_date.isoformat(), "2026-11-02")
            self.assertEqual([sheet.student_id for sheet in sheets], [30])
            self.assertTrue(old_ids.isdisjoint({sheet.id for sheet in sheets}))
            with pymupdf.open(stream=exam.pdf_file, filetype="pdf") as document:
                self.assertEqual(document.page_count, 4)

    def test_invalid_exam_date_is_rejected(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam(12, "2026-02-30", "Algorithms", [10])

    def test_decimal_strings_support_64bit_exam_and_student_identifiers(self):
        exam_id = 2**63 - 1
        student_ids = [2**31, 2**63 - 1]
        self.proxy.print_exam(
            str(exam_id), "2026-11-01", "Algorithms",
            [str(student_id) for student_id in student_ids],
        )
        with self.app.extensions["printing_session_factory"]() as session:
            exam = session.get(PrintedExam, exam_id)
            self.assertIsNotNone(exam)
            self.assertEqual([sheet.student_id for sheet in exam.sheets], student_ids)
            with pymupdf.open(stream=exam.pdf_file, filetype="pdf") as document:
                self.assertEqual(document.page_count, 8)
        self.assertEqual(self.client.get(f"/exams/{exam_id}/pdf").status_code, 200)

    def test_invalid_identifier_values_are_rejected(self):
        for identifier in ("0", "-1", "1.5", "abc", str(2**63), True, 1.5):
            with self.subTest(identifier=identifier, field="exam_id"):
                with self.assertRaises(Fault):
                    self.proxy.print_exam(identifier, "2026-11-01", "Algorithms", [10])
            with self.subTest(identifier=identifier, field="student_id"):
                with self.assertRaises(Fault):
                    self.proxy.print_exam(12, "2026-11-01", "Algorithms", [identifier])
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(PrintedExam.id)))

    def test_duplicate_ids_are_detected_after_normalizing_wire_types(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam("12", "2026-11-01", "Algorithms", [10, "010"])
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.get(PrintedExam, 12))

    def test_duplicate_student_ids_are_rejected_without_creating_exam(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam(12, "2026-11-01", "Algorithms", [10, 10])

        self.assertFalse((self.output_dir / "exam_12.pdf").exists())
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.get(PrintedExam, 12))

    def test_xmlrpc_root_path_remains_supported(self):
        root_proxy = ServerProxy(
            f"http://127.0.0.1:{self.server.server_address[1]}/"
        )
        try:
            result = root_proxy.print_exam(13, "2026-11-01", "Algorithms", [11])
        finally:
            root_proxy.__exit__(None, None, None)
        self.assertIn("exam_13.pdf", result)


if __name__ == "__main__":
    unittest.main()
