package main

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/netip"
	"net/url"
	"os"
	"os/signal"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/miekg/dns"
	"tailscale.com/tsnet"
)

var dnsLabel = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)

// The health listener binds container loopback, not the tailnet: it exists
// only for Docker's healthcheck, which runs this same binary with
// --health-probe inside the container.
const healthAddress = "127.0.0.1:41823"

type configuration struct {
	suffix      string
	hostname    string
	stateDir    string
	rootCA      string
	httpTarget  string
	httpsTarget string
	bootstrap   bool
}

func main() {
	var cfg configuration
	flag.StringVar(&cfg.suffix, "suffix", "", "private DNS suffix")
	flag.StringVar(&cfg.hostname, "hostname", "", "tailnet node hostname")
	flag.StringVar(&cfg.stateDir, "state-dir", "/var/lib/localghost-tailscale", "tsnet state directory")
	flag.StringVar(&cfg.rootCA, "root-ca", "/var/lib/localghost-root/rootCA.pem", "public root certificate")
	flag.StringVar(&cfg.httpTarget, "http-target", "traefik:80", "HTTP proxy target")
	flag.StringVar(&cfg.httpsTarget, "https-target", "traefik:443", "HTTPS TCP target")
	flag.BoolVar(&cfg.bootstrap, "bootstrap", false, "enroll from an auth key on stdin, then exit")
	healthProbe := flag.Bool("health-probe", false, "check the running gateway's health listener, then exit")
	flag.Parse()
	if *healthProbe {
		if err := probeHealth("http://" + healthAddress); err != nil {
			log.Fatal(err)
		}
		return
	}
	if err := validateConfiguration(cfg); err != nil {
		log.Fatal(err)
	}
	if cfg.bootstrap {
		if err := bootstrap(cfg); err != nil {
			log.Fatal(err)
		}
		return
	}

	ctx, cancel := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer cancel()
	if err := run(ctx, cfg); err != nil && !errors.Is(err, context.Canceled) {
		log.Fatal(err)
	}
}

func bootstrap(cfg configuration) error {
	value, err := io.ReadAll(io.LimitReader(os.Stdin, 4097))
	if err != nil {
		return fmt.Errorf("reading auth key: %w", err)
	}
	if len(value) > 4096 {
		return errors.New("auth key exceeds 4096 bytes")
	}
	authKey := strings.TrimSpace(string(value))
	if authKey == "" {
		return errors.New("auth key is required on stdin")
	}
	server := &tsnet.Server{
		Hostname: cfg.hostname,
		Dir:      cfg.stateDir,
		AuthKey:  authKey,
		Logf:     log.Printf,
	}
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	if _, err := server.Up(ctx); err != nil {
		return fmt.Errorf("enrolling tsnet node: %w", err)
	}
	ip4, ip6 := server.TailscaleIPs()
	if !ip4.IsValid() {
		return errors.New("enrolled node has no Tailscale IPv4 address")
	}
	fmt.Printf("IPv4=%s\nIPv6=%s\n", ip4, ip6)
	return nil
}

func validateConfiguration(cfg configuration) error {
	if !dnsLabel.MatchString(cfg.suffix) {
		return fmt.Errorf("suffix %q must be one lowercase DNS label", cfg.suffix)
	}
	if !dnsLabel.MatchString(cfg.hostname) {
		return fmt.Errorf("hostname %q must be one lowercase DNS label", cfg.hostname)
	}
	if len(cfg.hostname) > 63 {
		return fmt.Errorf("hostname %q exceeds 63 characters", cfg.hostname)
	}
	for name, target := range map[string]string{"HTTP": cfg.httpTarget, "HTTPS": cfg.httpsTarget} {
		if _, _, err := net.SplitHostPort(target); err != nil {
			return fmt.Errorf("invalid %s target: %w", name, err)
		}
	}
	return nil
}

func run(ctx context.Context, cfg configuration) error {
	server := &tsnet.Server{
		Hostname: cfg.hostname,
		Dir:      cfg.stateDir,
		AuthKey:  os.Getenv("TS_AUTHKEY"),
		Logf:     log.Printf,
	}
	defer server.Close()
	upContext, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	if _, err := server.Up(upContext); err != nil {
		return fmt.Errorf("connecting persisted tsnet node: %w", err)
	}
	ip4, ip6 := server.TailscaleIPs()
	if !ip4.IsValid() {
		return errors.New("tsnet did not receive an IPv4 address")
	}
	log.Printf("tailnet gateway ready hostname=%s IPv4=%s IPv6=%s suffix=%s", cfg.hostname, ip4, ip6, cfg.suffix)

	errCh := make(chan error, 6)
	startDNS(ctx, server, cfg.suffix, ip4, ip6, errCh)
	startHTTP(ctx, server, cfg, errCh)
	startTCPProxy(ctx, server, ":443", cfg.httpsTarget, errCh)
	startHealth(ctx, errCh)

	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-errCh:
		return err
	}
}

func startDNS(ctx context.Context, server *tsnet.Server, suffix string, ip4, ip6 netip.Addr, errCh chan<- error) {
	handler := dns.HandlerFunc(func(w dns.ResponseWriter, request *dns.Msg) {
		response := dnsResponse(request, suffix, ip4, ip6)
		_ = w.WriteMsg(response)
	})
	udp, err := server.ListenPacket("udp", dnsListenAddress(ip4))
	if err != nil {
		errCh <- fmt.Errorf("listening for UDP DNS: %w", err)
		return
	}
	tcp, err := server.Listen("tcp", ":53")
	if err != nil {
		_ = udp.Close()
		errCh <- fmt.Errorf("listening for TCP DNS: %w", err)
		return
	}
	udpServer := &dns.Server{PacketConn: udp, Handler: handler}
	tcpServer := &dns.Server{Listener: tcp, Handler: handler}
	go func() {
		if err := udpServer.ActivateAndServe(); err != nil && ctx.Err() == nil {
			errCh <- fmt.Errorf("serving UDP DNS: %w", err)
		}
	}()
	go func() {
		if err := tcpServer.ActivateAndServe(); err != nil && ctx.Err() == nil {
			errCh <- fmt.Errorf("serving TCP DNS: %w", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = udpServer.Shutdown()
		_ = tcpServer.Shutdown()
	}()
}

func dnsListenAddress(ip netip.Addr) string {
	return net.JoinHostPort(ip.String(), "53")
}

func dnsResponse(request *dns.Msg, suffix string, ip4, ip6 netip.Addr) *dns.Msg {
	response := new(dns.Msg)
	response.SetReply(request)
	response.Authoritative = true
	if len(request.Question) != 1 {
		response.Rcode = dns.RcodeFormatError
		return response
	}
	question := request.Question[0]
	name := strings.TrimSuffix(strings.ToLower(question.Name), ".")
	zone := "." + suffix
	if !strings.HasSuffix(name, zone) || name == suffix || !validTailnetName(strings.TrimSuffix(name, zone)) {
		response.Rcode = dns.RcodeNameError
		return response
	}
	switch question.Qtype {
	case dns.TypeA:
		response.Answer = append(response.Answer, &dns.A{
			Hdr: dns.RR_Header{Name: question.Name, Rrtype: dns.TypeA, Class: dns.ClassINET, Ttl: 5},
			A:   net.IP(ip4.AsSlice()),
		})
	case dns.TypeAAAA:
		if ip6.IsValid() {
			response.Answer = append(response.Answer, &dns.AAAA{
				Hdr:  dns.RR_Header{Name: question.Name, Rrtype: dns.TypeAAAA, Class: dns.ClassINET, Ttl: 5},
				AAAA: net.IP(ip6.AsSlice()),
			})
		}
	}
	return response
}

func validTailnetName(prefix string) bool {
	prefix = strings.TrimSuffix(prefix, ".")
	if prefix == "" {
		return false
	}
	parts := strings.Split(prefix, ".")
	if len(parts) > 2 {
		return false
	}
	for _, part := range parts {
		if !dnsLabel.MatchString(part) {
			return false
		}
	}
	return true
}

func healthHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok\n"))
	})
	return mux
}

func probeHealth(baseURL string) error {
	client := &http.Client{Timeout: 2 * time.Second}
	response, err := client.Get(baseURL + "/healthz")
	if err != nil {
		return fmt.Errorf("health listener unreachable: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return fmt.Errorf("health listener answered %d", response.StatusCode)
	}
	return nil
}

func startHealth(ctx context.Context, errCh chan<- error) {
	// Reached only after tsnet is up, so answering at all means the node is
	// enrolled and the tailnet listeners were started; any listener failure
	// exits the process, which Docker also observes.
	listener, err := net.Listen("tcp", healthAddress)
	if err != nil {
		errCh <- fmt.Errorf("listening for health checks: %w", err)
		return
	}
	healthServer := &http.Server{Handler: healthHandler(), ReadHeaderTimeout: 5 * time.Second}
	go func() {
		if err := healthServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("serving health checks: %w", err)
		}
	}()
	go func() {
		<-ctx.Done()
		_ = healthServer.Close()
	}()
}

func startHTTP(ctx context.Context, server *tsnet.Server, cfg configuration, errCh chan<- error) {
	listener, err := server.Listen("tcp", ":80")
	if err != nil {
		errCh <- fmt.Errorf("listening for HTTP: %w", err)
		return
	}
	target := &url.URL{Scheme: "http", Host: cfg.httpTarget}
	proxy := httputil.NewSingleHostReverseProxy(target)
	originalDirector := proxy.Director
	proxy.Director = func(request *http.Request) {
		host := request.Host
		originalDirector(request)
		request.Host = host
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, request *http.Request) {
		host := request.Host
		if parsed, _, err := net.SplitHostPort(host); err == nil {
			host = parsed
		}
		host = strings.TrimSuffix(strings.ToLower(host), ".")
		if host == "trust."+cfg.suffix && request.URL.Path == "/.well-known/localghost/root.pem" {
			serveRoot(w, cfg.rootCA)
			return
		}
		proxy.ServeHTTP(w, request)
	})
	httpServer := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		if err := httpServer.Serve(listener); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- fmt.Errorf("serving HTTP: %w", err)
		}
	}()
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = httpServer.Shutdown(shutdown)
	}()
}

func serveRoot(w http.ResponseWriter, path string) {
	value, err := os.ReadFile(filepath.Clean(path))
	if err != nil {
		http.Error(w, "public root is unavailable", http.StatusServiceUnavailable)
		return
	}
	block, rest := pem.Decode(value)
	if block == nil || block.Type != "CERTIFICATE" || len(strings.TrimSpace(string(rest))) != 0 {
		http.Error(w, "public root is invalid", http.StatusServiceUnavailable)
		return
	}
	certificate, err := x509.ParseCertificate(block.Bytes)
	if err != nil || !certificate.IsCA {
		http.Error(w, "public root is invalid", http.StatusServiceUnavailable)
		return
	}
	w.Header().Set("Content-Type", "application/x-pem-file")
	w.Header().Set("Cache-Control", "no-store")
	_, _ = w.Write(value)
}

func startTCPProxy(ctx context.Context, server *tsnet.Server, address, target string, errCh chan<- error) {
	listener, err := server.Listen("tcp", address)
	if err != nil {
		errCh <- fmt.Errorf("listening on %s: %w", address, err)
		return
	}
	go func() {
		<-ctx.Done()
		_ = listener.Close()
	}()
	go func() {
		for {
			incoming, err := listener.Accept()
			if err != nil {
				if ctx.Err() == nil {
					errCh <- fmt.Errorf("accepting on %s: %w", address, err)
				}
				return
			}
			go proxyConnection(ctx, incoming, target)
		}
	}()
}

func proxyConnection(ctx context.Context, incoming net.Conn, target string) {
	defer incoming.Close()
	dialer := net.Dialer{Timeout: 10 * time.Second}
	outgoing, err := dialer.DialContext(ctx, "tcp", target)
	if err != nil {
		log.Printf("proxy dial %s failed: %v", target, err)
		return
	}
	defer outgoing.Close()
	var wait sync.WaitGroup
	wait.Add(2)
	copyHalf := func(destination, source net.Conn) {
		defer wait.Done()
		_, _ = io.Copy(destination, source)
		if closer, ok := destination.(interface{ CloseWrite() error }); ok {
			_ = closer.CloseWrite()
		}
	}
	go copyHalf(outgoing, incoming)
	go copyHalf(incoming, outgoing)
	wait.Wait()
}
