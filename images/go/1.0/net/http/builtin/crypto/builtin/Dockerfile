FROM golang:1.0
ENV GOPATH=/go CGO_ENABLED=0
WORKDIR /go/src/myapp
COPY main.go .
RUN go get ./... 2>/dev/null || true && go build -o /app .
EXPOSE 8000
CMD ["/app"]
