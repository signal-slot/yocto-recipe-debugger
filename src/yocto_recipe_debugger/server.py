# Copyright (C) 2026 Signal Slot Inc.
# SPDX-License-Identifier: MIT

"""Language server: shows what BitBake really does with a recipe.

The analysis runs on save (and when a file is opened), because BitBake reads
the files from disk.  Results are kept per document and used for
diagnostics, hover and inlay hints.
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote

from lsprotocol import types
from pygls.lsp.server import LanguageServer
from pygls.uris import to_fs_path

from . import __version__
from .backend import statement_start
from .bitbake_process import BackendError, BitbakeProcess, BitbakeSettings

SOURCE = "bitbake"
RECIPE_SUFFIXES = (".bb", ".bbappend", ".inc")

SEVERITIES = {
    "warning": types.DiagnosticSeverity.Warning,
    "info": types.DiagnosticSeverity.Information,
    "unused": types.DiagnosticSeverity.Hint,
}

STATUS_LABELS = {
    "applied": "applied",
    "inactive-override": "not applied (override not active)",
    "shadowed-override": "not applied (another override wins)",
    "overwritten": "overwritten",
    "already-set": "ignored (already set)",
    "default-unused": "weak default not used",
    "default-discarded": "weak default discarded",
    "replaced-by-override": "replaced by an override",
    "removed": "removed by :remove",
}

OPERATORS = {
    "set": "=",
    "immediate": ":=",
    "set?": "?=",
    "append": "+=",
    "prepend": "=+",
    "postdot": ".=",
    "predot": "=.",
}

# The name part of an assignment or function definition: everything before
# the operator or "()".
NAME_PART = re.compile(
    r"^\s*(?:export\s+)?(?:(?:python|fakeroot)\s+)*[^\s=(]+?(?:\[[^\]]*\])?\s*"
    r"(?=\?\?=|\?=|:=|\+=|=\+|\.=|=\.|=|\(\s*\))")
# Characters of a BitBake variable name, including override suffixes.
WORD_START = re.compile(r"[A-Za-z0-9_\-+./~@%:]*$")
WORD_END = re.compile(r"^[A-Za-z0-9_\-+./~@%:]*")

HOVER_HISTORY_LIMIT = 40
BUSY_RETRY_SECONDS = 30
INLAY_VALUE_LIMIT = 80


@dataclass
class Analysis:
    path: str
    lines: list   # file content that BitBake analyzed
    result: dict


# ---------------------------------------------------------------------------
# Conversion from backend results (pure functions, unit tested)
# ---------------------------------------------------------------------------

def line_range(lines: list, first: int, last: int) -> types.Range:
    """Range covering 1-based lines ``first``..``last``."""
    start = max(first - 1, 0)
    end = max(last - 1, start)
    end_char = len(lines[end]) if end < len(lines) else 0
    return types.Range(start=types.Position(line=start, character=0),
                       end=types.Position(line=end, character=end_char))


def to_diagnostics(analysis: Analysis) -> list:
    out = []
    for d in analysis.result.get("diagnostics", []):
        severity = d.get("severity")
        out.append(types.Diagnostic(
            range=line_range(analysis.lines, d["line"], d.get("end_line", d["line"])),
            message=d["message"],
            severity=SEVERITIES.get(severity, types.DiagnosticSeverity.Information),
            code=d.get("code"),
            source=SOURCE,
            tags=[types.DiagnosticTag.Unnecessary] if severity == "unused" else None,
        ))
    return out


def error_diagnostic(path: str, lines: list, error: BackendError) -> types.Diagnostic:
    """Show a backend error in the file; at its line when it points here."""
    if error.file and error.line and error.file == path:
        rng = line_range(lines, error.line, error.line)
        severity = types.DiagnosticSeverity.Error
    else:
        rng = line_range(lines, 1, 1)
        severity = types.DiagnosticSeverity.Information
    return types.Diagnostic(range=rng, message=str(error), severity=severity, source=SOURCE)


def operator_of(event: dict) -> str:
    if event.get("flag") == "_defaultval":
        return "??="
    op = event.get("op") or ""
    if op.startswith(":"):
        m = re.match(r"^:(\w+)(?:\[(.*)\])?$", op)
        if m:
            return ":%s:%s" % (m.group(1), m.group(2)) if m.group(2) else ":" + m.group(1)
    return OPERATORS.get(op, op)


def lookup_variable(variables: dict, word: str) -> Optional[str]:
    """Find the variable for "SRC_URI:append:arm", "RDEPENDS:${PN}", ..."""
    parts = word.strip(":").split(":")
    while parts:
        name = ":".join(parts)
        if name in variables:
            return name
        parts.pop()
    return None


def variable_assigned_at(analysis: Analysis, line: int, character: int) -> Optional[str]:
    """Variable assigned by the statement at 0-based ``line``, when the
    cursor is on its name ("FILES:${PN}-doc" is found as "FILES:foo-doc")."""
    lines = analysis.lines
    if line >= len(lines):
        return None
    m = NAME_PART.match(lines[line])
    if not m or character > m.end():
        return None
    # The name is on the first line of the statement.  BitBake records the
    # last line of an assignment and the first line of a function.
    for name, var in analysis.result.get("variables", {}).items():
        for e in var.get("history") or []:
            if e.get("file") != analysis.path or str(e.get("op", "")).startswith("rename"):
                continue
            first = e["line"] if e.get("function") else statement_start(lines, e["line"])
            if first - 1 == line:
                # Prefer the exact variable ("FILES:foo-doc") when it has
                # its own entry; overrides ("X:arm") are shown under "X".
                return e["variable"] if e.get("variable") in analysis.result["variables"] else name
    return None


def one_line(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def code_block(text: str) -> str:
    fence = "```"
    while fence in text:
        fence += "`"
    return "%s\n%s\n%s" % (fence, text, fence)


def file_link(path: str, line) -> str:
    name = path.rsplit("/", 1)[-1]
    return "[%s:%s](file://%s#L%s)" % (name, line, quote(path), line)


def hover_markdown(result: dict, name: str) -> str:
    var = result["variables"][name]
    variants = result.get("variants") or ["target"]
    blocks = ["**%s** — %s (%s)" % (name, result["recipe"].rsplit("/", 1)[-1], variants[0])]
    value = var.get("value")
    blocks.append(code_block(value) if value is not None else "_not set_")
    for variant, other in (var.get("variants") or {}).items():
        if other != value:
            blocks.append("%s:" % variant)
            blocks.append(code_block(other) if other is not None else "_not set_")

    history = var.get("history") or []
    if history:
        blocks.append("**History** (parse order of each variable)")
        shown = history[-HOVER_HISTORY_LIMIT:]
        if len(shown) < len(history):
            blocks.append("_%d earlier operations not shown_" % (len(history) - len(shown)))
        items = []
        for event in shown:
            status = event.get("status", "applied")
            mark = "✓" if status == "applied" else "✗"
            label = "" if status == "applied" else " — " + STATUS_LABELS.get(status, status)
            detail = one_line(event.get("detail") or "", 60)
            items.append("- %s `%s %s` %s%s%s" % (
                mark, event.get("variable") or name, operator_of(event),
                file_link(event["file"], event["line"]),
                (" `%s`" % detail) if detail else "", label))
        blocks.append("\n".join(items))
    return "\n\n".join(blocks)


def inlay_hints(analysis: Analysis, current_lines: list, first: int, last: int) -> list:
    """Final value at the end of the last assignment of each variable.

    ``first``/``last`` are 0-based lines of the requested range.  A hint is
    only shown when the line still has the text BitBake analyzed, so that
    unsaved edits do not move hints to the wrong place.
    """
    hints = []
    for name, var in analysis.result.get("variables", {}).items():
        value = var.get("value")
        if value is None:
            continue
        # "rename" events repeat an assignment under its expanded name.  The
        # "value" of a function is its body, which does not fit in a hint.
        events = [e for e in var.get("history") or [] if e.get("file") == analysis.path]
        if any(e.get("function") for e in events):
            continue
        lines_here = [e["line"] for e in events
                      if not str(e.get("op", "")).startswith("rename")]
        if not lines_here:
            continue
        line = max(lines_here) - 1
        if not (first <= line <= last) or line >= len(current_lines) or line >= len(analysis.lines):
            continue
        if current_lines[line] != analysis.lines[line]:
            continue
        hints.append(types.InlayHint(
            position=types.Position(line=line, character=len(current_lines[line])),
            label="⇒ %s" % one_line(value, INLAY_VALUE_LIMIT),
            kind=types.InlayHintKind.Type,
            padding_left=True,
            tooltip=types.MarkupContent(kind=types.MarkupKind.Markdown,
                                        value="**%s** final value\n\n%s" % (name, code_block(value))),
        ))
    hints.sort(key=lambda h: h.position.line)
    return hints


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class RecipeServer(LanguageServer):
    def __init__(self):
        super().__init__("yocto-recipe-debugger", __version__)
        self.process: Optional[BitbakeProcess] = None
        self.analyses: dict = {}       # uri -> Analysis
        self.generation: dict = {}     # uri -> int, to drop stale results
        self.progress_ids = 0

    def configure(self, options: Optional[dict]) -> None:
        options = options or {}
        if self.process:
            self.process.close()
            self.process = None
        build_dir = options.get("buildDir")
        if not build_dir:
            self.show("Set yoctoRecipeDebugger.buildDir to your BitBake build directory.",
                      types.MessageType.Warning)
            return
        settings = BitbakeSettings(
            build_dir=build_dir,
            init_script=options.get("initScript") or "",
            command_wrapper=options.get("commandWrapper") or "",
        )
        self.process = BitbakeProcess(settings, on_stderr=self.log_line)

    def log_line(self, line: str) -> None:
        self.window_log_message(types.LogMessageParams(type=types.MessageType.Log, message=line))

    def show(self, message: str, kind=types.MessageType.Info) -> None:
        self.window_show_message(types.ShowMessageParams(type=kind, message=message))

    async def analyze(self, uri: str) -> None:
        path = to_fs_path(uri)
        if not path or not path.endswith(RECIPE_SUFFIXES) or not self.process:
            return
        generation = self.generation.get(uri, 0) + 1
        self.generation[uri] = generation

        self.progress_ids += 1
        token = "yrd-%d" % self.progress_ids
        progress = False
        try:
            await self.work_done_progress.create_async(token)
            self.work_done_progress.begin(token, types.WorkDoneProgressBegin(
                title="BitBake", message="Parsing %s" % path.rsplit("/", 1)[-1]))
            progress = True
        except Exception:  # the client may not support progress
            pass

        try:
            with open(path, errors="replace") as fp:
                lines = fp.read().splitlines()
        except OSError:
            lines = []
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, self.process.analyze, path)
            analysis = Analysis(path=result["file"], lines=lines, result=result)
            diagnostics = to_diagnostics(analysis)
        except BackendError as e:
            analysis = None
            diagnostics = [error_diagnostic(path, lines, e)]
            self.log_line("analysis of %s failed: %s" % (path, e))
            if e.code == "busy":
                asyncio.get_running_loop().call_later(
                    BUSY_RETRY_SECONDS, self.retry, uri, generation)
        finally:
            if progress:
                self.work_done_progress.end(token, types.WorkDoneProgressEnd())

        if self.generation.get(uri) != generation:
            return  # a newer analysis was started
        if analysis:
            self.analyses[uri] = analysis
        else:
            self.analyses.pop(uri, None)
        self.text_document_publish_diagnostics(
            types.PublishDiagnosticsParams(uri=uri, diagnostics=diagnostics))
        try:
            self.workspace_inlay_hint_refresh(None)
        except Exception:
            pass


    def retry(self, uri: str, generation: int) -> None:
        # Skip when the document was closed or analyzed again meanwhile.
        if self.generation.get(uri) == generation and uri in self.open_documents:
            asyncio.ensure_future(self.analyze(uri))

    @property
    def open_documents(self) -> set:
        return set(self.workspace.text_documents)


server = RecipeServer()


@server.feature(types.INITIALIZE)
def initialize(ls: RecipeServer, params: types.InitializeParams) -> None:
    ls.configure(params.initialization_options)


@server.feature(types.TEXT_DOCUMENT_DID_OPEN)
async def did_open(ls: RecipeServer, params: types.DidOpenTextDocumentParams) -> None:
    if params.text_document.uri not in ls.analyses:
        await ls.analyze(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_SAVE)
async def did_save(ls: RecipeServer, params: types.DidSaveTextDocumentParams) -> None:
    await ls.analyze(params.text_document.uri)


@server.feature(types.TEXT_DOCUMENT_DID_CLOSE)
def did_close(ls: RecipeServer, params: types.DidCloseTextDocumentParams) -> None:
    ls.analyses.pop(params.text_document.uri, None)


@server.feature(types.TEXT_DOCUMENT_HOVER)
def hover(ls: RecipeServer, params: types.HoverParams) -> Optional[types.Hover]:
    analysis = ls.analyses.get(params.text_document.uri)
    if not analysis:
        return None
    doc = ls.workspace.get_text_document(params.text_document.uri)
    name = variable_assigned_at(analysis, params.position.line, params.position.character)
    if not name:
        word = doc.word_at_position(params.position, re_start_word=WORD_START,
                                    re_end_word=WORD_END)
        name = lookup_variable(analysis.result.get("variables", {}), word)
    if not name:
        return None
    return types.Hover(contents=types.MarkupContent(
        kind=types.MarkupKind.Markdown, value=hover_markdown(analysis.result, name)))


@server.feature(types.TEXT_DOCUMENT_INLAY_HINT)
def inlay_hint(ls: RecipeServer, params: types.InlayHintParams) -> list:
    analysis = ls.analyses.get(params.text_document.uri)
    if not analysis:
        return []
    doc = ls.workspace.get_text_document(params.text_document.uri)
    current = [line.rstrip("\r\n") for line in doc.lines]
    return inlay_hints(analysis, current, params.range.start.line, params.range.end.line)


@server.feature(types.WORKSPACE_DID_CHANGE_CONFIGURATION)
def did_change_configuration(ls: RecipeServer, params: types.DidChangeConfigurationParams) -> None:
    settings = params.settings or {}
    if isinstance(settings, dict) and "yoctoRecipeDebugger" in settings:
        ls.configure(settings["yoctoRecipeDebugger"])
        ls.analyses.clear()


def main() -> None:
    # pygls logs every JSON-RPC message at INFO, which hides BitBake's
    # messages in the output channel.
    logging.basicConfig(level=logging.WARNING)
    server.start_io()
