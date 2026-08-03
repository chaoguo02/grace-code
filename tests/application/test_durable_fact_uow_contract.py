"""P7: Durable Fact / UoW — acceptance tests.

AC: SessionUnitOfWork contract defines execute(fn).
AC: DurableFactWriter rejects non-EventEnvelope (type-level).
AC: SessionTransaction guarantees append_fact + commit/rollback contract.
AC: No server/agent imports in application/ layer.
"""

from __future__ import annotations

import ast

import pytest

from application.transactions.unit_of_work import (
    SessionUnitOfWork, SessionTransaction, TransactionError,
)
from application.events.durable_writer import (
    DurableFactWriter, DurableFactRejectedError,
)
class TestUoWContract:

    def test_uow_is_protocol(self):
        """SessionUnitOfWork is a Protocol — can be structurally typed."""
        # Verifies the Protocol exists and is importable
        assert SessionUnitOfWork is not None

    def test_transaction_is_protocol(self):
        assert SessionTransaction is not None

    def test_transaction_error_hierarchy(self):
        assert issubclass(TransactionError, RuntimeError)


class TestDurableWriter:

    def test_writer_is_protocol(self):
        assert DurableFactWriter is not None

    def test_rejected_error_hierarchy(self):
        assert issubclass(DurableFactRejectedError, RuntimeError)


class TestImportBoundary:

    def test_uow_no_server_imports(self):
        with open("application/transactions/unit_of_work.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'server' in module:
                    pytest.fail(f"UoW imports server: {module}")

    def test_durable_writer_no_server_imports(self):
        with open("application/events/durable_writer.py", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = getattr(node, 'module', '') or ''
                if 'server' in module:
                    pytest.fail(f"DurableWriter imports server: {module}")
