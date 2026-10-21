FROM golang:1.25.3-alpine AS build
WORKDIR /src
COPY go.mod go.sum main.go main_test.go ./
RUN go mod download && go test -mod=readonly ./... && CGO_ENABLED=0 go build -trimpath -ldflags='-s -w' -o /steam-input .
FROM scratch
COPY --from=build /steam-input /steam-input
ENTRYPOINT ["/steam-input"]
