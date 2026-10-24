"""Integration tests against real BitBake.

They need a build directory made by tests/integration_setup.sh and are
skipped unless these environment variables are set:

  YRD_TEST_BUILD_DIR    build directory that includes the fixture layer
  YRD_TEST_INIT_SCRIPT  path to oe-init-build-env
  YRD_TEST_WRAPPER      optional command prefix, for example a "docker run"
                        command that enters a supported build host

See README.md for an example.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest
from lsprotocol import types
from pygls.lsp.client import LanguageClient

from yocto_recipe_debugger.bitbake_process import (
    BACKEND_SOURCE, BitbakeProcess, BitbakeSettings,
)

BUILD_DIR = os.environ.get("YRD_TEST_BUILD_DIR")
pytestmark = pytest.mark.skipif(not BUILD_DIR, reason="YRD_TEST_BUILD_DIR is not set")

LAYER = Path(__file__).parent / "fixtures" / "meta-yrd-test" / "recipes-test"
SAMPLE = LAYER / "yrd-sample" / "yrd-sample_1.0.bb"
APPEND = LAYER / "yrd-sample" / "yrd-sample_%.bbappend"
INC = LAYER / "yrd-include" / "yrd-include.inc"


def settings():
    return BitbakeSettings(build_dir=BUILD_DIR,
                           init_script=os.environ.get("YRD_TEST_INIT_SCRIPT", ""),
                           command_wrapper=os.environ.get("YRD_TEST_WRAPPER", ""))


@pytest.fixture(scope="module")
def results():
    process = BitbakeProcess(settings())
    try:
        yield {path: process.analyze(str(path)) for path in (SAMPLE, APPEND, INC)}
    finally:
        process.close()


def line_of(path, text):
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if line.startswith(text):
            return i
    raise AssertionError("%r not in %s" % (text, path))


def found(result, path, text):
    line = line_of(path, text)
    return sorted({d["code"] for d in result["diagnostics"] if d["line"] == line})


def test_variants(results):
    assert results[SAMPLE]["variants"] == ["target", "native"]


@pytest.mark.parametrize("text, expected", [
    ('YRD_INACTIVE:append:libc-musl', ["inactive-override"]),
    ('YRD_GLUE:append', ["missing-space"]),
    ('YRD_PATHS:append', []),
    ('YRD_WEAK ??=', ["default-discarded"]),
    ('YRD_WEAK +=', ["discards-default"]),
    ('YRD_QUES ?=', ["already-set"]),
    ('YRD_TWICE = "one"', ["overwritten"]),
    ('YRD_TWICE = "two"', ["overwritten"]),
    # Replaced in target, but used by the native variant.
    ('YRD_REPLACED = "base"', []),
    ('YRD_SHADOW:pn-yrd-sample', ["inactive-override", "shadowed-override"]),
    ('YRD_SHADOW:class-target', []),
    ('YRD_RM:append', ["removed"]),
    ('RDEPENDS:${PN}-extra', []),
    ('FILES:${PN}-extra', []),
    ('YRD_NATIVE:append:class-native', []),
    ('YRD_NATIVE:append:class-nativesdk', ["inactive-override"]),
    ('YRD_MULTI = "a', ["overwritten"]),
    ('do_install:append:libc-musl', ["inactive-override"]),
    # Defined again in the bbappend.
    ('do_configure()', ["overwritten"]),
    ('yrd_helper()', []),
    ('BBCLASSEXTEND', []),
])
def test_sample_recipe(results, text, expected):
    assert found(results[SAMPLE], SAMPLE, text) == expected


def test_ranges(results):
    by_line = {d["line"]: d for d in results[SAMPLE]["diagnostics"]}
    multi = line_of(SAMPLE, 'YRD_MULTI = "a')
    assert by_line[multi]["end_line"] == multi + 2
    func = line_of(SAMPLE, "do_install:append:libc-musl")
    assert by_line[func]["end_line"] == func + 2
    plain = line_of(SAMPLE, "do_configure()")
    assert by_line[plain]["end_line"] == plain + 2


def test_bbappend(results):
    result = results[APPEND]
    assert result["recipe"] == str(SAMPLE)
    assert found(result, APPEND, "YRD_APPEND_INACTIVE") == ["inactive-override"]
    assert found(result, APPEND, "python do_compile:prepend") == ["inactive-override"]
    assert results[SAMPLE]["variables"]["YRD_TWICE"]["value"] == "from-bbappend"
    assert found(result, APPEND, "do_configure()") == []


def test_plain_functions_are_in_history(results):
    variables = results[SAMPLE]["variables"]
    (helper,) = variables["yrd_helper"]["history"]
    assert (helper["line"], helper["function"]) == (line_of(SAMPLE, "yrd_helper()"), True)
    history = variables["do_configure"]["history"]
    assert [(h["file"], h["status"]) for h in history] == [
        (str(SAMPLE), "overwritten"), (str(APPEND), "applied")]


def test_include(results):
    result = results[INC]
    assert result["recipe"].endswith("yrd-include_1.0.bb")
    assert found(result, INC, 'YRD_INC = "from-inc"') == ["overwritten"]
    assert found(result, INC, "YRD_INC:append:libc-musl") == ["inactive-override"]


def test_hover_history_lists_variable_before_overrides(results):
    history = results[SAMPLE]["variables"]["YRD_REPLACED"]["history"]
    assert [h["variable"] for h in history] == ["YRD_REPLACED", "YRD_REPLACED:class-target"]


def test_package_variables_are_separate_in_hover(results):
    variables = results[SAMPLE]["variables"]
    assert "RDEPENDS:yrd-sample-extra" in variables
    names = {h["variable"] for h in variables["RDEPENDS"]["history"]}
    assert "RDEPENDS:yrd-sample-extra" not in names


def test_variable_values(results):
    variables = results[SAMPLE]["variables"]
    assert variables["YRD_GLUE"]["value"] == "alpha betagamma"
    assert variables["YRD_REPLACED"]["value"] == "target"
    assert variables["YRD_REPLACED"]["variants"]["native"] == "base"
    assert variables["YRD_NATIVE"]["variants"]["native"] == "x native-only"


# -- end to end through the language server ---------------------------------

async def run_server_session():
    client = LanguageClient("yrd-test", "0")
    diagnostics = asyncio.Queue()

    @client.feature(types.TEXT_DOCUMENT_PUBLISH_DIAGNOSTICS)
    def on_diagnostics(params):
        diagnostics.put_nowait(params)

    # YRD_TEST_SERVER_PYTHON (with PYTHONPATH) tests the layout bundled in
    # the VS Code extension: any host Python plus server/src and server/lib.
    python = os.environ.get("YRD_TEST_SERVER_PYTHON", sys.executable)
    await client.start_io(python, "-m", "yocto_recipe_debugger")
    try:
        await client.initialize_async(types.InitializeParams(
            capabilities=types.ClientCapabilities(),
            initialization_options={
                "buildDir": BUILD_DIR,
                "initScript": os.environ.get("YRD_TEST_INIT_SCRIPT", ""),
                "commandWrapper": os.environ.get("YRD_TEST_WRAPPER", ""),
            }))
        client.initialized(types.InitializedParams())
        uri = SAMPLE.resolve().as_uri()
        client.text_document_did_open(types.DidOpenTextDocumentParams(
            text_document=types.TextDocumentItem(uri=uri, language_id="bitbake", version=1,
                                                 text=SAMPLE.read_text())))
        published = await asyncio.wait_for(diagnostics.get(), timeout=600)

        glue = line_of(SAMPLE, "YRD_GLUE:append") - 1
        hover = await client.text_document_hover_async(types.HoverParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            position=types.Position(line=glue, character=3)))
        hints = await client.text_document_inlay_hint_async(types.InlayHintParams(
            text_document=types.TextDocumentIdentifier(uri=uri),
            range=types.Range(start=types.Position(line=0, character=0),
                              end=types.Position(line=200, character=0))))
        await client.shutdown_async(None)
        client.exit(None)
        return published, hover, hints
    finally:
        await client.stop()


def test_language_server_end_to_end():
    published, hover, hints = asyncio.run(run_server_session())

    by_code = {d.code: d for d in published.diagnostics}
    assert by_code["missing-space"].severity == types.DiagnosticSeverity.Warning
    unused = by_code["inactive-override"]
    assert unused.severity == types.DiagnosticSeverity.Hint
    assert list(unused.tags) == [types.DiagnosticTag.Unnecessary]

    assert hover is not None
    assert "alpha betagamma" in hover.contents.value
    assert "YRD_GLUE :append" in hover.contents.value

    glue = line_of(SAMPLE, "YRD_GLUE:append") - 1
    labels = {h.position.line: h.label for h in hints}
    assert labels[glue] == "⇒ alpha betagamma"


def test_busy_build_directory_is_not_joined():
    """While another BitBake holds bitbake.lock, the backend must not join it."""
    import fcntl

    from yocto_recipe_debugger.bitbake_process import BackendError

    process = BitbakeProcess(settings())
    with open(os.path.join(BUILD_DIR, "bitbake.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            with pytest.raises(BackendError) as excinfo:
                process.analyze(str(INC))
            assert excinfo.value.code == "busy"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            process.close()


def test_short_bitbake_command_is_waited_for():
    """A lock that is released soon (like "bitbake -e" on save) is not "busy"."""
    import fcntl
    import threading

    process = BitbakeProcess(settings())
    with open(os.path.join(BUILD_DIR, "bitbake.lock"), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        release = threading.Timer(3, fcntl.flock, (lock, fcntl.LOCK_UN))
        release.start()
        try:
            result = process.analyze(str(INC))
            assert result["recipe"].endswith("yrd-include_1.0.bb")
        finally:
            release.join()
            process.close()


# BitBake starts after the backend waited for the lock and before Tinfoil
# takes it.  Tinfoil must not connect to that BitBake.
STARTS_IN_THE_GAP = """
import fcntl, sys, time
namespace = {"__name__": "yrd_backend"}
exec(compile(open(%r).read(), "backend.py", "exec"), namespace)
backend = namespace["Backend"](sys.stdout)
lock = open(backend.lock_path(), "a+")
fcntl.flock(lock, fcntl.LOCK_EX)
start = time.time()
try:
    backend.start_tinfoil().shutdown()
    print("RESULT joined")
except namespace["BackendError"] as e:
    print("RESULT %%s %%d" %% (e.code, time.time() - start))
""" % str(BACKEND_SOURCE)


def test_bitbake_started_in_the_gap_is_not_joined():
    command = settings().command()
    command[-1] = STARTS_IN_THE_GAP  # run this script instead of the backend
    out = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         universal_newlines=True, timeout=600).stdout
    (result,) = [line.split()[1:] for line in out.splitlines() if line.startswith("RESULT")]
    code, seconds = result
    assert code == "busy"
    assert int(seconds) < 5  # no "Reconnecting to bitbake server" retries
