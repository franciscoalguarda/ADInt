import io
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from werkzeug.security import check_password_hash

if __package__:
    from . import su_app as user_service
else:
    import su_app as user_service


class UserServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "users.sqlite"
        self.engine = create_engine(f"sqlite:///{database_path.as_posix()}")
        user_service.Base.metadata.create_all(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        self.original_session = user_service.Session
        user_service.Session = self.session_factory
        with self.session_factory.begin() as session:
            session.add_all(
                user_service.Role(name=name) for name in user_service.ROLES
            )
        user_service.app.testing = True
        self.client = user_service.app.test_client()

    def tearDown(self):
        user_service.Session = self.original_session
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_user_pages_are_available(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/list_users").status_code, 200)
        self.assertEqual(self.client.get("/users/add").status_code, 200)
        self.assertEqual(self.client.get("/users/bulk-add").status_code, 200)

    def test_add_user_hashes_password_and_lists_role(self):
        response = self.client.post(
            "/users/add",
            data={
                "id": "71001",
                "name": "Test Professor",
                "password": "temporary-password",
                "role": "Professor",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Professor", response.data)
        self.assertIn(b"Professor", response.data)

        with self.session_factory() as session:
            user = session.get(user_service.User, 71001)
            self.assertIsNotNone(user)
            self.assertNotEqual(user.password, "temporary-password")
            self.assertTrue(check_password_hash(user.password, "temporary-password"))

    def test_add_user_accepts_multiple_roles_and_rejects_duplicate_id(self):
        form = {
            "id": "71002",
            "name": "Multi-role User",
            "password": "temporary-password",
            "role": ["Professor", "Administrator"],
        }
        self.assertEqual(
            self.client.post("/users/add", data=form).status_code, 302
        )
        duplicate_response = self.client.post(
            "/users/add",
            data={**form, "name": "Duplicate User"},
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertIn(b"already exists", duplicate_response.data)

        with self.session_factory() as session:
            user = session.get(user_service.User, 71002)
            self.assertEqual(
                {role.name for role in user.roles},
                {"Professor", "Administrator"},
            )
            self.assertIsNone(session.get(user_service.User, 71003))

    def test_bulk_add_imports_csv_users_and_multiple_roles(self):
        csv_contents = (
            "id,name,password,role\n"
            "72001,Student One,student-password,Student\n"
            "72002,Professor Two,professor-password,Professor|Exam invigilator\n"
        )
        response = self.client.post(
            "/users/bulk-add",
            data={
                "file": (io.BytesIO(csv_contents.encode("utf-8")), "users.csv")
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 users imported successfully", response.data)
        self.assertIn(b"Student One", response.data)
        self.assertIn(b"Professor Two", response.data)

        with self.session_factory() as session:
            user = session.get(user_service.User, 72002)
            self.assertEqual(
                {role.name for role in user.roles},
                {"Professor", "Exam invigilator"},
            )
            self.assertTrue(
                check_password_hash(user.password, "professor-password")
            )

    def test_bulk_add_rejects_invalid_batch_atomically(self):
        csv_contents = (
            "id,name,password,role\n"
            "73001,Valid First,first-password,Student\n"
            "73002,Invalid Second,second-password,Not a role\n"
        )
        response = self.client.post(
            "/users/bulk-add",
            data={
                "file": (io.BytesIO(csv_contents.encode("utf-8")), "users.csv")
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid user role", response.data)
        with self.session_factory() as session:
            self.assertIsNone(session.get(user_service.User, 73001))
            self.assertIsNone(session.get(user_service.User, 73002))

    def test_bulk_add_rejects_duplicate_ids_and_empty_files(self):
        duplicate_csv = (
            "id,name,password,role\n"
            "74001,First User,first-password,Student\n"
            "74001,Second User,second-password,Student\n"
        )
        response = self.client.post(
            "/users/bulk-add",
            data={
                "file": (io.BytesIO(duplicate_csv.encode("utf-8")), "users.csv")
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"User identifier 74001 already exists", response.data)
        with self.session_factory() as session:
            self.assertIsNone(session.get(user_service.User, 74001))

        response = self.client.post(
            "/users/bulk-add",
            data={"file": (io.BytesIO(b"id,name,password,role\n"), "empty.csv")},
            content_type="multipart/form-data",
        )
        self.assertIn(b"contains no users", response.data)


if __name__ == "__main__":
    unittest.main()
