import io
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from xmlrpc.client import Fault, dumps, loads

import pymupdf

if __package__:
    from .ps_app import create_app as create_printing_app
    from .printing_data import PrintedExam
else:
    from ps_app import create_app as create_printing_app
    from printing_data import PrintedExam


class AdminPrintingIntegrationTests(unittest.TestCase):
    """Exercise both services through the real XML-RPC encoder and Flask endpoint."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        with patch.dict(os.environ, {
            "ADMIN_DATABASE_URL": "sqlite:///:memory:",
        }):
            from admin_service import as_app
        as_app.app.extensions["admin_engine"].dispose()
        self.admin_module = as_app
        self.admin_app = as_app.create_app(database_url=f"sqlite:///{(self.root / 'admin.sqlite').as_posix()}")
        self.admin_app.testing = True
        self.addCleanup(self.admin_app.extensions["admin_engine"].dispose)
        self.printing_app = create_printing_app(
            database_url=f"sqlite:///{(self.root / 'printing.sqlite').as_posix()}",
            output_dir=self.root / "output",
        )
        self.printing_app.testing = True
        self.addCleanup(self.printing_app.extensions["printing_engine"].dispose)
        self.client = self.admin_app.test_client()
        printing_client = self.printing_app.test_client()

        class PrintingProxy:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def print_exam(self, *args):
                return self.call("print_exam", args)

            def call(self, method, args):
                self.calls.append((method, args))
                response = printing_client.post(
                    "/RPC2", data=dumps(args, methodname=method), content_type="text/xml",
                )
                if response.status_code != 200:
                    raise AssertionError(f"XML-RPC endpoint returned {response.status_code}")
                return loads(response.data)[0][0]

        self.proxy = PrintingProxy()
        rpc_patch = patch.object(as_app, "ServerProxy", return_value=self.proxy)
        rpc_patch.start()
        self.addCleanup(rpc_patch.stop)
        self.exam_date = date.today() + timedelta(days=7)
        self.assertEqual(self.client.post("/exams/schedule", data={
            "course": "Algorithms", "date": self.exam_date.isoformat(),
        }).status_code, 302)

    def enroll(self, student_ids):
        contents = "student_id\n" + "".join(f"{identifier}\n" for identifier in student_ids)
        return self.client.post("/exams/1/enroll", data={
            "file": (io.BytesIO(contents.encode()), "students.csv"),
        })

    def snapshot(self):
        with self.printing_app.extensions["printing_session_factory"]() as session:
            exam = session.get(PrintedExam, 1)
            return exam.pdf_file, {sheet.student_id: sheet.id for sheet in exam.sheets}

    '''
        Verify through real XML-RPC serialization that enrollment alone leaves
        the PDF unchanged, repeated orders reuse it, and a later explicit order
        adds students while preserving existing QR IDs and rendered pages.
        Repeated CSV IDs and existing enrollments must not add extra sheets.
    '''
    def test_new_students_update_pdf_only_after_another_printing_order(self):
        self.assertEqual(self.enroll([10, 20]).status_code, 302)
        self.assertEqual(self.proxy.calls, [])
        self.assertEqual(self.printing_app.test_client().get("/exams/1/pdf").status_code, 404)
        self.assertEqual(self.client.post("/exams/1/order-printing").status_code, 302)
        original_pdf, original_ids = self.snapshot()
        with pymupdf.open(stream=original_pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 8)
            original_page_images = [page.get_pixmap().tobytes("png") for page in document]
        self.assertEqual(self.client.post("/exams/1/order-printing").status_code, 302)
        self.assertEqual(self.snapshot(), (original_pdf, original_ids))
        calls = len(self.proxy.calls)
        enrollment = self.enroll([10, 30, 30, 20, 40])
        self.assertEqual(enrollment.status_code, 302)
        self.assertEqual(enrollment.headers["Location"], "/enrollments/success?count=2")
        repeated = self.enroll([10, 20, 30, 40])
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(repeated.headers["Location"], "/enrollments/success?count=0")
        self.assertEqual(len(self.proxy.calls), calls)
        self.assertEqual(self.snapshot(), (original_pdf, original_ids))
        self.assertEqual((self.root / "output" / "exam_1.pdf").read_bytes(), original_pdf)
        # Viewing and saving use the stored PDF until another printing order is sent.
        self.assertEqual(self.printing_app.test_client().get("/exams/1/pdf").data, original_pdf)
        self.assertEqual(self.printing_app.test_client().post("/exams/1/pdf/save").status_code, 302)
        self.assertEqual((self.root / "output" / "exam_1.pdf").read_bytes(), original_pdf)
        self.assertEqual(self.client.post("/exams/1/order-printing").status_code, 302)
        updated_pdf, updated_ids = self.snapshot()
        self.assertEqual(set(updated_ids), {10, 20, 30, 40})
        self.assertEqual({student: updated_ids[student] for student in original_ids}, original_ids)
        with pymupdf.open(stream=updated_pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 16)
            for index, image in enumerate(original_page_images):
                self.assertEqual(document[index].get_pixmap().tobytes("png"), image)
        self.assertEqual(self.printing_app.test_client().get("/exams/1/pdf").data, updated_pdf)
        self.assertEqual((self.root / "output" / "exam_1.pdf").read_bytes(), updated_pdf)

    '''
        Verify that enrollment succeeds while printing is offline without RPC
        calls or warnings, and the next explicit order expands the PDF with stable IDs.
    '''
    def test_enrollment_while_printing_is_offline_keeps_pdf_until_explicit_order(self):
        self.assertEqual(self.enroll([10, 20]).status_code, 302)
        self.client.post("/exams/1/order-printing")
        original_pdf, original_ids = self.snapshot()
        with patch.object(self.admin_module, "ServerProxy", side_effect=OSError("service offline")) as unavailable_proxy:
            response = self.enroll([30, 40])
            unavailable_proxy.assert_not_called()
        self.assertEqual(response.status_code, 302)
        confirmation = self.client.get(response.headers["Location"])
        self.assertIn(b"2 student(s) enrolled successfully.", confirmation.data)
        self.assertNotIn(b"The automatic PDF update", confirmation.data)
        self.assertNotIn(b'class="message notice"', confirmation.data)
        self.assertNotIn(b'class="message error"', confirmation.data)
        self.assertEqual(self.snapshot(), (original_pdf, original_ids))
        self.assertEqual(self.client.post("/exams/1/order-printing").status_code, 302)
        updated_pdf, updated_ids = self.snapshot()
        with pymupdf.open(stream=updated_pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 16)
        self.assertEqual({student: updated_ids[student] for student in original_ids}, original_ids)

    '''
        Verify that an externally created PDF survives an Admin restart and
        later enrollment unchanged until an explicit order adds the new student's pages.
    '''
    def test_pdf_created_outside_admin_requires_a_new_order_after_admin_restart(self):
        self.assertEqual(self.enroll([10, 20]).status_code, 302)
        self.proxy.print_exam("1", self.exam_date.isoformat(), "Algorithms", ["10", "20"])
        original_pdf, original_ids = self.snapshot()
        restarted = self.admin_module.create_app(database_url=self.admin_app.config["DATABASE_URL"])
        restarted.testing = True
        try:
            response = restarted.test_client().post("/exams/1/enroll", data={
                "file": (io.BytesIO(b"student_id\n30\n"), "students.csv"),
            })
            self.assertEqual(response.status_code, 302)
            self.assertEqual(self.snapshot(), (original_pdf, original_ids))
            self.assertEqual(restarted.test_client().post("/exams/1/order-printing").status_code, 302)
        finally:
            restarted.extensions["admin_engine"].dispose()
        updated_pdf, updated_ids = self.snapshot()
        with pymupdf.open(stream=updated_pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 12)
        self.assertEqual({student: updated_ids[student] for student in original_ids}, original_ids)

    '''
        Verify that Admin blocks late enrollment and printing without RPC calls,
        and direct XML-RPC also rejects late printing while preserving the PDF and IDs.
    '''
    def test_past_exam_cannot_be_printed_by_admin_or_direct_xmlrpc(self):
        self.enroll([10, 20])
        self.client.post("/exams/1/order-printing")
        original = self.snapshot()
        calls = len(self.proxy.calls)
        with patch.object(self.admin_module, "date", wraps=date) as admin_date:
            admin_date.today.return_value = self.exam_date + timedelta(days=1)
            response = self.client.post("/exams/1/order-printing")
            enrollment_response = self.enroll([30])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(enrollment_response.status_code, 409)
        self.assertIn(b"Students cannot be enrolled after the exam date.", enrollment_response.data)
        self.assertIn(b"cannot be printed after the exam date", response.data)
        self.assertEqual(len(self.proxy.calls), calls)
        with patch(f"{create_printing_app.__module__}.date", wraps=date) as printing_date:
            printing_date.today.return_value = self.exam_date + timedelta(days=1)
            with self.assertRaises(Fault):
                self.proxy.print_exam("1", self.exam_date.isoformat(), "Algorithms", ["10", "20", "30"])
        self.assertEqual(self.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
