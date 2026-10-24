# yocto-recipe-debugger

A language server that shows what BitBake really does with your recipe.

It does not parse recipes by itself. BitBake parses the recipe with
variable history tracking (the same data as `bitbake -e`), and the server
reads that history to decide, for each line, whether it has an effect.

## What it shows

Diagnostics (on open and on save):

| Code | Severity | Example |
| --- | --- | --- |
| `inactive-override` | faded | `FOO:append:libc-musl = " x"` when `libc-musl` is not in `OVERRIDES` |
| `shadowed-override` | faded | `FOO:pn-bar` when `FOO:class-target` takes precedence |
| `replaced-by-override` | faded | `FOO = "a"` when `FOO:arm = "b"` is active |
| `overwritten` | faded | `FOO = "a"` set again later (also by a bbappend or Python code) |
| `already-set` | faded | `FOO ?= "a"` when `FOO` already has a value |
| `default-unused` / `default-discarded` | faded | `FOO ??= "a"` that is never used |
| `discards-default` | warning | `FOO += "b"` after `FOO ??= "a"`: the result is `" b"` |
| `missing-space` | warning | `FOO:append = "b"`: the result is `"...ab"` |
| `removed` | info | `FOO:append = " b"` removed again by `FOO:remove = "b"` |

"Faded" lines are reported as hints with the `Unnecessary` tag, so the
editor shows them like dead code.

A line is reported as having no effect only when it has no effect in
**every** variant of the recipe (`BBCLASSEXTEND`). For example,
`:append:class-native` is fine when the recipe has a native variant.

Hover on a variable shows its final value, the value in other variants, and
the full history (file, line, operation, and whether it was applied).

Inlay hints show the final value at the end of the last assignment of each
variable in the file.

Supported files: `.bb`, `.bbappend` (analyzed with the recipe that uses it),
`.inc` (analyzed with a recipe in the same directory, or the one above, that
includes it). The analysis uses the configuration of your build directory
(`MACHINE`, `DISTRO`, layers), so results change when the configuration
changes.

## Layout

- `src/yocto_recipe_debugger/backend.py`: runs inside the BitBake
  environment. Standard library only, Python 3.6+. The server sends its
  source with `python3 -c`, so nothing needs to be installed or mounted for it.
- `src/yocto_recipe_debugger/server.py`: the language server (pygls).
- `editors/vscode/`: VS Code extension. The server and its Python
  dependencies are bundled, so it only needs `python3` (3.9+) on the host.
- `devenv/langdale/Dockerfile`: build host for Yocto 4.1 (see below).

## VS Code

Build and install the extension:

```sh
cd editors/vscode
npm install
npm run package
code --install-extension yocto-recipe-debugger-0.1.0.vsix
```

Settings (`.vscode/settings.json` of the workspace):

```json
{
  "yoctoRecipeDebugger.buildDir": "${workspaceFolder}/build",
  "yoctoRecipeDebugger.initScript": "${workspaceFolder}/poky/oe-init-build-env",
  "yoctoRecipeDebugger.commandWrapper": ""
}
```

`initScript` can be empty when `oe-init-build-env` is next to the build
directory (for example `<top>/poky/oe-init-build-env` and `<top>/build`).

### When BitBake does not run on your host

Old Yocto releases do not run on a new host. For example, BitBake of
langdale (4.1) uses `ast.Str`, which was removed in Python 3.12. Set
`commandWrapper` to a command that runs BitBake in a supported build host.
The paths must be the same inside and outside the container:

```sh
docker build -t yocto-host:langdale devenv/langdale
```

```json
{
  "yoctoRecipeDebugger.commandWrapper": "docker run --rm -i --user 1000:100 -e HOME=/tmp -v /path/to/top:/path/to/top yocto-host:langdale"
}
```

`-i` is required: the server talks to the backend over stdin and stdout.

### Notes

- Each analysis starts BitBake (tinfoil) and stops it again, so the build
  directory is not locked between analyses. The first analysis after a
  configuration change parses all recipes and can take minutes; later ones
  use the parse cache (a few seconds).
- The output channel "Yocto Recipe Debugger" shows BitBake's messages.

## Other editors

Any LSP client works. Start `python3 -m yocto_recipe_debugger` (with
`pygls` installed) and pass the settings as `initializationOptions`:
`{"buildDir": ..., "initScript": ..., "commandWrapper": ...}`.

## Tests

```sh
uv venv .venv && uv pip install --python .venv/bin/python -e '.[test]'
.venv/bin/python -m pytest            # unit tests; integration tests are skipped
```

Integration tests run real BitBake on the fixture layer in
`tests/fixtures/meta-yrd-test`:

```sh
tests/integration_setup.sh /path/to/poky .cache/test-build
export YRD_TEST_BUILD_DIR=$PWD/.cache/test-build
export YRD_TEST_INIT_SCRIPT=/path/to/poky/oe-init-build-env
export YRD_TEST_WRAPPER="docker run --rm -i --user 1000:100 -e HOME=/tmp -v /path/to/poky:/path/to/poky -v $PWD:$PWD yocto-host:langdale"
.venv/bin/python -m pytest
```
