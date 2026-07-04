FROM golang:1.13 AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    cmake ninja-build gcc g++ libssl-dev git pkg-config \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 0.4.0 \
    https://github.com/open-quantum-safe/liboqs /tmp/liboqs \
    && cmake -S /tmp/liboqs -B /tmp/liboqs/build \
       -DCMAKE_BUILD_TYPE=Release \
       -DBUILD_SHARED_LIBS=ON \
       -DOQS_BUILD_ONLY_LIB=ON \
       -DOQS_USE_OPENSSL=OFF \
       "-DCMAKE_C_FLAGS=-w" \
       -GNinja \
    && cmake --build /tmp/liboqs/build \
    && cmake --install /tmp/liboqs/build \
    && rm -rf /tmp/liboqs
WORKDIR /build
ENV GONOSUMDB=* GOTOOLCHAIN=local PKG_CONFIG_PATH=/usr/local/lib/pkgconfig
COPY go.mod main.go ./
RUN go mod download \
    && LDIR=$(go env GOMODCACHE)/$(go list -m github.com/open-quantum-safe/liboqs-go | tr ' ' '@') \
    && mkdir -p /usr/local/lib/pkgconfig/ \
    && cp "$LDIR/.config/liboqs.pc" /usr/local/lib/pkgconfig/ \
    && go build -mod=mod -o app main.go

FROM debian:bullseye-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl1.1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/lib/liboqs* /usr/local/lib/
RUN ldconfig
COPY --from=builder /build/app /app
EXPOSE 8000
CMD ["/app"]
