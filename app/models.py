from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_sha256 = Column(String(64), nullable=False)
    role = Column(String(32), nullable=False)  # initiator, treasurer, manager, accountant, admin
    full_name = Column(String(128), nullable=False, default="")
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    inflows_created = relationship("InflowPlan", back_populates="initiator")
    history = relationship("HistoryEntry", back_populates="user")


class LimitRule(Base):
    __tablename__ = "limit_rules"
    id = Column(Integer, primary_key=True)
    organization = Column(String(128), nullable=False)
    article = Column(String(128), nullable=False)
    currency = Column(String(16), nullable=False, default="RUB")
    limit_amount = Column(Numeric(15, 2), nullable=False, default=0)
    always_requires_approval = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("organization", "article", "currency", name="uq_limit_org_article_currency"),
    )


class BlacklistedCounterparty(Base):
    __tablename__ = "blacklisted_counterparties"
    id = Column(Integer, primary_key=True)
    counterparty = Column(String(128), nullable=False, unique=True)
    reason = Column(String(256), nullable=False, default="")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)


class InflowPlan(Base):
    __tablename__ = "inflow_plans"
    id = Column(Integer, primary_key=True)
    number = Column(String(32), nullable=False, unique=True, index=True)

    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    organization = Column(String(128), nullable=False)
    counterparty = Column(String(128), nullable=False)
    contract = Column(String(128), nullable=False)
    basis = Column(String(256), nullable=False)
    article = Column(String(128), nullable=False)
    channel = Column(String(32), nullable=False)  # account/cash
    planned_date = Column(Date, nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    currency = Column(String(16), nullable=False, default="RUB")

    status = Column(String(32), nullable=False, default="Черновик")
    comment = Column(Text, nullable=False, default="")
    change_reason = Column(String(256), nullable=False, default="")

    initiator_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    initiator = relationship("User", back_populates="inflows_created")

    approvals = relationship("ApprovalTask", back_populates="inflow", cascade="all, delete-orphan")
    history = relationship("HistoryEntry", back_populates="inflow", cascade="all, delete-orphan")


class ApprovalTask(Base):
    __tablename__ = "approval_tasks"
    id = Column(Integer, primary_key=True)
    inflow_id = Column(Integer, ForeignKey("inflow_plans.id"), nullable=False)
    inflow = relationship("InflowPlan", back_populates="approvals")

    created_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    sla_due = Column(DateTime, nullable=False)
    status = Column(String(32), nullable=False, default="Ожидает")  # Ожидает/Утверждено/Возврат/Отклонено

    approver_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    approver = relationship("User")

    decision_comment = Column(Text, nullable=False, default="")
    decided_at = Column(DateTime, nullable=True)


class HistoryEntry(Base):
    __tablename__ = "history_entries"
    id = Column(Integer, primary_key=True)
    inflow_id = Column(Integer, ForeignKey("inflow_plans.id"), nullable=False)
    inflow = relationship("InflowPlan", back_populates="history")

    changed_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)

    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    user = relationship("User", back_populates="history")

    field = Column(String(64), nullable=False)
    old_value = Column(String(256), nullable=False, default="")
    new_value = Column(String(256), nullable=False, default="")
    reason = Column(String(256), nullable=False, default="")
