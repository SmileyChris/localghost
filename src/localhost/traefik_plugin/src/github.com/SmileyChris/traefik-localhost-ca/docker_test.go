package traefik_localhost_ca

import (
	"context"
	"net/http"
	"reflect"
	"strings"
	"testing"
)

// TestValidateProjectName_Valid checks valid project names.
func TestValidateProjectName_Valid(t *testing.T) {
	tests := []struct {
		name string
		want bool
	}{
		{"simple", true},
		{"project-a", true},
		{"myproject1", true},
		{"a", true},
		{"abcdefghijklmnopqrstuvwxyz0123456789-abcdefghijklmn", true}, // 63 chars
		{"0leading-digit", true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ValidateProjectName(tt.name); got != tt.want {
				t.Errorf("ValidateProjectName(%q) = %v, want %v", tt.name, got, tt.want)
			}
		})
	}
}

// TestValidateProjectName_Invalid checks invalid project names.
func TestValidateProjectName_Invalid(t *testing.T) {
	tests := []struct {
		name string
		want bool
	}{
		{"", false},
		{"Project_A", false},    // uppercase
		{"project.name", false}, // dot
		{"project_name", false}, // underscore
		{"-leading-hyphen", false},
		{"trailing-hyphen-", false},
		{"a-b-c-d-e-f-g-h-i-j-k-l-m-n-o-p-q-r-s-t-u-v-w-x-y-z-0-1-2-3-4-5-6-7-8-9-a", false}, // 64 chars
		{"has space", false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := ValidateProjectName(tt.name); got != tt.want {
				t.Errorf("ValidateProjectName(%q) = %v, want %v", tt.name, got, tt.want)
			}
		})
	}
}

// TestProjectDomains_Valid verifies domain generation for valid projects.
func TestProjectDomains_Valid(t *testing.T) {
	tests := []struct {
		project string
		want    []string
	}{
		{
			project: "myapp",
			want:    []string{"myapp.localhost", "*.myapp.localhost"},
		},
		{
			project: "project-a",
			want:    []string{"project-a.localhost", "*.project-a.localhost"},
		},
		{
			project: "a",
			want:    []string{"a.localhost", "*.a.localhost"},
		},
	}
	for _, tt := range tests {
		t.Run(tt.project, func(t *testing.T) {
			got := ProjectDomains(tt.project)
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("ProjectDomains(%q) = %v, want %v", tt.project, got, tt.want)
			}
		})
	}
}

// TestProjectDomains_Invalid verifies that invalid project names return nil.
func TestProjectDomains_Invalid(t *testing.T) {
	tests := []string{
		"",
		"has_underscore",
		"-leading-hyphen",
		"CAPS",
	}
	for _, project := range tests {
		t.Run(project, func(t *testing.T) {
			if got := ProjectDomains(project); got != nil {
				t.Errorf("ProjectDomains(%q) = %v, want nil", project, got)
			}
		})
	}
}

// TestURLQueryEscape verifies that urlQueryEscape produces valid percent-
// encoded strings for Docker API filter parameters.
func TestURLQueryEscape(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"simple", "simple"},
		{"a b", "a%20b"},
		{"foo/bar", "foo%2Fbar"},
		{"{status:running}", "%7Bstatus%3Arunning%7D"},
	}
	for _, tt := range tests {
		t.Run(tt.input, func(t *testing.T) {
			got := urlQueryEscape(tt.input)
			if got != tt.want {
				t.Errorf("urlQueryEscape(%q) = %q, want %q", tt.input, got, tt.want)
			}
		})
	}
}

// TestNewDockerClient_InvalidEndpoint verifies that non-unix endpoints are
// rejected.
func TestNewDockerClient_InvalidEndpoint(t *testing.T) {
	_, err := NewDockerClient("tcp://127.0.0.1:2375", "test-net")
	if err == nil {
		t.Error("expected error for TCP endpoint")
	}
}

// TestNewDockerClient_Valid creates a client with a valid unix endpoint.
// It does not test actual Docker API communication.
func TestNewDockerClient_Valid(t *testing.T) {
	client, err := NewDockerClient("unix:///var/run/docker.sock", "test-net")
	if err != nil {
		t.Fatalf("NewDockerClient failed: %v", err)
	}
	if client == nil {
		t.Fatal("expected non-nil client")
	}
	if client.network != "test-net" {
		t.Errorf("network = %q, want %q", client.network, "test-net")
	}
	if client.endpoint != "/var/run/docker.sock" {
		t.Errorf("endpoint = %q, want %q", client.endpoint, "/var/run/docker.sock")
	}
}

// TestContainerNetworks verifies network extraction from dockerContainer.
func TestContainerNetworks(t *testing.T) {
	dc := dockerContainer{
		NetworkSettings: &struct {
			Networks map[string]struct{} `json:"Networks"`
		}{Networks: map[string]struct{}{"net1": {}, "net2": {}}},
	}
	nets := containerNetworks(dc)
	if !reflect.DeepEqual(nets, []string{"net1", "net2"}) {
		t.Fatalf("networks = %v, want sorted result", nets)
	}
}

func TestParseMetadataDomains(t *testing.T) {
	got, err := ParseMetadataDomains(" service.demo.localhost, demo.localhost,service.demo.localhost, *.demo.localhost ")
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"*.demo.localhost", "demo.localhost", "service.demo.localhost"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("ParseMetadataDomains = %v, want %v", got, want)
	}
}

func TestParseMetadataDomainsRejectsWholeValue(t *testing.T) {
	invalid := []string{
		"", "localhost", "Demo.localhost", "bad_name.localhost", "example.com",
		"third.mailpit.demo.localhost", "demo.*.localhost", "*foo.demo.localhost",
		"-bad.localhost", "bad-.localhost", "a..localhost",
	}
	for _, value := range invalid {
		t.Run(value, func(t *testing.T) {
			if got, err := ParseMetadataDomains("valid.localhost," + value); err == nil || got != nil {
				t.Fatalf("expected all-or-nothing rejection, got %v, %v", got, err)
			}
		})
	}
	tooMany := make([]string, maxMetadataDomains+1)
	for i := range tooMany {
		tooMany[i] = "valid.localhost"
	}
	if _, err := ParseMetadataDomains(strings.Join(tooMany, ",")); err == nil {
		t.Fatal("expected bounded domain count rejection")
	}
	if _, err := ParseMetadataDomains(strings.Repeat("a", maxMetadataLabelBytes+1)); err == nil {
		t.Fatal("expected metadata label byte limit rejection")
	}
}

func TestDockerDiscoveryIncludesManagedHostAndCompose(t *testing.T) {
	body := `[
	  {"Id":"bbbbbbbbbbbb2","Names":["/managed"],"State":"running","Labels":{"traefik.enable":"true","io.localhost.tls-domains":"host.localhost, *.host.localhost"},"NetworkSettings":{"Networks":{"test-net":{}}}},
	  {"Id":"aaaaaaaaaaaa1","Names":["/compose"],"State":"running","Labels":{"traefik.enable":"true","com.docker.compose.project":"demo"},"NetworkSettings":{"Networks":{"test-net":{}}}},
	  {"Id":"cccccccccccc3","State":"running","Labels":{"traefik.enable":"true","io.localhost.tls-domains":"example.com"},"NetworkSettings":{"Networks":{"test-net":{}}}},
	  {"Id":"dddddddddddd4","State":"running","Labels":{"traefik.enable":"true","io.localhost.tls-domains":"wrong.localhost"},"NetworkSettings":{"Networks":{"wrong-net":{}}}}
	]`
	client, err := NewDockerClient("unix:///unused", "test-net")
	if err != nil {
		t.Fatal(err)
	}
	client.client = &http.Client{Transport: roundTripFunc(func(*http.Request) (*http.Response, error) {
		return dockerResponse(body), nil
	})}
	got, err := client.ListOptedInContainers(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("got %d containers, want managed and Compose only: %#v", len(got), got)
	}
	if got[0].ProjectName != "demo" || !reflect.DeepEqual(got[1].MetadataDomains, []string{"*.host.localhost", "host.localhost"}) {
		t.Fatalf("unexpected deterministic discovery: %#v", got)
	}
}
