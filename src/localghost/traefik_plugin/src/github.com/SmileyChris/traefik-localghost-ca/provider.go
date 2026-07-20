// Package traefik_localghost_ca implements a Traefik provider plugin that
// supplies locally trusted TLS certificates for .localhost development domains.
package traefik_localghost_ca

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	baselineKey           = "baseline"
	maxProjects           = 100
	maxActiveCertificates = 256
)

type Config struct {
	DockerEndpoint string `json:"dockerEndpoint,omitempty"`
	Network        string `json:"network,omitempty"`
	StoragePath    string `json:"storagePath,omitempty"`
	PollInterval   string `json:"pollInterval,omitempty"`
	DomainSuffix   string `json:"domainSuffix,omitempty"`
	LeafLifetime   string `json:"leafLifetime,omitempty"`
	RenewBefore    string `json:"renewBefore,omitempty"`
}

func CreateConfig() *Config {
	return &Config{
		DockerEndpoint: "unix:///var/run/docker.sock",
		Network:        "localghost-https-poc",
		StoragePath:    "/var/lib/localghost-ca",
		PollInterval:   "250ms",
		DomainSuffix:   "localhost",
		LeafLifetime:   "24h",
		RenewBefore:    "6h",
	}
}

type projectCert struct {
	certPEM   []byte
	keyPEM    []byte
	domains   []string
	notBefore time.Time
	expires   time.Time
}

type certSpec struct {
	key     string
	kind    string
	name    string
	domains []string
	dir     string
}

type Provider struct {
	name           string
	pollInterval   time.Duration
	leafLifetime   time.Duration
	renewBefore    time.Duration
	dockerEndpoint string
	network        string
	storagePath    string
	domainSuffix   string

	dockerClient   *DockerClient
	listContainers func(context.Context) ([]ContainerInfo, error) // test override; nil in Traefik/Yaegi
	ca             *CertificateAuthority
	baseline       *projectCert
	storedCerts    map[string]*projectCert
	activeCerts    map[string]*projectCert
	lastSnapshot   []flatCert
	dockerLost     bool

	mu     sync.Mutex
	cancel func()
}

func New(_ context.Context, config *Config, name string) (*Provider, error) {
	pi, err := time.ParseDuration(config.PollInterval)
	if err != nil {
		return nil, fmt.Errorf("invalid pollInterval %q: %w", config.PollInterval, err)
	}
	ll, err := time.ParseDuration(config.LeafLifetime)
	if err != nil {
		return nil, fmt.Errorf("invalid leafLifetime %q: %w", config.LeafLifetime, err)
	}
	rbValue := config.RenewBefore
	if rbValue == "" {
		rbValue = "6h"
	}
	rb, err := time.ParseDuration(rbValue)
	if err != nil {
		return nil, fmt.Errorf("invalid renewBefore %q: %w", rbValue, err)
	}
	return &Provider{
		name: name, pollInterval: pi, leafLifetime: ll, renewBefore: rb,
		dockerEndpoint: config.DockerEndpoint, network: config.Network,
		storagePath: config.StoragePath, domainSuffix: config.DomainSuffix,
		storedCerts: make(map[string]*projectCert), activeCerts: make(map[string]*projectCert),
	}, nil
}

func (p *Provider) Init() error {
	if p.pollInterval < 100*time.Millisecond {
		return fmt.Errorf("pollInterval %v is below minimum 100ms", p.pollInterval)
	}
	if p.leafLifetime < time.Hour || p.leafLifetime > 720*time.Hour {
		return fmt.Errorf("leafLifetime %v is outside allowed range [1h, 720h]", p.leafLifetime)
	}
	if p.renewBefore <= 0 || p.renewBefore >= p.leafLifetime {
		return fmt.Errorf("renewBefore must be greater than zero and less than leafLifetime")
	}
	if p.domainSuffix != "localhost" {
		return fmt.Errorf("domainSuffix must be \"localhost\", got %q", p.domainSuffix)
	}
	if p.storagePath == "" {
		return fmt.Errorf("storagePath must not be empty")
	}
	if !strings.HasPrefix(p.dockerEndpoint, "unix://") {
		return fmt.Errorf("dockerEndpoint must be a unix:// URI, got %q", p.dockerEndpoint)
	}
	if p.network == "" {
		return fmt.Errorf("network must not be empty")
	}

	client, err := NewDockerClient(p.dockerEndpoint, p.network)
	if err != nil {
		return fmt.Errorf("creating Docker client: %w", err)
	}
	// Keep the concrete client. Assigning it to an interpreted interface field
	// triggers a reflect.Set panic in Traefik's Yaegi runtime.
	p.dockerClient = client

	if err := validateStorageDirectory(p.storagePath); err != nil {
		return err
	}
	ca, err := LoadSignerCA(p.storagePath)
	if err != nil {
		return fmt.Errorf("loading bootstrapped signer: %w", err)
	}
	p.ca = ca

	if err := p.loadStoredCertificates(); err != nil {
		return fmt.Errorf("validating stored certificates: %w", err)
	}
	if err := p.ensureBaseline(false); err != nil {
		return fmt.Errorf("ensuring baseline certificate: %w", err)
	}
	fmt.Fprintf(os.Stderr, "localghostCA[%s]: initialized (CA fingerprint: %s)\n", p.name, p.ca.Fingerprint())
	return nil
}

func (p *Provider) Provide(cfgChan chan<- json.Marshaler) error {
	ctx, cancel := context.WithCancel(context.Background())
	p.cancel = cancel
	go p.loop(ctx, cfgChan)
	return nil
}

func (p *Provider) Stop() error {
	if p.cancel != nil {
		p.cancel()
	}
	return nil
}

func (p *Provider) loop(ctx context.Context, cfgChan chan<- json.Marshaler) {
	p.publish(ctx, cfgChan)
	ticker := time.NewTicker(p.pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ticker.C:
			p.publish(ctx, cfgChan)
		case <-ctx.Done():
			return
		}
	}
}

func validateStorageDirectory(path string) error {
	if err := validateManagedDir(path); err != nil {
		return fmt.Errorf("checking signer storage directory: %w", err)
	}
	return nil
}

func (p *Provider) discoverContainers(ctx context.Context) ([]ContainerInfo, error) {
	if p.listContainers != nil {
		return p.listContainers(ctx)
	}
	return p.dockerClient.ListOptedInContainers(ctx)
}

func (p *Provider) publish(ctx context.Context, cfgChan chan<- json.Marshaler) {
	containers, err := p.discoverContainers(ctx)
	if err != nil {
		p.mu.Lock()
		if !p.dockerLost {
			fmt.Fprintf(os.Stderr, "localghostCA[%s]: Docker discovery lost: %v; retaining last snapshot\n", p.name, err)
			p.dockerLost = true
		}
		p.mu.Unlock()
		return
	}

	p.mu.Lock()
	if p.dockerLost {
		fmt.Fprintf(os.Stderr, "localghostCA[%s]: Docker discovery recovered\n", p.name)
		p.dockerLost = false
	}

	if err := p.ensureBaseline(true); err != nil {
		fmt.Fprintf(os.Stderr, "localghostCA[%s]: baseline renewal failed: %v; retaining last snapshot\n", p.name, err)
		p.mu.Unlock()
		return
	}
	desired, err := p.desiredSpecs(containers)
	if err != nil {
		fmt.Fprintf(os.Stderr, "localghostCA[%s]: discovery rejected: %v; retaining last snapshot\n", p.name, err)
		p.mu.Unlock()
		return
	}
	newActive := make(map[string]*projectCert, len(desired))
	for _, spec := range desired {
		cert, err := p.ensureSpec(spec, true)
		if err != nil {
			fmt.Fprintf(os.Stderr, "localghostCA[%s]: certificate update failed for %s %q: %v; retaining last snapshot\n", p.name, spec.kind, spec.name, err)
			p.mu.Unlock()
			return
		}
		newActive[spec.key] = cert
	}
	for key := range p.activeCerts {
		if _, ok := newActive[key]; !ok {
			fmt.Fprintf(os.Stderr, "localghostCA[%s]: removed %s from active snapshot\n", p.name, key)
		}
	}
	p.activeCerts = newActive
	snapshot := p.buildSnapshot()
	if snapshotsEqual(snapshot, p.lastSnapshot) {
		p.mu.Unlock()
		return
	}
	p.lastSnapshot = snapshot
	p.mu.Unlock()

	fmt.Fprintf(os.Stderr, "localghostCA[%s]: publishing complete TLS snapshot (%d certificates)\n", p.name, len(snapshot))
	if ctx.Err() != nil {
		return
	}
	// A select between this interpreted json.Marshaler value and ctx.Done()
	// panics in Yaegi v0.16.1. Keep the send outside p.mu and check
	// cancellation immediately before it.
	cfgChan <- &tlsPayload{certs: snapshot}
}

func (p *Provider) desiredSpecs(containers []ContainerInfo) ([]certSpec, error) {
	byKey := make(map[string]certSpec)
	claimed := make(map[string]struct{})
	projects := make(map[string]struct{})
	metadata := make(map[string]struct{})
	for _, c := range containers {
		if c.ProjectName != "" {
			projects[c.ProjectName] = struct{}{}
		}
		for _, domain := range c.MetadataDomains {
			metadata[domain] = struct{}{}
		}
	}
	if len(projects) > maxProjects {
		return nil, fmt.Errorf("discovered %d projects, maximum is %d", len(projects), maxProjects)
	}
	projectNames := sortedSetKeys(projects)
	for _, project := range projectNames {
		spec := p.projectSpec(project)
		byKey[spec.key] = spec
		for _, domain := range spec.domains {
			claimed[domain] = struct{}{}
		}
	}
	for _, domain := range sortedSetKeys(metadata) {
		if _, duplicate := claimed[domain]; duplicate {
			continue
		}
		spec := p.metadataSpec(domain)
		byKey[spec.key] = spec
		claimed[domain] = struct{}{}
	}
	keys := make([]string, 0, len(byKey))
	for key := range byKey {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	if len(keys) > maxActiveCertificates {
		return nil, fmt.Errorf("discovered %d certificates, maximum is %d", len(keys), maxActiveCertificates)
	}
	result := make([]certSpec, 0, len(keys))
	for _, key := range keys {
		result = append(result, byKey[key])
	}
	return result, nil
}

func sortedSetKeys(values map[string]struct{}) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	return keys
}

func (p *Provider) projectSpec(project string) certSpec {
	return certSpec{key: "project:" + project, kind: "project", name: project,
		domains: ProjectDomains(project), dir: filepath.Join(p.storagePath, "projects", project)}
}

func (p *Provider) metadataSpec(domain string) certSpec {
	digest := sha256.Sum256([]byte(domain))
	id := hex.EncodeToString(digest[:])
	return certSpec{key: "metadata:" + id, kind: "metadata", name: domain,
		domains: []string{domain}, dir: filepath.Join(p.storagePath, "metadata", id)}
}

func (p *Provider) ensureBaseline(runtime bool) error {
	spec := certSpec{key: baselineKey, kind: "baseline", name: "baseline",
		domains: []string{"localhost", "traefik.localhost"}, dir: filepath.Join(p.storagePath, "baseline")}
	cert, err := p.ensureSpec(spec, runtime)
	if err == nil {
		p.baseline = cert
	}
	return err
}

func (p *Provider) ensureSpec(spec certSpec, runtime bool) (*projectCert, error) {
	cert := p.storedCerts[spec.key]
	now := time.Now()
	if cert != nil && !now.Before(cert.notBefore) && cert.expires.Sub(now) > p.renewBefore {
		return cert, nil
	}
	action := "issued"
	if cert != nil {
		action = "renewed"
	}
	newCert, err := p.issueAndPersist(spec)
	if err != nil {
		return nil, err
	}
	p.storedCerts[spec.key] = newCert
	fmt.Fprintf(os.Stderr, "localghostCA[%s]: %s %s certificate SANs=%v expires=%s\n",
		p.name, action, spec.kind, spec.domains, newCert.expires.UTC().Format(time.RFC3339))
	_ = runtime // distinguishes the call site for readability; persistence is identical.
	return newCert, nil
}

func (p *Provider) issueAndPersist(spec certSpec) (*projectCert, error) {
	certPEM, keyPEM, err := p.ca.IssueLeaf(spec.domains, p.leafLifetime)
	if err != nil {
		return nil, fmt.Errorf("issuing leaf: %w", err)
	}
	cert, err := p.ca.ValidateLeafPair(certPEM, keyPEM, spec.domains)
	if err != nil {
		return nil, fmt.Errorf("validating newly issued leaf: %w", err)
	}
	if err := ensureSecureDir(spec.dir, 0700); err != nil {
		return nil, fmt.Errorf("creating certificate directory: %w", err)
	}
	if spec.kind == "metadata" {
		domainPath := filepath.Join(spec.dir, "domain")
		if existing, readErr := readRegularFile(domainPath, 4096); readErr == nil {
			if string(existing) != spec.name+"\n" {
				return nil, fmt.Errorf("metadata identity mismatch")
			}
		} else if os.IsNotExist(readErr) {
			if err := atomicWrite(domainPath, []byte(spec.name+"\n"), 0600); err != nil {
				return nil, fmt.Errorf("writing metadata identity: %w", err)
			}
		} else {
			return nil, fmt.Errorf("reading metadata identity: %w", readErr)
		}
	}
	validate := func(pair *pairData) error {
		_, err := p.ca.ValidateLeafPair(pair.cert, pair.key, spec.domains)
		return err
	}
	pair, err := commitVersionedPair(spec.dir, "leaf.pem", "leaf-key.pem", certPEM, keyPEM, 0644, 0600, validate, nil)
	if err != nil {
		return nil, fmt.Errorf("committing leaf pair: %w", err)
	}
	return &projectCert{certPEM: pair.cert, keyPEM: pair.key, domains: append([]string(nil), spec.domains...), notBefore: cert.NotBefore, expires: cert.NotAfter}, nil
}

func (p *Provider) loadStoredCertificates() error {
	baseline := certSpec{key: baselineKey, kind: "baseline", name: "baseline",
		domains: []string{"localhost", "traefik.localhost"}, dir: filepath.Join(p.storagePath, "baseline")}
	if err := p.loadOptionalSpec(baseline); err != nil {
		return err
	}
	if err := p.loadProjectCertificates(); err != nil {
		return err
	}
	return p.loadMetadataCertificates()
}

func (p *Provider) loadOptionalSpec(spec certSpec) error {
	pair, err := readVersionedPair(spec.dir, "leaf.pem", "leaf-key.pem")
	if err != nil {
		return fmt.Errorf("invalid committed %s state in %s: %w", spec.kind, spec.dir, err)
	}
	if pair == nil {
		return nil
	}
	if err := validatePrivateKeyPermissions(pair.keyPath); err != nil {
		return fmt.Errorf("invalid %s private-key permissions in %s: %w", spec.kind, spec.dir, err)
	}
	cert, err := p.ca.ValidateLeafPair(pair.cert, pair.key, spec.domains)
	if err != nil {
		return fmt.Errorf("invalid %s certificate in %s: %w", spec.kind, spec.dir, err)
	}
	loaded := &projectCert{certPEM: pair.cert, keyPEM: pair.key, domains: append([]string(nil), spec.domains...), notBefore: cert.NotBefore, expires: cert.NotAfter}
	p.storedCerts[spec.key] = loaded
	state := "reused"
	if time.Now().Before(cert.NotBefore) || time.Until(cert.NotAfter) <= p.renewBefore {
		state = "cached renewal-due"
	}
	fmt.Fprintf(os.Stderr, "localghostCA[%s]: %s stored %s certificate SANs=%v expires=%s\n",
		p.name, state, spec.kind, spec.domains, cert.NotAfter.UTC().Format(time.RFC3339))
	return nil
}

func (p *Provider) loadProjectCertificates() error {
	dir := filepath.Join(p.storagePath, "projects")
	if err := validateManagedDir(dir); err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("invalid projects directory: %w", err)
	}
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("reading projects directory: %w", err)
	}
	for _, entry := range entries {
		if !entry.IsDir() || !ValidateProjectName(entry.Name()) {
			return fmt.Errorf("unexpected project certificate entry %q", entry.Name())
		}
		if err := p.loadOptionalSpec(p.projectSpec(entry.Name())); err != nil {
			return err
		}
	}
	return nil
}

func (p *Provider) loadMetadataCertificates() error {
	dir := filepath.Join(p.storagePath, "metadata")
	if err := validateManagedDir(dir); err != nil {
		if os.IsNotExist(err) {
			return nil
		}
		return fmt.Errorf("invalid metadata directory: %w", err)
	}
	entries, err := os.ReadDir(dir)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return fmt.Errorf("reading metadata directory: %w", err)
	}
	for _, entry := range entries {
		if !entry.IsDir() || len(entry.Name()) != 64 {
			return fmt.Errorf("unexpected metadata certificate entry %q", entry.Name())
		}
		if _, err := hex.DecodeString(entry.Name()); err != nil {
			return fmt.Errorf("invalid metadata certificate key %q", entry.Name())
		}
		domainBytes, err := readRegularFile(filepath.Join(dir, entry.Name(), "domain"), 4096)
		if err != nil {
			return fmt.Errorf("reading metadata identity %q: %w", entry.Name(), err)
		}
		domain := strings.TrimSuffix(string(domainBytes), "\n")
		if err := ValidateMetadataDomain(domain); err != nil {
			return fmt.Errorf("invalid stored metadata domain: %w", err)
		}
		spec := p.metadataSpec(domain)
		if filepath.Base(spec.dir) != entry.Name() {
			return fmt.Errorf("metadata certificate key %q does not match domain %q", entry.Name(), domain)
		}
		if err := p.loadOptionalSpec(spec); err != nil {
			return err
		}
	}
	return nil
}

func validatePrivateKeyPermissions(path string) error {
	info, err := os.Lstat(path)
	if err != nil {
		return err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return fmt.Errorf("%s must be a regular file", path)
	}
	if info.Mode().Perm()&0077 != 0 {
		return fmt.Errorf("%s must not be accessible by group or others (mode %o)", path, info.Mode().Perm())
	}
	return nil
}

// flatCert and tlsPayload deliberately retain a custom flat JSON shape because
// Yaegi does not flatten embedded genconf certificate structs.
type flatCert struct {
	CertFile string   `json:"certFile,omitempty"`
	KeyFile  string   `json:"keyFile,omitempty"`
	Stores   []string `json:"stores,omitempty"`
}

type tlsPayload struct{ certs []flatCert }

func (p *tlsPayload) MarshalJSON() ([]byte, error) {
	return json.Marshal(map[string]interface{}{"tls": map[string]interface{}{"certificates": p.certs}})
}

// buildSnapshot creates one complete deterministic provider snapshot. Traefik
// applies a provider message as a complete configuration replacement, so old
// and new versions of the same SAN are not published together (selection
// between duplicate SAN certificates would itself be nondeterministic).
func (p *Provider) buildSnapshot() []flatCert {
	certs := make([]flatCert, 0, 1+len(p.activeCerts))
	if p.baseline != nil {
		certs = append(certs, flatCert{CertFile: string(p.baseline.certPEM), KeyFile: string(p.baseline.keyPEM)})
	}
	keys := make([]string, 0, len(p.activeCerts))
	for key := range p.activeCerts {
		keys = append(keys, key)
	}
	sort.Strings(keys)
	for _, key := range keys {
		cert := p.activeCerts[key]
		certs = append(certs, flatCert{CertFile: string(cert.certPEM), KeyFile: string(cert.keyPEM)})
	}
	return certs
}

func snapshotsEqual(left, right []flatCert) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index].CertFile != right[index].CertFile || left[index].KeyFile != right[index].KeyFile || len(left[index].Stores) != len(right[index].Stores) {
			return false
		}
		for storeIndex := range left[index].Stores {
			if left[index].Stores[storeIndex] != right[index].Stores[storeIndex] {
				return false
			}
		}
	}
	return true
}
