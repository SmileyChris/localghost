// Package traefik_localghost_client implements a Traefik middleware plugin
// that names requests from this machine as 127.0.0.1 and requests from the
// tailnet by the user behind them.
//
// The hub publishes its ports on loopback only, yet Docker hands Traefik those
// connections from an address of its own: the bridge gateway on a native
// daemon, the VM's gateway under Docker Desktop. Left alone, every
// application would be told that address instead of the loopback the browser
// used. Everything else that reaches Traefik is a container on one of its own
// networks (the tailnet gateway among them) or a tailnet device named by the
// gateway's PROXY header. So an IPv4 source that is the gateway, or is on
// none of Traefik's networks and is not a Tailscale address, is this machine.
//
// A Tailscale address is a device the gateway named. With tailnet hosting
// enabled the gateway also answers who owns that address, and the middleware
// records the answer in the identity headers Tailscale Serve uses. What a
// client sent in those headers is always dropped first.
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
	// WhoisURL is the tailnet gateway's identity lookup. Empty, as on a hub
	// without tailnet hosting, leaves every request anonymous.
	WhoisURL string `json:"whoisURL,omitempty"`
}

func CreateConfig() *Config { return &Config{RoutesPath: "/proc/net/route"} }

// tailnet is Tailscale's IPv4 range. Tailnet devices are off every local
// network, but they are anything but this machine.
var tailnet = &net.IPNet{IP: net.IPv4(100, 64, 0, 0).To4(), Mask: net.CIDRMask(10, 32)}

// tailnet6 is Tailscale's IPv6 range, fd7a:115c:a1e0::/48.
var tailnet6 = &net.IPNet{IP: net.ParseIP("fd7a:115c:a1e0::"), Mask: net.CIDRMask(48, 128)}

func isTailnet(ip net.IP) bool {
	return ip != nil && (tailnet.Contains(ip) || tailnet6.Contains(ip))
}

type client struct {
	next       http.Handler
	gateway    net.IP
	networks   []*net.IPNet
	identities *identities
}

func New(_ context.Context, next http.Handler, config *Config, _ string) (http.Handler, error) {
	if config == nil || config.RoutesPath == "" {
		return nil, errors.New("localghost client: routesPath is required")
	}
	c := &client{next: next}
	gateway, networks, err := readRoutes(config.RoutesPath)
	if err != nil {
		// Without its own networks to compare against, every source would
		// look like this machine. Leave addresses untouched instead;
		// applications then see Docker's address, as they would without us.
		fmt.Fprintf(os.Stdout, "localghost client: leaving client addresses alone: %v\n", err)
	} else {
		c.gateway, c.networks = gateway, networks
	}
	if config.WhoisURL != "" {
		c.identities = newIdentities(config.WhoisURL)
	}
	return c, nil
}

func (c *client) ServeHTTP(rw http.ResponseWriter, req *http.Request) {
	var answer *identity
	host, port, err := net.SplitHostPort(req.RemoteAddr)
	if err == nil {
		ip := net.ParseIP(host)
		switch {
		case c.isThisMachine(ip):
			req.RemoteAddr = net.JoinHostPort("127.0.0.1", port)
			// Traefik has already recorded the connection's address here. A
			// value it kept from a trusted sender is that sender's claim;
			// leave it be.
			if req.Header.Get("X-Real-Ip") == host {
				req.Header.Set("X-Real-Ip", "127.0.0.1")
			}
		case c.identities != nil:
			if peer := c.tailnetPeer(ip, req.Header.Get("X-Real-Ip")); peer != nil {
				answer = c.identities.lookup(peer.String())
			}
		}
	}
	setIdentity(req.Header, answer)
	c.next.ServeHTTP(rw, req)
}

// tailnetPeer returns the tailnet device behind a request, if any. On the
// HTTPS passthrough the gateway's PROXY header made the device the
// connection itself. On plain HTTP the gateway is the connection and names
// the device in X-Real-Ip, which Traefik kept because the gateway is a
// trusted sender; any other sender's claim is worth nothing.
func (c *client) tailnetPeer(connection net.IP, realIP string) net.IP {
	if isTailnet(connection) {
		return connection
	}
	if claimed := net.ParseIP(realIP); isTailnet(claimed) && c.identities.isGateway(connection) {
		return claimed
	}
	return nil
}

func (c *client) isThisMachine(ip net.IP) bool {
	// IPv6 is left alone: this table only lists IPv4 networks, so an IPv6
	// container address would otherwise be mistaken for the host.
	ip = ip.To4()
	if ip == nil || ip.IsLoopback() || c.gateway == nil {
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
