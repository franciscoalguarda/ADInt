from sqlalchemy import Column, Date, ForeignKey, Integer, String
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Exam(Base):
    __tablename__ = "exams"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, nullable=False)
    course = Column(String(200), nullable=False)

    def __repr__(self):
        return f"<Exam(id={self.id}, course={self.course!r}, date={self.date})>"


class Enrollment(Base):
    __tablename__ = "enrolled_students"

    exam_id = Column(Integer, ForeignKey("exams.id", ondelete="CASCADE"), primary_key=True)
    student_id = Column(Integer, primary_key=True)
