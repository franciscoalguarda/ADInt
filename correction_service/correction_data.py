from sqlalchemy import (
    Boolean, CheckConstraint, Column, Float, ForeignKey, Integer, LargeBinary,
    String, Text, UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, deferred, relationship

Base = declarative_base()


class ProfessorAssignment(Base):
    __tablename__ = "professor_assignment"
    __table_args__ = (
        UniqueConstraint("exam_id", "question_number"),
        CheckConstraint("question_number BETWEEN 1 AND 4"),
        CheckConstraint("exam_id > 0 AND professor_id > 0"),
    )

    id = Column(Integer, primary_key=True)
    professor_id = Column(Integer, nullable=False)
    exam_id = Column(Integer, nullable=False)
    question_number = Column(Integer, nullable=False)


class ExamCorrectionPdf(Base):
    __tablename__ = "exam_correction_pdfs"
    __table_args__ = (CheckConstraint("exam_id > 0"),)

    id = Column(Integer, primary_key=True)
    exam_id = Column(Integer, nullable=False)
    filename = Column(String(255), nullable=False)
    pdf_file = deferred(Column(LargeBinary, nullable=False))
    corrections = relationship(
        "QuestionCorrection", back_populates="source", cascade="all, delete-orphan"
    )


class QuestionCorrection(Base):
    __tablename__ = "question_corrections"
    __table_args__ = (
        UniqueConstraint("exam_correction_pdf_id", "page_number"),
        CheckConstraint("question_number BETWEEN 1 AND 4"),
        CheckConstraint("page_number > 0 AND sheet_number > 0"),
        CheckConstraint(
            "(evaluated = 0 AND grade IS NULL) OR (evaluated = 1 AND grade >= 0 AND grade IS NOT NULL)"
        ),
    )

    id = Column(Integer, primary_key=True)
    exam_correction_pdf_id = Column(
        Integer, ForeignKey("exam_correction_pdfs.id"), nullable=False
    )
    page_number = Column(Integer, nullable=False)
    sheet_number = Column(Integer, nullable=False)
    question_number = Column(Integer, nullable=False)
    page_pdf_file = deferred(Column(LargeBinary, nullable=False))
    page_png_file = deferred(Column(LargeBinary, nullable=False))
    evaluated = Column(Boolean, nullable=False, default=False)
    grade = Column(Float, nullable=True)
    comment = Column(Text, nullable=False, default="")
    source = relationship("ExamCorrectionPdf", back_populates="corrections")
