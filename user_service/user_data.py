from sqlalchemy import Table, Column, Integer, String, ForeignKey
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

UserRoles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id"), primary_key=True),
    Column("role_id", Integer, ForeignKey("roles.id"), primary_key=True)
)

class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    password = Column(String, nullable=False)

    roles = relationship(
        "Role",
        secondary=UserRoles,
        back_populates="users"
    )

    def __repr__(self):
        return "<User(id=%d, name='%s')>" % (self.id, self.name)

class Role(Base):
    __tablename__ = "roles"
    
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

    users = relationship(
        "User",
        secondary=UserRoles,
        back_populates="roles"
    )

    def __repr__(self):
        return "<Role(id=%d, name='%s')>" % (self.id, self.name)