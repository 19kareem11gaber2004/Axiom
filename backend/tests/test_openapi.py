"""Tests for the generated OpenAPI schema.

The schema is not documentation about the API - it *is* the contract the
client sees. Swagger UI renders its fields from it, and the generated
cURL is built from it. So a requirement the server enforces but the
schema does not declare is invisible to the client: the caller cannot
supply it, and the only symptom is a 403 they cannot diagnose.

That is exactly what happened with the workspace header. It was read
imperatively from the raw request, so it worked at runtime and never
reached the schema. These tests guard the difference.
"""

import warnings

import pytest

from app.auth.dependencies import WORKSPACE_HEADER
from app.main import app

# Operations that resolve "the workspace this request acts in" from the
# X-Workspace-ID header.
HEADER_SCOPED = [
    ("/api/v1/auth/me", "get"),
    ("/api/v1/documents", "post"),
    ("/api/v1/documents", "get"),
    ("/api/v1/documents/{document_id}", "get"),
    ("/api/v1/documents/{document_id}/file", "get"),
    ("/api/v1/documents/{document_id}/retry", "post"),
    ("/api/v1/documents/{document_id}", "delete"),
]

# Operations that do NOT need the header: they either need only the
# identity, or the workspace is already named in the path.
PATH_OR_USER_SCOPED = [
    ("/api/v1/workspaces", "get"),
    ("/api/v1/workspaces", "post"),
    ("/api/v1/workspaces/{workspace_id}", "get"),
    ("/api/v1/workspaces/{workspace_id}/members", "post"),
]


@pytest.fixture(scope="module")
def schema():
    """The schema of the real application.

    Other test modules register throwaway routes on the shared app to
    exercise the error handlers, and those routes produce "duplicate
    operation id" warnings when the schema is generated. They are an
    artefact of the test suite, not of the application, so they are
    filtered here rather than left to clutter the run.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*Duplicate Operation ID.*")
        return app.openapi()


def _header_parameters(schema, path, method):
    operation = schema["paths"][path][method]
    return [p for p in operation.get("parameters", []) if p.get("in") == "header"]


# ---------------------------------------------------------------------------
# The header is declared
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,method", HEADER_SCOPED)
def test_the_workspace_header_is_documented(schema, path, method):
    """Swagger can only offer a field that the schema declares."""
    headers = _header_parameters(schema, path, method)
    assert headers, "%s %s declares no header parameter" % (method.upper(), path)
    assert WORKSPACE_HEADER in [h["name"] for h in headers]


def test_the_documented_name_matches_the_header_the_server_reads(schema):
    """One name, one meaning.

    The schema must document the exact header the server reads, or the
    client sends the right value under the wrong name.
    """
    headers = _header_parameters(schema, "/api/v1/documents", "post")
    assert len(headers) == 1
    assert headers[0]["name"] == WORKSPACE_HEADER


def test_the_header_is_optional(schema):
    """An account with exactly one workspace must not have to send it.

    The server selects the only membership automatically, so the header
    is optional in the contract even though it becomes necessary when
    the account belongs to several workspaces.
    """
    headers = _header_parameters(schema, "/api/v1/documents", "post")
    assert headers[0].get("required") is False


def test_the_header_is_described(schema):
    """A field nobody understands is a field nobody fills in."""
    headers = _header_parameters(schema, "/api/v1/documents", "post")
    description = headers[0].get("description", "")
    assert WORKSPACE_HEADER in description or "workspace" in description.lower()


# ---------------------------------------------------------------------------
# The header is only declared where it is needed
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,method", PATH_OR_USER_SCOPED)
def test_operations_that_do_not_need_the_header_do_not_declare_it(
    schema, path, method
):
    """A workspace named in the path must not also be chosen by a header."""
    headers = _header_parameters(schema, path, method)
    assert WORKSPACE_HEADER not in [h["name"] for h in headers]


def test_the_whole_schema_contains_exactly_one_workspace_header(schema):
    """No hidden duplicates, and no stray header parameters anywhere."""
    found = []
    for path, item in schema["paths"].items():
        for method, operation in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            for parameter in operation.get("parameters", []):
                if parameter.get("in") == "header":
                    found.append((method.upper(), path, parameter["name"]))
    assert all(name == WORKSPACE_HEADER for _, _, name in found)
    assert len(found) == len(HEADER_SCOPED)


def test_the_security_scheme_is_still_declared(schema):
    """The token requirement must not be lost by the change."""
    operation = schema["paths"]["/api/v1/documents"]["post"]
    assert operation["security"] == [{"HTTPBearer": []}]
    assert "HTTPBearer" in schema["components"]["securitySchemes"]


def test_the_upload_still_takes_a_multipart_file(schema):
    """The body contract is unchanged."""
    operation = schema["paths"]["/api/v1/documents"]["post"]
    content = operation["requestBody"]["content"]
    assert "multipart/form-data" in content
    assert "202" in operation["responses"]
