// Package traefik_localghost_tailnet_ca gives Traefik a distinct local-plugin
// module identity for a second instance of the shared localghost CA provider.
package traefik_localghost_tailnet_ca

import (
	"context"

	localghostca "github.com/SmileyChris/traefik-localghost-ca"
)

type Config = localghostca.Config

func CreateConfig() *Config {
	return localghostca.CreateConfig()
}

func New(ctx context.Context, config *Config, name string) (*localghostca.Provider, error) {
	return localghostca.New(ctx, config, name)
}
