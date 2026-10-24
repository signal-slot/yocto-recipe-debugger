"""BitBake-side backend of yocto-recipe-debugger.

This file runs inside the BitBake build environment (after
oe-init-build-env), often in a container, so it must:

  * use only the Python standard library and BitBake itself,
  * stay compatible with Python 3.6 (the oldest Python supported by
    BitBake releases we target),
  * be a single self-contained file (the language server sends its source
    as ``python3 -c <source>``, so nothing needs to be mounted).

Protocol: one JSON object per line on stdin (request) and stdout (response).

  request:  {"id": 1, "method": "analyze", "params": {"file": "/abs/x.bb"}}
  response: {"id": 1, "result": {...}}  or  {"id": 1, "error": {...}}

The analysis does not re-implement BitBake parsing.  BitBake parses the
recipe with variable history tracking enabled, and this file only reads
the recorded history and decides, for every recorded operation, whether it
has an effect on the final value.  The rules below follow
bb.data_smart.DataSmart.getVarFlag() and bb.parse.ast.DataNode.eval().
"""

import json
import os
import re
import sys
import traceback

# Operations that change the base content of a variable, in the order they
# are evaluated while parsing (bb/parse/ast.py: DataNode.eval).
OP_SET = ("set", "immediate")           # "=", ":="
OP_SET_IF_UNSET = "set?"                # "?="
OP_ADD = ("append", "prepend", "postdot", "predot")  # "+=", "=+", ".=", "=."
OP_DELETE = "del"                       # unset
# "??=" is recorded as op "set" with flag "_defaultval".
FLAG_DEFAULT = "_defaultval"

# ":append", ":append[override]", ...  (bb/data_smart.py: setVar)
KEYWORD_OP_RE = re.compile(r"^:(append|prepend|remove)(?:\[(.*)\])?$")
# Splits "do_install:append:arm" (bb/data_smart.py: __setvar_regexp__).
SETVAR_RE = re.compile(r"(?P<base>.*?)(?P<keyword>:append|:prepend|:remove)(:(?P<add>[^A-Z]*))?$")
# BitBake treats a variable name suffix as an override when it starts with
# a lower case letter or a digit (data_smart.__override_regexp__).
OVERRIDE_RE = re.compile(r"[a-z0-9]+")

# Characters that separate items on purpose.  An append that starts with
# one of them (for example FILESEXTRAPATHS:prepend = "${THISDIR}/files:")
# is not a missing-space mistake.
SEPARATORS = ":,;"

SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
# "unused" findings describe lines that have no effect.  The editor shows
# them faded, like dead code.
SEVERITY_UNUSED = "unused"

MAX_VALUE_LENGTH = 20000
MAX_DETAIL_LENGTH = 2000

# How long to wait for another BitBake in the build directory to end.
BUSY_WAIT_SECONDS = 20


# ---------------------------------------------------------------------------
# Pure analysis (no BitBake import; unit tested on the host)
# ---------------------------------------------------------------------------

class Finding(object):
    """One statement about one history event in one recipe variant."""

    __slots__ = ("code", "severity", "message", "effective")

    def __init__(self, code, severity, message, effective):
        self.code = code
        self.severity = severity
        self.message = message
        # False when the finding means "this operation has no effect".
        self.effective = effective


def event_key(event):
    """Identify an event across recipe variants.

    The variable name itself can differ between variants
    (RDEPENDS:${PN} becomes RDEPENDS:foo or RDEPENDS:foo-native), but the
    file, line, operation and flag do not.
    """
    return (event.get("file"), event.get("line"), event.get("op"),
            event.get("flag") or "")


def where(event):
    text = "%s:%s" % (os.path.basename(event.get("file") or "?"), event.get("line", "?"))
    # The parser marks its own assignments with "parsing"; a plain "set"
    # without it comes from Python code such as an anonymous function.
    # Flags ("??=" is the flag _defaultval) never carry "parsing".
    if (event.get("op") in OP_SET and not event.get("parsing")
            and not event.get("flag") and not event.get("function")):
        text += " (Python code)"
    return text


def override_active(override, overrides):
    """True when every part of "a:b" is in OVERRIDES."""
    return all(part in overrides for part in override.split(":"))


def resolve_override(key, candidates, overrides):
    """Return the override key whose value replaces ``key``, or None.

    ``candidates`` maps an override string ("arm", "a:b") to the variable
    name that carries it ("FOO:arm").  This is a direct port of the
    selection loop in bb.data_smart.DataSmart.getVarFlag(), including its
    use of str.replace(), so that the result matches BitBake exactly.
    """
    overrides_set = set(overrides)
    active = {}
    for override, name in candidates.items():
        if override in overrides_set:
            active[override] = name
        elif ":" in override and set(override.split(":")).issubset(overrides_set):
            active[override] = name
    match = None
    modified = True
    while modified:
        modified = False
        for override in overrides:
            for a in list(active):
                if a.endswith(":" + override):
                    name = active.pop(a)
                    active[a.replace(":" + override, "")] = name
                    modified = True
                elif a == override:
                    match = active.pop(a)
    return match


class VariantAnalysis(object):
    """Classify every history event of one parsed recipe variant.

    Inputs are plain data so that this class can be tested without BitBake:

      histories:  {variable name: [event dict, ...]} from d.varhistory
      overrides:  OVERRIDES split on ":" (order matters)
      packages:   names in PACKAGES
      dynamic:    compiled regular expressions from PACKAGES_DYNAMIC
      expand:     function(str) -> str, d.expand
      value_of:   function(variable name) -> expanded value or None
    """

    def __init__(self, histories, overrides, packages, dynamic, expand, value_of):
        self.histories = histories
        self.overrides = list(overrides)
        self.overrides_set = set(overrides)
        self.packages = set(packages)
        self.dynamic = list(dynamic)
        self.expand = expand
        self.value_of = value_of
        # event key -> [Finding]
        self.findings = {}
        # event key -> variable name that the event belongs to
        self.owner = {}

    # -- helpers ---------------------------------------------------------

    def add(self, event, finding):
        self.findings.setdefault(event_key(event), []).append(finding)

    def is_deferred(self, part):
        """Override parts that only become active later (packaging, tasks)."""
        if part.startswith("task-"):
            return True
        if part in self.packages:
            return True
        return any(p.match(part) for p in self.dynamic)

    def split_scope(self, name):
        """Split "RDEPENDS:foo-dev:arm" into ("RDEPENDS:foo-dev", "arm").

        The scope is the part of the name that BitBake resolves at parse
        time.  Package and task suffixes belong to the scope because they
        only become overrides during packaging or task execution.
        """
        parts = name.split(":")
        if len(parts) == 1:
            return name, ""
        if not all(OVERRIDE_RE.match(p) for p in parts[1:]):
            return name, ""
        last_deferred = 0
        for i in range(1, len(parts)):
            if self.is_deferred(parts[i]):
                last_deferred = i
        scope = ":".join(parts[:last_deferred + 1])
        return scope, ":".join(parts[last_deferred + 1:])

    def safe_expand(self, text):
        try:
            return self.expand(text)
        except Exception:
            return None

    # -- main ------------------------------------------------------------

    def run(self):
        groups = {}
        for name in self.histories:
            scope, override = self.split_scope(name)
            groups.setdefault(scope, {})[override] = name
        for scope, members in groups.items():
            self.analyze_scope(scope, members)
        return self

    def analyze_scope(self, scope, members):
        """``members`` maps override string ("" for the scope itself) to name."""
        candidates = dict((o, n) for o, n in members.items() if o)

        # The chain of names that BitBake walks to find the value.  Each
        # step replaces the content of the previous one; the last one
        # provides the content.  Keyword operations (:append ...) of every
        # step are still applied.
        chain = [scope]
        while True:
            current = chain[-1]
            # Overrides of "current" are the members whose name extends it.
            prefix = current[len(scope):]  # "" or ":a"
            sub = {}
            for o, n in candidates.items():
                full = ":" + o
                if prefix and not full.startswith(prefix + ":"):
                    continue
                rest = full[len(prefix) + 1:]
                if rest:
                    sub[rest] = n
            nxt = resolve_override(current, sub, self.overrides)
            if not nxt or nxt in chain:
                break
            chain.append(nxt)
        provider = chain[-1]

        for override, name in members.items():
            events = self.histories.get(name) or []
            for event in events:
                self.owner[event_key(event)] = name
            if override and not override_active(override, self.overrides_set):
                missing = [p for p in override.split(":") if p not in self.overrides_set]
                for event in events:
                    if self.is_value_event(event):
                        self.add(event, Finding(
                            "inactive-override", SEVERITY_UNUSED,
                            "Not applied: override '%s' is not in OVERRIDES."
                            % ":".join(missing), False))
                continue
            if name not in chain:
                provider_event = self.first_content_event(provider)
                for event in events:
                    if self.is_value_event(event):
                        self.add(event, Finding(
                            "shadowed-override", SEVERITY_UNUSED,
                            "Not applied: %s takes precedence over %s%s."
                            % (provider, name, self.at(provider_event)), False))
                continue
            self.analyze_content(name, events, replaced_by=(None if name == provider else provider))
            self.analyze_keyword_ops(name, events, value_name=scope)

    def at(self, event):
        return (" (%s)" % where(event)) if event else ""

    def first_content_event(self, name):
        for event in self.histories.get(name) or []:
            if self.is_content_event(event):
                return event
        return None

    @staticmethod
    def is_content_event(event):
        flag = event.get("flag")
        if flag and flag != FLAG_DEFAULT:
            return False
        op = event.get("op")
        return op in OP_SET or op == OP_SET_IF_UNSET or op in OP_ADD

    @staticmethod
    def is_value_event(event):
        if VariantAnalysis.is_content_event(event):
            return True
        return bool(not event.get("flag") and KEYWORD_OP_RE.match(event.get("op") or ""))

    def analyze_content(self, name, events, replaced_by):
        """Follow "=", "?=", "+=", ... in parse order (DataNode.eval)."""
        contributors = []   # events that make up the current content
        defaults = []       # "??=" events
        has_content = False
        for event in events:
            flag = event.get("flag")
            op = event.get("op")
            if flag == FLAG_DEFAULT:
                defaults.append(event)
                continue
            if flag:
                continue
            if op in OP_SET:
                for old in contributors:
                    self.add(old, Finding(
                        "overwritten", SEVERITY_UNUSED,
                        "No effect: %s is set again at %s." % (name, where(event)), False))
                contributors = [event]
                has_content = True
            elif op == OP_SET_IF_UNSET:
                if has_content:
                    self.add(event, Finding(
                        "already-set", SEVERITY_UNUSED,
                        "No effect: %s already has a value from %s."
                        % (name, where(contributors[-1])), False))
                else:
                    contributors = [event]
                    has_content = True
            elif op in OP_ADD:
                contributors.append(event)
                has_content = True
            elif op == OP_DELETE:
                for old in contributors:
                    self.add(old, Finding(
                        "overwritten", SEVERITY_UNUSED,
                        "No effect: %s is deleted at %s." % (name, where(event)), False))
                contributors = []
                has_content = False

        if has_content:
            only_added = all(e.get("op") in OP_ADD for e in contributors)
            for event in defaults:
                if only_added:
                    self.add(event, Finding(
                        "default-discarded", SEVERITY_UNUSED,
                        "No effect: the weak default is discarded by %s at %s."
                        % (contributors[0].get("op"), where(contributors[0])), False))
                else:
                    self.add(event, Finding(
                        "default-unused", SEVERITY_UNUSED,
                        "No effect: %s has a value from %s, so the weak default is not used."
                        % (name, where(contributors[-1])), False))
            if only_added and defaults:
                # "FOO ??= a" + "FOO += b" gives " b", not "a b".
                first = contributors[0]
                self.add(first, Finding(
                    "discards-default", SEVERITY_WARNING,
                    "This operation discards the weak default (??=) at %s. "
                    "%s becomes '%s'. Use %s:%s instead."
                    % (where(defaults[-1]), name, (self.value_of(name) or "").strip(),
                       name, "prepend" if first.get("op") in ("prepend", "predot") else "append"),
                    True))
        else:
            for old in defaults[:-1]:
                self.add(old, Finding(
                    "overwritten", SEVERITY_UNUSED,
                    "No effect: the weak default is set again at %s." % where(defaults[-1]), False))
            if defaults:
                contributors = [defaults[-1]]

        if replaced_by:
            replacer = self.first_content_event(replaced_by)
            for event in contributors:
                self.add(event, Finding(
                    "replaced-by-override", SEVERITY_UNUSED,
                    "No effect: %s replaces this value%s."
                    % (replaced_by, self.at(replacer)), False))

    def analyze_keyword_ops(self, name, events, value_name):
        """Check :append / :prepend / :remove of a name that is in the chain."""
        removes = []
        for event in events:
            if event.get("flag"):
                continue
            m = KEYWORD_OP_RE.match(event.get("op") or "")
            if not m:
                continue
            override = m.group(2)
            if override and not override_active(override, self.overrides_set):
                missing = [p for p in override.split(":") if p not in self.overrides_set]
                self.add(event, Finding(
                    "inactive-override", SEVERITY_UNUSED,
                    "Not applied: override '%s' is not in OVERRIDES." % ":".join(missing),
                    False))
                continue
            if m.group(1) == "remove":
                removes.append(event)

        if self.is_function(name):
            return
        value = self.value_of(value_name)
        if value is None:
            return
        final_items = set(value.split())
        for event in events:
            if event.get("flag"):
                continue
            if any(not f.effective for f in self.findings.get(event_key(event), [])):
                continue
            op = event.get("op") or ""
            m = KEYWORD_OP_RE.match(op)
            if m and m.group(1) in ("append", "prepend"):
                self.check_missing_space(event, m.group(1), value, value_name)
            if (m and m.group(1) in ("append", "prepend")) or op in OP_ADD or op in OP_SET:
                self.check_removed(event, final_items, removes)

    def is_function(self, name):
        return any(event.get("function") for event in self.histories.get(name) or [])

    def check_missing_space(self, event, keyword, value, value_name):
        detail = event.get("detail") or ""
        if not detail:
            return
        # Check the expanded text: "${@... ',virgl' ...}" starts with "$"
        # but expands to ",virgl", which is a separator on purpose.
        text = self.safe_expand(detail)
        if not text:
            return
        edge = text[0] if keyword == "append" else text[-1]
        if edge.isspace() or edge in SEPARATORS:
            return
        if keyword == "append":
            i = value.rfind(text)
            if i <= 0:
                return
            neighbour = value[i - 1]
            start, end = i, i + len(text)
        else:
            i = value.find(text)
            if i < 0 or i + len(text) >= len(value):
                return
            neighbour = value[i + len(text)]
            start, end = i, i + len(text)
        if neighbour.isspace() or neighbour in SEPARATORS:
            return
        context = value[max(0, start - 20):min(len(value), end + 20)]
        self.add(event, Finding(
            "missing-space", SEVERITY_WARNING,
            "%s without a %s space joins two words: %s = '...%s...'"
            % (":" + keyword, "leading" if keyword == "append" else "trailing",
               value_name, context.strip()),
            True))

    def check_removed(self, event, final_items, removes):
        if not removes:
            return
        text = self.safe_expand(event.get("detail") or "")
        if not text:
            return
        items = text.split()
        if not items or any(item in final_items for item in items):
            return
        remover = None
        for r in removes:
            removed = (self.safe_expand(r.get("detail") or "") or "").split()
            if all(item in removed for item in items):
                remover = r
                break
        if remover is None:
            return
        self.add(event, Finding(
            "removed", SEVERITY_INFO,
            "Everything added here is removed by :remove at %s." % where(remover),
            False))


def function_event(name, flags):
    """Build a history event for a shell or Python function definition.

    BitBake does not record function definitions in the variable history,
    but it keeps "filename" and "lineno" (the first line) as flags.
    Returns (variable the event belongs to, event) or None.
    """
    filename = flags.get("filename")
    lineno = flags.get("lineno")
    if not flags.get("func") or not filename or filename == "autogenerated" or not lineno:
        return None
    try:
        line = int(lineno)
    except (TypeError, ValueError):
        return None
    event = {"variable": name, "file": filename, "line": line, "detail": "",
             "function": True, "line_is_start": True}
    m = SETVAR_RE.match(name)
    if m:
        override = m.group("add")
        event["op"] = "%s[%s]" % (m.group("keyword"), override) if override else m.group("keyword")
        return m.group("base"), event
    event["op"] = "set"
    return name, event


def is_internal(event):
    """Events recorded by BitBake's own Python code (for example ast.py
    rewriting BBCLASSEXTEND) are not something a recipe author can change."""
    return (event.get("file") or "").endswith(".py")


def merge_variants(per_variant, target_files):
    """Combine findings of all variants into diagnostics for target files.

    ``per_variant`` is a list of (variant name, VariantAnalysis, histories).
    An operation is reported as having no effect only when it has no effect
    in every variant; ":append:class-native" is fine when the recipe has a
    native variant.
    """
    seen = {}      # event key -> event
    results = {}   # event key -> [(variant, [Finding])]
    for variant, analysis, histories in per_variant:
        for name, events in histories.items():
            for event in events:
                if event.get("file") not in target_files:
                    continue
                key = event_key(event)
                seen.setdefault(key, event)
                results.setdefault(key, []).append(
                    (variant, analysis.findings.get(key, [])))

    diagnostics = []
    for key, variant_results in results.items():
        event = seen[key]
        names = [v or "target" for v, _ in variant_results]
        unused_everywhere = all(any(not f.effective for f in fs) for _, fs in variant_results)
        reported = set()
        for variant, findings in variant_results:
            for f in findings:
                if not f.effective and not unused_everywhere:
                    continue
                if f.code in reported:
                    continue
                reported.add(f.code)
                variants = [v or "target" for v, fs in variant_results
                            if any(x.code == f.code for x in fs)]
                message = f.message
                if len(names) > 1:
                    message += " [%s]" % ", ".join(variants)
                diagnostics.append({
                    "file": event.get("file"),
                    "line": event.get("line"),
                    "line_is_start": bool(event.get("line_is_start")),
                    "code": f.code,
                    "severity": f.severity,
                    "message": message,
                })
    diagnostics.sort(key=lambda d: (d["file"], d["line"] or 0, d["code"]))
    return diagnostics


def status_of(findings):
    for f in findings:
        if not f.effective:
            return f.code
    return "applied"


# ---------------------------------------------------------------------------
# Source helpers
# ---------------------------------------------------------------------------

FUNCTION_START_RE = re.compile(
    r"^\s*(?:(?:python|fakeroot)\s+)*[^\s=]*\s*\(\s*\)\s*\{\s*$")
VAR_REF_RE = re.compile(r"\$\{([A-Za-z0-9_\-+./:~@%]+)\}")
ASSIGN_NAME_RE = re.compile(
    r"^\s*(?:export\s+)?([A-Za-z0-9_\-+.${}/:~@%]+?)(?:\[[^\]]*\])?\s*"
    r"(?:\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=)")


def statement_start(lines, line):
    """BitBake records the last line of a statement; return its first line.

    ``lines`` is the file content split into lines, ``line`` is 1-based.
    """
    if line < 1 or line > len(lines):
        return line
    if lines[line - 1].strip() == "}":
        i = line - 1
        while i >= 1:
            if FUNCTION_START_RE.match(lines[i - 1]):
                return i
            if lines[i - 1].strip() == "}":
                break
            i -= 1
        return line
    start = line
    while start > 1 and lines[start - 2].rstrip("\n").endswith("\\"):
        start -= 1
    return start


def statement_end(lines, line):
    """Last line of a function block that starts at ``line`` (1-based)."""
    if line < 1 or line > len(lines) or not FUNCTION_START_RE.match(lines[line - 1]):
        return line
    for i in range(line, len(lines)):
        if lines[i].rstrip() == "}":
            return i + 1
    return line


def referenced_names(text):
    """Variable names that the file assigns or references."""
    names = set(VAR_REF_RE.findall(text))
    for line in text.splitlines():
        m = ASSIGN_NAME_RE.match(line)
        if m and "$" not in m.group(1):
            names.add(m.group(1).split(":")[0])
    return names


# ---------------------------------------------------------------------------
# BitBake side
# ---------------------------------------------------------------------------

class BackendError(Exception):
    def __init__(self, message, file=None, line=None, code=None):
        Exception.__init__(self, message)
        self.file = file
        self.line = line
        # "busy": another BitBake uses the build directory; try again later.
        self.code = code


_realpaths = {}


def realpath(path):
    """os.path.realpath() with a cache; histories repeat the same files."""
    if path not in _realpaths:
        _realpaths[path] = os.path.realpath(path)
    return _realpaths[path]


def clip(text, limit):
    if text is None:
        return None
    text = str(text)
    if len(text) > limit:
        return text[:limit] + "\n... (%d more characters)" % (len(text) - limit)
    return text


def add_layer_libs(bbpath):
    """Make the "oe" package (and other layer lib/ dirs) importable.

    Values returned by the BitBake server are unpickled here, and some of
    them are instances of classes from meta/lib/oe.  BitBake does the same
    in base.bbclass: oe_import().
    """
    libs = [os.path.join(d, "lib") for d in bbpath.split(":") if d]
    sys.path[0:0] = [d for d in libs if d not in sys.path and os.path.isdir(d)]


class Backend(object):
    def __init__(self, out):
        self.out = out

    @staticmethod
    def lock_path():
        import bb.cookerdata
        topdir = bb.cookerdata.findTopdir()
        if not topdir:
            raise BackendError("Cannot find the build directory (conf/bblayers.conf).")
        return os.path.join(topdir, "bitbake.lock")

    @staticmethod
    def try_lock(path):
        """True when nobody holds the lock (it is released again at once)."""
        import bb.utils
        lock = bb.utils.lockfile(path, False, False)
        if lock is None:
            return False
        bb.utils.unlockfile(lock)
        return True

    def wait_until_free(self, timeout):
        """Wait until nobody holds bitbake.lock; False on timeout."""
        import time
        path = self.lock_path()
        deadline = time.time() + timeout
        while not self.try_lock(path):
            if time.time() >= deadline:
                return False
            time.sleep(0.05)
        return True

    def wait_for_server_exit(self):
        """Tinfoil.shutdown() returns before the BitBake server exits.

        Wait until our server releases bitbake.lock, so that the next
        analysis does not take it for a BitBake started by the user.
        """
        self.wait_until_free(30)

    def check_not_busy(self):
        """Do not join a BitBake that is already running in this build.

        Tinfoil would connect to the running server (bitbake.sock).  That
        disturbs the user's build, and fails when the server runs in another
        container.  Test the lock the same way bb.main.lockBitbake() does.

        Short commands, such as the "bitbake -e" that other editor
        extensions run on save, end in a few seconds, so wait a little
        before giving up.
        """
        if not self.wait_until_free(BUSY_WAIT_SECONDS):
            raise BackendError("BitBake is running in this build directory. "
                               "The file is analyzed again when it is free.", code="busy")

    def open_tinfoil(self):
        import bb.tinfoil
        self.check_not_busy()
        tinfoil = bb.tinfoil.Tinfoil(tracking=True, output=sys.stderr)
        try:
            tinfoil.prepare(config_only=False, quiet=2)
            add_layer_libs(tinfoil.config_data.getVar("BBPATH") or "")
        except BaseException:
            tinfoil.shutdown()
            raise
        return tinfoil

    # -- recipe lookup ---------------------------------------------------

    def recipes_for(self, tinfoil, path):
        """Recipe files whose parse includes ``path``, best match first."""
        if path.endswith(".bb"):
            return [path]
        if path.endswith(".bbappend"):
            for preferred in (True, False):
                found = []
                for fn in tinfoil.all_recipe_files(variants=False, preferred_only=preferred):
                    appends = tinfoil.get_file_appends(fn)
                    if path in [os.path.realpath(a) for a in appends]:
                        found.append(fn)
                if found:
                    return found
            for fn, skipped in tinfoil.get_skipped_recipes().items():
                if fn.startswith("virtual:"):
                    continue
                appends = tinfoil.get_file_appends(fn)
                if path in [os.path.realpath(a) for a in appends]:
                    raise BackendError("%s is skipped in the current configuration: %s"
                                       % (os.path.basename(fn),
                                          getattr(skipped, "skipreason", skipped)))
            raise BackendError("No recipe uses this bbappend in the current configuration.")
        return self.recipes_including(tinfoil, path)

    def recipes_including(self, tinfoil, path):
        """Recipes next to ``path`` (or one level up) that include it.

        Recipes often include files through variables
        ("require ${BPN}.inc"), so the text cannot tell.  Each candidate is
        parsed and BitBake's own list of included files (__depends) decides.
        """
        here = os.path.dirname(path)
        candidates = []
        for d in (here, os.path.dirname(here)):
            try:
                entries = sorted(os.listdir(d))
            except OSError:
                continue
            for f in entries:
                fn = os.path.join(d, f)
                if f.endswith(".bb") and os.path.isfile(fn):
                    candidates.append(fn)
        # Prefer recipes that are actually used: preferred versions first,
        # then other parsed recipes, then skipped ones.
        preferred = set(tinfoil.all_recipe_files(variants=False, preferred_only=True))
        parsed = set(tinfoil.all_recipe_files(variants=False))
        candidates.sort(key=lambda fn: (fn not in preferred, fn not in parsed))
        for fn in candidates:
            if self.includes(tinfoil, fn, path):
                return [fn]
        raise BackendError("No recipe in this directory or the one above includes %s."
                           % os.path.basename(path))

    @staticmethod
    def includes(tinfoil, recipe, path):
        try:
            d = tinfoil.parse_recipe_file(recipe)
        except Exception:
            return False
        try:
            depends = d.getVar("__depends", False) or []
        finally:
            tinfoil.run_command("dataStoreConnectorRelease", d.dsindex)
        return any(realpath(dep[0]) == path for dep in depends)

    # -- analysis --------------------------------------------------------

    def analyze(self, params):
        path = os.path.realpath(params["file"])
        tinfoil = self.open_tinfoil()
        try:
            return self.analyze_with(tinfoil, path)
        finally:
            tinfoil.shutdown()
            self.wait_for_server_exit()

    def analyze_with(self, tinfoil, path):
        try:
            with open(path, errors="replace") as fp:
                source = fp.read()
        except OSError as e:
            raise BackendError("Cannot read %s: %s" % (path, e))
        lines = source.splitlines()

        last_error = None
        for recipe in self.recipes_for(tinfoil, path):
            try:
                variants = self.parse_variants(tinfoil, recipe)
            except BackendError as e:
                last_error = e
                continue
            per_variant = [(v, a, h) for v, a, h, _ in variants]
            if not any(any(e.get("file") == path for e in evs)
                       for _, _, h in per_variant for evs in h.values()):
                last_error = BackendError("%s does not use this file." % os.path.basename(recipe))
                continue
            return self.build_result(path, lines, source, recipe, variants)
        raise last_error or BackendError("No recipe found for this file.")

    def parse_variants(self, tinfoil, recipe):
        try:
            d = tinfoil.parse_recipe_file(recipe)
        except Exception as e:
            raise self.parse_error(e)
        datastores = [("", d)]
        extend = []
        for variant in (d.getVar("BBCLASSEXTEND") or "").split():
            if variant not in extend:
                extend.append(variant)
        for variant in extend:
            if variant.startswith("mcextend:"):
                continue
            try:
                datastores.append((variant, tinfoil.parse_recipe_file(
                    "virtual:%s:%s" % (variant, recipe))))
            except Exception as e:
                sys.stderr.write("yrd: cannot parse variant %s of %s: %s\n" % (variant, recipe, e))

        result = []
        for variant, data in datastores:
            histories = {}
            names = list(data.keys())
            for name in names:
                events = []
                for event in data.varhistory.variable(name):
                    if event.get("file"):
                        event["file"] = realpath(event["file"])
                    if not is_internal(event):
                        events.append(event)
                if events:
                    histories[name] = events
            # Function definitions are missing from the history.  Only
            # names with ":" can carry an override or :append, which keeps
            # the number of remote calls small.
            for name in names:
                if ":" not in name:
                    continue
                found = function_event(name, data.getVarFlags(name) or {})
                if found:
                    owner, event = found
                    event["file"] = realpath(event["file"])
                    histories.setdefault(owner, []).append(event)
            packages = (data.getVar("PACKAGES") or "").split()
            dynamic = []
            for pattern in (data.getVar("PACKAGES_DYNAMIC") or "").split():
                try:
                    dynamic.append(re.compile(pattern))
                except re.error:
                    pass
            overrides = [o for o in (data.getVar("OVERRIDES") or "").split(":") if o]
            cache = {}

            def value_of(name, data=data, cache=cache):
                if name not in cache:
                    try:
                        value = data.getVar(name)
                    except Exception:
                        value = None
                    # Internal variables (__BBANONFUNCS, ...) hold lists
                    # and sets; only text values are meaningful here.
                    cache[name] = value if isinstance(value, str) else None
                return cache[name]

            def expand(text, data=data):
                return data.expand(text)

            analysis = VariantAnalysis(histories, overrides, packages, dynamic,
                                       expand, value_of).run()
            result.append((variant, analysis, histories, (data, value_of, overrides)))
        return result

    @staticmethod
    def parse_error(exc):
        file = getattr(exc, "filename", None)
        line = getattr(exc, "lineno", None)
        return BackendError("BitBake cannot parse the recipe: %s" % exc, file, line)

    def build_result(self, path, lines, source, recipe, variants):
        per_variant = [(v, a, h) for v, a, h, _ in variants]
        diagnostics = merge_variants(per_variant, set([path]))
        for diag in diagnostics:
            if diag.pop("line_is_start", False):
                diag["end_line"] = statement_end(lines, diag["line"])
            else:
                end = diag["line"] or 1
                diag["end_line"] = end
                diag["line"] = statement_start(lines, end)

        default_variant = variants[0]
        _, analysis, histories, (data, value_of, overrides) = default_variant

        # Variables shown on hover: names assigned or referenced in the
        # file, and every variable that has an event in the file.
        names = referenced_names(source)
        for name, events in histories.items():
            if any(e.get("file") == path for e in events):
                names.add(analysis.split_scope(name)[0])

        variables = {}
        for name in sorted(names):
            # The variable itself first, then its overrides in the order
            # they appear in this file.  Each one is in parse order; BitBake
            # does not record the order between them.
            def first_line_here(n):
                lines = [e.get("line") or 0 for e in histories[n] if e.get("file") == path]
                return (not lines, min(lines) if lines else 0, n)
            # Package variables (FILES:foo-doc) are separate scopes, the
            # same way the analysis treats them.
            related = ([name] if name in histories else []) + sorted(
                (n for n in histories
                 if n.startswith(name + ":") and analysis.split_scope(n)[0] == name),
                key=first_line_here)
            value = value_of(name)
            if value is None and not related:
                continue
            history = []
            for n in related:
                for event in histories[n]:
                    if event.get("flag") and event.get("flag") != FLAG_DEFAULT:
                        continue
                    history.append({
                        "variable": n,
                        "file": event.get("file"),
                        "line": event.get("line"),
                        "op": event.get("op"),
                        "flag": event.get("flag"),
                        "detail": clip(event.get("detail"), MAX_DETAIL_LENGTH),
                        "status": status_of(analysis.findings.get(event_key(event), [])),
                        "function": bool(event.get("function")),
                    })
            other = {}
            for variant, _, _, (vdata, vvalue_of, _) in variants[1:]:
                other[variant] = clip(vvalue_of(name), MAX_VALUE_LENGTH)
            variables[name] = {
                "value": clip(value, MAX_VALUE_LENGTH),
                "variants": other,
                "history": history,
            }

        return {
            "file": path,
            "recipe": recipe,
            "variants": [v or "target" for v, _, _, _ in variants],
            "overrides": ":".join(overrides),
            "diagnostics": diagnostics,
            "variables": variables,
        }

    # -- protocol --------------------------------------------------------

    def handle(self, request):
        method = request.get("method")
        if method == "ping":
            return {"python": sys.version.split()[0]}
        if method == "analyze":
            return self.analyze(request.get("params") or {})
        raise BackendError("Unknown method: %s" % method)

    def serve(self, stream):
        for line in stream:
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = {"id": request.get("id")}
            try:
                response["result"] = self.handle(request)
            except BackendError as e:
                response["error"] = {"message": str(e), "file": e.file, "line": e.line,
                                     "code": e.code}
            except BaseException as e:  # BitBake raises SystemExit on fatal errors
                if isinstance(e, KeyboardInterrupt):
                    raise
                sys.stderr.write(traceback.format_exc())
                response["error"] = {"message": "%s: %s" % (type(e).__name__, e)}
            self.out.write(json.dumps(response) + "\n")
            self.out.flush()


def main():
    # Keep the protocol on a private copy of stdout.  BitBake and the
    # recipes it runs may print to fd 1; send all of that to stderr.
    out = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    Backend(out).serve(sys.stdin)


if __name__ == "__main__":
    main()
