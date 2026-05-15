# syntax=docker/dockerfile:1

# Build mpiFileUtils and required dependencies from source.
#
# Build options (build type, feature flags, compiler flags) are read from
# cmake/build-options.cmake inside the build context.  To customise a
# build from an external repo or CI pipeline, copy your own version of that
# file over the default before running docker build:
#
#   cp my-options.cmake mpifileutils/cmake/build-options.cmake
#   docker build -t mpifileutils:custom mpifileutils/
#
# See cmake/build-options-example.cmake for an annotated example.

FROM ubuntu:26.04 AS build

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      autoconf \
      automake \
      build-essential \
      ca-certificates \
      cmake \
      git \
      libarchive-dev \
      libattr1-dev \
      libbz2-dev \
      libcap-dev \
      libopenmpi-dev \
      libtool \
      m4 \
      openmpi-bin \
      pkg-config \
      wget && \
    rm -rf /var/lib/apt/lists/*

ENV CC=mpicc
ENV CXX=mpicxx

WORKDIR /usr/local/src

    # wget -q https://github.com/hpc/libcircle/releases/download/v0.3/libcircle-0.3.0.tar.gz; \
    # tar -xzf libcircle-0.3.0.tar.gz; \
    # cd libcircle-0.3.0; \

RUN set -eux; \
    git clone -b backtrace-on-mpi-error https://github.com/jeking3/libcircle.git; \
    cd libcircle; \
    git checkout 59a194aeaa67868f0159be38547668e2e92b0775; \
    autoreconf -i; \
    ./configure; \
    make -j"$(nproc)"; \
    make install

RUN set -eux; \
    wget -q https://github.com/llnl/lwgrp/releases/download/v1.0.6/lwgrp-1.0.6.tar.gz; \
    tar -xzf lwgrp-1.0.6.tar.gz; \
    cd lwgrp-1.0.6; \
    ./configure; \
    make -j"$(nproc)"; \
    make install

RUN set -eux; \
    wget -q https://github.com/llnl/dtcmp/releases/download/v1.1.5/dtcmp-1.1.5.tar.gz; \
    tar -xzf dtcmp-1.1.5.tar.gz; \
    cd dtcmp-1.1.5; \
    ./configure --with-lwgrp=/usr/local; \
    make -j"$(nproc)"; \
    make install

WORKDIR /usr/local/src/mpifileutils
COPY . /usr/local/src/mpifileutils

RUN set -eux; \
    cmake -S . -B build \
      -C cmake/build-options.cmake; \
    cmake --build build -j"$(nproc)"; \
    cmake --install build; \
    test -x /usr/local/bin/dsync

# Runtime minimal image with built artifacts.
FROM ubuntu:26.04 AS runtime

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      libarchive13 \
      libattr1 \
      libbz2-1.0 \
      libcap2 \
      libopenmpi40 \
      openmpi-bin && \
    rm -rf /var/lib/apt/lists/*

COPY --from=build /usr/local /usr/local

WORKDIR /workspace

RUN ldconfig

CMD ["/bin/bash"]

