from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class used by all SQLAlchemy models in the Admin Service."""

    pass


class Exam(Base):
    """An exam scheduled by an administrator."""

    __tablename__ = "exams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    course: Mapped[str] = mapped_column(String(200), nullable=False)

    def __repr__(self):
        return f"<Exam(id={self.id}, course={self.course!r}, date={self.date})>"


class Enrollment(Base):
    """A student's enrollment; the composite key prevents duplicate enrollment."""

    __tablename__ = "enrolled_students"

    exam_id: Mapped[int] = mapped_column(
        ForeignKey("exams.id", ondelete="CASCADE"), primary_key=True
    )
    student_id: Mapped[int] = mapped_column(Integer, primary_key=True)
