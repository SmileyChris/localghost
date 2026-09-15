package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/netip"
	"time"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tsnet"
)

// identityAddress is where the gateway answers who owns a tailnet address.
// Unlike the health listener it binds every interface, because the caller is
// the hub's client middleware in the Traefik container, reached over the
// localghost Docker network. It is not published to the host.
const identityAddress = ":41824"

// identity is what the gateway tells the hub about one tailnet address. The
// hub turns it into the same headers Tailscale Serve sets, so an application
// written for Serve reads them unchanged.
type identity struct {
	// Login is the user's login name, or "tagged-devices" for a tagged node.
	Login string `json:"login"`
	// Name is the user's display name, or "Tagged Device" for a tagged node.
	Name string `json:"name"`
	// ProfilePic is the user's profile picture URL, if any.
	ProfilePic string `json:"profilePic,omitempty"`
}

type whoisLookup func(ctx context.Context, address string) (*apitype.WhoIsResponse, error)

// identityHandler answers GET /whois?ip=<tailnet address> with the identity
// behind that address. Only a bare address is accepted: a port would name a
// connection, and the hub only knows the client's address.
func identityHandler(lookup whoisLookup) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /whois", func(w http.ResponseWriter, r *http.Request) {
		address, err := netip.ParseAddr(r.URL.Query().Get("ip"))
		if err != nil {
			http.Error(w, "ip must be a tailnet address", http.StatusBadRequest)
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		response, err := lookup(ctx, address.String())
		if err != nil || response == nil || response.Node == nil || response.UserProfile == nil {
			http.Error(w, "no tailnet peer at that address", http.StatusNotFound)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(identityOf(response))
	})
	return mux
}

// identityOf renders a WhoIs answer the way Tailscale Serve names the same
// peer in its identity headers: a tagged node has no user, so it is named as
// a tagged device rather than by whoever enrolled it.
func identityOf(response *apitype.WhoIsResponse) identity {
	if response.Node.IsTagged() {
		return identity{Login: "tagged-devices", Name: "Tagged Device"}
	}
	return identity{
		Login:      response.UserProfile.LoginName,
		Name:       response.UserProfile.DisplayName,
		ProfilePic: response.UserProfile.ProfilePicURL,
	}
}

func startIdentity(ctx context.Context, server *tsnet.Server, errCh chan<- error) {
	client, err := server.LocalClient()
	if err != nil {
		errCh <- fmt.Errorf("opening the tsnet local client: %w", err)
		return
	}
	listener, err := net.Listen("tcp", identityAddress)
	if err != nil {
		errCh <- fmt.Errorf("listening for identity lookups: %w", err)
		return
	}
	identityServer := &http.Server{Handler: identityHandler(client.WhoIs), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := identityServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("serving identity lookups: %w", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = identityServer.Close()
	}()
	log.Printf("identity lookups listening on %s", identityAddress)
}
