"""SQLAlchemy models for the cross-border export declaration & remittance facts service.

Design invariants:
  * The intake ledger is append-only: every source document version and every line is
    retained with its original location, hash, source version and event/received times.
  * Declaration status and receipt (settlement) status live in separate ledgers and
    never share a "settled" column.
  * Balances are maintained on both sides (bank transaction and export line) and
    committed amounts can never exceed the actual amount on either side.
  * Confirmed reconciliation facts are only produced through the balance controller;
    candidates never consume balances.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

MONEY = Numeric(18, 2)
QTY = Numeric(18, 4)


class Entity(Base):
    __tablename__ = "entities"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str | None] = mapped_column(String(8))
    active: Mapped[bool] = mapped_column(Boolean, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Contract(Base):
    __tablename__ = "contracts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    contract_no: Mapped[str] = mapped_column(String(128), nullable=False)
    buyer_name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("entity_id", "contract_no", name="uq_contract_entity_no"),)


class SourceDocument(Base):
    """来源受理账 — intake ledger, append-only."""

    __tablename__ = "source_documents"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"), index=True)
    external_no: Mapped[str | None] = mapped_column(String(128), index=True)
    original_location: Mapped[str] = mapped_column(Text, nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    currency: Mapped[str | None] = mapped_column(String(3))
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    quantity: Mapped[Decimal | None] = mapped_column(QTY)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    supersedes_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    ingested_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_source_documents_doctype_received", "doc_type", "received_time"),
        UniqueConstraint("doc_type", "external_no", "source_version", name="uq_document_version"),
    )


class InvoiceLine(Base):
    __tablename__ = "invoice_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id"))

    __table_args__ = (UniqueConstraint("document_id", "line_no", name="uq_invoice_line_no"),)


class CustomsLine(Base):
    """报关商品行. Revisions form a chain via revises_id; only is_current=True is live."""

    __tablename__ = "customs_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    hs_code: Mapped[str | None] = mapped_column(String(20))
    sku: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    declaration_no: Mapped[str | None] = mapped_column(String(64), index=True)
    revises_id: Mapped[int | None] = mapped_column(ForeignKey("customs_lines.id"), unique=True)
    revision_no: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_current: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    changed_fields: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("document_id", "line_no", name="uq_customs_line_no"),
        Index("ix_customs_current_entity", "entity_id", "is_current"),
    )


class PackingLine(Base):
    """装箱单行 — corroborating quantity evidence only, never an amount source."""

    __tablename__ = "packing_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sku: Mapped[str | None] = mapped_column(String(64), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    packages: Mapped[int | None] = mapped_column(Integer)
    net_weight: Mapped[Decimal | None] = mapped_column(QTY)
    export_line_id: Mapped[int | None] = mapped_column(ForeignKey("export_lines.id"), index=True)

    __table_args__ = (UniqueConstraint("document_id", "line_no", name="uq_packing_line_no"),)


class TaxLine(Base):
    """税务审核行 — returned per declaration batch; may arrive out of order."""

    __tablename__ = "tax_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    declaration_no: Mapped[str | None] = mapped_column(String(64), index=True)
    review_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    issue_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sequence_no: Mapped[int | None] = mapped_column(Integer)
    applied_sequence: Mapped[int | None] = mapped_column(Integer)
    order_conflict: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    export_line_id: Mapped[int | None] = mapped_column(ForeignKey("export_lines.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("document_id", "line_no", name="uq_tax_line_no"),)


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    txn_ref: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    payer_name: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    value_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    remittance_info: Mapped[str | None] = mapped_column(Text)
    # true when remittance_info uniquely identifies a single batch/export line
    has_line_unique_reference: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExportLine(Base):
    """原出口行 — canonical line combining invoice + current customs revision."""

    __tablename__ = "export_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    contract_id: Mapped[int | None] = mapped_column(ForeignKey("contracts.id"), index=True)
    invoice_line_id: Mapped[int | None] = mapped_column(ForeignKey("invoice_lines.id"))
    customs_line_id: Mapped[int | None] = mapped_column(ForeignKey("customs_lines.id"))
    invoice_no: Mapped[str | None] = mapped_column(String(128), index=True)
    declaration_no: Mapped[str | None] = mapped_column(String(64), index=True)
    sku: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    declared_quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    declared_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    export_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False, default="C")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("customs_line_id", name="uq_export_line_customs"),
        CheckConstraint("declared_amount >= 0", name="ck_export_declared_amount"),
    )


class Dossier(Base):
    __tablename__ = "dossiers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    case_ref: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    export_line_id: Mapped[int | None] = mapped_column(ForeignKey("export_lines.id"), index=True)
    bank_transaction_id: Mapped[int | None] = mapped_column(ForeignKey("bank_transactions.id"), index=True)
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False, default="C")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    summary: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DossierMember(Base):
    __tablename__ = "dossier_members"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    dossier_id: Mapped[int] = mapped_column(ForeignKey("dossiers.id"), nullable=False, index=True)
    member_type: Mapped[str] = mapped_column(String(32), nullable=False)
    member_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str | None] = mapped_column(String(32))
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        UniqueConstraint("dossier_id", "member_type", "member_id", name="uq_dossier_member"),
    )


class DeclarationLedger(Base):
    """申报状态账 — declaration state only. Separate from settlement facts."""

    __tablename__ = "declaration_ledgers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    export_line_id: Mapped[int] = mapped_column(
        ForeignKey("export_lines.id"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_ref: Mapped[str | None] = mapped_column(String(128))
    review_note: Mapped[str | None] = mapped_column(Text)
    correction_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class DeclarationEvent(Base):
    __tablename__ = "declaration_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    export_line_id: Mapped[int] = mapped_column(ForeignKey("export_lines.id"), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey("source_documents.id"))
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class ReceiptLedger(Base):
    """到账状态账 — receipt state only. Never shares settlement columns with declarations."""

    __tablename__ = "receipt_ledgers"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id"), nullable=False, unique=True, index=True
    )
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="unmatched")
    pending_allocation: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conflict_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ReceiptEvent(Base):
    __tablename__ = "receipt_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id"), nullable=False, index=True
    )
    from_state: Mapped[str | None] = mapped_column(String(32))
    to_state: Mapped[str] = mapped_column(String(32), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class Balance(Base):
    """Occupancy controller row: one per bank transaction and one per export line."""

    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    ref_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    committed: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=0)
    available: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("side", "ref_id", name="uq_balance_side_ref"),
        CheckConstraint("total >= 0", name="ck_balance_total"),
        CheckConstraint("committed >= 0 AND available >= 0", name="ck_balance_non_negative"),
        CheckConstraint("committed + available = total", name="ck_balance_conservation"),
    )


class BalanceHold(Base):
    """Ledger of each occupancy movement, linked to the decision that caused it."""

    __tablename__ = "balance_holds"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    balance_id: Mapped[int] = mapped_column(ForeignKey("balances.id"), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    ref_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reconciliation_line_id: Mapped[int] = mapped_column(
        ForeignKey("reconciliation_lines.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # occupy | freeze | release | transfer
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    note: Mapped[str | None] = mapped_column(Text)


class ReconciliationCandidate(Base):
    __tablename__ = "reconciliation_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id"), nullable=False, index=True
    )
    export_line_id: Mapped[int] = mapped_column(ForeignKey("export_lines.id"), nullable=False, index=True)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False)
    proposed_amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="proposed")
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(128))

    __table_args__ = (
        CheckConstraint("proposed_amount > 0", name="ck_candidate_amount_positive"),
        Index("ix_candidate_bank_status", "bank_transaction_id", "status"),
    )


class ReconciliationLine(Base):
    """已证实对账行 — created only by the balance controller."""

    __tablename__ = "reconciliation_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    bank_transaction_id: Mapped[int] = mapped_column(
        ForeignKey("bank_transactions.id"), nullable=False, index=True
    )
    export_line_id: Mapped[int] = mapped_column(ForeignKey("export_lines.id"), nullable=False, index=True)
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_candidates.id"))
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False)
    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    rules: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    confirmed_by: Mapped[str] = mapped_column(String(128), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_reason: Mapped[str | None] = mapped_column(Text)
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voided_by: Mapped[str | None] = mapped_column(String(128))
    void_reason: Mapped[str | None] = mapped_column(Text)
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_lines.id"))

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_recon_line_amount_positive"),
        Index("ix_recon_line_status", "entity_id", "status"),
    )


class DifferenceCase(Base):
    __tablename__ = "difference_cases"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    ref_type: Mapped[str | None] = mapped_column(String(32))
    ref_id: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_difference_entity_state", "entity_id", "state"),)


class ReverseCandidate(Base):
    """退运凭证 candidate: reverse quantity, never auto-applied."""

    __tablename__ = "reverse_candidates"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)
    export_line_id: Mapped[int | None] = mapped_column(ForeignKey("export_lines.id"), index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), nullable=False)
    effective_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[Decimal] = mapped_column(QTY, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(MONEY)
    currency: Mapped[str | None] = mapped_column(String(3))
    unique_attribution: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_grade: Mapped[str] = mapped_column(String(2), nullable=False, default="C")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="candidate")
    reasons: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CompileRun(Base):
    __tablename__ = "compile_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"), index=True)
    run_by: Mapped[str] = mapped_column(String(128), nullable=False)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    facts_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unallocated_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    differences_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin | analyst | viewer
    api_key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserEntityGrant(Base):
    __tablename__ = "user_entity_grants"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id"), nullable=False, index=True)

    __table_args__ = (UniqueConstraint("user_id", "entity_id", name="uq_user_entity_grant"),)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int | None] = mapped_column(ForeignKey("entities.id"))
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    object_type: Mapped[str] = mapped_column(String(40), nullable=False)
    object_id: Mapped[int | None] = mapped_column(BigInteger)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
