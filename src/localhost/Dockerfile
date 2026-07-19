FROM traefik:v3.7.7

# Traefik loads local plugins from Go source.  There is deliberately no Go
# toolchain or post-start installation step in this image.
COPY traefik_plugin/src/ /plugins-local/src/
