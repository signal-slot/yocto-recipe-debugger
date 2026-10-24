# Build host for Yocto 4.1 (langdale).
# langdale's BitBake uses ast.Str, which was removed in Python 3.12,
# so it needs a distribution whose python3 is 3.11 or older.
# Package list: Yocto 4.1 "Required Packages for the Build Host" (Ubuntu).
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
        gawk wget git diffstat unzip texinfo gcc build-essential chrpath \
        socat cpio python3 python3-pip python3-pexpect xz-utils debianutils \
        iputils-ping python3-git python3-jinja2 python3-subunit zstd \
        liblz4-tool file locales ca-certificates \
    && sed -i 's/^# *en_US.UTF-8/en_US.UTF-8/' /etc/locale.gen \
    && locale-gen \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8
