// Package traefik_localghost_client implements a Traefik middleware plugin
// that names requests from this machine as 127.0.0.1.
//
// The hub publishes its ports on loopback only, yet Docker hands Traefik those
// connections from an address of its own: the bridge gateway on a native
// daemon, the VM's gateway under Docker Desktop. Left alone, every
// application would be told that address instead of the loopback the browser
// used. Everything else that reaches Traefik is a container on one of its own
// networks (the tailnet gateway among them) or a tailnet device named by the
// gateway's PROXY header. So an IPv4 source that is the gateway, or is on
// none of Traefik's networks and is not a Tailscale address, is this machine.
package traefik_localghost_client

import (
	"bufio"
	"context"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"strings"
)

type Config struct {
	// RoutesPath is the kernel's IPv4 routing table inside the Traefik
	// container: its default route names the host, and its directly
	// connected routes name the networks containers arrive from.
	RoutesPath string `json:"routesPath,omitempty"`
}

func CreateConfig() *Config { return &Config{RoutesPath: "/proc/net/route"} }

// tailnet is Tailscale's IPv4 range. Tailnet devices are off every local
// network, but they are anything but this machine.
var tailnet = &net.IPNet{IP: net.IPv4(100, 64, 0, 0).To4(), Mask: net.CIDRMask(10, 32)}

type client struct {
	next     http.Handler
	gateway  net.IP
	networks []*net.IPNet
}

func New(_ context.Context, next http.Handler, config *Config, _ string) (http.Handler, error) {
	if config == nil || config.RoutesPath == "" {
		return nil, errors.New("localghost client: routesPath is required")
	}
	gateway, networks, err := readRoutes(config.RoutesPath)
	if err != nil {
		// Without its own networks to compare against, every source would
		// look like this machine. Pass requests through untouched instead;
		// applications then see Docker's address, as they would without us.
		fmt.Fprintf(os.Stdout, "localghost client: leaving client addresses alone: %v\n", err)
		return next, nil
	}
	return &client{next: next, gateway: gateway, networks: networks}, nil
}

func (c *client) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	host, port, err := net.SplitHostPort(req.RemoteAddr)
	if err == nil && c.isThisMachine(net.ParseIP(host)) {
		req.RemoteAddr = net.JoinHostPort("127.0.0.1", port)
		// Traefik has already recorded the connection's address here. A value
		// it kept from a trusted sender is that sender's claim; leave it be.
		if req.Header.Get("X-Real-Ip") == host {
			req.Header.Set("X-Real-Ip", "127.0.0.1")
		}
	}
	c.next.ServeHTTP(rw, req)
}

func (c *client) isThisMachine(ip net.IP) bool {
	// IPv6 is left alone: this table only lists IPv4 networks, so an IPv6
	// container address would otherwise be mistaken for the host.
	ip = ip.To4()
	if ip == nil || ip.IsLoopback() {
		return false
	}
	if ip.Equal(c.gateway) {
		return true
	}
	if tailnet.Contains(ip) {
		return false
	}
	for _, network := range c.networks {
		if network.Contains(ip) {
			return false
		}
	}
	return true
}

// readRoutes returns the default gateway and the directly connected networks
// from a /proc/net/route table.
func readRoutes(path string) (net.IP, []*net.IPNet, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, nil, err
	}
	defer file.Close()
	var gateway net.IP
	var networks []*net.IPNet
	scanner := bufio.NewScanner(file)
	scanner.Scan() // header row
	for scanner.Scan() {
		fields := strings.Fields(scanner.Text())
		if len(fields) < 8 {
			continue
		}
		destination, errDestination := parseHexIPv4(fields[1])
		via, errVia := parseHexIPv4(fields[2])
		mask, errMask := parseHexIPv4(fields[7])
		if errDestination != nil || errVia != nil || errMask != nil {
			continue
		}
		switch {
		case destination.Equal(net.IPv4zero) && !via.Equal(net.IPv4zero):
			gateway = via
		case via.Equal(net.IPv4zero):
			networks = append(networks, &net.IPNet{IP: destination, Mask: net.IPMask(mask)})
		}
	}
	if err := scanner.Err(); err != nil {
		return nil, nil, err
	}
	if gateway == nil || len(networks) == 0 {
		return nil, nil, fmt.Errorf("no default gateway and connected network in %s", path)
	}
	return gateway, networks, nil
}

// parseHexIPv4 decodes a /proc/net/route address, which is little-endian hex.
func parseHexIPv4(field string) (net.IP, error) {
	raw, err := hex.DecodeString(field)
	if err != nil {
		return nil, err
	}
	if len(raw) != 4 {
		return nil, fmt.Errorf("not an IPv4 route field: %q", field)
	}
	return net.IPv4(raw[3], raw[2], raw[1], raw[0]).To4(), nil
}
