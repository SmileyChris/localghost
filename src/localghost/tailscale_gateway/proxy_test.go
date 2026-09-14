package main

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"
)

func TestHTTPProxyReplacesWhatTheClientClaimsAboutItself(t *testing.T) {
	seen := make(chan *http.Request, 1)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		seen <- request
	}))
	defer backend.Close()
	target, err := url.Parse(backend.URL)
	if err != nil {
		t.Fatal(err)
	}

	request := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
	request.RemoteAddr = "100.101.102.103:4242"
	request.Header.Set("X-Forwarded-For", "127.0.0.1")
	request.Header.Set("X-Real-Ip", "127.0.0.1")
	request.Header.Set("X-Forwarded-Port", "1")
	request.Header.Set("Forwarded", "for=127.0.0.1")
	newHTTPProxy(target.Host).ServeHTTP(httptest.NewRecorder(), request)

	received := <-seen
	if got := received.Header.Values("X-Forwarded-For"); len(got) != 1 || got[0] != "100.101.102.103" {
		t.Fatalf("X-Forwarded-For = %q, want only the tailnet peer", got)
	}
	if got := received.Header.Get("X-Real-Ip"); got != "100.101.102.103" {
		t.Fatalf("X-Real-Ip = %q, want the tailnet peer", got)
	}
	if got := received.Header.Get("X-Forwarded-Proto"); got != "http" {
		t.Fatalf("X-Forwarded-Proto = %q, want http", got)
	}
	for _, name := range []string{"X-Forwarded-Port", "Forwarded"} {
		if got := received.Header.Get(name); got != "" {
			t.Fatalf("%s = %q, want the client's claim dropped", name, got)
		}
	}
	if received.Host != "demo.tail1234" {
		t.Fatalf("Host = %q, want the original host", received.Host)
	}
}

func TestProxyProtocolHeaderNamesTheTailnetPeer(t *testing.T) {
	tcp := func(ip string, port int) net.Addr {
		return &net.TCPAddr{IP: net.ParseIP(ip), Port: port}
	}
	cases := []struct {
		name        string
		source      net.Addr
		destination net.Addr
		want        string
	}{
		{"ipv4", tcp("100.101.102.103", 4242), tcp("100.64.0.1", 443), "PROXY TCP4 100.101.102.103 100.64.0.1 4242 443\r\n"},
		{"ipv6", tcp("fd7a:115c:a1e0::1", 4242), tcp("fd7a:115c:a1e0::2", 443), "PROXY TCP6 fd7a:115c:a1e0::1 fd7a:115c:a1e0::2 4242 443\r\n"},
		{"mixed families", tcp("fd7a:115c:a1e0::1", 4242), tcp("100.64.0.1", 443), "PROXY UNKNOWN\r\n"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := proxyProtocolHeader(tc.source, tc.destination); got != tc.want {
				t.Fatalf("got %q, want %q", got, tc.want)
			}
		})
	}
}

// relayOnce accepts one connection on a fresh listener, relays it to a fresh
// target, and returns the client end and the target's end of the stream.
func relayOnce(t *testing.T, proxyProtocol bool) (client, upstream net.Conn) {
	t.Helper()
	target, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = target.Close() })
	front, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = front.Close() })
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() {
		incoming, err := front.Accept()
		if err != nil {
			return
		}
		proxyConnection(ctx, incoming, target.Addr().String(), proxyProtocol)
	}()
	client, err = net.Dial("tcp", front.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = client.Close() })
	upstream, err = target.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = upstream.Close() })
	_ = upstream.SetReadDeadline(time.Now().Add(5 * time.Second))
	return client, upstream
}

func TestProxyConnectionAnnouncesThePeerBeforeTheStream(t *testing.T) {
	client, upstream := relayOnce(t, true)
	if _, err := client.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	reader := bufio.NewReader(upstream)
	line, err := reader.ReadString('\n')
	if err != nil {
		t.Fatal(err)
	}
	want := fmt.Sprintf("PROXY TCP4 127.0.0.1 127.0.0.1 %d %d\r\n",
		client.LocalAddr().(*net.TCPAddr).Port, client.RemoteAddr().(*net.TCPAddr).Port)
	if line != want {
		t.Fatalf("header = %q, want %q", line, want)
	}
	payload := make([]byte, 5)
	if _, err := io.ReadFull(reader, payload); err != nil || string(payload) != "hello" {
		t.Fatalf("payload = %q (%v), want the client's bytes untouched", payload, err)
	}
}

func TestProxyConnectionWithoutProxyProtocolRelaysOnlyTheStream(t *testing.T) {
	client, upstream := relayOnce(t, false)
	if _, err := client.Write([]byte("hello")); err != nil {
		t.Fatal(err)
	}
	payload := make([]byte, 5)
	if _, err := io.ReadFull(upstream, payload); err != nil || string(payload) != "hello" {
		t.Fatalf("payload = %q (%v), want the client's bytes first", payload, err)
	}
}
