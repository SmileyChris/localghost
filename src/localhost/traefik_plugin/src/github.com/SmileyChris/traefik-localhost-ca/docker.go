package traefik_localhost_ca

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"regexp"
	"sort"
	"strings"
	"time"
)

const (
	metadataDomainsLabel  = "io.localhost.tls-domains"
	maxMetadataDomains    = 32
	maxMetadataLabelBytes = 4096
	maxDockerResponse     = 16 << 20
)

var (
	projectNameRE = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
	dnsLabelRE    = regexp.MustCompile(`^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$`)
)

// ContainerInfo contains the certificate discovery metadata from one opted-in
// running container on the configured network. ProjectName is empty for a
// non-Compose managed-host container.
type ContainerInfo struct {
	ID              string
	Name            string
	ProjectName     string
	MetadataDomains []string
	Networks        []string
}

type dockerContainer struct {
	ID              string            `json:"Id"`
	Names           []string          `json:"Names"`
	State           string            `json:"State"`
	Labels          map[string]string `json:"Labels"`
	NetworkSettings *struct {
		Networks map[string]struct{} `json:"Networks"`
	} `json:"NetworkSettings"`
}

type DockerClient struct {
	endpoint string
	network  string
	client   *http.Client
}

func NewDockerClient(endpoint, network string) (*DockerClient, error) {
	if !strings.HasPrefix(endpoint, "unix://") {
		return nil, fmt.Errorf("docker endpoint must be unix://, got %q", endpoint)
	}
	socketPath := strings.TrimPrefix(endpoint, "unix://")
	if socketPath == "" {
		return nil, fmt.Errorf("docker endpoint socket path must not be empty")
	}
	transport := &http.Transport{
		DialContext: func(_ context.Context, _, _ string) (net.Conn, error) {
			return net.Dial("unix", socketPath)
		},
		MaxIdleConns:      1,
		IdleConnTimeout:   30 * time.Second,
		DisableKeepAlives: false,
	}
	return &DockerClient{
		endpoint: socketPath,
		network:  network,
		client:   &http.Client{Transport: transport, Timeout: 10 * time.Second},
	}, nil
}

func (d *DockerClient) ListOptedInContainers(ctx context.Context) ([]ContainerInfo, error) {
	filters := map[string][]string{
		"status": {"running"},
		"label":  {"traefik.enable=true"},
	}
	filterJSON, err := json.Marshal(filters)
	if err != nil {
		return nil, fmt.Errorf("marshaling Docker filters: %w", err)
	}
	url := fmt.Sprintf("http://localhost/containers/json?all=false&filters=%s", urlQueryEscape(string(filterJSON)))
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, fmt.Errorf("creating Docker request: %w", err)
	}
	resp, err := d.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("Docker API request failed: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("Docker API returned status %d", resp.StatusCode)
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, maxDockerResponse+1))
	if err != nil {
		return nil, fmt.Errorf("reading Docker response: %w", err)
	}
	if len(body) > maxDockerResponse {
		return nil, fmt.Errorf("Docker response exceeds %d bytes", maxDockerResponse)
	}
	var containers []dockerContainer
	if err := json.Unmarshal(body, &containers); err != nil {
		return nil, fmt.Errorf("decoding Docker response: %w", err)
	}

	result := make([]ContainerInfo, 0, len(containers))
	for _, c := range containers {
		if c.State != "" && c.State != "running" || !d.isOnNetwork(c) {
			continue
		}
		project := c.Labels["com.docker.compose.project"]
		if project != "" && !ValidateProjectName(project) {
			fmt.Fprintf(os.Stderr, "localhostCA: rejected container %s invalid project label (length %d)\n", shortID(c.ID), len(project))
			project = ""
		}

		var domains []string
		if raw, ok := c.Labels[metadataDomainsLabel]; ok {
			domains, err = ParseMetadataDomains(raw)
			if err != nil {
				fmt.Fprintf(os.Stderr, "localhostCA: rejected container %s %s label (length %d): %v\n", shortID(c.ID), metadataDomainsLabel, len(raw), err)
				// Metadata is all-or-nothing, but a valid Compose project on the
				// same container remains independently discoverable.
				domains = nil
			}
		}
		if project == "" && len(domains) == 0 {
			continue
		}
		name := ""
		if len(c.Names) > 0 {
			name = strings.TrimPrefix(c.Names[0], "/")
		}
		result = append(result, ContainerInfo{
			ID: c.ID, Name: name, ProjectName: project,
			MetadataDomains: domains, Networks: containerNetworks(c),
		})
	}
	sort.Slice(result, func(i, j int) bool { return result[i].ID < result[j].ID })
	return result, nil
}

func shortID(id string) string {
	if len(id) > 12 {
		return id[:12]
	}
	return id
}

func (d *DockerClient) isOnNetwork(c dockerContainer) bool {
	if c.NetworkSettings == nil || c.NetworkSettings.Networks == nil {
		return false
	}
	_, ok := c.NetworkSettings.Networks[d.network]
	return ok
}

func containerNetworks(c dockerContainer) []string {
	if c.NetworkSettings == nil || c.NetworkSettings.Networks == nil {
		return nil
	}
	networks := make([]string, 0, len(c.NetworkSettings.Networks))
	for name := range c.NetworkSettings.Networks {
		networks = append(networks, name)
	}
	sort.Strings(networks)
	return networks
}

// ParseMetadataDomains validates an entire comma-separated metadata value.
// Results are trimmed, deduplicated, and sorted. Any bad entry rejects the
// complete value.
func ParseMetadataDomains(raw string) ([]string, error) {
	if len(raw) > maxMetadataLabelBytes {
		return nil, fmt.Errorf("metadata label exceeds %d bytes", maxMetadataLabelBytes)
	}
	parts := strings.Split(raw, ",")
	if len(parts) > maxMetadataDomains {
		return nil, fmt.Errorf("too many domains (maximum %d)", maxMetadataDomains)
	}
	seen := make(map[string]struct{}, len(parts))
	for _, part := range parts {
		domain := strings.TrimSpace(part)
		if err := ValidateMetadataDomain(domain); err != nil {
			return nil, err
		}
		seen[domain] = struct{}{}
	}
	if len(seen) == 0 {
		return nil, fmt.Errorf("metadata domain list is empty")
	}
	result := make([]string, 0, len(seen))
	for domain := range seen {
		result = append(result, domain)
	}
	sort.Strings(result)
	return result, nil
}

// ValidateMetadataDomain enforces the documented project.localhost,
// service.project.localhost, and *.project.localhost conventions.
func ValidateMetadataDomain(domain string) error {
	if domain == "" {
		return fmt.Errorf("empty metadata domain")
	}
	if len(domain) > 253 {
		return fmt.Errorf("domain %q exceeds 253 characters", domain)
	}
	if domain != strings.ToLower(domain) {
		return fmt.Errorf("domain %q must be lowercase", domain)
	}
	labels := strings.Split(domain, ".")
	if len(labels) < 2 || labels[len(labels)-1] != "localhost" {
		return fmt.Errorf("domain %q must end in .localhost", domain)
	}
	if len(labels) != 2 && len(labels) != 3 {
		return fmt.Errorf("domain %q is outside supported hostname depth", domain)
	}
	for i, label := range labels[:len(labels)-1] {
		if label == "*" {
			if i != 0 || len(labels) != 3 {
				return fmt.Errorf("domain %q has an unsupported wildcard", domain)
			}
			continue
		}
		if !dnsLabelRE.MatchString(label) {
			return fmt.Errorf("domain %q contains an invalid DNS label", domain)
		}
	}
	return nil
}

func urlQueryEscape(s string) string {
	var result strings.Builder
	for _, b := range []byte(s) {
		switch {
		case b >= 'a' && b <= 'z', b >= 'A' && b <= 'Z', b >= '0' && b <= '9', b == '-', b == '_', b == '.', b == '~':
			result.WriteByte(b)
		default:
			fmt.Fprintf(&result, "%%%02X", b)
		}
	}
	return result.String()
}

func ValidateProjectName(name string) bool { return projectNameRE.MatchString(name) }

func ProjectDomains(project string) []string {
	if !ValidateProjectName(project) {
		return nil
	}
	return []string{project + ".localhost", "*." + project + ".localhost"}
}
