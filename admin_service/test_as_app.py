import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch
from xmlrpc.client import dumps

from sqlalchemy import select

from admin_data import Enrollment, Exam
from as_app import create_app


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
        self.assertIn(b"Exam scheduled successfully", response.data)
        return 1

    def test_schedule_and_list_exam(self):
        """A scheduled exam is persisted and appears in the exam list."""
        self.schedule_exam()

        response = self.client.get("/exams")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(self.exam_date.isoformat().encode(), response.data)

    def test_rejects_invalid_exam_date(self):
        """Invalid dates are reported instead of being stored."""
        response = self.client.post(
            "/exams/schedule",
            data={"course": "Algorithms", "date": "not-a-date"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter a valid exam date", response.data)

    def test_bulk_enrollment_is_listed(self):
        """Valid CSV rows become visible in the enrollment listing."""
        exam_id = self.schedule_exam()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n20\n"), "students.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 student(s) enrolled successfully", response.data)
        self.assertIn(b"Algorithms", response.data)
        self.assertIn(b">10<", response.data)
        self.assertIn(b">20<", response.data)

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

    def test_exam_can_be_scheduled_for_today(self):
        today = date(2030, 1, 15)
        with patch("as_app.date", wraps=date) as server_date:
            server_date.today.return_value = today
            response = self.client.post(
                "/exams/schedule",
                data={"course": "Algorithms", "date": today.isoformat()},
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/exams")
        with self.app.extensions["admin_session_factory"]() as session:
            self.assertEqual(session.get(Exam, 1).date, today)

    def test_duplicate_ids_reject_entire_upload(self):
        """Duplicate IDs within one CSV cause the whole upload to be rejected."""
        exam_id = self.schedule_exam()
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )

        self.assertIn(b"duplicate student IDs", response.data)
        session_factory = self.app.extensions["admin_session_factory"]
        with session_factory() as session:
            count = session.scalar(
                select(Exam.id).where(Exam.id == exam_id)
            )
            enrolled_count = session.scalar(
                select(Enrollment.student_id).where(Enrollment.exam_id == exam_id)
            )
        self.assertEqual(count, exam_id)
        self.assertIsNone(enrolled_count)

    def test_existing_enrollment_rejects_upload_without_partial_changes(self):
        """Existing enrollments reject a batch without inserting its other students."""
        exam_id = self.schedule_exam()
        self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )
        response = self.client.post(
            f"/exams/{exam_id}/enroll",
            data={"file": (io.BytesIO(b"student_id\n20\n10\n"), "students.csv")},
            content_type="multipart/form-data",
        )

        self.assertIn(b"Students already enrolled in this exam: 10", response.data)
        session_factory = self.app.extensions["admin_session_factory"]
        with session_factory() as session:
            student_ids = session.scalars(
                select(Enrollment.student_id)
                .where(Enrollment.exam_id == exam_id)
                .order_by(Enrollment.student_id)
            ).all()
        self.assertEqual(student_ids, [10])

    def test_printing_order_calls_xmlrpc_contract(self):
        """The XML-RPC request contains exam details and enrolled student IDs."""
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
        self.assertIn(b"Queued", response.data)
        server_proxy.assert_called_once_with("http://printing.test:5003")
        proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms", ["10", "20"]
        )

    def test_printing_order_supports_64bit_identifiers(self):
        """Large exam and student IDs can be serialized by the real XML-RPC encoder."""
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
        self.assertIn(b"Queued", response.data)
        proxy.__enter__.return_value.print_exam.assert_called_once_with(
            str(exam_id), self.exam_date.isoformat(), "Algorithms",
            [str(student_id) for student_id in student_ids],
        )

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

    def test_printing_failure_is_reported(self):
        """A network failure from the Printing Service is shown to the administrator."""
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
        self.assertIn(b"Printing Service unavailable", response.data)

    def test_printing_without_enrollments_is_not_sent(self):
        """An exam with no enrolled students cannot be sent for printing."""
        exam_id = self.schedule_exam()
        with patch("as_app.ServerProxy") as server_proxy:
            response = self.client.post(
                f"/exams/{exam_id}/order-printing",
                follow_redirects=True,
            )

        self.assertIn(b"Enroll students before ordering", response.data)
        server_proxy.assert_not_called()

    def test_printing_rejection_is_not_reported_as_success(self):
        """A negative Printing Service response is not presented as success."""
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


if __name__ == "__main__":
    unittest.main()
