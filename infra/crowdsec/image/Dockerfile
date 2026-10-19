# Backport upstream #4678 (e63ec0f): never emit half-written file log lines.
# Remove this image override when a stable upstream release includes the fix.
FROM golang:1.26-alpine3.23 AS build
RUN apk add --no-cache git g++ gcc libc-dev make bash binutils-gold pkgconfig curl
WORKDIR /build
RUN curl -fsSL https://github.com/google/re2/archive/refs/tags/2023-03-01.tar.gz | tar xz && \
    make -C re2-2023-03-01 install
RUN git clone --depth 1 --branch v1.7.8 https://github.com/crowdsecurity/crowdsec.git /build/crowdsec
WORKDIR /build/crowdsec
RUN sed -i '/ReOpen:   true,/a\        CompleteLines: true,' pkg/acquisition/modules/file/run.go && \
    test "$(grep -c 'CompleteLines: true' pkg/acquisition/modules/file/run.go)" = 1
ENV GOMAXPROCS=2 GOFLAGS=-p=2
RUN make crowdsec DOCKER_BUILD=1 BUILD_STATIC=1 BUILD_VERSION=v1.7.8-complete-lines1 CGO_CFLAGS=-D_LARGEFILE64_SOURCE
FROM crowdsecurity/crowdsec:v1.7.8 AS runtime
COPY --from=build /build/crowdsec/cmd/crowdsec/crowdsec /usr/local/bin/crowdsec

# Optional regression target: upstream's partial-write + truncation test against
# the same patched source. Does not alter the shipped binary.
FROM build AS test
RUN curl -fsSL https://raw.githubusercontent.com/crowdsecurity/crowdsec/e63ec0f101786f7d804c404a2f2b8fc744889f33/pkg/acquisition/modules/file/file_test.go \
    -o /tmp/upstream_file_test.go && \
    sed -n '/^func appendToFile/,$p' /tmp/upstream_file_test.go >> pkg/acquisition/modules/file/file_test.go
RUN CGO_CFLAGS=-D_LARGEFILE64_SOURCE PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    go test -tags re2_cgo ./pkg/acquisition/modules/file -run TestLiveAcquisitionPartialLine -count=1 -v

FROM runtime AS final
