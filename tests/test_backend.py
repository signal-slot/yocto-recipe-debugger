"""Unit tests for the analysis rules (no BitBake needed).

The event dictionaries have the same shape as bb.data_smart.VariableHistory
records.  The integration tests check the same rules against real BitBake.
"""

from yocto_recipe_debugger import backend
from yocto_recipe_debugger.backend import (
    VariantAnalysis, function_event, merge_variants, resolve_override,
    statement_end, statement_start,
)

F = "/layer/recipe.bb"
OVERRIDES = ["linux", "arm", "pn-foo", "class-target", "forcevariable"]


def ev(var, line, op="set", detail="", flag=None, file=F, parsing=True):
    e = {"variable": var, "file": file, "line": line, "op": op, "detail": detail}
    if flag:
        e["flag"] = flag
    if parsing:
        e["parsing"] = True
    return e


def analyze(histories, values=None, overrides=OVERRIDES, packages=()):
    values = values or {}
    return VariantAnalysis(histories, overrides, packages, [],
                           expand=lambda text: text,
                           value_of=lambda name: values.get(name)).run()


def codes(analysis, event):
    return [f.code for f in analysis.findings.get(backend.event_key(event), [])]


# -- override resolution ------------------------------------------------------

def test_resolve_override_last_in_overrides_wins():
    candidates = {"pn-foo": "X:pn-foo", "class-target": "X:class-target"}
    assert resolve_override("X", candidates, OVERRIDES) == "X:class-target"


def test_resolve_override_needs_every_part_active():
    assert resolve_override("X", {"arm:musl": "X:arm:musl"}, OVERRIDES) is None
    assert resolve_override("X", {"arm:linux": "X:arm:linux"}, OVERRIDES) == "X:arm:linux"


# -- keyword operations -------------------------------------------------------

def test_inactive_append_override():
    e = ev("X", 2, ":append[libc-musl]", " musl", parsing=False)
    a = analyze({"X": [ev("X", 1, detail="a"), e]}, {"X": "a"})
    assert codes(a, e) == ["inactive-override"]


def test_active_append_has_no_finding():
    e = ev("X", 2, ":append[arm]", " b", parsing=False)
    a = analyze({"X": [ev("X", 1, detail="a"), e]}, {"X": "a b"})
    assert codes(a, e) == []


def test_missing_leading_space():
    e = ev("X", 2, ":append", "c", parsing=False)
    a = analyze({"X": [ev("X", 1, detail="a b"), e]}, {"X": "a bc"})
    assert codes(a, e) == ["missing-space"]


def test_separator_is_not_missing_space():
    e = ev("X", 2, ":append", ",virgl", parsing=False)
    a = analyze({"X": [ev("X", 1, detail="swrast"), e]}, {"X": "swrast,virgl"})
    assert codes(a, e) == []


def test_missing_space_uses_expanded_text():
    # "${@...}" starts with "$" but expands to ",virgl".
    e = ev("X", 2, ":append", "${@'%s' % ',virgl'}", parsing=False)
    a = VariantAnalysis({"X": [ev("X", 1, detail="swrast"), e]}, OVERRIDES, [], [],
                        expand=lambda text: ",virgl" if text.startswith("${@") else text,
                        value_of=lambda name: "swrast,virgl").run()
    assert codes(a, e) == []


def test_missing_space_ignores_functions():
    e = ev("do_install", 5, ":append", "echo hi", parsing=False)
    e["function"] = True
    a = analyze({"do_install": [ev("do_install", 1, detail="true"), e]},
                {"do_install": "trueecho hi"})
    assert codes(a, e) == []


def test_removed_append():
    add = ev("X", 2, ":append", " drop", parsing=False)
    rm = ev("X", 3, ":remove", "drop", parsing=False)
    a = analyze({"X": [ev("X", 1, detail="keep"), add, rm]}, {"X": "keep"})
    assert codes(a, add) == ["removed"]


# -- content operations -------------------------------------------------------

def test_set_twice_overwrites_first():
    first, second = ev("X", 1, detail="one"), ev("X", 2, detail="two")
    a = analyze({"X": [first, second]}, {"X": "two"})
    assert codes(a, first) == ["overwritten"]
    assert codes(a, second) == []


def test_set_if_unset_after_set():
    q = ev("X", 2, "set?", "second")
    a = analyze({"X": [ev("X", 1, detail="first"), q]}, {"X": "first"})
    assert codes(a, q) == ["already-set"]


def test_weak_default_discarded_by_plus_equals():
    weak = ev("X", 1, "set", "default", flag="_defaultval")
    add = ev("X", 2, "append", "extra")
    a = analyze({"X": [weak, add]}, {"X": " extra"})
    assert codes(a, weak) == ["default-discarded"]
    assert codes(a, add) == ["discards-default"]


def test_weak_default_unused_after_set():
    weak = ev("X", 1, "set", "default", flag="_defaultval")
    a = analyze({"X": [weak, ev("X", 2, detail="real")]}, {"X": "real"})
    assert codes(a, weak) == ["default-unused"]


def test_weak_default_used_when_nothing_else():
    weak = ev("X", 1, "set", "default", flag="_defaultval")
    a = analyze({"X": [weak]}, {"X": "default"})
    assert codes(a, weak) == []


def test_base_replaced_by_active_override():
    base = ev("X", 1, detail="base")
    over = ev("X:class-target", 2, detail="target")
    a = analyze({"X": [base], "X:class-target": [over]}, {"X": "target"})
    assert codes(a, base) == ["replaced-by-override"]
    assert codes(a, over) == []


def test_base_append_survives_override():
    append = ev("X", 2, ":append", " more", parsing=False)
    over = ev("X:class-target", 3, detail="target")
    a = analyze({"X": [ev("X", 1, detail="base"), append], "X:class-target": [over]},
                {"X": "target more"})
    assert codes(a, append) == []


def test_shadowed_override():
    pn = ev("X:pn-foo", 1, detail="pn")
    cls = ev("X:class-target", 2, detail="class")
    a = analyze({"X:pn-foo": [pn], "X:class-target": [cls]}, {"X": "class"})
    assert codes(a, pn) == ["shadowed-override"]
    assert codes(a, cls) == []


def test_inactive_override_assignment():
    e = ev("X:qemux86", 1, detail="x86")
    a = analyze({"X:qemux86": [e]}, {"X": None})
    assert codes(a, e) == ["inactive-override"]


def test_package_suffix_is_not_an_override():
    e = ev("RDEPENDS:foo-extra", 1, detail="bash")
    a = analyze({"RDEPENDS:foo-extra": [e]}, {"RDEPENDS:foo-extra": "bash"},
                packages=["foo", "foo-extra"])
    assert codes(a, e) == []


def test_override_on_package_variable():
    e = ev("RDEPENDS:foo-extra", 1, ":append[qemux86]", " x", parsing=False)
    a = analyze({"RDEPENDS:foo-extra": [ev("RDEPENDS:foo-extra", 0, detail="bash"), e]},
                {"RDEPENDS:foo-extra": "bash"}, packages=["foo-extra"])
    assert codes(a, e) == ["inactive-override"]


def test_overwritten_by_python_code_is_named():
    static = ev("FILES:foo-doc", 10, detail="/usr/share/doc")
    python = ev("FILES:foo-doc", 3, detail="", parsing=False)
    a = analyze({"FILES:foo-doc": [static, python]}, {"FILES:foo-doc": ""},
                packages=["foo-doc"])
    (finding,) = a.findings[backend.event_key(static)]
    assert finding.code == "overwritten"
    assert "(Python code)" in finding.message


# -- functions ----------------------------------------------------------------

def test_function_event_from_flags():
    owner, event = function_event("do_install:append:libc-musl",
                                  {"func": 1, "filename": F, "lineno": "12"})
    assert owner == "do_install"
    assert event["op"] == ":append[libc-musl]"
    assert event["line"] == 12 and event["line_is_start"]


def test_function_event_ignores_autogenerated():
    assert function_event("do_install", {"func": 1, "filename": "autogenerated",
                                         "lineno": 1}) is None


# -- variants -----------------------------------------------------------------

def test_merge_reports_only_when_unused_in_every_variant():
    native_append = ev("X", 2, ":append[class-native]", " n", parsing=False)
    hist_target = {"X": [ev("X", 1, detail="x"), native_append]}
    target = analyze(hist_target, {"X": "x"})
    hist_native = {"X": [ev("X", 1, detail="x"), dict(native_append)]}
    native = analyze(hist_native, {"X": "x n"},
                     overrides=["linux", "class-native", "forcevariable"])
    diags = merge_variants([("", target, hist_target), ("native", native, hist_native)], {F})
    assert [d for d in diags if d["line"] == 2] == []


def test_merge_names_variants_when_unused_everywhere():
    sdk_append = ev("X", 2, ":append[class-nativesdk]", " n", parsing=False)
    hist = {"X": [ev("X", 1, detail="x"), sdk_append]}
    target = analyze(hist, {"X": "x"})
    native = analyze({"X": [dict(e) for e in hist["X"]]}, {"X": "x"},
                     overrides=["linux", "class-native"])
    diags = merge_variants([("", target, hist), ("native", native, hist)], {F})
    (diag,) = [d for d in diags if d["line"] == 2]
    assert diag["code"] == "inactive-override"
    assert diag["message"].endswith("[target, native]")


# -- source helpers -----------------------------------------------------------

def test_statement_start_of_continued_value():
    lines = ['X = "a \\', '    b \\', '    c"', 'Y = "1"']
    assert statement_start(lines, 3) == 1
    assert statement_start(lines, 4) == 4


def test_statement_start_and_end_of_function():
    lines = ["do_install:append() {", "    echo hi", "}", ""]
    assert statement_start(lines, 3) == 1
    assert statement_end(lines, 1) == 3


def test_weak_default_is_not_called_python_code():
    weak = ev("X", 1, "set", "default", flag="_defaultval", parsing=False)
    assert "(Python code)" not in backend.where(weak)
    assert "(Python code)" in backend.where(ev("X", 3, parsing=False))
