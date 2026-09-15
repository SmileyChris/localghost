package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"

	"tailscale.com/client/tailscale/apitype"
	"tailscale.com/tailcfg"
)

func TestWhoisNamesTheTailnetUser(t *testing.T) {
	lookup := func(_ context.Context, address string) (*apitype.WhoIsResponse, error) {
		if address != "100.101.102.103" {
			t.Fatalf("looked up %q, want the queried address", address)
		}
		return &apitype.WhoIsResponse{
			Node: &tailcfg.Node{Name: "alice-laptop.tail1234.ts.net."},
			UserProfile: &tailcfg.UserProfile{
				LoginName:     "alice@example.com",
				DisplayName:   "Alice Example",
				ProfilePicURL: "https://example.com/alice.png",
			},
		}, nil
	}
	recorder := httptest.NewRecorder()
	identityHandler(lookup).ServeHTTP(recorder, httptest.NewRequest("GET", "/whois?ip=100.101.102.103", nil))
	if recorder.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", recorder.Code, recorder.Body)
	}
	var got identity
	if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := identity{Login: "alice@example.com", Name: "Alice Example", ProfilePic: "https://example.com/alice.png"}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestWhoisNamesATaggedNodeAsSuch(t *testing.T) {
	lookup := func(context.Context, string) (*apitype.WhoIsResponse, error) {
		return &apitype.WhoIsResponse{
			Node:        &tailcfg.Node{Name: "ci-runner.tail1234.ts.net.", Tags: []string{"tag:ci"}},
			UserProfile: &tailcfg.UserProfile{LoginName: "tagged-devices"},
		}, nil
	}
	recorder := httptest.NewRecorder()
	identityHandler(lookup).ServeHTTP(recorder, httptest.NewRequest("GET", "/whois?ip=100.101.102.104", nil))
	var got identity
	if err := json.Unmarshal(recorder.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	want := identity{Login: "tagged-devices", Name: "Tagged Device"}
	if got != want {
		t.Fatalf("got %+v, want %+v", got, want)
	}
}

func TestWhoisRejectsWhatItCannotAnswer(t *testing.T) {
	cases := []struct {
		name   string
		target string
		lookup whoisLookup
		status int
	}{
		{"missing address", "/whois", nil, http.StatusBadRequest},
		{"not an address", "/whois?ip=demo.tail1234", nil, http.StatusBadRequest},
		{"address with a port", "/whois?ip=100.64.0.1:443", nil, http.StatusBadRequest},
		{"unknown peer", "/whois?ip=100.64.0.9", func(context.Context, string) (*apitype.WhoIsResponse, error) {
			return nil, errors.New("peer not found")
		}, http.StatusNotFound},
		{"wrong path", "/healthz?ip=100.64.0.9", nil, http.StatusNotFound},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			lookup := tc.lookup
			if lookup == nil {
				lookup = func(context.Context, string) (*apitype.WhoIsResponse, error) {
					t.Fatal("lookup must not run")
					return nil, nil
				}
			}
			recorder := httptest.NewRecorder()
			identityHandler(lookup).ServeHTTP(recorder, httptest.NewRequest("GET", tc.target, nil))
			if recorder.Code != tc.status {
				t.Fatalf("status = %d, want %d", recorder.Code, tc.status)
			}
		})
	}
}
