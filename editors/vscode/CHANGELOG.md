# Changelog

## Unreleased

- Supports `.conf` files (`local.conf`, `layer.conf`, machine and distro
  confs, and the `.inc` files that they include). Lines are decided by the
  images (setting `images`, or the images built before), or by recipe
  `foo` for `pn-foo` overrides.
- New warning `blocks-default`: `+=` that comes before a `?=` default, as
  `DISTRO_FEATURES += "x"` in `local.conf` does.

## 0.1.0

First release.

- Diagnostics for lines that have no effect, `:append` without a leading
  space, and `+=` that discards a `??=` default.
- Hover with the final value, the values in other variants, and the
  history of the variable.
- Inlay hints with the final value.
- Supports `.bb`, `.bbappend`, and `.inc` files.
