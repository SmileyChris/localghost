FROM golang:1.26.3-alpine AS build

WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY main.go ./
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /localghost-tailscale-gateway .

FROM scratch
COPY --from=build /localghost-tailscale-gateway /localghost-tailscale-gateway
# Docker initializes named-volume roots as UID 0. The gateway needs to create
# tsnet's encrypted node state there, but receives no host mounts or Linux
# capabilities and runs with a read-only root filesystem.
ENTRYPOINT ["/localghost-tailscale-gateway"]
