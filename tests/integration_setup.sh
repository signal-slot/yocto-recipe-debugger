#!/bin/sh
# Copyright (C) 2026 Signal Slot Inc.
# SPDX-License-Identifier: MIT

# Create a throw-away build directory that adds the fixture layer and
# the lines of fixtures/local.conf.
# Usage: tests/integration_setup.sh <poky dir> <build dir>
set -eu
POKY=$(realpath "$1")
BUILD=$(realpath -m "$2")
LAYER=$(realpath "$(dirname "$0")/fixtures/meta-yrd-test")
mkdir -p "$BUILD/conf"
sed "s|##OEROOT##|$POKY|g; s|##COREBASE##|$POKY|g" "$POKY/meta-poky/conf/templates/default/bblayers.conf.sample" > "$BUILD/conf/bblayers.conf" 2>/dev/null \
  || sed "s|##OEROOT##|$POKY|g; s|##COREBASE##|$POKY|g" "$POKY/meta-poky/conf/bblayers.conf.sample" > "$BUILD/conf/bblayers.conf"
echo "BBLAYERS += \"$LAYER\"" >> "$BUILD/conf/bblayers.conf"
cat > "$BUILD/conf/local.conf" <<CONF
MACHINE = "qemux86-64"
DISTRO = "poky"
CONF_VERSION = "2"
CONF
cat "$(dirname "$0")/fixtures/local.conf" >> "$BUILD/conf/local.conf"
echo "$BUILD"
