YRD_TWICE = "from-bbappend"
YRD_APPEND_INACTIVE:append:libc-musl = " musl"

python do_compile:prepend:class-nativesdk() {
    bb.note("never")
}

do_configure() {
    echo bbappend
}
