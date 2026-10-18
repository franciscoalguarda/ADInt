import csv
import io
import os

from flask import Flask, flash, render_template, request, redirect, url_for
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import selectinload, sessionmaker
from sqlalchemy.exc import IntegrityError
from os import path
from werkzeug.security import generate_password_hash

from user_data import Role, User, Base

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("USER_SECRET_KEY") or os.urandom(32)

DATABASE_FILE = path.join(path.dirname(path.abspath(__file__)), "users.sqlite")
db_exists = False
if path.exists(DATABASE_FILE):
    db_exists = True
    print("\t database already exists")

engine = create_engine('sqlite:///%s'%(DATABASE_FILE), echo=False)

Base.metadata.create_all(engine)

Session = sessionmaker(bind=engine)
# session = Session()

'''
    Database access functions
'''
ROLES = ["Administrator", "Professor", "Exam invigilator", "Student"]

'''
    Function to create default roles in the database if they do not already exist.
'''
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


# User id validation
def validate_user_id(value):
    try:
        user_id = int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid user identifier.") from error

    # Validação que é um inteiro positivo, único e não superior ao valor máximo de 64 bits (2^63 - 1)
    if user_id <= 0 or user_id > 2**63 - 1:
        raise ValueError("Identifier must be a positive 64-bit integer.")

    return user_id

# User name and password validation
def validate_user_details(name, password):
    if not name or not password.strip():
        raise ValueError("Name and password are required.")

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
        raise ValueError("Select at least one user role.")

    for role_name in role_names:
        if role_name not in ROLES:
            raise ValueError("Invalid user role.")

# User creation and saving
def create_user(user_data, session):
    user_id = validate_user_id(user_data.get("id", ""))
    name = (user_data.get("name") or "").strip()
    password = user_data.get("password") or ""
    role_names = get_role_names(user_data)

    validate_user_details(name, password)
    validate_role_names(role_names)
    if session.get(User, user_id) is not None:
        raise ValueError(f"User identifier {user_id} already exists.")

    role_query = select(Role).where(Role.name.in_(role_names))
    user = User(
        id=user_id,
        name=name,
        password=generate_password_hash(password),
        roles=session.scalars(role_query).all(),
    )
    session.add(user)
    return user

# CSV import
def validate_csv_upload(upload):
    if upload is None or not upload.filename:
        raise ValueError("Please select a CSV file.")

    if not upload.filename.lower().endswith(".csv"):
        raise ValueError("Only CSV files are accepted.")

# CSV header validation
def validate_csv_headers(headers):
    required_columns = {"id", "name", "password", "role"}
    missing_columns = not required_columns.issubset(headers)
    duplicate_columns = len(headers) != len(set(headers))

    if missing_columns or duplicate_columns:
        raise ValueError(
            "CSV must contain unique id, name, password and role columns."
        )

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
        raise ValueError(f"Invalid number of CSV fields at line {line_number}.")

# CSV user row extraction
def get_csv_user_rows(reader):
    for row in reader:
        line_number = reader.line_num
        validate_csv_row(row, line_number)
        if any(value.strip() for value in row.values()):
            yield line_number, row

# CSV user saving
def save_csv_users(reader, session):
    count = 0
    for line_number, row in get_csv_user_rows(reader):
        try:
            create_user(row, session)
        except ValueError as error:
            raise ValueError(f"CSV line {line_number}: {error}") from error
        count += 1

    if count == 0:
        raise ValueError("CSV file contains no users.")

    return count

# CSV import handling
def import_users_from_csv(upload):
    try:
        reader = read_users_csv(upload)
        with Session.begin() as session:
            return save_csv_users(reader, session)
    except UnicodeDecodeError as error:
        raise ValueError("The CSV file must use UTF-8 encoding.") from error
    except csv.Error as error:
        raise ValueError("Invalid CSV format.") from error
    except IntegrityError as error:
        raise ValueError(
            "Duplicate user identifier. No users were imported."
        ) from error


# Web application routes
@app.route("/")
def index():
    return render_template("index.html")

# SU_1 - List users
@app.route("/list_users")
def list_users():
    users = get_all_users()
    return render_template("listUsers.html", users=users)

# SU_2 - Add one user
@app.route("/users/add", methods=["GET", "POST"])
def add_user():
    if request.method == "POST":
        try:
            with Session.begin() as session:
                create_user(request.form, session)
        except ValueError as error:
            flash(str(error), "error")
        except IntegrityError:
            flash("User identifier already exists.", "error")
        else:
            flash("User added successfully.", "success")
            return redirect(url_for("list_users"))

    return render_template("addUser.html", roles=ROLES)

# SU_3 - Bulk add users
@app.route("/users/bulk-add", methods=["GET", "POST"])
def bulk_add_users():
    if request.method == "POST":
        try:
            count = import_users_from_csv(request.files.get("file"))
        except ValueError as error:
            flash(str(error), "error")
        else:
            flash(f"{count} users imported successfully.", "success")
            return redirect(url_for("list_users"))

    return render_template("bulkAddUsers.html")

if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5001,
        debug=True,
    )
