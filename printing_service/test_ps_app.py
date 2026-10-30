import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch
from xmlrpc.client import Fault, ServerProxy

import pymupdf
import qrcode
from sqlalchemy import event, select

if __package__:
    from .ps_app import create_app, create_server, restore_committed_pdf
    from .printing_data import PrintedExam, ResolutionSheet
else:
    from ps_app import create_app, create_server, restore_committed_pdf
    from printing_data import PrintedExam, ResolutionSheet


class PrintingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.exam_date = date.today() + timedelta(days=7)
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

    '''
        Verify that XML-RPC printing saves four question pages per student in
        the database and output PDF, using a unique sheet ID per student and
        four QR images on each page with the same ID across that student's pages.
    '''
    def test_print_exam_creates_four_answer_pages_per_student(self):
        with patch("qrcode.make", wraps=qrcode.make) as qr_factory:
            result = self.proxy.print_exam(
                12, self.exam_date.isoformat(), "Algorithms", [10, 20]
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
            self.assertEqual(exam.exam_date, self.exam_date)
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

    '''
        Verify that the listing shows exam details and PDF actions, serves the
        stored PDF inline, and returns 404 for missing exam view and save requests.
    '''
    def test_web_page_lists_exams_and_serves_saved_pdf(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(self.exam_date.isoformat().encode(), response.data)
        self.assertIn(b"1", response.data)
        self.assertIn(b'href="/exams/12/pdf"', response.data)
        self.assertIn(b'action="/exams/12/pdf/save"', response.data)
        self.assertIn(b"Open PDF", response.data)
        self.assertIn(b"Save to output", response.data)

        response = self.client.get("/exams/12/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertEqual(response.headers["Content-Disposition"], "inline; filename=exam_12.pdf")
        self.assertEqual(self.client.get("/exams/999/pdf").status_code, 404)
        self.assertEqual(self.client.post("/exams/999/pdf/save").status_code, 404)

    '''
        Verify that view and save URLs reject IDs outside the positive signed
        64-bit range but serve and save the PDF for the largest valid ID.
    '''
    def test_pdf_urls_reject_out_of_range_ids_and_accept_the_largest_id(self):
        for identifier in (0, -1, 2**63, 10**100):
            for method, suffix in (("GET", ""), ("POST", "/save")):
                with self.subTest(identifier=identifier, method=method):
                    self.assertEqual(self.client.open(f"/exams/{identifier}/pdf{suffix}", method=method).status_code, 404)

        exam_id = 2**63 - 1
        self.proxy.print_exam(str(exam_id), self.exam_date.isoformat(), "Algorithms", [10])
        response = self.client.get(f"/exams/{exam_id}/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data.startswith(b"%PDF-"))
        self.assertEqual(self.client.post(f"/exams/{exam_id}/pdf/save").status_code, 302)
        self.assertEqual((self.output_dir / f"exam_{exam_id}.pdf").read_bytes(), response.data)

    '''
        Verify that POST save recreates missing output and replaces an outdated
        copy using stored PDF bytes without regenerating sheets or changing QR IDs.
        The action redirects to confirmation and rejects GET.
    '''
    def test_save_to_output_restores_the_latest_pdf_without_regenerating_sheets(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        original_pdf = self.client.get("/exams/12/pdf").data
        with self.app.extensions["printing_session_factory"]() as session:
            original_ids = [sheet.id for sheet in session.get(PrintedExam, 12).sheets]
        output_path = self.output_dir / "exam_12.pdf"
        output_path.unlink()
        self.output_dir.rmdir()

        module_name = create_app.__module__
        with patch(f"{module_name}.create_exam_pdf", side_effect=AssertionError("Must use stored PDF")):
            response = self.client.post("/exams/12/pdf/save", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/exams/pdf/saved")
        self.assertIn(b"PDF saved to the output folder.", response.data)
        self.assertNotIn("Content-Disposition", response.headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertEqual(output_path.read_bytes(), original_pdf)
        self.assertEqual(self.client.get("/exams/12/pdf").data, original_pdf)
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertEqual([sheet.id for sheet in session.get(PrintedExam, 12).sheets], original_ids)
        output_path.write_bytes(b"Outdated copy")
        self.assertEqual(self.client.post("/exams/12/pdf/save").status_code, 302)
        self.assertEqual(output_path.read_bytes(), original_pdf)
        self.assertEqual(self.client.get("/exams/12/pdf/save").status_code, 405)

    '''
        Verify that an output replacement failure returns 500, logs an error,
        preserves the stored and published PDF, and removes the temporary file.
    '''
    def test_save_to_output_failure_preserves_the_pdf_and_reports_an_error(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
        original_pdf = self.client.get("/exams/12/pdf").data
        with patch.object(Path, "replace", side_effect=OSError("Cannot replace output file")):
            with self.assertLogs(self.app.logger, level="ERROR"):
                response = self.client.post("/exams/12/pdf/save")
        self.assertEqual(response.status_code, 500)
        self.assertIn(b"Could not save the PDF to the output folder.", response.data)
        self.assertNotIn(b"PDF saved to the output folder.", response.data)
        self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), original_pdf)
        self.assertEqual(self.client.get("/exams/12/pdf").data, original_pdf)
        self.assertEqual(list(self.output_dir.glob("*.pdf")), [self.output_dir / "exam_12.pdf"])

    '''
        Verify that updating the course and exam date adds new students while
        retaining existing sheets and their IDs in the regenerated PDF.
    '''
    def test_printing_metadata_changes_preserve_existing_resolution_sheets(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        session_factory = self.app.extensions["printing_session_factory"]
        with session_factory() as session:
            old_ids = set(
                session.scalars(
                    select(ResolutionSheet.id).where(ResolutionSheet.exam_id == 12)
                ).all()
            )

        new_date = self.exam_date + timedelta(days=1)
        self.proxy.print_exam(12, new_date.isoformat(), "Databases", [30])

        with session_factory() as session:
            exam = session.get(PrintedExam, 12)
            sheets = session.scalars(
                select(ResolutionSheet).where(ResolutionSheet.exam_id == 12)
            ).all()
            self.assertEqual(exam.course, "Databases")
            self.assertEqual(exam.exam_date, new_date)
            self.assertEqual([sheet.student_id for sheet in sheets], [10, 20, 30])
            self.assertTrue(old_ids.issubset({sheet.id for sheet in sheets}))
            with pymupdf.open(stream=exam.pdf_file, filetype="pdf") as document:
                self.assertEqual(document.page_count, 12)

    '''
        Verify that an impossible calendar date is rejected with an XML-RPC fault.
    '''
    def test_invalid_exam_date_is_rejected(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam(12, "2026-02-30", "Algorithms", [10])

    '''
        Verify that repeating an order with reversed student order preserves
        every sheet ID and the exact database and output PDF bytes.
    '''
    def test_repeated_order_preserves_qr_identifiers_and_pdf_bytes(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        original_pdf = self.client.get("/exams/12/pdf").data
        factory = self.app.extensions["printing_session_factory"]
        with factory() as session:
            original_ids = {sheet.student_id: sheet.id for sheet in session.get(PrintedExam, 12).sheets}
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [20, 10])
        self.assertEqual(self.client.get("/exams/12/pdf").data, original_pdf)
        self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), original_pdf)
        with factory() as session:
            self.assertEqual({sheet.student_id: sheet.id for sheet in session.get(PrintedExam, 12).sheets}, original_ids)
            for identifier in original_ids.values():
                self.assertIsNotNone(session.get(ResolutionSheet, identifier))

    '''
        Verify that two additional students expand the PDF from eight to sixteen
        pages without changing existing IDs, and stale orders do not remove students.
    '''
    def test_pdf_grows_from_eight_to_sixteen_pages_and_stale_orders_keep_all_students(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        factory = self.app.extensions["printing_session_factory"]
        with factory() as session:
            original_ids = {sheet.student_id: sheet.id for sheet in session.get(PrintedExam, 12).sheets}
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20, 30, 40])
        expanded_pdf = self.client.get("/exams/12/pdf").data
        with pymupdf.open(stream=expanded_pdf, filetype="pdf") as document:
            self.assertEqual(document.page_count, 16)
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        self.assertEqual(self.client.get("/exams/12/pdf").data, expanded_pdf)
        with factory() as session:
            ids = {sheet.student_id: sheet.id for sheet in session.get(PrintedExam, 12).sheets}
            self.assertEqual(set(ids), {10, 20, 30, 40})
            self.assertEqual({student: ids[student] for student in original_ids}, original_ids)
            self.assertEqual(len(set(ids.values())), 4)

    '''
        Verify that past dates allow first printing and updates, while repeated
        orders preserve the PDF bytes and existing sheet identifiers.
    '''
    def test_printing_after_exam_date_preserves_reprints_and_allows_generation(self):
        module_name = create_app.__module__
        with patch(f"{module_name}.date", wraps=date) as server_date:
            server_date.today.return_value = self.exam_date
            self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
            original_pdf = self.client.get("/exams/12/pdf").data
            with self.app.extensions["printing_session_factory"]() as session:
                original_id = session.get(PrintedExam, 12).sheets[0].id
            server_date.today.return_value = self.exam_date + timedelta(days=1)
            self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
            self.assertEqual(self.client.get("/exams/12/pdf").data, original_pdf)
            self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), original_pdf)
            self.proxy.print_exam(13, self.exam_date.isoformat(), "Algorithms", [10, 20])
            self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        for exam_id in (12, 13):
            with pymupdf.open(stream=self.client.get(f"/exams/{exam_id}/pdf").data, filetype="pdf") as document:
                self.assertEqual(document.page_count, 8)
        with self.app.extensions["printing_session_factory"]() as session:
            sheets = {sheet.student_id: sheet.id for sheet in session.get(PrintedExam, 12).sheets}
            self.assertEqual(set(sheets), {10, 20})
            self.assertEqual(sheets[10], original_id)
            self.assertEqual(session.get(PrintedExam, 12).exam_date, self.exam_date)
            self.assertEqual(session.get(PrintedExam, 13).exam_date, self.exam_date)

    '''
        Verify that failing to publish an expanded PDF raises an XML-RPC fault,
        rolls back new sheets, preserves the old PDF and QR IDs, and cleans temporary files.
    '''
    def test_failed_pdf_copy_keeps_previous_pdf_and_qr_identifiers(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
        original_pdf = self.client.get("/exams/12/pdf").data
        with self.app.extensions["printing_session_factory"]() as session:
            identifier = session.get(PrintedExam, 12).sheets[0].id
        with patch.object(Path, "replace", side_effect=OSError("Cannot replace output file")):
            with self.assertLogs(self.server.app.logger, level="ERROR"):
                with self.assertRaises(Fault):
                    self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        self.assertEqual(self.client.get("/exams/12/pdf").data, original_pdf)
        self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), original_pdf)
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertEqual([(sheet.student_id, sheet.id) for sheet in session.get(PrintedExam, 12).sheets], [(10, identifier)])
        self.assertEqual(list(self.output_dir.glob("*.pdf")), [self.output_dir / "exam_12.pdf"])

    '''
        Verify that concurrent orders retain both newly added students and the
        original sheet ID, publishing the same twelve-page PDF stored in the database.
    '''
    def test_concurrent_orders_preserve_existing_students_and_publish_the_latest_pdf(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
        with self.app.extensions["printing_session_factory"]() as session:
            original_id = session.get(PrintedExam, 12).sheets[0].id
        def submit(students):
            with ServerProxy(f"http://127.0.0.1:{self.server.server_address[1]}") as proxy:
                return proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", students)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ([10, 20], [10, 30])))
        self.assertTrue(all("exam_12.pdf" in result for result in results))
        with self.app.extensions["printing_session_factory"]() as session:
            exam = session.get(PrintedExam, 12)
            ids = {sheet.student_id: sheet.id for sheet in exam.sheets}
            self.assertEqual(set(ids), {10, 20, 30})
            self.assertEqual(ids[10], original_id)
            contents = exam.pdf_file
        self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), contents)
        with pymupdf.open(stream=contents, filetype="pdf") as document:
            self.assertEqual(document.page_count, 12)

    '''
        Verify that a database commit failure restores the previous output PDF
        and sheet IDs, or removes the output when the first order is rolled back.
        A subsequent order must still succeed.
    '''
    def test_commit_failure_restores_output_to_the_committed_database(self):
        factory = self.server.app.extensions["printing_session_factory"]
        for exam_id, existing in ((12, True), (13, False)):
            with self.subTest(existing=existing):
                output_path = self.output_dir / f"exam_{exam_id}.pdf"
                if existing:
                    self.proxy.print_exam(exam_id, self.exam_date.isoformat(), "Algorithms", [10])
                    original_pdf = output_path.read_bytes()
                    with factory() as session:
                        original_ids = [(sheet.student_id, sheet.id) for sheet in session.get(PrintedExam, exam_id).sheets]

                def fail_commit(session):
                    raise RuntimeError("Simulated database commit failure")

                event.listen(factory.class_, "before_commit", fail_commit, once=True)
                try:
                    with self.assertLogs(self.server.app.logger, level="ERROR"):
                        with self.assertRaises(Fault):
                            self.proxy.print_exam(exam_id, self.exam_date.isoformat(), "Algorithms", [10, 20])
                finally:
                    event.remove(factory.class_, "before_commit", fail_commit)

                with factory() as session:
                    exam = session.get(PrintedExam, exam_id)
                    if existing:
                        self.assertEqual(exam.pdf_file, original_pdf)
                        self.assertEqual(output_path.read_bytes(), original_pdf)
                        self.assertEqual([(sheet.student_id, sheet.id) for sheet in exam.sheets], original_ids)
                    else:
                        self.assertIsNone(exam)
                        self.assertFalse(output_path.exists())
                self.proxy.print_exam(exam_id, self.exam_date.isoformat(), "Algorithms", [10, 20])
                with factory() as session:
                    self.assertEqual(output_path.read_bytes(), session.get(PrintedExam, exam_id).pdf_file)

    '''
        Verify that decimal XML-RPC strings support exam and student IDs beyond
        32 bits, including the signed 64-bit maximum, and produce an accessible PDF.
    '''
    def test_decimal_strings_support_64bit_exam_and_student_identifiers(self):
        exam_id = 2**63 - 1
        student_ids = [2**31, 2**63 - 1]
        self.proxy.print_exam(
            str(exam_id), self.exam_date.isoformat(), "Algorithms",
            [str(student_id) for student_id in student_ids],
        )
        with self.app.extensions["printing_session_factory"]() as session:
            exam = session.get(PrintedExam, exam_id)
            self.assertIsNotNone(exam)
            self.assertEqual([sheet.student_id for sheet in exam.sheets], student_ids)
            with pymupdf.open(stream=exam.pdf_file, filetype="pdf") as document:
                self.assertEqual(document.page_count, 8)
        self.assertEqual(self.client.get(f"/exams/{exam_id}/pdf").status_code, 200)

    '''
        Verify that recovery after a failed commit preserves a newer successful
        printing order instead of restoring an outdated PDF or removing new sheets.
    '''
    def test_commit_recovery_preserves_an_order_completed_before_recovery(self):
        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10])
        factory = self.server.app.extensions["printing_session_factory"]
        with factory() as session:
            original_id = session.get(PrintedExam, 12).sheets[0].id

        def fail_commit(session):
            raise RuntimeError("Simulated database commit failure")

        def recover_after_another_order(*args):
            with ServerProxy(f"http://127.0.0.1:{self.server.server_address[1]}") as proxy:
                proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 30])
            restore_committed_pdf(*args)

        event.listen(factory.class_, "before_commit", fail_commit, once=True)
        try:
            with patch(f"{create_app.__module__}.restore_committed_pdf", side_effect=recover_after_another_order):
                with self.assertLogs(self.server.app.logger, level="ERROR"):
                    with self.assertRaises(Fault):
                        self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 20])
        finally:
            event.remove(factory.class_, "before_commit", fail_commit)

        with factory() as session:
            exam = session.get(PrintedExam, 12)
            self.assertEqual({sheet.student_id for sheet in exam.sheets}, {10, 30})
            self.assertEqual(next(sheet.id for sheet in exam.sheets if sheet.student_id == 10), original_id)
            self.assertEqual((self.output_dir / "exam_12.pdf").read_bytes(), exam.pdf_file)

    '''
        Verify that invalid, nonpositive, oversized, boolean, and fractional exam
        or student IDs raise XML-RPC faults without creating any exams.
    '''
    def test_invalid_identifier_values_are_rejected(self):
        for identifier in ("0", "-1", "1.5", "abc", str(2**63), True, 1.5):
            with self.subTest(identifier=identifier, field="exam_id"):
                with self.assertRaises(Fault):
                    self.proxy.print_exam(identifier, self.exam_date.isoformat(), "Algorithms", [10])
            with self.subTest(identifier=identifier, field="student_id"):
                with self.assertRaises(Fault):
                    self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [identifier])
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(PrintedExam.id)))

    '''
        Verify that an integer ID and its zero-padded decimal string are treated
        as duplicates and rejected without creating the exam.
    '''
    def test_duplicate_ids_are_detected_after_normalizing_wire_types(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam("12", self.exam_date.isoformat(), "Algorithms", [10, "010"])
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.get(PrintedExam, 12))

    '''
        Verify that duplicate student IDs raise an XML-RPC fault without
        creating an exam record or an output PDF.
    '''
    def test_duplicate_student_ids_are_rejected_without_creating_exam(self):
        with self.assertRaises(Fault):
            self.proxy.print_exam(12, self.exam_date.isoformat(), "Algorithms", [10, 10])

        self.assertFalse((self.output_dir / "exam_12.pdf").exists())
        with self.app.extensions["printing_session_factory"]() as session:
            self.assertIsNone(session.get(PrintedExam, 12))

    '''
        Verify that the root URL still accepts XML-RPC printing requests.
    '''
    def test_xmlrpc_root_path_remains_supported(self):
        root_proxy = ServerProxy(
            f"http://127.0.0.1:{self.server.server_address[1]}/"
        )
        try:
            result = root_proxy.print_exam(13, self.exam_date.isoformat(), "Algorithms", [11])
        finally:
            root_proxy.__exit__(None, None, None)
        self.assertIn("exam_13.pdf", result)


if __name__ == "__main__":
    unittest.main()
