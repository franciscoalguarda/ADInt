from flask import Flask, render_template, request, send_from_directory, redirect, url_for, jsonify
from sqlalchemy import create_engine, select, Column, Integer, String, Date, ForeignKey
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.exc import IntegrityError
import datetime
from os import path

from user_data import User, Base

app = Flask(__name__)

DATABASE_FILE = "users.sqlite"
db_exists = False
if path.exists(DATABASE_FILE):
    db_exists = True
    print("\t database already exists")

engine = create_engine('sqlite:///%s'%(DATABASE_FILE), echo=False)

Base.metadata.create_all(engine)

Session = sessionmaker(bind=engine)
# session = Session()

VALID_ROLES = (
    "Administrator",
    "Professor",
    "Student",
    "Exam invigilator"
)

'''
    Database access functions
'''


'''
    Web Application routes
'''
# Home page
@app.route("/")
def index():
    return render_template("index.html")

# SU_1 - List users page
@app.route("/list_users")
def list_users():
    with Session() as session:
        users = session.scalars(select(User).order_by(User.id)).all()

    return render_template("listUsers.html", users=users)

# SU_2 - Add user page


# SU_3 - Bulk add users page



if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=5001,
        debug=True
    )




# def create_user(data):
#     """Validate user data and create a User object."""

#     try:
#         user_id = int(str(data.get("id", "")).strip())
#     except (TypeError, ValueError):
#         raise ValueError("Invalid user identifier.")

#     if user_id <= 0:
#         raise ValueError("Identifier must be positive.")

#     name = (data.get("name") or "").strip()
#     password = data.get("password") or ""
#     role = (data.get("role") or "").strip()

#     if not name or not password:
#         raise ValueError("Name and password are required.")

#     if role not in VALID_ROLES:
#         raise ValueError("Invalid user role.")

#     return User(
#         id=user_id,
#         name=name,
#         password=generate_password_hash(password),
#         role=role
#     )

# # SU_2 - Add user
# @app.route("/users/add", methods=["GET", "POST"])
# def add_user():

#     if request.method == "POST":
#         try:
#             user = create_user(request.form)

#             with Session.begin() as session:
#                 if session.get(User, user.id) is not None:
#                     raise ValueError("User identifier already exists.")

#                 session.add(user)

#             flash("User added successfully.", "success")
#             return redirect(url_for("list_users"))

#         except ValueError as error:
#             flash(str(error), "error")

#         except IntegrityError:
#             flash("User identifier already exists.", "error")

#     return render_template("addUser.html")


# # SU_3 - Bulk add users
# @app.route("/users/bulk-add", methods=["GET", "POST"])
# def bulk_add_users():

#     if request.method == "POST":
#         file = request.files.get("file")

#         if not file or not file.filename:
#             flash("Please select a CSV file.", "error")
#             return redirect(url_for("bulk_add_users"))

#         if not file.filename.lower().endswith(".csv"):
#             flash("Only CSV files are accepted.", "error")
#             return redirect(url_for("bulk_add_users"))

#         try:
#             content = file.read().decode("utf-8-sig")
#             reader = csv.DictReader(io.StringIO(content))

#             required = {"id", "name", "password", "role"}

#             if not required.issubset(reader.fieldnames or []):
#                 raise ValueError("Invalid CSV columns.")

#             count = 0
#             imported_ids = set()

#             with Session.begin() as session:
#                 for line_number, row in enumerate(reader, start=2):
#                     if not any(row.values()):
#                         continue

#                     user = create_user(row)

#                     if user.id in imported_ids:
#                         raise ValueError(
#                             f"Duplicate identifier at line {line_number}."
#                         )

#                     if session.get(User, user.id) is not None:
#                         raise ValueError(
#                             f"User {user.id} already exists."
#                         )

#                     imported_ids.add(user.id)
#                     session.add(user)
#                     count += 1

#                 if count == 0:
#                     raise ValueError("CSV file contains no users.")

#             flash(f"{count} users imported successfully.", "success")
#             return redirect(url_for("list_users"))

#         except (ValueError, UnicodeDecodeError, csv.Error) as error:
#             flash(str(error), "error")

#         except IntegrityError:
#             flash("Duplicate user identifier.", "error")

#     return render_template("bulkAddUsers.html")
