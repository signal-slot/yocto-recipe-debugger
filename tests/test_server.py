# Copyright (C) 2026 Signal Slot Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for turning backend results into LSP objects."""

from lsprotocol import types

from yocto_recipe_debugger.bitbake_process import BackendError
from yocto_recipe_debugger.server import (
    Analysis, error_diagnostic, hover_markdown, inlay_hints, lookup_variable,
    operator_of, to_diagnostics,
)

PATH = "/layer/foo.bb"
LINES = ['FOO = "a"', 'FOO:append:musl = " b"', 'BAR = "x \\', '    y"']

RESULT = {
    "file": PATH,
    "recipe": PATH,
    "variants": ["target", "native"],
    "diagnostics": [
        {"file": PATH, "line": 2, "end_line": 2, "code": "inactive-override",
         "severity": "unused", "message": "Not applied"},
        {"file": PATH, "line": 3, "end_line": 4, "code": "missing-space",
         "severity": "warning", "message": "joins"},
    ],
    "variables": {
        "FOO": {"value": "a", "variants": {"native": "a"}, "history": [
            {"variable": "FOO", "file": PATH, "line": 1, "op": "set", "detail": "a",
             "status": "applied"},
            {"variable": "FOO", "file": PATH, "line": 2, "op": ":append[musl]", "detail": " b",
             "status": "inactive-override"},
        ]},
        "BAR": {"value": "x y", "variants": {"native": "z"}, "history": [
            {"variable": "BAR", "file": PATH, "line": 4, "op": "set", "detail": "x y",
             "status": "applied"},
        ]},
    },
}


def analysis():
    return Analysis(path=PATH, lines=list(LINES), result=RESULT)


def test_diagnostics_severity_tags_and_range():
    unused, warning = to_diagnostics(analysis())
    assert unused.severity == types.DiagnosticSeverity.Hint
    assert unused.tags == [types.DiagnosticTag.Unnecessary]
    assert warning.severity == types.DiagnosticSeverity.Warning
    assert warning.tags is None
    assert (warning.range.start.line, warning.range.end.line) == (2, 3)
    assert warning.range.end.character == len(LINES[3])


def test_error_in_this_file_points_to_line():
    diag = error_diagnostic(PATH, LINES, BackendError("bad", PATH, 3))
    assert diag.severity == types.DiagnosticSeverity.Error
    assert diag.range.start.line == 2


def test_error_elsewhere_is_information_on_first_line():
    diag = error_diagnostic(PATH, LINES, BackendError("skipped"))
    assert diag.severity == types.DiagnosticSeverity.Information
    assert diag.range.start.line == 0


def test_lookup_strips_override_suffixes():
    variables = {"SRC_URI": {}, "RDEPENDS:foo-dev": {}}
    assert lookup_variable(variables, "SRC_URI:append:arm") == "SRC_URI"
    assert lookup_variable(variables, "RDEPENDS:foo-dev:append") == "RDEPENDS:foo-dev"
    assert lookup_variable(variables, "OTHER") is None


def test_operator_names():
    assert operator_of({"op": "set", "flag": "_defaultval"}) == "??="
    assert operator_of({"op": "set?"}) == "?="
    assert operator_of({"op": ":append[arm]"}) == ":append:arm"
    assert operator_of({"op": ":remove"}) == ":remove"


def test_hover_shows_value_variants_and_status():
    text = hover_markdown(RESULT, "BAR")
    assert "x y" in text
    assert "native:" in text and "z" in text
    text = hover_markdown(RESULT, "FOO")
    assert "native:" not in text  # same value as target
    assert "✗ `FOO :append:musl`" in text
    assert "not applied (override not active)" in text
    assert "(file:///layer/foo.bb#L2)" in text


def test_inlay_hint_on_last_assignment_line():
    hints = inlay_hints(analysis(), list(LINES), 0, 10)
    assert [(h.position.line, h.label) for h in hints] == [(1, "⇒ a"), (3, "⇒ x y")]


def test_inlay_hint_hidden_when_line_was_edited():
    edited = list(LINES)
    edited[1] = 'FOO:append = " c"'
    hints = inlay_hints(analysis(), edited, 0, 10)
    assert [h.position.line for h in hints] == [3]


def test_no_inlay_hint_for_functions():
    result = dict(RESULT, variables={"do_install": {"value": "base_do_install", "history": [
        {"variable": "do_install", "file": PATH, "line": 1, "op": ":append",
         "status": "applied", "function": True}]}})
    assert inlay_hints(Analysis(PATH, list(LINES), result), list(LINES), 0, 10) == []


def test_hover_on_assignment_name_uses_recorded_assignment():
    lines = ['FILES:${PN}-doc = "/usr/share/doc"', 'X = "${FILES}"']
    result = {"variables": {
        # The variable itself is listed first, so a naive search finds it.
        "FILES": {"value": "", "history": [
            {"variable": "FILES:foo-doc", "file": PATH, "line": 1, "op": "set"}]},
        "FILES:foo-doc": {"value": "/usr/share/doc", "history": [
            {"variable": "FILES:foo-doc", "file": PATH, "line": 1, "op": "set"}]},
    }}
    a = Analysis(PATH, lines, result)
    from yocto_recipe_debugger.server import variable_assigned_at
    assert variable_assigned_at(a, 0, 3) == "FILES:foo-doc"
    # In the value part, the word under the cursor decides instead.
    assert variable_assigned_at(a, 0, 25) is None
    assert variable_assigned_at(a, 1, 8) is None
