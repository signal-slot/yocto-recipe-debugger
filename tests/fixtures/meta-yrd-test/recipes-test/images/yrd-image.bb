# Copyright (C) 2026 Signal Slot Inc.
# SPDX-License-Identifier: MIT

# Image for the tests of tests/fixtures/local.conf.
SUMMARY = "yocto-recipe-debugger test image"
LICENSE = "MIT"

inherit core-image

IMAGE_INSTALL = "packagegroup-core-boot"
YRD_CONF_BLOCK ?= "a b"
