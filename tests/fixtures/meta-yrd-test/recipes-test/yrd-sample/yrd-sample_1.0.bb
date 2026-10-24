SUMMARY = "Fixture for yocto-recipe-debugger tests"
LICENSE = "MIT"
LIC_FILES_CHKSUM = "file://${COMMON_LICENSE_DIR}/MIT;md5=0835ade698e0bcf8506ecda2f7b4f302"

# Override that is never active in this recipe.
YRD_INACTIVE = "base"
YRD_INACTIVE:append:libc-musl = " musl"

# :append without a leading space.
YRD_GLUE = "alpha beta"
YRD_GLUE:append = "gamma"

# Separator on purpose: not a mistake.
YRD_PATHS = "/a"
YRD_PATHS:append = ":/b"

# Weak default lost because of +=.
YRD_WEAK ??= "default"
YRD_WEAK += "extra"

# ?= after a value is already set.
YRD_QUES = "first"
YRD_QUES ?= "second"

# = set twice in the recipe; bbappend sets it again.
YRD_TWICE = "one"
YRD_TWICE = "two"

# Base value replaced by an active override.
YRD_REPLACED = "base"
YRD_REPLACED:class-target = "target"

# class-target comes after pn-* in OVERRIDES, so it wins.
YRD_SHADOW:pn-yrd-sample = "pn"
YRD_SHADOW:class-target = "class"

# Item added and then removed.
YRD_RM = "keep"
YRD_RM:append = " drop"
YRD_RM:remove = "drop"

# Package-specific variables are not overrides.
PACKAGES =+ "${PN}-extra"
RDEPENDS:${PN}-extra = "bash"
FILES:${PN}-extra = "${bindir}/extra"

# Only active in the native variant.
YRD_NATIVE = "x"
YRD_NATIVE:append:class-native = " native-only"
YRD_NATIVE:append:class-nativesdk = " never"

# Multi-line value: BitBake reports the last line.
YRD_MULTI = "a \
    b \
    c"
YRD_MULTI = "replaced"

do_install:append:libc-musl() {
    echo musl
}

BBCLASSEXTEND = "native"
