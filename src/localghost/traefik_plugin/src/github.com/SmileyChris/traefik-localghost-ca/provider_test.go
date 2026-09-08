package traefik_localghost_ca

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type fakeLister struct {
	containers []ContainerInfo
	err        error
}

func (f *fakeLister) ListOptedInContainers(context.Context) ([]ContainerInfo, error) {
	return f.containers, f.err
}

func newTestProvider(t *testing.T) *Provider {
	t.Helper()
	config := CreateConfig()
	root, signer := t.TempDir(), t.TempDir()
	if _, err := BootstrapCA(root, signer); err != nil {
		t.Fatal(err)
	}
	config.StoragePath = signer
	config.PollInterval = "100ms"
	p, err := New(context.Background(), config, "test")
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Init(); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestConfigRenewBeforeValidation(t *testing.T) {
	for _, value := range []string{"0s", "-1s", "24h", "25h"} {
		t.Run(value, func(t *testing.T) {
			config := CreateConfig()
			root, signer := t.TempDir(), t.TempDir()
			if _, err := BootstrapCA(root, signer); err != nil {
				t.Fatal(err)
			}
			config.StoragePath = signer
			config.RenewBefore = value
			p, err := New(context.Background(), config, "test")
			if err != nil {
				if value == "-1s" {
					return
				}
				t.Fatal(err)
			}
			if err := p.Init(); err == nil {
				t.Fatalf("expected renewBefore %s to be rejected", value)
			}
		})
	}
}

func TestDesiredSpecsDeduplicatesProjectsAndMetadata(t *testing.T) {
	p := newTestProvider(t)
	containers := []ContainerInfo{
		{ProjectName: "demo", MetadataDomains: []string{"host.localhost", "host.localhost"}},
		{ProjectName: "demo", MetadataDomains: []string{"host.localhost", "other.localhost"}},
	}
	specs, err := p.desiredSpecs(containers)
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 3 {
		t.Fatalf("got %d specs, want project plus two unique metadata domains: %#v", len(specs), specs)
	}
	if specs[0].key > specs[1].key || specs[1].key > specs[2].key {
		t.Fatalf("specs are not deterministic: %#v", specs)
	}
}

func TestDesiredSpecsEnforcesGlobalLimits(t *testing.T) {
	p := newTestProvider(t)
	projects := make([]ContainerInfo, maxProjects+1)
	for i := range projects {
		projects[i].ProjectName = fmt.Sprintf("project-%03d", i)
	}
	if _, err := p.desiredSpecs(projects); err == nil {
		t.Fatal("expected project limit rejection")
	}

	metadata := make([]string, maxActiveCertificates+1)
	for i := range metadata {
		metadata[i] = fmt.Sprintf("host-%03d.localhost", i)
	}
	if _, err := p.desiredSpecs([]ContainerInfo{{MetadataDomains: metadata}}); err == nil {
		t.Fatal("expected certificate limit rejection")
	}
}

func TestPublishPreservesSnapshotOnDockerFailureAndRecovers(t *testing.T) {
	p := newTestProvider(t)
	fake := &fakeLister{containers: []ContainerInfo{{ProjectName: "demo"}}}
	p.listContainers = fake.ListOptedInContainers
	ch := make(chan json.Marshaler, 3)
	p.publish(context.Background(), ch)
	first := <-ch
	firstJSON, err := first.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	if got := snapshotCertificateCount(t, firstJSON); got != 2 {
		t.Fatalf("first snapshot has %d certificates, want baseline plus project", got)
	}
	firstSnapshot := append([]byte(nil), p.lastSnapshot...)

	fake.err = errors.New("daemon unavailable")
	p.publish(context.Background(), ch)
	p.publish(context.Background(), ch)
	if len(ch) != 0 {
		t.Fatal("Docker failure should retain the existing snapshot without publishing an empty replacement")
	}
	if !p.dockerLost || string(p.lastSnapshot) != string(firstSnapshot) {
		t.Fatal("provider did not retain failure state and complete snapshot")
	}

	fake.err = nil
	fake.containers = nil
	p.publish(context.Background(), ch)
	recovered := <-ch
	if _, err := recovered.MarshalJSON(); err != nil {
		t.Fatal(err)
	}
	recoveredJSON, err := recovered.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	if got := snapshotCertificateCount(t, recoveredJSON); got != 1 {
		t.Fatalf("inactive project remained in recovered snapshot: got %d certificates", got)
	}
	if p.dockerLost {
		t.Fatal("Docker recovery state was not cleared")
	}
}

func snapshotCertificateCount(t *testing.T, payload []byte) int {
	t.Helper()
	var decoded struct {
		TLS struct {
			Certificates []flatCert `json:"certificates"`
		} `json:"tls"`
	}
	if err := json.Unmarshal(payload, &decoded); err != nil {
		t.Fatal(err)
	}
	return len(decoded.TLS.Certificates)
}

func TestTailnetProviderMirrorsRouterAndMetadata(t *testing.T) {
	p := &Provider{domainSuffix: "tail1234"}
	containers := []ContainerInfo{{
		ProjectName:     "demo",
		MetadataDomains: []string{"api.demo.localhost"},
		Labels: map[string]string{
			"traefik.http.routers.demo.rule":        "Host(`demo.localhost`) || Host(`api.demo.localhost`)",
			"traefik.http.routers.demo.service":     "demo",
			"traefik.http.routers.demo.entrypoints": "web, websecure",
			"traefik.http.routers.demo.middlewares": "headers",
			"traefik.http.routers.demo.priority":    "42",
			"traefik.http.routers.demo.tls":         "true",
		},
	}}

	specs, err := p.desiredSpecs(containers)
	if err != nil {
		t.Fatal(err)
	}
	foundMetadata := false
	for _, spec := range specs {
		if spec.kind == "metadata" && len(spec.domains) == 1 && spec.domains[0] == "api.demo.tail1234" {
			foundMetadata = true
		}
	}
	if !foundMetadata {
		t.Fatalf("tailnet metadata domain was not translated: %#v", specs)
	}
	router := p.mirroredRouters(containers)["demo"]
	if router.Rule != "Host(`demo.tail1234`) || Host(`api.demo.tail1234`)" {
		t.Fatalf("unexpected mirrored rule %q", router.Rule)
	}
	if router.Service != "demo@docker" || len(router.Middlewares) != 1 || router.Middlewares[0] != "headers@docker" || router.TLS == nil || router.Priority != 42 {
		t.Fatalf("router references were not preserved: %#v", router)
	}
}

func TestMirroredRouterNameCollisionsAreDropped(t *testing.T) {
	p := &Provider{domainSuffix: "tail1234"}
	labels := func(service string) map[string]string {
		return map[string]string{
			"traefik.http.routers.web.rule":        "Host(`" + service + ".localhost`)",
			"traefik.http.routers.web.service":     service,
			"traefik.http.routers.web.entrypoints": "web",
		}
	}
	containers := []ContainerInfo{{Labels: labels("one")}, {Labels: labels("two")}}
	if routers := p.mirroredRouters(containers); len(routers) != 0 {
		t.Fatalf("conflicting router definitions must not be mirrored: %#v", routers)
	}
	replicas := []ContainerInfo{{Labels: labels("one")}, {Labels: labels("one")}}
	if routers := p.mirroredRouters(replicas); len(routers) != 1 {
		t.Fatalf("identical replicas should mirror once: %#v", routers)
	}
}

func TestPublishSkipsUnchangedSnapshot(t *testing.T) {
	p := newTestProvider(t)
	p.listContainers = func(context.Context) ([]ContainerInfo, error) { return nil, nil }
	ch := make(chan json.Marshaler, 2)

	p.publish(context.Background(), ch)
	<-ch
	p.publish(context.Background(), ch)

	if len(ch) != 0 {
		t.Fatal("unchanged discovery published a duplicate TLS snapshot")
	}
}

func TestRuntimeRenewalUsesNewCertificateAndActualExpiry(t *testing.T) {
	p := newTestProvider(t)
	fake := &fakeLister{containers: []ContainerInfo{{MetadataDomains: []string{"managed.localhost"}}}}
	p.listContainers = fake.ListOptedInContainers
	ch := make(chan json.Marshaler, 2)
	p.publish(context.Background(), ch)
	<-ch
	spec := p.metadataSpec("managed.localhost")
	old := p.storedCerts[spec.key]
	oldPEM := string(old.certPEM)
	old.expires = time.Now().Add(p.renewBefore / 2)
	p.publish(context.Background(), ch)
	<-ch
	got := p.storedCerts[spec.key]
	if string(got.certPEM) == oldPEM {
		t.Fatal("renewal did not replace certificate")
	}
	cert, err := p.ca.ValidateLeafPair(got.certPEM, got.keyPEM, []string{"managed.localhost"})
	if err != nil {
		t.Fatal(err)
	}
	if !got.expires.Equal(cert.NotAfter) {
		t.Fatalf("cached expiry %s does not equal certificate NotAfter %s", got.expires, cert.NotAfter)
	}

	futurePEM := string(got.certPEM)
	got.notBefore = time.Now().Add(time.Hour)
	p.publish(context.Background(), ch)
	<-ch
	if string(p.storedCerts[spec.key].certPEM) == futurePEM {
		t.Fatal("not-yet-valid cached certificate was reused")
	}
}

func TestInactiveRenewalDueCertificateIsNotRenewedAtStartup(t *testing.T) {
	p := newTestProvider(t)
	spec := p.projectSpec("inactive")
	certPEM, keyPEM, err := p.ca.IssueLeaf(spec.domains, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := commitVersionedPair(spec.dir, "leaf.pem", "leaf-key.pem", certPEM, keyPEM, 0644, 0600, func(pair *pairData) error {
		_, err := p.ca.ValidateLeafPair(pair.cert, pair.key, spec.domains)
		return err
	}, nil); err != nil {
		t.Fatal(err)
	}

	config := CreateConfig()
	config.StoragePath = p.storagePath
	reloaded, err := New(context.Background(), config, "reload")
	if err != nil {
		t.Fatal(err)
	}
	if err := reloaded.Init(); err != nil {
		t.Fatal(err)
	}
	got, err := readVersionedPair(spec.dir, "leaf.pem", "leaf-key.pem")
	if err != nil {
		t.Fatal(err)
	}
	if string(got.cert) != string(certPEM) {
		t.Fatal("inactive renewal-due certificate was renewed during startup")
	}
}

func TestPublishCancellationDoesNotBlock(t *testing.T) {
	p := newTestProvider(t)
	p.listContainers = func(context.Context) ([]ContainerInfo, error) { return nil, nil }
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	done := make(chan struct{})
	go func() {
		p.publish(ctx, make(chan json.Marshaler))
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("publish blocked after cancellation")
	}
}

func TestInitRejectsWorldWritableStorageAndMissingBootstrapWithoutCreatingCA(t *testing.T) {
	for name, mode := range map[string]os.FileMode{"world-writable": 0777, "secure-but-empty": 0700} {
		t.Run(name, func(t *testing.T) {
			storage := t.TempDir()
			if err := os.Chmod(storage, mode); err != nil {
				t.Fatal(err)
			}
			config := CreateConfig()
			config.StoragePath = storage
			p, err := New(context.Background(), config, "test")
			if err != nil {
				t.Fatal(err)
			}
			if err := p.Init(); err == nil {
				t.Fatal("invalid storage was accepted")
			}
			entries, err := os.ReadDir(storage)
			if err != nil || len(entries) != 0 {
				t.Fatalf("provider created CA state: %v %v", entries, err)
			}
		})
	}
}

func TestInitRejectsSymlinkedCertificateCollectionDirectories(t *testing.T) {
	for _, collection := range []string{"projects", "metadata"} {
		t.Run(collection, func(t *testing.T) {
			p := newTestProvider(t)
			outside := t.TempDir()
			if err := os.Symlink(outside, filepath.Join(p.storagePath, collection)); err != nil {
				t.Fatal(err)
			}
			config := CreateConfig()
			config.StoragePath = p.storagePath
			reloaded, err := New(context.Background(), config, "reload")
			if err != nil {
				t.Fatal(err)
			}
			if err := reloaded.Init(); err == nil || !strings.Contains(err.Error(), "not a real directory") {
				t.Fatalf("expected symlinked %s directory rejection, got %v", collection, err)
			}
		})
	}
}

func TestInitRejectsInsecureStoredKeyPermissions(t *testing.T) {
	p := newTestProvider(t)
	pair, err := readVersionedPair(filepath.Join(p.storagePath, "baseline"), "leaf.pem", "leaf-key.pem")
	if err != nil {
		t.Fatal(err)
	}
	keyPath := pair.keyPath
	if err := os.Chmod(keyPath, 0644); err != nil {
		t.Fatal(err)
	}
	config := CreateConfig()
	config.StoragePath = p.storagePath
	reloaded, err := New(context.Background(), config, "reload")
	if err != nil {
		t.Fatal(err)
	}
	if err := reloaded.Init(); err == nil || !strings.Contains(err.Error(), "private-key permissions") {
		t.Fatalf("expected insecure key permissions error, got %v", err)
	}
}

func TestInitRejectsCorruptStoredBaseline(t *testing.T) {
	p := newTestProvider(t)
	pair, err := readVersionedPair(filepath.Join(p.storagePath, "baseline"), "leaf.pem", "leaf-key.pem")
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(pair.keyPath, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(pair.keyPath, []byte("not a key"), 0600); err != nil {
		t.Fatal(err)
	}
	config := CreateConfig()
	config.StoragePath = p.storagePath
	reloaded, err := New(context.Background(), config, "reload")
	if err != nil {
		t.Fatal(err)
	}
	if err := reloaded.Init(); err == nil || !strings.Contains(err.Error(), "invalid baseline certificate") {
		t.Fatalf("expected clear baseline corruption error, got %v", err)
	}
}

func TestTLSMarshalKeepsFlatInlinePEMShape(t *testing.T) {
	payload := &tlsPayload{certs: []flatCert{{CertFile: "CERT", KeyFile: "KEY"}}}
	data, err := payload.MarshalJSON()
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]interface{}
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), `"certFile":"CERT"`) || strings.Contains(string(data), "Certificate") {
		t.Fatalf("unexpected payload shape: %s", data)
	}
}

type roundTripFunc func(*http.Request) (*http.Response, error)

func (f roundTripFunc) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

func dockerResponse(body string) *http.Response {
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}
}

func writeRegistryEntry(t *testing.T, dir, name, hostname string) {
	t.Helper()
	payload := fmt.Sprintf(
		`{"hostname":%q,"name":%q,"directory":"/tmp/x","type":"django","last_started":"2026-08-30T12:00:00+00:00"}`,
		hostname, name)
	if err := os.WriteFile(filepath.Join(dir, name+".json"), []byte(payload), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDesiredSpecsIncludeRegistryHostnames(t *testing.T) {
	p := newTestProvider(t)
	dir := t.TempDir()
	writeRegistryEntry(t, dir, "blog", "blog.localhost")
	p.registryPath = dir
	specs, err := p.desiredSpecs(nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 1 || specs[0].kind != "metadata" || specs[0].name != "blog.localhost" {
		t.Fatalf("unexpected specs: %#v", specs)
	}
}

func TestRegistryHostnamesDeduplicateAgainstRunningContainers(t *testing.T) {
	p := newTestProvider(t)
	dir := t.TempDir()
	writeRegistryEntry(t, dir, "host", "host.localhost")
	p.registryPath = dir
	specs, err := p.desiredSpecs([]ContainerInfo{{MetadataDomains: []string{"host.localhost"}}})
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 1 {
		t.Fatalf("expected single deduplicated spec: %#v", specs)
	}
}

func TestRegistrySkipsInvalidAndUnreadableEntries(t *testing.T) {
	p := newTestProvider(t)
	dir := t.TempDir()
	writeRegistryEntry(t, dir, "evil", "evil.example.com")
	if err := os.WriteFile(filepath.Join(dir, "bad.json"), []byte("{nope"), 0o644); err != nil {
		t.Fatal(err)
	}
	writeRegistryEntry(t, dir, "good", "good.localhost")
	p.registryPath = dir
	specs, err := p.desiredSpecs(nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 1 || specs[0].name != "good.localhost" {
		t.Fatalf("unexpected specs: %#v", specs)
	}
}

func TestMissingRegistryPathIsIgnored(t *testing.T) {
	p := newTestProvider(t)
	p.registryPath = filepath.Join(t.TempDir(), "absent")
	specs, err := p.desiredSpecs(nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(specs) != 0 {
		t.Fatalf("unexpected specs: %#v", specs)
	}
}

func TestHTTPModeMirrorsOnlyPlainRouters(t *testing.T) {
	p := &Provider{domainSuffix: "work", httpOnly: true}
	containers := []ContainerInfo{{
		Labels: map[string]string{
			"traefik.http.routers.demo.rule":               "Host(`demo.localhost`)",
			"traefik.http.routers.demo.service":            "demo",
			"traefik.http.routers.demo.entrypoints":        "web",
			"traefik.http.routers.demo-secure.rule":        "Host(`demo.localhost`)",
			"traefik.http.routers.demo-secure.service":     "demo",
			"traefik.http.routers.demo-secure.entrypoints": "websecure",
			"traefik.http.routers.demo-secure.tls":         "true",
		},
	}}
	routers := p.mirroredRouters(containers)
	if len(routers) != 1 {
		t.Fatalf("HTTP mode must skip TLS routers: %#v", routers)
	}
	router := routers["demo"]
	if router.Rule != "Host(`demo.work`)" || router.TLS != nil {
		t.Fatalf("unexpected plain router %#v", router)
	}
}

func TestHTTPModeNeedsNoSignerAndPublishesNoCertificates(t *testing.T) {
	config := CreateConfig()
	config.Mode = "http"
	config.StoragePath = ""
	config.DomainSuffix = "work"
	config.PollInterval = "100ms"
	p, err := New(context.Background(), config, "test")
	if err != nil {
		t.Fatal(err)
	}
	if err := p.Init(); err != nil {
		t.Fatalf("HTTP mode must initialize without a bootstrapped signer: %v", err)
	}
	p.listContainers = func(context.Context) ([]ContainerInfo, error) {
		return []ContainerInfo{{
			ProjectName: "demo",
			Labels: map[string]string{
				"traefik.http.routers.demo.rule":        "Host(`demo.localhost`)",
				"traefik.http.routers.demo.service":     "demo",
				"traefik.http.routers.demo.entrypoints": "web",
			},
		}}, nil
	}
	ch := make(chan json.Marshaler, 1)
	p.publish(context.Background(), ch)
	payload := (<-ch).(*tlsPayload)
	if len(payload.certs) != 0 {
		t.Fatalf("HTTP mode must not issue certificates: %#v", payload.certs)
	}
	if _, ok := payload.routers["demo"]; !ok {
		t.Fatalf("HTTP mode must still mirror routers: %#v", payload.routers)
	}
}

func TestModeRejectsUnknownValues(t *testing.T) {
	config := CreateConfig()
	config.Mode = "plain"
	if _, err := New(context.Background(), config, "test"); err == nil {
		t.Fatal("unknown mode was accepted")
	}
}
