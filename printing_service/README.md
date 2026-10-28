# Printing Service

The Printing Service provides an exam-sheet listing page and an XML-RPC method
for generating resolution sheets. It creates exactly four answer pages for
every enrolled student. Each student's four pages carry the same unique QR
identifier.

## Run

Install the dependencies and start the service from the repository root:

```powershell
python -m pip install -r printing_service\requirements.txt
python printing_service\ps_app.py
```

The web page and XML-RPC endpoint are available at
<http://127.0.0.1:5003>. `print_exam` is registered through the
Python 3-compatible Flask-XML-RPC-Reloaded extension at `/RPC2`, the default
path used by `xmlrpc.client.ServerProxy`; the root POST path is also supported
for compatibility. The original PyPI `Flask-XML-RPC` package is Python 2-only,
so this compatible reloaded fork is used instead. The Admin Service can use its
default `ADMIN_PRINTING_RPC_URL` without changes.

The `exam_id` and `student_ids` arguments accept decimal strings or integers.
The Admin Service sends decimal strings so identifiers up to
`9223372036854775807` can cross XML-RPC without its 32-bit integer restriction.
The Printing Service validates and converts them to integers before storing
them in SQLite. Invalid, out-of-range or duplicate identifiers are rejected.

The PDF template is `printing_service\Exam-template.pdf`. Its first page
contains instructions and is omitted; the four question pages are copied for
each student. The generated PDF therefore contains exactly
`4 × number_of_students` pages, matching the project specification and the
Correction Service's accepted format.

## Data and generated PDFs

The service stores exam information, generated PDF bytes, and resolution-sheet
records in its own SQLite database at `printing_service\printing.sqlite`.
Resolution-sheet IDs are database-generated integer identifiers, unique across
the lifetime of the database, and are encoded into QR codes. Each record links
the identifier to its exam and student. Reprinting an exam replaces its
current PDF and sheet records but does not reuse the previous identifiers.

The generated PDF is also written to `printing_service\output` as a convenient
copy. The listing page shows printed exams and links to the PDF stored in the
database.

Configuration:

- `PRINTING_HOST`: bind address (defaults to `127.0.0.1`).
- `PRINTING_PORT`: listening port for the web and XML-RPC endpoints (defaults
  to `5003`).
- `PRINTING_DATABASE_URL`: SQLAlchemy database URL (defaults to the local
  SQLite file).
- `PRINTING_OUTPUT_DIR`: generated PDF copy directory (defaults to
  `printing_service\output`).
