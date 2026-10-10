package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/json"
	"encoding/pem"
	"io"
	"math/big"
	"net/http"
	"net/http/httptest"
	"net/netip"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/miekg/dns"
)

func TestDNSResponse(t *testing.T) {
	ip4 := netip.MustParseAddr("100.64.0.10")
	ip6 := netip.MustParseAddr("fd7a:115c:a1e0::10")
	tests := []struct {
		name  string
		kind  uint16
		rcode int
		count int
	}{
		{"demo.tail1234.", dns.TypeA, dns.RcodeSuccess, 1},
		{"mail.demo.tail1234.", dns.TypeAAAA, dns.RcodeSuccess, 1},
		{"tail1234.", dns.TypeA, dns.RcodeNameError, 0},
		{"too.deep.demo.tail1234.", dns.TypeA, dns.RcodeNameError, 0},
		{"demo.localhost.", dns.TypeA, dns.RcodeNameError, 0},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := new(dns.Msg)
			request.SetQuestion(test.name, test.kind)
			response := dnsResponse(request, "tail1234", ip4, ip6)
			if response.Rcode != test.rcode || len(response.Answer) != test.count {
				t.Fatalf("rcode=%d answers=%v", response.Rcode, response.Answer)
			}
		})
	}
}

func TestDNSResponseUsesACalmTTL(t *testing.T) {
	request := new(dns.Msg)
	request.SetQuestion("demo.tail1234.", dns.TypeA)
	response := dnsResponse(request, "tail1234", netip.MustParseAddr("100.64.0.10"), netip.Addr{})
	if len(response.Answer) != 1 || response.Answer[0].Header().Ttl != 60 {
		t.Fatalf("answer = %v", response.Answer)
	}
}

func testRootCA(t *testing.T) (string, string) {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Localghost .tail1234 Development Root CA"},
		NotBefore:             time.Now().Add(-time.Hour),
		NotAfter:              time.Now().Add(time.Hour),
		KeyUsage:              x509.KeyUsageCertSign,
		BasicConstraintsValid: true,
		IsCA:                  true,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "rootCA.pem")
	value := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})
	if err := os.WriteFile(path, value, 0o600); err != nil {
		t.Fatal(err)
	}
	digest := sha256.Sum256(der)
	return path, "SHA256:" + strings.ToUpper(hex.EncodeToString(digest[:]))
}

func TestTrustHostRedirectsBrowsersToTheIndex(t *testing.T) {
	path, _ := testRootCA(t)
	cfg := configuration{suffix: "tail1234", rootCA: path}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "http://trust.tail1234/", nil)
	if !serveTrust(recorder, request, cfg) {
		t.Fatal("expected the trust host to be handled")
	}
	if recorder.Code != http.StatusMovedPermanently || recorder.Header().Get("Location") != "http://localghost.tail1234/" {
		t.Fatalf("status = %d, location = %q", recorder.Code, recorder.Header().Get("Location"))
	}
}

func TestIndexHostServesTheRootAndProxiesTheRest(t *testing.T) {
	path, _ := testRootCA(t)
	cfg := configuration{suffix: "tail1234", rootCA: path}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "http://localghost.tail1234/.well-known/localghost/root.pem", nil)
	if !serveTrust(recorder, request, cfg) {
		t.Fatal("expected the root download to be handled")
	}
	expected, _ := os.ReadFile(path)
	if recorder.Body.String() != string(expected) {
		t.Fatalf("root.pem body = %q", recorder.Body.String())
	}
	request = httptest.NewRequest("GET", "http://localghost.tail1234/", nil)
	if serveTrust(httptest.NewRecorder(), request, cfg) {
		t.Fatal("expected the index page to be left to Traefik")
	}
}

func TestTailnetAddressDescribesTheHub(t *testing.T) {
	path, _ := testRootCA(t)
	cfg := configuration{suffix: "tail1234", rootCA: path}
	expected, _ := os.ReadFile(path)
	for _, host := range []string{"100.64.0.10", "[fd7a:115c:a1e0::1]"} {
		recorder := httptest.NewRecorder()
		request := httptest.NewRequest("GET", "http://"+host+"/.well-known/localghost/hub.json", nil)
		if !serveTrust(recorder, request, cfg) {
			t.Fatalf("expected %s to be handled", host)
		}
		var hub struct{ Suffix, Root string }
		if err := json.NewDecoder(recorder.Body).Decode(&hub); err != nil {
			t.Fatal(err)
		}
		if hub.Suffix != "tail1234" || hub.Root != string(expected) {
			t.Fatalf("hub = %+v", hub)
		}
	}
	request := httptest.NewRequest("GET", "http://100.64.0.10/", nil)
	if serveTrust(httptest.NewRecorder(), request, cfg) {
		t.Fatal("expected other paths on the address to be left to Traefik")
	}
}

func TestTrustHostStillServesTheRootPEM(t *testing.T) {
	path, _ := testRootCA(t)
	cfg := configuration{suffix: "tail1234", rootCA: path}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "http://trust.tail1234/.well-known/localghost/root.pem", nil)
	if !serveTrust(recorder, request, cfg) {
		t.Fatal("expected the trust host to be handled")
	}
	expected, _ := os.ReadFile(path)
	body, _ := io.ReadAll(recorder.Result().Body)
	if string(body) != string(expected) {
		t.Fatalf("root.pem body = %q", body)
	}
}

func TestOtherHostsAreLeftToTheProxy(t *testing.T) {
	path, _ := testRootCA(t)
	cfg := configuration{suffix: "tail1234", rootCA: path}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "http://demo.tail1234/", nil)
	if serveTrust(recorder, request, cfg) {
		t.Fatal("expected proxy hosts to be left alone")
	}
}

func TestValidateConfiguration(t *testing.T) {
	valid := configuration{suffix: "tail1234", hostname: "localghost-tail1234", httpTarget: "traefik:80", httpsTarget: "traefik:443"}
	if err := validateConfiguration(valid); err != nil {
		t.Fatal(err)
	}
	invalid := valid
	invalid.suffix = "not.valid"
	if err := validateConfiguration(invalid); err == nil {
		t.Fatal("expected invalid suffix")
	}
}

func TestDNSListenAddressIncludesAnExplicitIP(t *testing.T) {
	ip := netip.MustParseAddr("100.64.0.10")
	if got := dnsListenAddress(ip); got != "100.64.0.10:53" {
		t.Fatalf("dnsListenAddress() = %q", got)
	}
}

func TestDNSListenAddressesCoverBothFamilies(t *testing.T) {
	ip4 := netip.MustParseAddr("100.64.0.10")
	ip6 := netip.MustParseAddr("fd7a:115c:a1e0::10")
	got := dnsListenAddresses(ip4, ip6)
	want := []string{"100.64.0.10:53", "[fd7a:115c:a1e0::10]:53"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("dnsListenAddresses() = %v", got)
	}
	if got := dnsListenAddresses(ip4, netip.Addr{}); !reflect.DeepEqual(got, []string{"100.64.0.10:53"}) {
		t.Fatalf("dnsListenAddresses() without IPv6 = %v", got)
	}
}

func TestHealthHandlerAnswersOnlyHealthz(t *testing.T) {
	server := httptest.NewServer(healthHandler())
	defer server.Close()
	response, err := http.Get(server.URL + "/healthz")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("healthz status = %d", response.StatusCode)
	}
	response, err = http.Get(server.URL + "/other")
	if err != nil {
		t.Fatal(err)
	}
	response.Body.Close()
	if response.StatusCode != http.StatusNotFound {
		t.Fatalf("other status = %d", response.StatusCode)
	}
}

func TestProbeHealthReportsListenerState(t *testing.T) {
	server := httptest.NewServer(healthHandler())
	if err := probeHealth(server.URL); err != nil {
		t.Fatalf("healthy probe failed: %v", err)
	}
	server.Close()
	if err := probeHealth(server.URL); err == nil {
		t.Fatal("expected probe of a closed listener to fail")
	}
	failing := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusServiceUnavailable)
	}))
	defer failing.Close()
	if err := probeHealth(failing.URL); err == nil {
		t.Fatal("expected probe of an unhealthy listener to fail")
	}
}

func TestHTTPOnlyGatewayLeavesTheTrustHostToTheProxy(t *testing.T) {
	cfg := configuration{suffix: "work"}
	if cfg.servesHTTPS() {
		t.Fatal("expected an empty root path to mean HTTP only")
	}
	recorder := httptest.NewRecorder()
	request := httptest.NewRequest("GET", "http://trust.work/", nil)
	if serveTrust(recorder, request, cfg) {
		t.Fatal("expected no trust page without a root")
	}
}

func TestHTTPOnlyConfigurationNeedsNoHTTPSTarget(t *testing.T) {
	cfg := configuration{suffix: "work", hostname: "localghost-work", httpTarget: "traefik:80"}
	if err := validateConfiguration(cfg); err != nil {
		t.Fatal(err)
	}
	withRoot := cfg
	withRoot.rootCA = "/var/lib/localghost-root/rootCA.pem"
	if err := validateConfiguration(withRoot); err == nil {
		t.Fatal("expected an HTTPS target to be required alongside a root")
	}
}
