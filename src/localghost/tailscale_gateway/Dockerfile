FROM golang:1.26.3-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /localghost-tailscale-gateway .

FROM scratch
COPY --from=build /localghost-tailscale-gateway /localghost-tailscale-gateway
USER 65532:65532
ENTRYPOINT ["/localghost-tailscale-gateway"]
