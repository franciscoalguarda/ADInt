import csv
import io
import os

from flask import Flask, redirect, render_template, request, url_for
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.exc import IntegrityError
from os import path
from werkzeug.security import check_password_hash, generate_password_hash

from user_data import Role, User, Base

app = Flask(__name__)

DATABASE_FILE = path.join(path.dirname(path.abspath(__file__)), "users.sqlite")
db_exists = False
if path.exists(DATABASE_FILE):
    db_exists = True
    print("\t database already exists")

engine = create_engine(os.environ.get("USER_DATABASE_URL", 'sqlite:///%s'%(DATABASE_FILE)), echo=False)

# Force foreign key constraints verification in SQLite
@event.listens_for(engine, "connect")
def enable_foreign_keys(connection, _):
    connection.execute("PRAGMA foreign_keys=ON")


Base.metadata.create_all(engine)

Session = sessionmaker(bind=engine)
# session = Session()

# Database access functions
ROLES = ["Administrator", "Professor", "Exam invigilator", "Student"]

# Function to create default roles in the database if they do not already exist.
def create_roles():
    with Session.begin() as session:
        for role_name in ROLES:
            role = session.scalar(
                select(Role).where(Role.name == role_name)
            )

            if role is None:
                session.add(Role(name=role_name))

    print("Default roles verified.")


create_roles()

# User listing
def get_all_users():
    query = select(User).options(selectinload(User.roles)).order_by(User.id)

    with Session() as session:
        return session.scalars(query).all()


def get_total_users():
    with Session() as session:
        return session.scalar(select(func.count(User.id)))


def get_role_counts():
    query = (
        select(Role.name, func.count(User.id))
        .outerjoin(Role.users)
        .group_by(Role.id, Role.name)
    )
    with Session() as session:
        role_counts = dict(session.execute(query).all())

    return {role: role_counts.get(role, 0) for role in ROLES}


# User id validation
def validate_user_id(value):
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_id") from error

    # Validate that the identifier is a unique positive integer within the signed 64-bit range (2^63 - 1).
    if user_id <= 0 or user_id > 2**63 - 1:
        raise ValueError("invalid_id_range")

    return user_id

# User name and password validation
def validate_user_details(name, password):
    if not name or not password.strip():
        raise ValueError("missing_details")

# Role lookup by user data
def get_role_names(user_data):
    if hasattr(user_data, "getlist"):
        role_values = user_data.getlist("role")
    else:
        role_values = [user_data.get("role") or ""]

    role_names = []
    for value in role_values:
        for role_name in value.split("|"):
            role_name = role_name.strip()
            if role_name not in role_names:
                role_names.append(role_name)

    return role_names

# Role validation
def validate_role_names(role_names):
    if not role_names or role_names == [""]:
        raise ValueError("missing_role")

    for role_name in role_names:
        if role_name not in ROLES:
            raise ValueError("invalid_role")

# User creation and saving
def create_user(user_data, session):
    user_id = validate_user_id(user_data.get("id", ""))
    name = (user_data.get("name") or "").strip()
    password = user_data.get("password") or ""
    role_names = get_role_names(user_data)

    validate_user_details(name, password)
    validate_role_names(role_names)
    if session.get(User, user_id) is not None:
        raise ValueError("duplicate_id")

    role_query = select(Role).where(Role.name.in_(role_names))
    roles = session.scalars(role_query).all()
    if len(roles) != len(role_names):
        raise ValueError("unavailable_role")

    user = User(
        id=user_id,
        name=name,
        password=generate_password_hash(password),
        roles=roles,
    )
    session.add(user)
    return user

# CSV import
def validate_csv_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError("missing_file")

    if not upload.filename.lower().endswith(".csv"):
        raise ValueError("invalid_csv")

# CSV header validation
def validate_csv_headers(headers):
    required_columns = {"id", "name", "password", "role"}
    missing_columns = not required_columns.issubset(headers)
    duplicate_columns = len(headers) != len(set(headers))

    if missing_columns or duplicate_columns:
        raise ValueError("invalid_csv_headers")

# CSV reading
def read_users_csv(upload):
    validate_csv_upload(upload)
    contents = upload.read().decode("utf-8-sig")
    stream = io.StringIO(contents, newline="")
    reader = csv.DictReader(stream, strict=True)
    validate_csv_headers(reader.fieldnames or [])
    return reader

# CSV row validation
def validate_csv_row(row, line_number):
    extra_fields = None in row
    missing_fields = None in row.values()

    if extra_fields or missing_fields:
        raise ValueError("invalid_csv_row")

# CSV user row extraction
def get_csv_user_rows(reader):
    for row in reader:
        line_number = reader.line_num
        validate_csv_row(row, line_number)
        if any(value.strip() for value in row.values()):
            yield line_number, row

class CsvUserConflictError(ValueError):
    def __init__(self, user_id, line_number):
        super().__init__("conflicting_csv_user")
        self.user_id = user_id
        self.line_number = line_number


# CSV user saving
def save_csv_users(reader, session):
    count = 0
    original_users = {}
    for line_number, row in get_csv_user_rows(reader):
        user_id = validate_user_id(row.get("id", ""))
        if user_id not in original_users:
            original_users[user_id] = session.get(User, user_id)
        elif original_users[user_id] is None:
            # For users first created by this upload, keep the first occurrence.
            continue
        existing_user = original_users[user_id]
        if existing_user is not None:
            if (
                existing_user.name != (row.get("name") or "").strip()
                or {role.name for role in existing_user.roles} != set(get_role_names(row))
                or not check_password_hash(existing_user.password, row.get("password") or "")
            ):
                raise CsvUserConflictError(user_id, line_number)
            continue
        create_user(row, session)
        count += 1

    if not original_users:
        raise ValueError("empty_csv")

    return count

# CSV import handling
def import_users_from_csv(upload):
    try:
        reader = read_users_csv(upload)
        with Session.begin() as session:
            # Serialize imports before checking IDs so overlapping uploads
            # validate users committed by the previous import.
            if session.bind.dialect.name == "sqlite":
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            return save_csv_users(reader, session)
    except UnicodeDecodeError as error:
        raise ValueError("invalid_csv") from error
    except csv.Error as error:
        raise ValueError("invalid_csv") from error
    except IntegrityError as error:
        raise ValueError("csv_integrity_error") from error


def get_error_message(reason, **details):
    messages = {
        "duplicate_id": "User identifier already exists.",
        "invalid_role": "Invalid user role.",
        "unavailable_role": "One or more selected roles are unavailable. Please try again later.",
        "invalid_csv": "Invalid CSV file.",
        "invalid_id": "Invalid user identifier.",
        "invalid_id_range": "Identifier must be a positive 64-bit integer.",
        "missing_details": "Name and password are required.",
        "missing_role": "Select at least one user role.",
        "missing_file": "Please select a CSV file.",
        "invalid_csv_headers": "CSV must contain unique id, name, password and role columns.",
        "invalid_csv_row": "Invalid number of CSV fields.",
        "empty_csv": "CSV file contains no users.",
        "csv_integrity_error": "Could not save the CSV import. No users were imported.",
        "conflicting_csv_user": (
            "User identifier {user_id} conflicts with existing user data on CSV line "
            "{line_number}. No users were imported."
        ),
    }
    return messages.get(
        reason,
        "Could not complete the operation.",
    ).format(**details)


# Web application routes
@app.route("/")
def index():
    return render_template(
        "index.html",
        total_users=get_total_users(),
        role_counts=get_role_counts(),
    )

# SU_1 - List users
@app.route("/list_users")
def list_users():
    users = get_all_users()
    return render_template("listUsers.html", users=users)


@app.route("/list_users/sucess")
def list_users_success():
    count = request.args.get("count", type=int)
    success = "User added successfully."
    if count == 0:
        success = "No new users were imported."
    elif count is not None and count > 0:
        success = f"{count} users imported successfully."

    return render_template("listUsers.html", users=get_all_users(), success=success)


# SU_2 - Add one user
@app.route("/users/add", methods=["GET", "POST"])
def add_user():
    error_message = None
    if request.method == "POST":
        try:
            with Session.begin() as session:
                create_user(request.form, session)
        except ValueError as error:
            error_message = get_error_message(str(error))
        except IntegrityError:
            error_message = get_error_message("duplicate_id")
        else:
            return redirect(url_for("list_users_success"))

    return render_template("addUser.html", roles=ROLES, error=error_message)

# SU_3 - Bulk add users
@app.route("/users/bulk-add", methods=["GET", "POST"])
def bulk_add_users():
    error_message = None
    if request.method == "POST":
        try:
            count = import_users_from_csv(request.files.get("file"))
        except CsvUserConflictError as error:
            error_message = get_error_message(
                str(error), user_id=error.user_id, line_number=error.line_number
            )
        except ValueError as error:
            error_message = get_error_message(str(error))
        else:
            return redirect(url_for("list_users_success", count=count))

    return render_template("bulkAddUsers.html", error=error_message)

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5001,
        debug=True,
    )
