// Package traefik_localhost_ca implements a Traefik provider plugin that
// supplies locally trusted TLS certificates for .localhost development domains.
package traefik_localhost_ca

import (
	"bytes"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"fmt"
	"math/big"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"time"
)

const (
	rootStateDir                     = "root"
	intermediateStateDir             = "intermediate"
	rootCertFile                     = "rootCA.pem"
	rootKeyFile                      = "rootCA-key.pem"
	intermediateCertFile             = "intermediate.pem"
	intermediateKeyFile              = "intermediate-key.pem"
	caPerm               os.FileMode = 0644
	caKeyPerm            os.FileMode = 0600
)

// CertificateAuthority is the online, name-constrained intermediate signer.
// It deliberately has no root private-key field.
type CertificateAuthority struct {
	rootCert         *x509.Certificate
	intermediateCert *x509.Certificate
	intermediateKey  *ecdsa.PrivateKey
	storagePath      string
}

// BootstrapCA creates or validates the offline root and constrained online
// signer. It is idempotent. Intermediate rotation requires purging both
// volumes rather than silently changing an existing identity.
func BootstrapCA(rootPath, signerPath string) (*CertificateAuthority, error) {
	if err := ensureSecureDir(rootPath, 0700); err != nil {
		return nil, fmt.Errorf("preparing root-only storage: %w", err)
	}
	if err := ensureSecureDir(signerPath, 0700); err != nil {
		return nil, fmt.Errorf("preparing signer storage: %w", err)
	}
	if err := validateManagedDir(rootPath); err != nil {
		return nil, err
	}
	if err := validateManagedDir(signerPath); err != nil {
		return nil, err
	}

	rootBase := filepath.Join(rootPath, rootStateDir)
	rootPair, err := readVersionedPair(rootBase, rootCertFile, rootKeyFile)
	if err != nil {
		return nil, fmt.Errorf("loading root: %w", err)
	}
	if rootPair == nil {
		if _, err := os.Lstat(filepath.Join(signerPath, rootCertFile)); err == nil {
			return nil, fmt.Errorf("signer has a public root but root-only committed state is absent; purge is required")
		} else if !os.IsNotExist(err) {
			return nil, err
		}
		certPEM, keyPEM, err := generateRoot()
		if err != nil {
			return nil, err
		}
		rootPair, err = commitVersionedPair(rootBase, rootCertFile, rootKeyFile, certPEM, keyPEM, caPerm, caKeyPerm, validateRootPair, nil)
		if err != nil {
			return nil, fmt.Errorf("committing root: %w", err)
		}
	}
	rootCert, rootKey, err := parseAndValidateRoot(rootPair)
	if err != nil {
		return nil, err
	}

	publicRootPath := filepath.Join(signerPath, rootCertFile)
	publicPEM, publicErr := readRegularFile(publicRootPath, 1<<20)
	if os.IsNotExist(publicErr) {
		if err := atomicWrite(publicRootPath, rootPair.cert, caPerm); err != nil {
			return nil, fmt.Errorf("copying public root to signer: %w", err)
		}
		publicPEM = rootPair.cert
	} else if publicErr != nil {
		return nil, fmt.Errorf("reading signer public root: %w", publicErr)
	}
	publicCert, err := parseSingleCertificatePEM(publicPEM)
	if err != nil {
		return nil, fmt.Errorf("invalid signer public root: %w", err)
	}
	if !bytes.Equal(publicCert.Raw, rootCert.Raw) {
		return nil, fmt.Errorf("signer public root differs from root-only identity; purge is required")
	}

	intermediateBase := filepath.Join(signerPath, intermediateStateDir)
	intermediatePair, err := readVersionedPair(intermediateBase, intermediateCertFile, intermediateKeyFile)
	if err != nil {
		return nil, fmt.Errorf("loading intermediate: %w", err)
	}
	if intermediatePair == nil {
		certPEM, keyPEM, err := generateIntermediate(rootCert, rootKey)
		if err != nil {
			return nil, err
		}
		validate := func(pair *pairData) error {
			_, _, err := parseAndValidateIntermediate(pair, rootCert)
			return err
		}
		intermediatePair, err = commitVersionedPair(intermediateBase, intermediateCertFile, intermediateKeyFile, certPEM, keyPEM, caPerm, caKeyPerm, validate, nil)
		if err != nil {
			return nil, fmt.Errorf("committing intermediate: %w", err)
		}
	}
	intermediateCert, intermediateKey, err := parseAndValidateIntermediate(intermediatePair, rootCert)
	if err != nil {
		return nil, err
	}
	return &CertificateAuthority{rootCert: rootCert, intermediateCert: intermediateCert, intermediateKey: intermediateKey, storagePath: signerPath}, nil
}

// LoadSignerCA loads bootstrap state from the signer volume. It never creates
// root or intermediate state.
func LoadSignerCA(signerPath string) (*CertificateAuthority, error) {
	if err := validateManagedDir(signerPath); err != nil {
		return nil, fmt.Errorf("invalid signer storage: %w", err)
	}
	rootPEM, err := readRegularFile(filepath.Join(signerPath, rootCertFile), 1<<20)
	if err != nil {
		return nil, fmt.Errorf("bootstrap public root is missing or invalid: %w", err)
	}
	root, err := parseSingleCertificatePEM(rootPEM)
	if err != nil {
		return nil, fmt.Errorf("parsing bootstrap public root: %w", err)
	}
	if err := validateRootCertificate(root); err != nil {
		return nil, err
	}
	pair, err := readVersionedPair(filepath.Join(signerPath, intermediateStateDir), intermediateCertFile, intermediateKeyFile)
	if err != nil {
		return nil, fmt.Errorf("loading bootstrap intermediate: %w", err)
	}
	if pair == nil {
		return nil, fmt.Errorf("bootstrap intermediate is absent")
	}
	intermediate, key, err := parseAndValidateIntermediate(pair, root)
	if err != nil {
		return nil, err
	}
	return &CertificateAuthority{rootCert: root, intermediateCert: intermediate, intermediateKey: key, storagePath: signerPath}, nil
}

func generateRoot() ([]byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generating root key: %w", err)
	}
	serial, err := randomSerial()
	if err != nil {
		return nil, nil, err
	}
	now := time.Now()
	template := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: "Localhost Proxy Development Root CA"},
		NotBefore: now.Add(-time.Hour), NotAfter: now.Add(10 * 365 * 24 * time.Hour),
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true, IsCA: true, SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return nil, nil, fmt.Errorf("creating root certificate: %w", err)
	}
	return marshalPair(der, key)
}

func generateIntermediate(root *x509.Certificate, rootKey *ecdsa.PrivateKey) ([]byte, []byte, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generating intermediate key: %w", err)
	}
	serial, err := randomSerial()
	if err != nil {
		return nil, nil, err
	}
	now := time.Now()
	notAfter := now.Add(5 * 365 * 24 * time.Hour)
	if !notAfter.Before(root.NotAfter) {
		notAfter = root.NotAfter.Add(-time.Hour)
	}
	template := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: "Localhost Constrained Signing CA"},
		NotBefore: now.Add(-time.Hour), NotAfter: notAfter,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageCRLSign | x509.KeyUsageDigitalSignature,
		BasicConstraintsValid: true, IsCA: true, MaxPathLen: 0, MaxPathLenZero: true,
		PermittedDNSDomainsCritical: true, PermittedDNSDomains: []string{"localhost"},
		SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, root, &key.PublicKey, rootKey)
	if err != nil {
		return nil, nil, fmt.Errorf("creating intermediate certificate: %w", err)
	}
	return marshalPair(der, key)
}

func marshalPair(certDER []byte, key *ecdsa.PrivateKey) ([]byte, []byte, error) {
	keyDER, err := x509.MarshalECPrivateKey(key)
	if err != nil {
		return nil, nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: certDER}), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}), nil
}

func validateRootPair(pair *pairData) error {
	_, _, err := parseAndValidateRoot(pair)
	return err
}

func parseAndValidateRoot(pair *pairData) (*x509.Certificate, *ecdsa.PrivateKey, error) {
	if err := validatePrivateKeyPermissions(pair.keyPath); err != nil {
		return nil, nil, fmt.Errorf("invalid root private-key permissions: %w", err)
	}
	cert, err := parseSingleCertificatePEM(pair.cert)
	if err != nil {
		return nil, nil, err
	}
	key, err := parsePrivateKeyPEM(pair.key)
	if err != nil {
		return nil, nil, err
	}
	if err := validateRootCertificate(cert); err != nil {
		return nil, nil, err
	}
	if err := keysMatch(cert.PublicKey, key); err != nil {
		return nil, nil, fmt.Errorf("root certificate/key mismatch: %w", err)
	}
	return cert, key, nil
}

func validateRootCertificate(cert *x509.Certificate) error {
	now := time.Now()
	if !cert.IsCA || cert.KeyUsage&x509.KeyUsageCertSign == 0 {
		return fmt.Errorf("root is not a certificate authority")
	}
	if now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) {
		return fmt.Errorf("root is outside its validity period")
	}
	if len(cert.PermittedDNSDomains) != 0 || len(cert.ExcludedDNSDomains) != 0 {
		return fmt.Errorf("root must be unconstrained")
	}
	if err := cert.CheckSignatureFrom(cert); err != nil {
		return fmt.Errorf("root is not self-signed: %w", err)
	}
	return nil
}

func parseAndValidateIntermediate(pair *pairData, root *x509.Certificate) (*x509.Certificate, *ecdsa.PrivateKey, error) {
	if err := validatePrivateKeyPermissions(pair.keyPath); err != nil {
		return nil, nil, fmt.Errorf("invalid intermediate private-key permissions: %w", err)
	}
	cert, err := parseSingleCertificatePEM(pair.cert)
	if err != nil {
		return nil, nil, fmt.Errorf("parsing intermediate: %w", err)
	}
	key, err := parsePrivateKeyPEM(pair.key)
	if err != nil {
		return nil, nil, fmt.Errorf("parsing intermediate key: %w", err)
	}
	now := time.Now()
	if !cert.IsCA || cert.KeyUsage&x509.KeyUsageCertSign == 0 || !cert.BasicConstraintsValid || !cert.MaxPathLenZero || cert.MaxPathLen != 0 {
		return nil, nil, fmt.Errorf("intermediate must be a pathLen=0 certificate authority")
	}
	if now.Before(cert.NotBefore) || !now.Before(cert.NotAfter) || cert.NotAfter.After(root.NotAfter) {
		return nil, nil, fmt.Errorf("intermediate is outside its permitted validity period")
	}
	if !cert.PermittedDNSDomainsCritical || !reflect.DeepEqual(cert.PermittedDNSDomains, []string{"localhost"}) || len(cert.ExcludedDNSDomains) != 0 {
		return nil, nil, fmt.Errorf("intermediate has invalid DNS name constraints")
	}
	if err := cert.CheckSignatureFrom(root); err != nil {
		return nil, nil, fmt.Errorf("intermediate is not signed by current root: %w", err)
	}
	if err := keysMatch(cert.PublicKey, key); err != nil {
		return nil, nil, fmt.Errorf("intermediate certificate/key mismatch: %w", err)
	}
	return cert, key, nil
}

// ValidateIssueDomains centrally enforces every supported issuance contract.
func ValidateIssueDomains(domains []string) error {
	if reflect.DeepEqual(domains, []string{"localhost", "traefik.localhost"}) {
		return nil
	}
	if len(domains) == 2 && strings.HasSuffix(domains[0], ".localhost") {
		project := strings.TrimSuffix(domains[0], ".localhost")
		if ValidateProjectName(project) && reflect.DeepEqual(domains, ProjectDomains(project)) {
			return nil
		}
	}
	if len(domains) == 1 {
		if err := ValidateMetadataDomain(domains[0]); err == nil {
			return nil
		}
	}
	return fmt.Errorf("SANs %v are outside the supported localhost issuance contracts", domains)
}

// IssueLeaf signs only with the constrained intermediate and returns the leaf
// followed by the intermediate. The root is never served.
func (ca *CertificateAuthority) IssueLeaf(domains []string, lifetime time.Duration) ([]byte, []byte, error) {
	if err := ValidateIssueDomains(domains); err != nil {
		return nil, nil, err
	}
	if lifetime <= 0 {
		return nil, nil, fmt.Errorf("leaf lifetime must be positive")
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, nil, fmt.Errorf("generating leaf key: %w", err)
	}
	serial, err := randomSerial()
	if err != nil {
		return nil, nil, err
	}
	now := time.Now()
	if !now.Add(lifetime).Before(ca.intermediateCert.NotAfter) {
		return nil, nil, fmt.Errorf("requested leaf validity would exceed intermediate expiry %s", ca.intermediateCert.NotAfter.UTC().Format(time.RFC3339))
	}
	template := &x509.Certificate{
		SerialNumber: serial, Subject: pkix.Name{CommonName: domains[0]},
		NotBefore: now.Add(-time.Minute), NotAfter: now.Add(lifetime),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageKeyEncipherment,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth}, DNSNames: domains,
		SignatureAlgorithm: x509.ECDSAWithSHA256,
	}
	der, err := x509.CreateCertificate(rand.Reader, template, ca.intermediateCert, &key.PublicKey, ca.intermediateKey)
	if err != nil {
		return nil, nil, fmt.Errorf("creating leaf certificate: %w", err)
	}
	leafPEM, keyPEM, err := marshalPair(der, key)
	if err != nil {
		return nil, nil, err
	}
	chain := append(append([]byte(nil), leafPEM...), pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.intermediateCert.Raw})...)
	return chain, keyPEM, nil
}

func (ca *CertificateAuthority) ValidateLeafPair(certPEM, keyPEM []byte, expectedDomains []string) (*x509.Certificate, error) {
	certs, err := parseCertificateChainPEM(certPEM)
	if err != nil {
		return nil, err
	}
	if len(certs) != 2 || !bytes.Equal(certs[1].Raw, ca.intermediateCert.Raw) {
		return nil, fmt.Errorf("served chain must contain exactly leaf plus current intermediate")
	}
	cert := certs[0]
	key, err := parsePrivateKeyPEM(keyPEM)
	if err != nil {
		return nil, fmt.Errorf("parsing private key: %w", err)
	}
	if err := keysMatch(cert.PublicKey, key); err != nil {
		return nil, fmt.Errorf("certificate/key mismatch: %w", err)
	}
	if err := ValidateIssueDomains(expectedDomains); err != nil {
		return nil, fmt.Errorf("invalid expected issuance contract: %w", err)
	}
	if !reflect.DeepEqual(cert.DNSNames, expectedDomains) {
		return nil, fmt.Errorf("SANs %v do not exactly match expected %v", cert.DNSNames, expectedDomains)
	}
	if err := cert.CheckSignatureFrom(ca.intermediateCert); err != nil {
		return nil, fmt.Errorf("leaf is not signed by current intermediate: %w", err)
	}
	if err := cert.VerifyHostname(firstConcreteDomain(expectedDomains)); err != nil {
		return nil, fmt.Errorf("leaf hostname validation failed: %w", err)
	}
	if cert.NotAfter.After(ca.intermediateCert.NotAfter) {
		return nil, fmt.Errorf("leaf validity exceeds current intermediate")
	}
	if cert.IsCA || cert.KeyUsage&x509.KeyUsageDigitalSignature == 0 {
		return nil, fmt.Errorf("certificate is not a server-auth leaf")
	}
	hasServerAuth := false
	for _, usage := range cert.ExtKeyUsage {
		if usage == x509.ExtKeyUsageServerAuth {
			hasServerAuth = true
		}
	}
	if !hasServerAuth {
		return nil, fmt.Errorf("certificate lacks server-auth extended key usage")
	}
	return cert, nil
}

func firstConcreteDomain(domains []string) string {
	for _, domain := range domains {
		if !strings.HasPrefix(domain, "*.") {
			return domain
		}
	}
	return "validation." + strings.TrimPrefix(domains[0], "*.")
}

func parseSingleCertificatePEM(data []byte) (*x509.Certificate, error) {
	block, rest := pem.Decode(data)
	if block == nil || block.Type != "CERTIFICATE" || len(bytes.TrimSpace(rest)) != 0 {
		return nil, fmt.Errorf("expected exactly one CERTIFICATE PEM block")
	}
	return x509.ParseCertificate(block.Bytes)
}

func parseCertificateChainPEM(data []byte) ([]*x509.Certificate, error) {
	var certs []*x509.Certificate
	for len(bytes.TrimSpace(data)) != 0 {
		block, rest := pem.Decode(data)
		if block == nil || block.Type != "CERTIFICATE" {
			return nil, fmt.Errorf("certificate chain contains invalid PEM")
		}
		cert, err := x509.ParseCertificate(block.Bytes)
		if err != nil {
			return nil, err
		}
		certs = append(certs, cert)
		data = rest
	}
	return certs, nil
}

func parsePrivateKeyPEM(keyPEM []byte) (*ecdsa.PrivateKey, error) {
	block, rest := pem.Decode(keyPEM)
	if block == nil || len(bytes.TrimSpace(rest)) != 0 {
		return nil, fmt.Errorf("expected exactly one private-key PEM block")
	}
	key, err := x509.ParseECPrivateKey(block.Bytes)
	if err != nil {
		parsed, parseErr := x509.ParsePKCS8PrivateKey(block.Bytes)
		if parseErr != nil {
			return nil, err
		}
		var ok bool
		key, ok = parsed.(*ecdsa.PrivateKey)
		if !ok {
			return nil, fmt.Errorf("private key is not ECDSA")
		}
	}
	if key.Curve != elliptic.P256() {
		return nil, fmt.Errorf("private key is not ECDSA P-256")
	}
	return key, nil
}

func keysMatch(publicKey interface{}, privateKey *ecdsa.PrivateKey) error {
	want, err := x509.MarshalPKIXPublicKey(publicKey)
	if err != nil {
		return err
	}
	got, err := x509.MarshalPKIXPublicKey(privateKey.Public())
	if err != nil {
		return err
	}
	if !bytes.Equal(want, got) {
		return fmt.Errorf("public keys differ")
	}
	return nil
}

func (ca *CertificateAuthority) PublicCertificatePEM() []byte {
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: ca.rootCert.Raw})
}
func (ca *CertificateAuthority) Fingerprint() string { return certificateFingerprint(ca.rootCert) }
func (ca *CertificateAuthority) IntermediateFingerprint() string {
	return certificateFingerprint(ca.intermediateCert)
}
func certificateFingerprint(cert *x509.Certificate) string {
	digest := sha256.Sum256(cert.Raw)
	return fmt.Sprintf("SHA256:%X", digest)
}
func (ca *CertificateAuthority) StoragePath() string { return ca.storagePath }

func randomSerial() (*big.Int, error) {
	limit := new(big.Int).Lsh(big.NewInt(1), 128)
	return rand.Int(rand.Reader, limit)
}
