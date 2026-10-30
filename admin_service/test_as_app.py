import io
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, timedelta
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from unittest.mock import ANY, MagicMock, patch
from xmlrpc.client import Fault, ProtocolError, ServerProxy, dumps

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from admin_data import Enrollment, Exam
with patch.dict(os.environ, {"ADMIN_DATABASE_URL": "sqlite:///:memory:"}):
    from as_app import TimeoutSafeTransport, TimeoutTransport, create_app


class AdminServiceTests(unittest.TestCase):
    """Exercise Admin Service routes against an isolated temporary SQLite database."""

    def setUp(self):
        # Each test gets a fresh database and a test client; printing is mocked below.
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "test.sqlite"
        self.app = create_app(
            database_url=f"sqlite:///{database_path.as_posix()}",
            printing_rpc_url="http://printing.test:5003",
        )
        self.app.config.update(TESTING=True)
        self.default_proxy = MagicMock()
        rpc_patch = patch("as_app.ServerProxy", return_value=self.default_proxy)
        rpc_patch.start()
        self.addCleanup(rpc_patch.stop)
        self.client = self.app.test_client()
        self.exam_date = date.today() + timedelta(days=7)

    def tearDown(self):
        # Close SQLite handles before removing the temporary database directory.
        self.app.extensions["admin_engine"].dispose()
        self.temp_dir.cleanup()

    def schedule_exam(self):
        """Create a common exam fixture through the public scheduling route."""
        response = self.client.post(
            "/exams/schedule",
            data={"course": "Algorithms", "date": self.exam_date.isoformat()},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/exams/success")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"Exam scheduled successfully", response.data)
        return 1

    '''
        Verify that scheduling an exam makes its course and date appear in the exam list.
    '''
    def test_schedule_and_list_exam(self):
        self.schedule_exam()

        response = self.client.get("/exams")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(self.exam_date.isoformat().encode(), response.data)

    '''
        Verify that a malformed exam date displays a validation message.
    '''
    def test_rejects_invalid_exam_date(self):
        response = self.client.post(
            "/exams/schedule",
            data={"course": "Algorithms", "date": "not-a-date"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter a valid exam date", response.data)

    '''
        Verify that a valid CSV enrolls both students and shows their exam and
        enrollment count on the confirmation page.
    '''
    def test_bulk_enrollment_is_listed(self):
        exam_id = self.schedule_exam()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 student(s) enrolled successfully", response.data)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertEqual(response.request.args.get("count"), "2")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(b">10<", response.data)
        self.assertIn(b">20<", response.data)

    '''
        Verify that scheduling in the past preserves the submitted form,
        displays an error, and leaves the exam database empty.
    '''
    def test_past_exam_date_is_rejected_without_saving(self):
        today = date(2030, 1, 15)
        past_date = (today - timedelta(days=1)).isoformat()
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = today
            response = self.client.post(
                "/exams/schedule",
                data={"course": "Algorithms", "date": past_date},
            )

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertIn(b"Exam date cannot be in the past.", response.data)
        self.assertIn(b'value="Algorithms"', response.data)
        self.assertIn(f'value="{past_date}"'.encode(), response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(Exam.id)))

    '''
        Verify that today's date is accepted and persisted before redirecting to confirmation.
    '''
    def test_exam_can_be_scheduled_for_today(self):
        today = date(2030, 1, 15)
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = today
            response = self.client.post(
                "/exams/schedule",
                data={"course": "Algorithms", "date": today.isoformat()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/exams/success")
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.get(Exam, 1).date, today)

    '''
        Verify that repeated student IDs, including equivalent numeric forms,
        are imported once and the confirmation counts only unique students.
    '''
    def test_duplicate_csv_ids_are_imported_once(self):
        exam_id = self.schedule_exam()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n010\n20\n20\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertEqual(response.request.args.get("count"), "2")
        self.assertIn(b"2 student(s) enrolled successfully.", response.data)
        self.default_proxy.assert_not_called()
        session_factory = self.app.extensions["admin_session_factory"]
        with session_factory() as session:
            student_ids = session.scalars(
                select(Enrollment.student_id)
                .where(Enrollment.exam_id == exam_id)
                .order_by(Enrollment.student_id)
            ).all()
        self.assertEqual(student_ids, [10, 20])

    '''
        Verify that rows with missing or extra fields report the offending line
        and reject the entire enrollment upload, including earlier valid rows.
    '''
    def test_invalid_csv_field_counts_reject_entire_upload(self):
        exam_id = self.schedule_exam()
        cases = (
            (b"student_id\n,1001\n", 2),
            (b"student_id\n1001\n,1002\n", 3),
            (b"student_id\n1001,unexpected\n", 2),
            (b"student_id,note\n1001,Valid\n1002\n", 3),
        )
        for contents, line_number in cases:
            with self.subTest(contents=contents):
                response = self.client.post(
                    f"/exams/{exam_id}/enroll",
                    data={"file": (io.BytesIO(contents), "students.csv")},
                    content_type="multipart/form-data",
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertIn(
                    f"Invalid number of CSV fields on line {line_number}.".encode(),
                    response.data,
                )
                self.assertIn(b'enctype="multipart/form-data"', response.data)
                with self.app.extensions["admin_session_factory"]() as session:
                    self.assertIsNone(session.scalar(select(Enrollment.student_id)))

    '''
        Verify that an unclosed quoted CSV field reports a format error and saves no students.
    '''
    def test_unclosed_csv_quotes_are_reported_without_saving(self):
        exam_id = self.schedule_exam()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (
                io.BytesIO(b'student_id\n1001\n"1002\n'), "students.csv"
            )},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid CSV format.", response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(Enrollment.student_id)))

    '''
        Verify that enrollment accepts the student_identifier alias, blank lines,
        extra columns, and quoted commas while saving the correct student IDs.
    '''
    def test_valid_csv_extra_columns_and_quoted_commas_are_supported(self):
        exam_id = self.schedule_exam()
        contents = b'student_identifier,note\n\n1001,"Surname, Name"\n1002,Other\n'
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(contents), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 student(s) enrolled successfully", response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(
                session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(),
                [1001, 1002],
            )

    '''
        Verify that existing enrollments and repeated CSV IDs are skipped while
        new students are imported and only new enrollments are counted.
    '''
    def test_existing_enrollments_are_skipped_while_new_students_are_imported(self):
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n20\n10\n20\n010\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertEqual(response.request.args.get("count"), "1")
        self.assertIn(b"1 student(s) enrolled successfully.", response.data)
        self.default_proxy.assert_not_called()
        session_factory = self.app.extensions["admin_session_factory"]
        with session_factory() as session:
            student_ids = session.scalars(
                select(Enrollment.student_id)
                .where(Enrollment.exam_id == exam_id)
                .order_by(Enrollment.student_id)
            ).all()
        self.assertEqual(student_ids, [10, 20])

    '''
        Verify that uploading only existing students is a successful no-op with
        a repeatable zero-enrollment message and no printing calls.
    '''
    def test_upload_with_only_existing_students_reports_no_new_enrollments(self):
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv")},
            content_type="multipart/form-data",
        )

        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n010\n20\n20\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertEqual(response.request.args.get("count"), "0")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"No new students were enrolled.", response.data)
        self.assertNotIn(b'class="message error"', response.data)
        refreshed = self.client.get(response.request.full_path)
        self.assertIn(b"No new students were enrolled.", refreshed.data)
        self.default_proxy.assert_not_called()
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(
                session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(),
                [10, 20],
            )

    '''
        Verify that a student already enrolled in another exam can still be
        imported into the selected exam without duplicating their CSV entries.
    '''
    def test_existing_enrollments_in_another_exam_do_not_skip_students(self):
        first_exam_id = self.schedule_exam()
        with self.app.extensions["admin_session_factory"].begin() as session:
            session.add(Enrollment(exam_id=first_exam_id, student_id=10))
        response = self.client.post(
            "/exams/schedule",
            data={"course": "Databases", "date": self.exam_date.isoformat()},
        )
        self.assertEqual(response.status_code, 302)

        response = self.client.post(
            "/exams/2/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n10\n20\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.args.get("count"), "2")
        self.assertIn(b"2 student(s) enrolled successfully.", response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            enrollments = session.execute(
                select(Enrollment.exam_id, Enrollment.student_id)
                .order_by(Enrollment.exam_id, Enrollment.student_id)
            ).all()
        self.assertEqual(enrollments, [(first_exam_id, 10), (2, 10), (2, 20)])

    '''
        Verify that printing sends exam details and decimal student IDs to the
        configured XML-RPC service and shows a refreshable confirmation.
    '''
    def test_printing_order_calls_xmlrpc_contract(self):
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv")},
            content_type="multipart/form-data",
        )
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = "Queued"
        with patch("as_app.ServerProxy", return_value=proxy) as server_proxy:
            response = self.client.post(
                f"/exams/{exam_id}/order-printing",
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Printing order sent successfully.", response.data)
        self.assertEqual(response.request.path, "/exams/printing/success")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        refreshed = self.client.get(response.request.path)
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn(b"Printing order sent successfully.", refreshed.data)
        server_proxy.assert_called_once_with("http://printing.test:5003", transport=ANY)
        proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20"]
        )

    '''
        Verify with the real XML-RPC encoder that large exam and student IDs
        are sent as decimal strings, including the largest signed 64-bit value.
    '''
    def test_printing_order_supports_64bit_identifiers(self):
        exam_id = 2**63 - 1
        student_ids = [2**31, 2**63 - 1]
        session_factory = self.app.extensions["admin_session_factory"]
        with session_factory.begin() as session:
            session.add(Exam(
                id=exam_id, date=self.exam_date, course="Algorithms"
            ))
            session.flush()
            session.add_all(
                Enrollment(exam_id=exam_id, student_id=student_id)
                for student_id in student_ids
            )

        def serialize_request(*args):
            dumps(args, methodname="print_exam")
            return "Queued"

        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.side_effect = serialize_request
        with patch("as_app.ServerProxy", return_value=proxy):
            response = self.client.post(
                f"/exams/{exam_id}/order-printing", follow_redirects=True
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Printing order sent successfully.", response.data)
        proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms",
            [str(student_id) for student_id in student_ids],
        )

    '''
        Verify that both XML-RPC transports create the appropriate HTTP or HTTPS
        connection with the configured timeout.
    '''
    def test_printing_transport_supports_http_and_https_with_a_timeout(self):
        for transport_type, connection_type in (
            (TimeoutTransport, HTTPConnection),
            (TimeoutSafeTransport, HTTPSConnection),
        ):
            with self.subTest(transport=transport_type.__name__):
                transport = transport_type(0.1)
                connection = transport.make_connection("printing.test")
                try:
                    self.assertIsInstance(connection, connection_type)
                    self.assertEqual(connection.timeout, 0.1)
                finally:
                    transport.close()

    '''
        Verify against an unresponsive HTTP server that enrollment makes no
        printing call, while an explicit order times out promptly and reports an error.
    '''
    def test_enrollment_ignores_printing_and_explicit_printing_order_times_out(self):
        release = threading.Event()

        class UnresponsivePrinting(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                release.wait(5)
                self.close_connection = True

        server = ThreadingHTTPServer(("127.0.0.1", 0), UnresponsivePrinting)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        self.app.config.update(
            PRINTING_RPC_URL=f"http://127.0.0.1:{server.server_port}",
            PRINTING_RPC_TIMEOUT=0.1,
        )
        exam_id = self.schedule_exam()
        try:
            with patch("as_app.ServerProxy", wraps=ServerProxy) as server_proxy:
                started = monotonic()
                response = self.client.post(f"/exams/{exam_id}/enroll", data={
                    "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
                })
                self.assertLess(monotonic() - started, 2)
                self.assertEqual(response.status_code, 302)
                server_proxy.assert_not_called()
                confirmation = self.client.get(response.headers["Location"])
                self.assertIn(b"1 student(s) enrolled successfully.", confirmation.data)
                self.assertNotIn(b'class="message error"', confirmation.data)
                with self.app.extensions["admin_session_factory"]() as session:
                    self.assertEqual(session.scalars(select(Enrollment.student_id)).all(), [10])

                started = monotonic()
                response = self.client.post(f"/exams/{exam_id}/order-printing")
                self.assertLess(monotonic() - started, 2)
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Could not contact the Printing Service.", response.data)
                self.assertNotIn("Location", response.headers)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            server_thread.join()

    '''
        Verify that a student ID above SQLite's signed 64-bit limit rejects
        the entire upload, including an earlier valid student.
    '''
    def test_enrollment_rejects_ids_outside_sqlite_range_atomically(self):
        exam_id = self.schedule_exam()
        contents = f"student_id\n10\n{2**63}\n".encode()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(contents), "students.csv")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"positive 64-bit integers", response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(Enrollment.student_id)))

    '''
        Verify that a printing network failure displays an error without
        redirecting to success.
    '''
    def test_printing_failure_is_reported(self):
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.side_effect = OSError(
            "Printing Service unavailable"
        )
        with patch("as_app.ServerProxy", return_value=proxy):
            response = self.client.post(
                f"/exams/{exam_id}/order-printing",
                follow_redirects=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Could not contact the Printing Service", response.data)
        self.assertEqual(response.request.path, f"/exams/{exam_id}/order-printing")
        self.assertEqual(response.history, ())
        self.assertNotIn("Set-Cookie", response.headers)

    '''
        Verify that printing an exam with no enrollments reports an error
        without contacting the Printing Service.
    '''
    def test_printing_without_enrollments_is_not_sent(self):
        exam_id = self.schedule_exam()
        with patch("as_app.ServerProxy") as server_proxy:
            response = self.client.post(
                f"/exams/{exam_id}/order-printing",
                follow_redirects=True,
            )

        self.assertIn(b"Enroll students before ordering", response.data)
        server_proxy.assert_not_called()

    '''
        Verify that a False XML-RPC result displays rejection rather than a success message.
    '''
    def test_printing_rejection_is_not_reported_as_success(self):
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = False
        with patch("as_app.ServerProxy", return_value=proxy):
            response = self.client.post(
                f"/exams/{exam_id}/order-printing",
                follow_redirects=True,
            )

        self.assertIn(b"Printing Service rejected the printing order", response.data)
        self.assertNotIn(b"Printing order sent successfully", response.data)

    '''
        Verify that the main pages load or initial success and error messages.
    '''
    def test_pages_do_not_require_a_session_or_display_initial_messages(self):
        self.assertIsNone(self.app.secret_key)
        for path in ("/", "/exams", "/exams/schedule", "/enrollments"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertNotIn(b'class="message error"', response.data)
                self.assertNotIn(b'class="message success"', response.data)

    '''
        Verify that missing or oversized course names preserve the form and
        display specific errors without saving an exam.
    '''
    def test_schedule_errors_preserve_the_form_without_a_session(self):
        for course, message in (("", b"Course is required."), ("A" * 201, b"Course must be at most 200 characters.")):
            with self.subTest(course=course):
                response = self.client.post("/exams/schedule", data={
                    "course": course, "date": self.exam_date.isoformat(),
                })
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertIn(f'value="{course}"'.encode(), response.data)
                self.assertIn(f'value="{self.exam_date.isoformat()}"'.encode(), response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(Exam.id)))
        self.assertNotIn(b'class="message error"', self.client.get("/exams/schedule").data)

    '''
        Verify that invalid uploads, encoding, headers, and student values
        display specific errors without saving enrollments.
    '''
    def test_enrollment_validation_errors_use_specific_messages_without_saving(self):
        exam_id = self.schedule_exam()
        cases = (
            (None, None, b"Choose a CSV file to upload."),
            (b"student_id\n10\n", "students.txt", b"Only CSV files are accepted."),
            (b"\xff", "students.csv", b"The CSV file must use UTF-8 encoding."),
            (b"wrong_header\n10\n", "students.csv", b"CSV must contain a student_id column"),
            (b"student_id,student_id\n10,20\n", "students.csv", b"CSV headers must be unique."),
            (b"Student_ID, student_id \n10,20\n", "students.csv", b"CSV headers must be unique."),
            (b"student_id,student_identifier\n10,20\n", "students.csv", b"Use only one identifier column"),
            (b"student_id,student_identifier\n10,10\n", "students.csv", b"Use only one identifier column"),
            (b"student_id,note\n10,Valid\n,Missing\n", "students.csv", b"Missing student identifier on CSV line 3."),
            (b"student_id\n10\ninvalid\n", "students.csv", b"Invalid student identifier on CSV line 3."),
            (b"student_id\n10\n10\ninvalid\n", "students.csv", b"Invalid student identifier on CSV line 4."),
            (b"student_id\n10\n0\n", "students.csv", b"Student identifiers must be positive 64-bit integers (CSV line 3)."),
            (b"student_id\n", "students.csv", b"The CSV file contains no student identifiers."),
        )
        for contents, filename, message in cases:
            with self.subTest(contents=contents, filename=filename):
                data = {} if contents is None else {"file": (io.BytesIO(contents), filename)}
                response = self.client.post(f"/exams/{exam_id}/enroll", data=data,
                                            content_type="multipart/form-data")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertIn(b'class="message error" role="alert"', response.data)
                self.assertIn(b"Algorithms", response.data)
                with self.app.extensions["admin_session_factory"]() as session:
                    self.assertIsNone(session.scalar(select(Enrollment.student_id)))
        self.assertNotIn(b'class="message error"', self.client.get(f"/exams/{exam_id}/enroll").data)

    '''
        Verify that an enrollment integrity conflict displays a predefined
        error without exposing database details or redirecting.
    '''
    def test_enrollment_database_conflict_is_reported_without_a_session(self):
        exam_id = self.schedule_exam()
        conflict = IntegrityError("INSERT", {}, RuntimeError("concurrent enrollment"))
        session_factory = self.app.extensions["admin_session_factory"]
        with patch.object(session_factory, "begin", side_effect=conflict):
            response = self.client.post(f"/exams/{exam_id}/enroll", data={
                "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
            }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertNotIn("Location", response.headers)
        self.assertIn(b"One or more students are already enrolled in this exam.", response.data)
        self.assertNotIn(b"concurrent enrollment", response.data)

    '''
        Verify that confirmation URLs remain repeatable without printing calls
        or database changes, and invalid counts use a generic message.
        Normal listings must not display success or error messages.
    '''
    def test_success_routes_are_repeatable_and_normal_lists_have_no_messages(self):
        cases = (
            ("/exams/success", b"Exam scheduled successfully."),
            ("/enrollments/success?count=2", b"2 student(s) enrolled successfully."),
            ("/enrollments/success?count=0", b"No new students were enrolled."),
            ("/exams/printing/success", b"Printing order sent successfully."),
        )
        with patch("as_app.ServerProxy") as server_proxy:
            for path, message in cases:
                with self.subTest(path=path):
                    for _ in range(2):
                        response = self.client.get(path)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(message, response.data)
                        self.assertNotIn("Set-Cookie", response.headers)
            server_proxy.assert_not_called()
        for count in (None, "", "invalid", "-1"):
            response = self.client.get("/enrollments/success", query_string={"count": count})
            self.assertIn(b"Students enrolled successfully.", response.data)
            self.assertNotIn(b"student(s)", response.data)
        for path in ("/exams", "/enrollments"):
            response = self.client.get(path)
            self.assertNotIn(b'class="message error"', response.data)
            self.assertNotIn(b'class="message success"', response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertIsNone(session.scalar(select(Exam.id)))
            self.assertIsNone(session.scalar(select(Enrollment.student_id)))

    '''
        Verify that enrollment and printing routes return 404 for missing or
        out-of-range exam IDs without contacting printing.
    '''
    def test_missing_and_out_of_range_exam_ids_return_not_found(self):
        with patch("as_app.ServerProxy") as server_proxy:
            for exam_id in (0, 999, 2**63, 10**30):
                for method, suffix in (("GET", "enroll"), ("POST", "enroll"), ("POST", "order-printing")):
                    with self.subTest(exam_id=exam_id, method=method, suffix=suffix):
                        response = self.client.open(f"/exams/{exam_id}/{suffix}", method=method)
                        self.assertEqual(response.status_code, 404)
                        self.assertNotIn("Set-Cookie", response.headers)
            server_proxy.assert_not_called()

    '''
        Verify that SQLite connections enforce foreign keys, reject enrollments
        for missing exams, and delete enrollments when their exam is deleted.
    '''
    def test_foreign_keys_reject_orphaned_enrollments_and_cascade_exam_deletion(self):
        engine = self.app.extensions["admin_engine"]
        with engine.connect() as first, engine.connect() as second:
            for connection in (first, second):
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)
        session_factory = self.app.extensions["admin_session_factory"]
        with self.assertRaises(IntegrityError):
            with session_factory.begin() as session:
                session.add(Enrollment(exam_id=999, student_id=10))
        exam_id = self.schedule_exam()
        self.assertEqual(self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        }).status_code, 302)
        with engine.begin() as connection:
            connection.execute(Exam.__table__.delete().where(Exam.id == exam_id))
        with session_factory() as session:
            self.assertIsNone(session.scalar(select(Enrollment.student_id)))

    '''
        Verify that XML-RPC faults and HTTP protocol errors display predefined
        messages without internal details, while a None result is accepted as success.
    '''
    def test_printing_rpc_errors_use_messages_and_default_success_is_supported(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        cases = (
            (Fault(1, "internal details"), b"The Printing Service could not complete the printing order."),
            (ProtocolError("http://printing.test", 503, "internal details", {}), b"Could not contact the Printing Service."),
        )
        for error, message in cases:
            with self.subTest(error=type(error).__name__):
                proxy = MagicMock()
                proxy.__enter__.return_value.print_exam.side_effect = error
                with patch("as_app.ServerProxy", return_value=proxy):
                    response = self.client.post(f"/exams/{exam_id}/order-printing")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertNotIn(b"internal details", response.data)
                self.assertIn(f'action="/exams/{exam_id}/order-printing"'.encode(), response.data)
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = None
        with patch("as_app.ServerProxy", return_value=proxy):
            response = self.client.post(f"/exams/{exam_id}/order-printing")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/exams/printing/success")
        self.assertNotIn("Set-Cookie", response.headers)

    '''
        Verify that malformed XML, non-RPC replies, and invalid XML-RPC values
        display the printing error instead of causing an internal server error.
    '''
    def test_invalid_xmlrpc_replies_are_reported_without_server_errors(self):
        class InvalidPrintingResponse(BaseHTTPRequestHandler):
            payload = b""

            def log_message(self, *args):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(self.payload)))
                self.end_headers()
                self.wfile.write(self.payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), InvalidPrintingResponse)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.app.config["PRINTING_RPC_URL"] = f"http://127.0.0.1:{server.server_port}"
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        try:
            for payload in (
                b"not XML",
                b"<html><body>Unexpected reply</body></html>",
                b"<methodResponse><params><param><value><boolean>invalid</boolean>"
                b"</value></param></params></methodResponse>",
                b"<methodResponse><params><param><value><int>invalid</int>"
                b"</value></param></params></methodResponse>",
            ):
                with self.subTest(payload=payload):
                    InvalidPrintingResponse.payload = payload
                    with patch("as_app.ServerProxy", wraps=ServerProxy):
                        response = self.client.post(f"/exams/{exam_id}/order-printing")
                    self.assertEqual(response.status_code, 200)
                    self.assertNotIn("Location", response.headers)
                    self.assertIn(b"The Printing Service could not complete the printing order.", response.data)
                    self.assertNotIn(b"Printing order sent successfully.", response.data)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    '''
        Verify that late printing calls XML-RPC and remains available in the
        exam listing while the enrollment action is closed.
    '''
    def test_printing_after_exam_date_is_allowed_while_enrollment_is_closed(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = self.exam_date + timedelta(days=1)
            with patch("as_app.ServerProxy", return_value=self.default_proxy):
                response = self.client.post(f"/exams/{exam_id}/order-printing")
            listing = self.client.get("/exams")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/exams/printing/success")
        self.default_proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10"]
        )
        self.assertIn(b"Enrollment closed", listing.data)
        self.assertIn(f'action="/exams/{exam_id}/order-printing"'.encode(), listing.data)
        self.assertNotIn(f'href="/exams/{exam_id}/enroll"'.encode(), listing.data)

    '''
        Verify that printing and additional enrollment remain allowed on the exam date.
    '''
    def test_printing_on_exam_date_is_allowed(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = True
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = self.exam_date
            with patch("as_app.ServerProxy", return_value=proxy):
                response = self.client.post(f"/exams/{exam_id}/order-printing")
                enrollment_response = self.client.post(f"/exams/{exam_id}/enroll", data={
                    "file": (io.BytesIO(b"student_id\n20\n"), "students.csv"),
                })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(enrollment_response.status_code, 302)
        proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10"]
        )

    '''
        Verify that late enrollment GET and POST requests return 409, hide the
        upload form, preserve enrolled students, and make no printing calls.
    '''
    def test_late_enrollment_is_blocked_without_changing_students_or_pdf_status(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = True
        with patch("as_app.ServerProxy", return_value=proxy):
            self.client.post(f"/exams/{exam_id}/order-printing")
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = self.exam_date + timedelta(days=1)
            with patch("as_app.ServerProxy") as server_proxy:
                for method in ("GET", "POST"):
                    response = self.client.open(f"/exams/{exam_id}/enroll", method=method, data={
                        "file": (io.BytesIO(b"student_id\n20\n"), "students.csv"),
                    })
                    self.assertEqual(response.status_code, 409)
                    self.assertIn(b"Students cannot be enrolled after the exam date.", response.data)
                    self.assertNotIn(b'name="file"', response.data)
                server_proxy.assert_not_called()
            listing = self.client.get("/exams")
        self.assertIn(b"Enrollment closed", listing.data)
        self.assertIn(f'action="/exams/{exam_id}/order-printing"'.encode(), listing.data)
        self.assertNotIn(f'href="/exams/{exam_id}/enroll"'.encode(), listing.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(), [10])

    '''
        Verify that adding enrollments never triggers printing automatically
        and that the next explicit order includes all currently enrolled students.
    '''
    def test_new_enrollments_require_an_explicit_printing_order_with_all_students(self):
        exam_id = self.schedule_exam()
        proxy = MagicMock()
        proxy.__enter__.return_value.print_exam.return_value = True
        with patch("as_app.ServerProxy", return_value=proxy) as server_proxy:
            self.client.post(f"/exams/{exam_id}/enroll", data={
                "file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv"),
            })
            server_proxy.assert_not_called()
            self.assertEqual(self.client.post(f"/exams/{exam_id}/order-printing").status_code, 302)
            response = self.client.post(f"/exams/{exam_id}/enroll", data={
                "file": (io.BytesIO(b"student_id\n30\n40\n"), "students.csv"),
            })
            self.assertEqual(server_proxy.call_count, 1)
            self.assertEqual(proxy.__enter__.return_value.print_exam.call_count, 1)
            self.assertEqual(self.client.post(f"/exams/{exam_id}/order-printing").status_code, 302)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/enrollments/success?count=2")
        self.assertEqual(proxy.__enter__.return_value.print_exam.call_count, 2)
        proxy.__enter__.return_value.print_exam.assert_called_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20", "30", "40"]
        )

    '''
        Verify that enrollment succeeds while printing is offline and remains
        independent after an Admin restart, with explicit orders using persisted students.
    '''
    def test_enrollment_without_printing_and_explicit_orders_work_after_restart(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv"),
        })
        proxy = MagicMock()
        remote = proxy.__enter__.return_value
        remote.print_exam.return_value = True
        with patch("as_app.ServerProxy", return_value=proxy):
            self.client.post(f"/exams/{exam_id}/order-printing")
        with patch("as_app.ServerProxy", side_effect=OSError("service offline")) as unavailable_proxy:
            response = self.client.post(f"/exams/{exam_id}/enroll", data={
                "file": (io.BytesIO(b"student_id\n30\n40\n"), "students.csv"),
            }, follow_redirects=True)
            unavailable_proxy.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertIn(b"2 student(s) enrolled successfully.", response.data)
        self.assertNotIn(b"The automatic PDF update could not be confirmed.", response.data)
        self.assertNotIn(b'class="message notice"', response.data)
        self.assertNotIn(b'class="message error"', response.data)
        self.assertNotIn(b'name="file"', response.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(), [10, 20, 30, 40])
        restarted = create_app(database_url=self.app.config["DATABASE_URL"], printing_rpc_url="http://printing.test:5003")
        restarted.testing = True
        try:
            with patch("as_app.ServerProxy", return_value=proxy):
                response = restarted.test_client().post(f"/exams/{exam_id}/order-printing")
            self.assertEqual(response.status_code, 302)
            remote.print_exam.assert_called_with(str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20", "30", "40"])
            with patch("as_app.ServerProxy", return_value=proxy) as server_proxy:
                response = restarted.test_client().post(f"/exams/{exam_id}/enroll", data={
                    "file": (io.BytesIO(b"student_id\n50\n"), "students.csv"),
                })
                server_proxy.assert_not_called()
            self.assertEqual(response.status_code, 302)
            remote.print_exam.assert_called_with(str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20", "30", "40"])
            with patch("as_app.ServerProxy", return_value=proxy):
                self.assertEqual(restarted.test_client().post(f"/exams/{exam_id}/order-printing").status_code, 302)
            remote.print_exam.assert_called_with(str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20", "30", "40", "50"])
        finally:
            restarted.extensions["admin_engine"].dispose()

    '''
        Verify that the first enrollment succeeds without the Printing Service,
        persists students, and shows a refreshable confirmation without warnings.
    '''
    def test_first_enrollment_succeeds_when_only_admin_is_running(self):
        exam_id = self.schedule_exam()
        with patch("as_app.ServerProxy", side_effect=OSError("service offline")) as server_proxy:
            response = self.client.post(f"/exams/{exam_id}/enroll", data={
                "file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv"),
            }, follow_redirects=True)
            server_proxy.assert_not_called()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.history), 1)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/enrollments/success")
        self.assertEqual(response.request.args.get("count"), "2")
        self.assertNotIn("printing", response.request.args)
        self.assertIn(b"2 student(s) enrolled successfully.", response.data)
        self.assertNotIn(b"The automatic PDF update", response.data)
        self.assertNotIn(b'class="message notice"', response.data)
        self.assertNotIn(b'class="message error"', response.data)
        self.assertNotIn(b'name="file"', response.data)
        self.assertNotIn(b"service offline", response.data)
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        for _ in range(2):
            refreshed = self.client.get(response.request.full_path)
            self.assertIn(b"2 student(s) enrolled successfully.", refreshed.data)
            self.assertNotIn(b'class="message error"', refreshed.data)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(), [10, 20])

    '''
        Verify that obsolete printing query parameters cannot add warnings,
        errors, or injected script markup to enrollment confirmations.
    '''
    def test_enrollment_confirmation_ignores_old_printing_notice_parameters(self):
        for status in ("unavailable", "", "other", "<script>invalid</script>"):
            response = self.client.get("/enrollments/success", query_string={"count": "2", "printing": status})
            self.assertIn(b'class="message success"', response.data)
            self.assertNotIn(b'class="message notice"', response.data)
            self.assertNotIn(b'class="message error"', response.data)
            self.assertNotIn(b"The automatic PDF update", response.data)
            self.assertNotIn(b"<script>", response.data)
        self.assertNotIn(b'class="message notice"', self.client.get("/enrollments").data)

    '''
        Verify that an enrollment added during printing triggers a second RPC
        request containing the updated student list before success is reported.
    '''
    def test_first_printing_order_retries_if_students_are_added_during_the_request(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        proxy = MagicMock()
        def printing_response(exam_id_arg, exam_date, course, student_ids):
            if student_ids == ["10"]:
                response = self.app.test_client().post(f"/exams/{exam_id}/enroll", data={
                    "file": (io.BytesIO(b"student_id\n20\n"), "students.csv"),
                })
                self.assertEqual(response.status_code, 302)
            return True
        proxy.__enter__.return_value.print_exam.side_effect = printing_response
        with patch("as_app.ServerProxy", return_value=proxy):
            self.assertEqual(self.client.post(f"/exams/{exam_id}/order-printing").status_code, 302)
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.scalars(select(Enrollment.student_id).order_by(Enrollment.student_id)).all(), [10, 20])
        self.assertEqual(proxy.__enter__.return_value.print_exam.call_count, 2)
        proxy.__enter__.return_value.print_exam.assert_called_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20"]
        )

    '''
        Verify that continuously changing enrollments stop printing retries
        after three calls and display a message asking for another explicit order.
    '''
    def test_printing_reports_continuous_enrollment_changes(self):
        exam_id = self.schedule_exam()
        self.client.post(f"/exams/{exam_id}/enroll", data={
            "file": (io.BytesIO(b"student_id\n10\n"), "students.csv"),
        })
        additions = iter((20, 30, 40))
        proxy = MagicMock()
        def printing_response(*args):
            with self.app.extensions["admin_session_factory"].begin() as session:
                session.add(Enrollment(exam_id=exam_id, student_id=next(additions)))
            return True
        proxy.__enter__.return_value.print_exam.side_effect = printing_response
        with patch("as_app.ServerProxy", return_value=proxy):
            response = self.client.post(f"/exams/{exam_id}/order-printing")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enrollments changed during the PDF update.", response.data)
        self.assertEqual(proxy.__enter__.return_value.print_exam.call_count, 3)

    '''
        Verify that opening a legacy database preserves existing exam data
        and retains exactly the required exam and enrollment columns.
    '''
    def test_existing_sqlite_database_keeps_the_required_exam_columns(self):
        database_path = Path(self.temp_dir.name) / "legacy.sqlite"
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("CREATE TABLE exams (id INTEGER PRIMARY KEY, date DATE NOT NULL, course VARCHAR(200) NOT NULL)")
            connection.execute("INSERT INTO exams (id, date, course) VALUES (1, ?, 'Existing exam')", (self.exam_date.isoformat(),))
            connection.commit()
        legacy_app = create_app(database_url=f"sqlite:///{database_path.as_posix()}")
        try:
            with legacy_app.extensions["admin_session_factory"]() as session:
                exam = session.get(Exam, 1)
                self.assertEqual(exam.course, "Existing exam")
            with legacy_app.extensions["admin_engine"].connect() as connection:
                self.assertEqual({row[1] for row in connection.exec_driver_sql("PRAGMA table_info(exams)")}, {"id", "date", "course"})
                self.assertEqual({row[1] for row in connection.exec_driver_sql("PRAGMA table_info(enrolled_students)")}, {"exam_id", "student_id"})
        finally:
            legacy_app.extensions["admin_engine"].dispose()


if __name__ == "__main__":
    unittest.main()
