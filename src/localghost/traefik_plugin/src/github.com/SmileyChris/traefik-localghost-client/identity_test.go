package traefik_localghost_client

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

var identityHeaders = []string{"Tailscale-User-Login", "Tailscale-User-Name", "Tailscale-User-Profile-Pic"}

// whoisServer plays the gateway: it answers one tailnet address and counts
// how often it is asked.
func whoisServer(t *testing.T) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/whois" {
			t.Errorf("path = %s, want /whois", r.URL.Path)
		}
		switch r.URL.Query().Get("ip") {
		case "100.101.102.103":
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"login":"alice@example.com","name":"Alice Example","profilePic":"https://example.com/alice.png"}`))
		case "100.101.102.104":
			_, _ = w.Write([]byte(`{"login":"tagged-devices","name":"Tagged Device"}`))
		default:
			http.Error(w, "no tailnet peer at that address", http.StatusNotFound)
		}
	}))
	t.Cleanup(server.Close)
	return server, &calls
}

// identityOf runs one request through the middleware and returns the
// Tailscale identity headers the next handler saw.
func identityOf(t *testing.T, config *Config, remote string, claimed map[string]string) map[string]string {
	t.Helper()
	seen := map[string]string{}
	next := http.HandlerFunc(func(_ http.ResponseWriter, req *http.Request) {
		for _, name := range identityHeaders {
			if value := req.Header.Get(name); value != "" {
				seen[name] = value
			}
		}
	})
	handler, err := New(context.Background(), next, config, "localghost-client")
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
	req.RemoteAddr = remote
	for name, value := range claimed {
		req.Header.Set(name, value)
	}
	handler.ServeHTTP(httptest.NewRecorder(), req)
	return seen
}

func TestTailnetRequestsCarryTheUsersIdentity(t *testing.T) {
	server, _ := whoisServer(t)
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	got := identityOf(t, config, "100.101.102.103:5000", nil)
	want := map[string]string{
		"Tailscale-User-Login":       "alice@example.com",
		"Tailscale-User-Name":        "Alice Example",
		"Tailscale-User-Profile-Pic": "https://example.com/alice.png",
	}
	if len(got) != len(want) {
		t.Fatalf("got %v, want %v", got, want)
	}
	for name, value := range want {
		if got[name] != value {
			t.Fatalf("%s = %q, want %q", name, got[name], value)
		}
	}
}

func TestATaggedNodeHasNoProfilePicture(t *testing.T) {
	server, _ := whoisServer(t)
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	got := identityOf(t, config, "100.101.102.104:5000", nil)
	if got["Tailscale-User-Login"] != "tagged-devices" || got["Tailscale-User-Name"] != "Tagged Device" {
		t.Fatalf("got %v, want the tagged device named", got)
	}
	if _, ok := got["Tailscale-User-Profile-Pic"]; ok {
		t.Fatalf("got %v, want no profile picture header", got)
	}
}

func TestWhatAClientClaimsAboutItselfIsDropped(t *testing.T) {
	server, calls := whoisServer(t)
	claimed := map[string]string{
		"Tailscale-User-Login":       "root@example.com",
		"Tailscale-User-Name":        "Root",
		"Tailscale-User-Profile-Pic": "https://example.com/root.png",
	}
	cases := []struct {
		name, remote string
		whoisURL     string
		wantLogin    string
	}{
		{"this machine", "172.19.0.1:40226", server.URL + "/whois", ""},
		{"a container on the hub's network", "172.19.0.3:5000", server.URL + "/whois", ""},
		{"an unknown tailnet address", "100.64.0.9:5000", server.URL + "/whois", ""},
		{"a tailnet device is renamed, not trusted", "100.101.102.103:5000", server.URL + "/whois", "alice@example.com"},
		{"tailnet hosting disabled", "100.101.102.103:5000", "", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := calls.Load()
			config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: tc.whoisURL}
			got := identityOf(t, config, tc.remote, claimed)
			if got["Tailscale-User-Login"] != tc.wantLogin {
				t.Fatalf("login = %q, want %q", got["Tailscale-User-Login"], tc.wantLogin)
			}
			if tc.wantLogin == "" && len(got) != 0 {
				t.Fatalf("got %v, want every identity header dropped", got)
			}
			// Only a tailnet address is worth asking the gateway about.
			asked := calls.Load() > before
			isTailnet := tc.remote[:4] == "100." && tc.whoisURL != ""
			if asked != isTailnet {
				t.Fatalf("asked the gateway = %t, want %t", asked, isTailnet)
			}
		})
	}
}

func TestIdentityIsCachedPerAddress(t *testing.T) {
	server, calls := whoisServer(t)
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	handler, err := New(context.Background(), http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}), config, "localghost-client")
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		req := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
		req.RemoteAddr = "100.101.102.103:5000"
		handler.ServeHTTP(httptest.NewRecorder(), req)
	}
	req := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
	req.RemoteAddr = "100.64.0.9:5000"
	handler.ServeHTTP(httptest.NewRecorder(), req)
	handler.ServeHTTP(httptest.NewRecorder(), req)
	if calls.Load() != 2 {
		t.Fatalf("gateway asked %d times, want once per address, misses included", calls.Load())
	}
}

func TestACachedIdentityExpires(t *testing.T) {
	server, calls := whoisServer(t)
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	handler, err := New(context.Background(), http.HandlerFunc(func(http.ResponseWriter, *http.Request) {}), config, "localghost-client")
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	handler.(*client).identities.now = func() time.Time { return now }
	req := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
	req.RemoteAddr = "100.101.102.103:5000"
	handler.ServeHTTP(httptest.NewRecorder(), req)
	now = now.Add(identityTTL + time.Second)
	handler.ServeHTTP(httptest.NewRecorder(), req)
	if calls.Load() != 2 {
		t.Fatalf("gateway asked %d times, want again after the entry expired", calls.Load())
	}
}

func TestAnUnreachableGatewayLeavesRequestsAnonymous(t *testing.T) {
	server, _ := whoisServer(t)
	server.Close()
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	got := identityOf(t, config, "100.101.102.103:5000", map[string]string{"Tailscale-User-Login": "root@example.com"})
	if len(got) != 0 {
		t.Fatalf("got %v, want the request passed through without identity", got)
	}
}

// On plain HTTP the gateway is the connection and names the tailnet peer in
// X-Real-Ip, which Traefik keeps because the gateway is a trusted sender. The
// middleware believes that only when the connection really is the gateway.
func TestOnPlainHTTPTheGatewaysWordNamesThePeer(t *testing.T) {
	server, calls := whoisServer(t)
	gatewayHost, _, _ := net.SplitHostPort(server.Listener.Addr().String())
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	cases := []struct {
		name, remote, realIP, wantLogin string
	}{
		{"the gateway naming a device", gatewayHost + ":5000", "100.101.102.103", "alice@example.com"},
		{"the gateway naming an IPv6 device", gatewayHost + ":5000", "fd7a:115c:a1e0::1", ""},
		{"another container claiming a device", "172.19.0.7:5000", "100.101.102.103", ""},
		{"this machine claiming a device", "172.19.0.1:40226", "100.101.102.103", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := calls.Load()
			got := identityOf(t, config, tc.remote, map[string]string{"X-Real-Ip": tc.realIP, "Tailscale-User-Login": "root@example.com"})
			if got["Tailscale-User-Login"] != tc.wantLogin {
				t.Fatalf("login = %q, want %q", got["Tailscale-User-Login"], tc.wantLogin)
			}
			if asked := calls.Load() > before; asked != (tc.remote[:len(gatewayHost)] == gatewayHost) {
				t.Fatalf("asked the gateway = %t, want only when the gateway spoke", asked)
			}
		})
	}
}

func TestAnIPv6TailnetDeviceIsLookedUp(t *testing.T) {
	server, calls := whoisServer(t)
	config := &Config{RoutesPath: writeRoutes(t, routes), WhoisURL: server.URL + "/whois"}
	identityOf(t, config, "[fd7a:115c:a1e0::1]:5000", nil)
	if calls.Load() != 1 {
		t.Fatalf("gateway asked %d times, want once for the IPv6 peer", calls.Load())
	}
}
