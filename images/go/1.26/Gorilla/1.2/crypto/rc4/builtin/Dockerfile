FROM golang:1.26 AS builder
WORKDIR /build
ENV CGO_ENABLED=0 GONOSUMDB=* GOTOOLCHAIN=local
COPY go.mod main.go ./
RUN go mod tidy && go build -o app main.go

FROM scratch
COPY --from=builder /build/app /app
EXPOSE 8000
CMD ["/app"]
