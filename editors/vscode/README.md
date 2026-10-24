# Yocto Recipe Debugger

Shows what BitBake really does with your recipe.

BitBake parses the recipe with your build configuration (`MACHINE`,
`DISTRO`, layers). The extension reads BitBake's variable history and
shows, for each line, whether it has an effect.

![Lines with no effect are faded, problems are underlined, and final values are shown at the end of the line](https://raw.githubusercontent.com/signal-slot/yocto-recipe-debugger/main/docs/images/overview.png)

- **Lines with no effect are faded**: an `:append:libc-musl` when
  `libc-musl` is not in `OVERRIDES`, a value that is set again later
  (also by a bbappend), a `?=` after the value is set, and more.
- **Warnings**: `:append` without a leading space, `+=` that discards a
  `??=` default.
- **Hover** shows the final value, the value in other variants
  (`BBCLASSEXTEND`), and the history of the variable.
- **Inlay hints** show the final value at the last assignment.

![Hover shows the final value and the history](https://raw.githubusercontent.com/signal-slot/yocto-recipe-debugger/main/docs/images/hover.png)

Supported files: `.bb`, `.bbappend`, and `.inc`. `.inc` files under a
`recipes-*` directory are BitBake files; other `.inc` files are analyzed
only when the workspace has a `.bb` or `.bbappend` file. The analysis runs
when a file is opened or saved.

## Requirements

- A BitBake build directory (the one with `conf/local.conf`).
- Python 3.9 or later on this machine, to run the language server.
- BitBake must run, on this machine or in a container (see
  `commandWrapper`).

## Settings

```json
{
  "yoctoRecipeDebugger.buildDir": "${workspaceFolder}/build",
  "yoctoRecipeDebugger.initScript": "${workspaceFolder}/poky/oe-init-build-env",
  "yoctoRecipeDebugger.commandWrapper": ""
}
```

| Setting | Description |
| --- | --- |
| `buildDir` | BitBake build directory. |
| `initScript` | `oe-init-build-env`. Empty: search next to the build directory. |
| `commandWrapper` | Command prefix that runs BitBake in a supported build host, for example `docker run --rm -i -v /work:/work my-yocto-image`. Paths must be the same inside and outside. |
| `python` | Python used to run the language server (default `python3`). |

## Notes

- Each analysis starts BitBake and stops it again, so the build directory
  is not locked between analyses. While another BitBake uses the build
  directory, the extension waits and tries again.
- The first analysis after a configuration change parses all recipes and
  can take minutes. Later analyses use the parse cache.
- The output channel "Yocto Recipe Debugger" shows BitBake's messages.
- Works together with the official BitBake extension
  (`yocto-project.yocto-bitbake`, tested with 2.10.0). When it runs
  `bitbake -e` on save, this extension waits until it ends.

More details: <https://github.com/signal-slot/yocto-recipe-debugger>

## License

MIT, except the BitBake-side backend (`backend.py`), which is
GPL-2.0-only. See the LICENSE file.
