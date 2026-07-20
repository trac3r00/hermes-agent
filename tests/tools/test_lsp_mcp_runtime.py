"""Focused tests for tools/lsp_mcp_runtime.py.

Tests cover two areas: MCP tool schema structure validation and
diagnostic classification helpers.  No live language servers, no
network calls — pure unit tests.
"""

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from tools.lsp_mcp_runtime import (
    ALL_LSP_SCHEMAS,
    DIAGNOSTICS_SCHEMA,
    GOTO_DEFINITION_SCHEMA,
    LSP_STATUS_SCHEMA,
    RENAME_SCHEMA,
    DiagnosticKind,
    Severity,
    classify_diagnostic,
    classify_severity,
    filter_by_severity,
    get_all_schemas,
    has_blocking_errors,
    is_new_diagnostic,
    summarize_diagnostics,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_diag(
    severity: int = 1,
    message: str = "error",
    code: Any = None,
    source: str = "pyright",
    line: int = 0,
    col: int = 0,
    end_line: int = 0,
    end_col: int = 10,
) -> Dict[str, Any]:
    d: Dict[str, Any] = {"severity": severity, "message": message, "source": source}
    if code is not None:
        d["code"] = code
    d["range"] = {
        "start": {"line": line, "character": col},
        "end": {"line": end_line, "character": end_col},
    }
    return d


def _assert_schema_valid(schema: dict, expected_name: str) -> None:
    """Assert an OpenAI function-calling schema has the required shape."""
    assert schema["name"] == expected_name
    assert "description" in schema
    assert len(schema["description"]) > 0
    params = schema["parameters"]
    assert params["type"] == "object"
    assert "properties" in params
    assert "required" in params


# ---------------------------------------------------------------------------
# Schema structure tests
# ---------------------------------------------------------------------------

class TestSchemaStructure:
    """Every schema must be a valid OpenAI function-calling dict."""

    def test_lsp_status_schema(self):
        _assert_schema_valid(LSP_STATUS_SCHEMA, "lsp.status")
        assert "workspace" in LSP_STATUS_SCHEMA["parameters"]["properties"]

    def test_diagnostics_schema(self):
        _assert_schema_valid(DIAGNOSTICS_SCHEMA, "diagnostics")
        props = DIAGNOSTICS_SCHEMA["parameters"]["properties"]
        assert "path" in props
        assert "path" in DIAGNOSTICS_SCHEMA["parameters"]["required"]
        assert "severity" in props
        assert props["severity"]["enum"] == ["error", "warning", "info", "hint"]

    def test_goto_definition_schema(self):
        _assert_schema_valid(GOTO_DEFINITION_SCHEMA, "goto_definition")
        props = GOTO_DEFINITION_SCHEMA["parameters"]["properties"]
        for key in ("path", "line", "character"):
            assert key in props
            assert key in GOTO_DEFINITION_SCHEMA["parameters"]["required"]

    def test_rename_schema(self):
        _assert_schema_valid(RENAME_SCHEMA, "rename")
        props = RENAME_SCHEMA["parameters"]["properties"]
        for key in ("path", "line", "character", "new_name"):
            assert key in props
            assert key in RENAME_SCHEMA["parameters"]["required"]

    def test_line_params_are_integer_and_minimum_one(self):
        """Line/column params must be 1-indexed integers."""
        for schema in (GOTO_DEFINITION_SCHEMA, RENAME_SCHEMA):
            props = schema["parameters"]["properties"]
            assert props["line"]["type"] == "integer"
            assert props["line"]["minimum"] == 1
            assert props["character"]["type"] == "integer"
            assert props["character"]["minimum"] == 1

    def test_all_schemas_in_dict(self):
        assert set(ALL_LSP_SCHEMAS.keys()) == {
            "lsp.status", "diagnostics", "goto_definition", "rename",
        }


class TestGetAllSchemas:
    """get_all_schemas() wraps each schema in OpenAI function-calling format."""

    def test_returns_list_of_wrapped_schemas(self):
        result = get_all_schemas()
        assert isinstance(result, list)
        assert len(result) == 4

    def test_wrapping_format(self):
        for item in get_all_schemas():
            assert item["type"] == "function"
            assert "function" in item
            assert "name" in item["function"]


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

class TestClassifySeverity:
    def test_error(self):
        assert classify_severity(_make_diag(severity=1)) == Severity.ERROR

    def test_warning(self):
        assert classify_severity(_make_diag(severity=2)) == Severity.WARNING

    def test_info(self):
        assert classify_severity(_make_diag(severity=3)) == Severity.INFO

    def test_hint(self):
        assert classify_severity(_make_diag(severity=4)) == Severity.HINT

    def test_missing_defaults_to_error(self):
        assert classify_severity({}) == Severity.ERROR


# ---------------------------------------------------------------------------
# Diagnostic kind classification
# ---------------------------------------------------------------------------

class TestClassifyDiagnostic:
    def test_syntax(self):
        d = _make_diag(message="unexpected token")
        assert classify_diagnostic(d) == DiagnosticKind.SYNTAX

    def test_syntax_indent(self):
        d = _make_diag(message="expected indentation")
        assert classify_diagnostic(d) == DiagnosticKind.SYNTAX

    def test_type_error(self):
        d = _make_diag(message="Argument of type 'str' is not assignable")
        assert classify_diagnostic(d) == DiagnosticKind.TYPE

    def test_import(self):
        d = _make_diag(message="Cannot resolve import 'requests'")
        assert classify_diagnostic(d) == DiagnosticKind.IMPORT

    def test_import_no_module(self):
        d = _make_diag(message="no module named 'numpy'")
        assert classify_diagnostic(d) == DiagnosticKind.IMPORT

    def test_name_undefined(self):
        d = _make_diag(message="'foo' is not defined")
        assert classify_diagnostic(d) == DiagnosticKind.NAME

    def test_name_unresolved(self):
        d = _make_diag(message="unresolved reference 'bar'")
        assert classify_diagnostic(d) == DiagnosticKind.NAME

    def test_unused(self):
        d = _make_diag(message="Variable 'x' is unused")
        assert classify_diagnostic(d) == DiagnosticKind.UNUSED

    def test_unreachable(self):
        d = _make_diag(message="Code is unreachable")
        assert classify_diagnostic(d) == DiagnosticKind.UNUSED

    def test_other_when_no_match(self):
        d = _make_diag(message="something unrelated to keywords")
        assert classify_diagnostic(d) == DiagnosticKind.OTHER

    def test_code_field_checked(self):
        d = _make_diag(message="weird message", code="report-imports")
        assert classify_diagnostic(d) == DiagnosticKind.IMPORT

    def test_case_insensitive(self):
        d = _make_diag(message="UNEXPECTED TOKEN")
        assert classify_diagnostic(d) == DiagnosticKind.SYNTAX


# ---------------------------------------------------------------------------
# is_new_diagnostic
# ---------------------------------------------------------------------------

class TestIsNewDiagnostic:
    def test_identical_diag_is_not_new(self):
        d = _make_diag(message="error A", line=5)
        assert is_new_diagnostic(d, [d]) is False

    def test_different_message_is_new(self):
        d1 = _make_diag(message="error A")
        d2 = _make_diag(message="error B")
        assert is_new_diagnostic(d2, [d1]) is True

    def test_different_line_is_new(self):
        d1 = _make_diag(line=5)
        d2 = _make_diag(line=10)
        assert is_new_diagnostic(d2, [d1]) is True

    def test_empty_baseline_means_new(self):
        d = _make_diag()
        assert is_new_diagnostic(d, []) is True

    def test_baseline_has_different_code(self):
        d1 = _make_diag(code="report-undefined")
        d2 = _make_diag(code="report-type")
        assert is_new_diagnostic(d2, [d1]) is True


# ---------------------------------------------------------------------------
# filter_by_severity
# ---------------------------------------------------------------------------

class TestFilterBySeverity:
    def test_error_only_default(self):
        diags = [
            _make_diag(severity=1),
            _make_diag(severity=2),
            _make_diag(severity=3),
        ]
        result = filter_by_severity(diags)
        assert len(result) == 1
        assert result[0]["severity"] == 1

    def test_warning_and_above(self):
        diags = [
            _make_diag(severity=1),
            _make_diag(severity=2),
            _make_diag(severity=3),
        ]
        result = filter_by_severity(diags, min_severity=Severity.WARNING)
        assert len(result) == 2
        assert {d["severity"] for d in result} == {1, 2}

    def test_all_severities(self):
        diags = [_make_diag(severity=i) for i in range(1, 5)]
        result = filter_by_severity(diags, min_severity=Severity.HINT)
        assert len(result) == 4

    def test_empty_input(self):
        assert filter_by_severity([]) == []


# ---------------------------------------------------------------------------
# has_blocking_errors
# ---------------------------------------------------------------------------

class TestHasBlockingErrors:
    def test_true_when_errors_present(self):
        diags = [_make_diag(severity=1), _make_diag(severity=2)]
        assert has_blocking_errors(diags) is True

    def test_false_when_warnings_only(self):
        diags = [_make_diag(severity=2), _make_diag(severity=3)]
        assert has_blocking_errors(diags) is False

    def test_false_when_empty(self):
        assert has_blocking_errors([]) is False


# ---------------------------------------------------------------------------
# summarize_diagnostics
# ---------------------------------------------------------------------------

class TestSummarizeDiagnostics:
    def test_counts_errors_and_warnings(self):
        diags = [
            _make_diag(severity=1),
            _make_diag(severity=1),
            _make_diag(severity=2),
        ]
        s = summarize_diagnostics(diags)
        assert s["errors"] == 2
        assert s["warnings"] == 1

    def test_kinds_aggregation(self):
        diags = [
            _make_diag(message="unexpected token"),
            _make_diag(message="unexpected token"),
            _make_diag(message="undefined variable"),
        ]
        s = summarize_diagnostics(diags)
        assert s["kinds"]["syntax"] == 2
        assert s["kinds"]["name"] == 1

    def test_kinds_excluded_when_flag_off(self):
        diags = [_make_diag(message="unexpected token")]
        s = summarize_diagnostics(diags, include_kinds=False)
        assert "kinds" not in s

    def test_empty(self):
        s = summarize_diagnostics([])
        assert s == {"errors": 0, "warnings": 0, "kinds": {}}


# ---------------------------------------------------------------------------
# Handler error paths (mocked agent.lsp)
# ---------------------------------------------------------------------------

class TestHandlerErrorPaths:
    """Handlers must return JSON errors when agent.lsp is unavailable."""

    @patch("tools.lsp_mcp_runtime.get_service", create=True)
    def test_lsp_status_no_service(self, _mock):
        from tools.lsp_mcp_runtime import _handle_lsp_status
        with patch.dict("sys.modules", {"agent.lsp": MagicMock(get_service=lambda: None)}):
            result = json.loads(_handle_lsp_status({}))
            assert result["active"] is False

    def test_diagnostics_missing_path(self):
        from tools.lsp_mcp_runtime import _handle_diagnostics
        result = json.loads(_handle_diagnostics({}))
        assert "error" in result

    def test_goto_definition_missing_params(self):
        from tools.lsp_mcp_runtime import _handle_goto_definition
        result = json.loads(_handle_goto_definition({}))
        assert "error" in result

    def test_rename_missing_params(self):
        from tools.lsp_mcp_runtime import _handle_rename
        result = json.loads(_handle_rename({}))
        assert "error" in result

    def test_diagnostics_no_lsp_service(self):
        from tools.lsp_mcp_runtime import _handle_diagnostics
        mock_svc = MagicMock()
        mock_svc.is_active.return_value = False
        mock_lsp = MagicMock(get_service=lambda: None)
        with patch.dict("sys.modules", {"agent.lsp": mock_lsp}):
            result = json.loads(_handle_diagnostics({"path": "/tmp/x.py"}))
            assert result["diagnostics"] == []

    def test_rename_no_lsp_service(self):
        from tools.lsp_mcp_runtime import _handle_rename
        mock_lsp = MagicMock(get_service=lambda: None)
        with patch.dict("sys.modules", {"agent.lsp": mock_lsp}):
            result = json.loads(_handle_rename({
                "path": "/tmp/x.py", "line": 1, "character": 1, "new_name": "foo",
            }))
            assert "error" in result

    def test_goto_definition_no_lsp_service(self):
        from tools.lsp_mcp_runtime import _handle_goto_definition
        mock_lsp = MagicMock(get_service=lambda: None)
        with patch.dict("sys.modules", {"agent.lsp": mock_lsp}):
            result = json.loads(_handle_goto_definition({
                "path": "/tmp/x.py", "line": 1, "character": 1,
            }))
            assert "error" in result
