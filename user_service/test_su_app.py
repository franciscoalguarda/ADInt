import io
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
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
        event.listen(self.engine, "connect", user_service.enable_foreign_keys)
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
        self.assertIsNone(user_service.app.secret_key)
        for route in ("/", "/list_users", "/users/add", "/users/bulk-add"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b'class="message error"', response.data)
                self.assertNotIn(b'class="message success"', response.data)
                self.assertNotIn("Set-Cookie", response.headers)

    def test_foreign_keys_are_enabled_on_every_service_connection(self):
        with user_service.engine.connect() as first, user_service.engine.connect() as second:
            for connection in (first, second):
                self.assertEqual(connection.exec_driver_sql("PRAGMA foreign_keys").scalar(), 1)

    def test_database_rejects_orphaned_user_role_associations(self):
        with self.session_factory.begin() as session:
            user_service.create_user({
                "id": "77001", "name": "Existing Student", "password": "test-password",
                "role": "Student",
            }, session)
            role_id = session.scalar(select(user_service.Role.id).where(
                user_service.Role.name == "Student"
            ))
        associations = user_service.Base.metadata.tables["user_roles"]
        statements = (
            associations.insert().values(user_id=77002, role_id=role_id),
            associations.insert().values(user_id=77001, role_id=999999),
            user_service.User.__table__.delete().where(user_service.User.id == 77001),
            user_service.Role.__table__.delete().where(user_service.Role.id == role_id),
        )
        for index, statement in enumerate(statements):
            with self.subTest(statement=index):
                with self.assertRaises(IntegrityError):
                    with self.engine.begin() as connection:
                        connection.execute(statement)
        with self.session_factory() as session:
            user = session.get(user_service.User, 77001)
            self.assertEqual([role.name for role in user.roles], ["Student"])
            self.assertEqual(len(session.execute(select(associations)).all()), 1)

    def test_missing_database_role_prevents_user_creation_and_preserves_form(self):
        with self.session_factory.begin() as session:
            session.delete(session.scalar(select(user_service.Role).where(
                user_service.Role.name == "Professor"
            )))
        form = {"id": "77003", "name": "Missing Role User", "password": "temporary-password"}
        for roles in (["Professor"], ["Student", "Professor"]):
            with self.subTest(roles=roles):
                response = self.client.post("/users/add", data={**form, "role": roles})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(b"One or more selected roles are unavailable. Please try again later.", response.data)
                self.assertIn(b'value="77003"', response.data)
                self.assertIn(b'value="Missing Role User"', response.data)
                self.assertNotIn(b"temporary-password", response.data)
                for role in roles:
                    self.assertRegex(response.data, f'value="{role}"\\s+checked'.encode())
                with self.session_factory() as session:
                    self.assertIsNone(session.get(user_service.User, 77003))
                    self.assertEqual(session.execute(select(
                        user_service.Base.metadata.tables["user_roles"]
                    )).all(), [])
        user_service.create_roles()
        self.assertEqual(self.client.post("/users/add", data={
            **form, "role": ["Student", "Professor"],
        }).status_code, 302)
        with self.session_factory() as session:
            self.assertEqual({role.name for role in session.get(user_service.User, 77003).roles},
                             {"Student", "Professor"})

    def test_missing_database_role_rolls_back_the_entire_csv_import(self):
        with self.session_factory.begin() as session:
            session.delete(session.scalar(select(user_service.Role).where(
                user_service.Role.name == "Professor"
            )))
            user_service.create_user({
                "id": "78000", "name": "Existing Student", "password": "existing-password",
                "role": "Student",
            }, session)
        contents = (
            "id,name,password,role\n"
            "78001,Valid Student,first-password,Student\n"
            "78002,Missing Role,second-password,Student|Professor\n"
        )
        response = self.client.post("/users/bulk-add", data={
            "file": (io.BytesIO(contents.encode()), "users.csv"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("Location", response.headers)
        self.assertIn(b"One or more selected roles are unavailable. Please try again later.", response.data)
        with self.session_factory() as session:
            self.assertEqual(session.scalars(select(user_service.User.id)).all(), [78000])
            self.assertEqual([role.name for role in session.get(user_service.User, 78000).roles], ["Student"])

    def test_home_counts_users_once_and_includes_each_assigned_role(self):
        empty = self.client.get("/")
        self.assertIn(b'id="total-users">0</dd>', empty.data)
        for role in user_service.ROLES:
            count_id = f'role-{role.lower().replace(" ", "-")}-count'
            self.assertIn(f'id="{count_id}">0</dd>'.encode(), empty.data)

        with self.session_factory.begin() as session:
            user_service.create_user({
                "id": "76001", "name": "First User", "password": "first-password",
                "role": "Professor|Student",
            }, session)
            user_service.create_user({
                "id": "76002", "name": "Second User", "password": "second-password",
                "role": "Student",
            }, session)

        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="total-users">2</dd>', response.data)
        self.assertIn(b'id="role-professor-count">1</dd>', response.data)
        self.assertIn(b'id="role-student-count">2</dd>', response.data)
        self.assertIn(b'id="role-administrator-count">0</dd>', response.data)
        self.assertIn(b'id="role-exam-invigilator-count">0</dd>', response.data)
        self.assertIn(b'href="/list_users"', response.data)
        self.assertIn(b'href="/users/add"', response.data)
        self.assertIn(b'href="/users/bulk-add"', response.data)
        self.assertNotIn(b"first-password", response.data)

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
        self.assertEqual(len(response.history), 1)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/list_users/sucess")
        self.assertNotIn("Set-Cookie", response.history[0].headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b"User added successfully.", response.data)
        self.assertIn(b"Test Professor", response.data)
        self.assertIn(b"Professor", response.data)

        refreshed = self.client.get(response.request.full_path)
        self.assertEqual(refreshed.status_code, 200)
        self.assertIn(b"User added successfully.", refreshed.data)

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
        response = self.client.post("/users/add", data=form)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/list_users/sucess")
        self.assertNotIn("Set-Cookie", response.headers)
        duplicate_response = self.client.post(
            "/users/add",
            data={**form, "name": "Duplicate User"},
            follow_redirects=True,
        )
        self.assertEqual(duplicate_response.status_code, 200)
        self.assertEqual(duplicate_response.history, ())
        self.assertEqual(duplicate_response.request.path, "/users/add")
        self.assertNotIn("Location", duplicate_response.headers)
        self.assertIn(b"already exists", duplicate_response.data)
        self.assertIn(b'class="message error"', duplicate_response.data)
        self.assertIn(b'value="71002"', duplicate_response.data)
        self.assertIn(b'value="Duplicate User"', duplicate_response.data)
        self.assertRegex(duplicate_response.data, rb'value="Professor"\s+checked')
        self.assertRegex(duplicate_response.data, rb'value="Administrator"\s+checked')
        self.assertNotIn(b"temporary-password", duplicate_response.data)
        self.assertNotIn("Set-Cookie", duplicate_response.headers)

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
        self.assertEqual(len(response.history), 1)
        self.assertEqual(response.history[0].status_code, 302)
        self.assertEqual(response.request.path, "/list_users/sucess")
        self.assertEqual(response.request.args.get("count"), "2")
        self.assertNotIn("Set-Cookie", response.headers)
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
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/users/bulk-add")
        self.assertNotIn("Location", response.headers)
        self.assertIn(b'enctype="multipart/form-data"', response.data)
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
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/users/bulk-add")
        self.assertNotIn("Location", response.headers)
        self.assertIn(b"User identifier already exists.", response.data)
        with self.session_factory() as session:
            self.assertIsNone(session.get(user_service.User, 74001))

        response = self.client.post(
            "/users/bulk-add",
            data={"file": (io.BytesIO(b"id,name,password,role\n"), "empty.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertIn(b"contains no users", response.data)

    def test_success_route_displays_confirmation(self):
        success = self.client.get("/list_users/sucess")
        self.assertEqual(success.status_code, 200)
        self.assertIn(b"User added successfully.", success.data)
        listing = self.client.get("/list_users")
        self.assertNotIn(b'class="message success"', listing.data)
        self.assertNotIn(b'class="message error"', listing.data)

    def test_add_user_renders_form_with_validation_messages(self):
        form = {
            "id": "75001", "name": "Test User", "password": "test-password",
            "role": "Student",
        }
        cases = (
            ({"id": "invalid"}, b"Invalid user identifier."),
            ({"id": "0"}, b"Identifier must be a positive 64-bit integer."),
            ({"name": ""}, b"Name and password are required."),
            ({"role": ""}, b"Select at least one user role."),
            ({"role": "Unknown"}, b"Invalid user role."),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                response = self.client.post("/users/add", data={**form, **changes})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.request.path, "/users/add")
                self.assertNotIn("Location", response.headers)
                self.assertNotIn("Set-Cookie", response.headers)
                self.assertIn(message, response.data)
                self.assertIn(b'<form method="post"', response.data)
                self.assertNotIn(b"test-password", response.data)

    def test_bulk_add_renders_form_with_invalid_csv_message(self):
        response = self.client.post(
            "/users/bulk-add",
            data={"file": (io.BytesIO(b"\xff"), "users.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.request.path, "/users/bulk-add")
        self.assertNotIn("Location", response.headers)
        self.assertNotIn("Set-Cookie", response.headers)
        self.assertIn(b'enctype="multipart/form-data"', response.data)
        self.assertIn(b"Invalid CSV file.", response.data)

    def test_success_route_handles_invalid_import_counts(self):
        for count in ("invalid", "", "0", "-1"):
            with self.subTest(count=count):
                response = self.client.get(
                    "/list_users/sucess", query_string={"count": count}
                )
                self.assertEqual(response.status_code, 200)
                self.assertNotIn(b"users imported successfully", response.data)


if __name__ == "__main__":
    unittest.main()
