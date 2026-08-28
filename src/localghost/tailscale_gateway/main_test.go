package main

import (
	"net/netip"
	"testing"

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
