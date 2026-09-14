package traefik_localghost_client

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"
)

// A Traefik container on one network, 172.19.0.0/16, whose host is 172.19.0.1.
const routes = "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n" +
	"eth0\t00000000\t010013AC\t0003\t0\t0\t0\t00000000\t0\t0\t0\n" +
	"eth0\t000013AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n"

func writeRoutes(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "route")
	if err := os.WriteFile(path, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return path
}

// serve runs one request through the middleware and returns what the next
// handler saw as the client address and X-Real-Ip.
func serve(t *testing.T, routesPath, remote, realIP string) (string, string) {
	t.Helper()
	var seenRemote, seenRealIP string
	next := http.HandlerFunc(func(_ http.ResponseWriter, req *http.Request) {
		seenRemote, seenRealIP = req.RemoteAddr, req.Header.Get("X-Real-Ip")
	})
	handler, err := New(context.Background(), next, &Config{RoutesPath: routesPath}, "localghost-client")
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("GET", "http://demo.localhost/", nil)
	req.RemoteAddr = remote
	if realIP != "" {
		req.Header.Set("X-Real-Ip", realIP)
	}
	handler.ServeHTTP(httptest.NewRecorder(), req)
	return seenRemote, seenRealIP
}

func TestRequestsFromThisMachineAreNamedLoopback(t *testing.T) {
	path := writeRoutes(t, routes)
	cases := []struct {
		name, remote string
	}{
		{"native daemon's bridge gateway", "172.19.0.1:40226"},
		{"Docker Desktop's VM gateway", "192.168.65.1:40226"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			host := tc.remote[:len(tc.remote)-len(":40226")]
			remote, realIP := serve(t, path, tc.remote, host)
			if remote != "127.0.0.1:40226" || realIP != "127.0.0.1" {
				t.Fatalf("got %s / %s, want 127.0.0.1:40226 / 127.0.0.1", remote, realIP)
			}
		})
	}
}

func TestOtherClientsKeepTheirAddress(t *testing.T) {
	path := writeRoutes(t, routes)
	cases := []struct {
		name, remote, realIP string
	}{
		{"a container on Traefik's network, such as the tailnet gateway", "172.19.0.3:5000", "172.19.0.3"},
		{"a tailnet device named by a PROXY header", "100.101.102.103:5000", "100.101.102.103"},
		{"an IPv6 peer", "[fd7a:115c:a1e0::1]:5000", "fd7a:115c:a1e0::1"},
		{"loopback itself", "127.0.0.1:5000", "127.0.0.1"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			remote, realIP := serve(t, path, tc.remote, tc.realIP)
			if remote != tc.remote || realIP != tc.realIP {
				t.Fatalf("got %s / %s, want %s / %s untouched", remote, realIP, tc.remote, tc.realIP)
			}
		})
	}
}

func TestATrustedSendersRealIPIsNotOverwritten(t *testing.T) {
	remote, realIP := serve(t, writeRoutes(t, routes), "172.19.0.1:40226", "10.9.9.9")
	if remote != "127.0.0.1:40226" || realIP != "10.9.9.9" {
		t.Fatalf("got %s / %s, want the connection renamed and the claim kept", remote, realIP)
	}
}

func TestUnreadableRoutesLeaveEveryAddressAlone(t *testing.T) {
	for name, path := range map[string]string{
		"missing table":    filepath.Join(t.TempDir(), "absent"),
		"no default route": writeRoutes(t, "Iface\tDestination\tGateway\n"),
	} {
		t.Run(name, func(t *testing.T) {
			remote, realIP := serve(t, path, "172.19.0.1:40226", "172.19.0.1")
			if remote != "172.19.0.1:40226" || realIP != "172.19.0.1" {
				t.Fatalf("got %s / %s, want the request passed through", remote, realIP)
			}
		})
	}
}

func TestReadRoutesFindsTheHostAndTheLocalNetwork(t *testing.T) {
	gateway, networks, err := readRoutes(writeRoutes(t, routes))
	if err != nil {
		t.Fatal(err)
	}
	if gateway.String() != "172.19.0.1" {
		t.Fatalf("gateway = %s, want 172.19.0.1", gateway)
	}
	if len(networks) != 1 || networks[0].String() != "172.19.0.0/16" {
		t.Fatalf("networks = %v, want [172.19.0.0/16]", networks)
	}
}
