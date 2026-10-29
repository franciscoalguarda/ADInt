from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, deferred, relationship


Base = declarative_base()


class PrintedExam(Base):
    __tablename__ = "printed_exams"

    id = Column(Integer, primary_key=True)
    exam_date = Column(Date, nullable=False)
    course = Column(String(255), nullable=False)
    pdf_file = deferred(Column(LargeBinary, nullable=False))
    sheets = relationship(
        "ResolutionSheet",
        back_populates="exam",
        cascade="all, delete-orphan",
        order_by="ResolutionSheet.id",
    )


class ResolutionSheet(Base):
    __tablename__ = "resolution_sheets"
    __table_args__ = (
        UniqueConstraint("exam_id", "student_id"),
        CheckConstraint("exam_id > 0 AND student_id > 0"),
        {"sqlite_autoincrement": True},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    exam_id = Column(Integer, ForeignKey("printed_exams.id"), nullable=False)
    student_id = Column(Integer, nullable=False)
    exam = relationship("PrintedExam", back_populates="sheets")
