# The certificate-authority bootstrap is a one-shot job, so compiling it here
# rather than with `go run` per invocation keeps a Go toolchain out of both the
# hub image and every `localghost trust`.
FROM golang:1.24.6-alpine AS bootstrap
WORKDIR /src
COPY traefik_plugin/src/github.com/SmileyChris/traefik-localghost-ca/ ./
RUN CGO_ENABLED=0 go build -trimpath -o /out/localghost-bootstrap ./cmd/bootstrap

FROM traefik:v3.7.7

# Traefik loads local plugins from Go source.  There is deliberately no Go
# toolchain or post-start installation step in this image.
COPY traefik_plugin/src/ /plugins-local/src/
COPY --from=bootstrap /out/localghost-bootstrap /usr/local/bin/localghost-bootstrap
