package traefik_localghost_ca

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func bootstrapTestCA(t *testing.T) (*CertificateAuthority, string, string) {
	t.Helper()
	root, signer := t.TempDir(), t.TempDir()
	ca, err := BootstrapCA(root, signer)
	if err != nil {
		t.Fatal(err)
	}
	return ca, root, signer
}

func TestBootstrapCreatesSplitPersistentConstrainedCA(t *testing.T) {
	ca, root, signer := bootstrapTestCA(t)
	rootFP, intermediateFP := ca.Fingerprint(), ca.IntermediateFingerprint()
	loaded, err := BootstrapCA(root, signer)
	if err != nil {
		t.Fatal(err)
	}
	if loaded.Fingerprint() != rootFP || loaded.IntermediateFingerprint() != intermediateFP {
		t.Fatal("bootstrap identities changed across an idempotent restart")
	}
	if !loaded.intermediateCert.PermittedDNSDomainsCritical || len(loaded.intermediateCert.PermittedDNSDomains) != 1 || loaded.intermediateCert.PermittedDNSDomains[0] != "localhost" {
		t.Fatalf("unexpected constraints: %#v", loaded.intermediateCert.PermittedDNSDomains)
	}
	if !loaded.intermediateCert.MaxPathLenZero || loaded.intermediateCert.MaxPathLen != 0 {
		t.Fatal("intermediate is not pathLen=0")
	}
	if _, err := os.Stat(filepath.Join(signer, rootKeyFile)); !os.IsNotExist(err) {
		t.Fatalf("root key leaked to signer root: %v", err)
	}
	if matches, err := filepath.Glob(filepath.Join(signer, "**", rootKeyFile)); err != nil || len(matches) != 0 {
		t.Fatalf("root key leaked to signer: %v %v", matches, err)
	}
	if _, err := os.Stat(filepath.Join(signer, rootCertFile)); err != nil {
		t.Fatal("public root was not copied to signer")
	}
}

func TestLoadSignerFailsWithoutBootstrapAndNeverCreatesRoot(t *testing.T) {
	signer := t.TempDir()
	if _, err := LoadSignerCA(signer); err == nil {
		t.Fatal("missing bootstrap state was accepted")
	}
	if entries, err := os.ReadDir(signer); err != nil || len(entries) != 0 {
		t.Fatalf("provider loader created state: %v %v", entries, err)
	}
}

func TestIssueLeafChainAndCentralContracts(t *testing.T) {
	ca, _, _ := bootstrapTestCA(t)
	for _, domains := range [][]string{
		{"localhost", "traefik.localhost"},
		{"demo.localhost", "*.demo.localhost"},
		{"host.localhost"},
		{"*.demo.localhost"},
	} {
		certPEM, keyPEM, err := ca.IssueLeaf(domains, 24*time.Hour)
		if err != nil {
			t.Fatalf("IssueLeaf(%v): %v", domains, err)
		}
		cert, err := ca.ValidateLeafPair(certPEM, keyPEM, domains)
		if err != nil {
			t.Fatal(err)
		}
		if len(cert.DNSNames) != len(domains) {
			t.Fatal("SAN count changed")
		}
		chain, err := parseCertificateChainPEM(certPEM)
		if err != nil || len(chain) != 2 {
			t.Fatalf("served chain is not leaf+intermediate: %v, %d", err, len(chain))
		}
		for _, chainCert := range chain {
			if string(chainCert.Raw) == string(ca.rootCert.Raw) {
				t.Fatal("served chain contains root")
			}
		}
	}
	for _, domains := range [][]string{nil, {"example.com"}, {"third.mailpit.demo.localhost"}, {"localhost"}, {"demo.localhost", "*.other.localhost"}} {
		if _, _, err := ca.IssueLeaf(domains, time.Hour); err == nil {
			t.Fatalf("unsupported SANs were signed: %v", domains)
		}
	}
}

func TestNameConstraintsVerifySupportedAndRejectConstructedOutOfScopeLeaf(t *testing.T) {
	ca, _, _ := bootstrapTestCA(t)
	certPEM, _, err := ca.IssueLeaf([]string{"demo.localhost", "*.demo.localhost"}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	chain, _ := parseCertificateChainPEM(certPEM)
	roots := x509.NewCertPool()
	roots.AddCert(ca.rootCert)
	intermediates := x509.NewCertPool()
	intermediates.AddCert(ca.intermediateCert)
	for _, name := range []string{"demo.localhost", "mail.demo.localhost"} {
		if _, err := chain[0].Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, DNSName: name}); err != nil {
			t.Fatalf("supported %s did not verify: %v", name, err)
		}
	}
	bad := rawLeafForTest(t, ca, "example.com")
	if _, err := bad.Verify(x509.VerifyOptions{Roots: roots, Intermediates: intermediates, DNSName: "example.com"}); err == nil || !strings.Contains(err.Error(), "not permitted") {
		t.Fatalf("out-of-scope leaf did not fail name constraints: %v", err)
	}
}

func rawLeafForTest(t *testing.T, ca *CertificateAuthority, domain string) *x509.Certificate {
	t.Helper()
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	template := &x509.Certificate{SerialNumber: big.NewInt(42), Subject: pkix.Name{CommonName: domain}, NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour), DNSNames: []string{domain}, KeyUsage: x509.KeyUsageDigitalSignature, ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}}
	der, err := x509.CreateCertificate(rand.Reader, template, ca.intermediateCert, &key.PublicKey, ca.intermediateKey)
	if err != nil {
		t.Fatal(err)
	}
	cert, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatal(err)
	}
	return cert
}

func TestOpenSSLVerifiesSupportedAndRejectsNameConstraintViolation(t *testing.T) {
	if _, err := exec.LookPath("openssl"); err != nil {
		t.Skip("openssl is unavailable")
	}
	ca, _, _ := bootstrapTestCA(t)
	dir := t.TempDir()
	rootPath := filepath.Join(dir, "root.pem")
	intermediatePath := filepath.Join(dir, "intermediate.pem")
	validPath := filepath.Join(dir, "valid.pem")
	invalidPath := filepath.Join(dir, "invalid.pem")
	if err := os.WriteFile(rootPath, ca.PublicCertificatePEM(), 0644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(intermediatePath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.intermediateCert.Raw}), 0644); err != nil {
		t.Fatal(err)
	}
	validChain, _, err := ca.IssueLeaf([]string{"demo.localhost", "*.demo.localhost"}, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	valid, err := parseCertificateChainPEM(validChain)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(validPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: valid[0].Raw}), 0644); err != nil {
		t.Fatal(err)
	}
	invalid := rawLeafForTest(t, ca, "example.com")
	if err := os.WriteFile(invalidPath, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: invalid.Raw}), 0644); err != nil {
		t.Fatal(err)
	}
	common := []string{"verify", "-CAfile", rootPath, "-untrusted", intermediatePath}
	if output, err := exec.Command("openssl", append(common, "-verify_hostname", "mail.demo.localhost", validPath)...).CombinedOutput(); err != nil {
		t.Fatalf("OpenSSL rejected supported chain: %v: %s", err, output)
	}
	if output, err := exec.Command("openssl", append(common, "-verify_hostname", "example.com", invalidPath)...).CombinedOutput(); err == nil || !strings.Contains(strings.ToLower(string(output)), "permitted subtree") {
		t.Fatalf("OpenSSL did not enforce DNS constraints: %v: %s", err, output)
	}
}

func TestVersionedPairFailureBeforePointerPreservesCommittedGeneration(t *testing.T) {
	base := filepath.Join(t.TempDir(), "pair")
	validate := func(pair *pairData) error {
		if string(pair.cert) == "" || string(pair.key) == "" {
			return errors.New("empty")
		}
		return nil
	}
	first, err := commitVersionedPair(base, "cert", "key", []byte("old-cert"), []byte("old-key"), 0644, 0600, validate, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, point := range []string{"after-certificate", "after-key", "before-pointer"} {
		t.Run(point, func(t *testing.T) {
			hook := func(got string) error {
				if got == point {
					return errors.New("injected crash")
				}
				return nil
			}
			if _, err := commitVersionedPair(base, "cert", "key", []byte("new-cert"), []byte("new-key"), 0644, 0600, validate, hook); err == nil {
				t.Fatal("failure was not injected")
			}
			got, err := readVersionedPair(base, "cert", "key")
			if err != nil {
				t.Fatal(err)
			}
			if got.generation != first.generation || string(got.cert) != "old-cert" || string(got.key) != "old-key" {
				t.Fatalf("old commit changed at %s: %#v", point, got)
			}
		})
	}
}

func TestVersionedPairIgnoresIncompleteUnreferencedGeneration(t *testing.T) {
	base := filepath.Join(t.TempDir(), "pair")
	if err := ensureSecureDir(filepath.Join(base, generationsDir, "gen-00000000000000000000000000000000"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(base, generationsDir, "gen-00000000000000000000000000000000", "cert"), []byte("partial"), 0644); err != nil {
		t.Fatal(err)
	}
	pair, err := readVersionedPair(base, "cert", "key")
	if err != nil || pair != nil {
		t.Fatalf("unreferenced incomplete generation was not tolerated: %#v %v", pair, err)
	}
}

func TestVersionedPairRejectsMalformedTraversalAndSymlinkState(t *testing.T) {
	for name, pointer := range map[string]string{"traversal": "../outside\n", "absolute": "/tmp/outside\n", "malformed": "gen-nope\n"} {
		t.Run(name, func(t *testing.T) {
			base := filepath.Join(t.TempDir(), "pair")
			if err := ensureSecureDir(base, 0700); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(base, currentFile), []byte(pointer), 0600); err != nil {
				t.Fatal(err)
			}
			if _, err := readVersionedPair(base, "cert", "key"); err == nil {
				t.Fatal("bad pointer accepted")
			}
		})
	}
	t.Run("symlink pointer", func(t *testing.T) {
		base := filepath.Join(t.TempDir(), "pair")
		if err := ensureSecureDir(base, 0700); err != nil {
			t.Fatal(err)
		}
		target := filepath.Join(t.TempDir(), "pointer")
		if err := os.WriteFile(target, []byte("gen-00000000000000000000000000000000\n"), 0600); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(target, filepath.Join(base, currentFile)); err != nil {
			t.Fatal(err)
		}
		if _, err := readVersionedPair(base, "cert", "key"); err == nil {
			t.Fatal("symlink pointer accepted")
		}
	})
}

func TestBootstrapAndSignerRejectInsecureKeyPermissions(t *testing.T) {
	_, root, signer := bootstrapTestCA(t)
	rootPair, err := readVersionedPair(filepath.Join(root, rootStateDir), rootCertFile, rootKeyFile)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(rootPair.keyPath, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := BootstrapCA(root, signer); err == nil {
		t.Fatal("insecure root key accepted")
	}

	_, root, signer = bootstrapTestCA(t)
	intermediatePair, err := readVersionedPair(filepath.Join(signer, intermediateStateDir), intermediateCertFile, intermediateKeyFile)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(intermediatePair.keyPath, 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := LoadSignerCA(signer); err == nil {
		t.Fatal("insecure intermediate key accepted")
	}
}

func TestStrictCertificatePEMRejectsPrivateOrMultipleBlocks(t *testing.T) {
	ca, _, _ := bootstrapTestCA(t)
	root := ca.PublicCertificatePEM()
	private := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: []byte("secret")})
	for _, data := range [][]byte{append(append([]byte(nil), root...), root...), append(append([]byte(nil), root...), private...)} {
		if _, err := parseSingleCertificatePEM(data); err == nil {
			t.Fatal("non-single public certificate PEM accepted")
		}
	}
}
