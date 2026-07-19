package traefik_localhost_ca

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
)

const (
	generationsDir = "generations"
	currentFile    = "current"
)

var generationNameRE = regexp.MustCompile(`^gen-[0-9a-f]{32}$`)

type pairData struct {
	cert       []byte
	key        []byte
	generation string
	certPath   string
	keyPath    string
}

type failureHook func(string) error

// ensureSecureDir creates path one component at a time and refuses symlinks or
// non-directories. This prevents managed state writes from following a link.
func ensureSecureDir(path string, perm os.FileMode) error {
	clean := filepath.Clean(path)
	volume := filepath.VolumeName(clean)
	rest := strings.TrimPrefix(clean, volume)
	absolute := filepath.IsAbs(clean)
	parts := strings.Split(strings.TrimPrefix(rest, string(filepath.Separator)), string(filepath.Separator))
	current := volume
	if absolute {
		current += string(filepath.Separator)
	}
	for _, part := range parts {
		if part == "" || part == "." {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if os.IsNotExist(err) {
			if err := os.Mkdir(current, perm); err != nil && !os.IsExist(err) {
				return fmt.Errorf("creating directory %s: %w", current, err)
			}
			info, err = os.Lstat(current)
		}
		if err != nil {
			return fmt.Errorf("checking directory %s: %w", current, err)
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return fmt.Errorf("managed path %s is not a real directory", current)
		}
	}
	return nil
}

func validateManagedDir(path string) error {
	clean := filepath.Clean(path)
	volume := filepath.VolumeName(clean)
	rest := strings.TrimPrefix(clean, volume)
	absolute := filepath.IsAbs(clean)
	parts := strings.Split(strings.TrimPrefix(rest, string(filepath.Separator)), string(filepath.Separator))
	current := volume
	if absolute {
		current += string(filepath.Separator)
	}
	var final os.FileInfo
	for _, part := range parts {
		if part == "" || part == "." {
			continue
		}
		current = filepath.Join(current, part)
		info, err := os.Lstat(current)
		if err != nil {
			return err
		}
		if info.Mode()&os.ModeSymlink != 0 || !info.IsDir() {
			return fmt.Errorf("managed path component %s is not a real directory", current)
		}
		final = info
	}
	if final == nil {
		return fmt.Errorf("managed path %s has no directory component", path)
	}
	if final.Mode().Perm()&0002 != 0 {
		return fmt.Errorf("directory %s must not be world-writable", path)
	}
	return nil
}

func readRegularFile(path string, max int64) ([]byte, error) {
	info, err := os.Lstat(path)
	if err != nil {
		return nil, err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, fmt.Errorf("%s is not a regular file", path)
	}
	if info.Size() > max {
		return nil, fmt.Errorf("%s exceeds %d bytes", path, max)
	}
	return os.ReadFile(path)
}

func readVersionedPair(base, certName, keyName string) (*pairData, error) {
	if err := validateManagedDir(base); err != nil {
		if os.IsNotExist(err) {
			return nil, nil
		}
		return nil, err
	}
	pointerPath := filepath.Join(base, currentFile)
	pointer, err := readRegularFile(pointerPath, 128)
	if os.IsNotExist(err) {
		// Uncommitted generations are deliberately ignored.
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("reading current generation: %w", err)
	}
	if len(pointer) == 0 || pointer[len(pointer)-1] != '\n' || strings.Count(string(pointer), "\n") != 1 {
		return nil, fmt.Errorf("malformed current generation pointer in %s", pointerPath)
	}
	generation := strings.TrimSuffix(string(pointer), "\n")
	if !generationNameRE.MatchString(generation) {
		return nil, fmt.Errorf("invalid current generation %q", generation)
	}
	genDir := filepath.Join(base, generationsDir, generation)
	if err := validateManagedDir(filepath.Join(base, generationsDir)); err != nil {
		return nil, fmt.Errorf("invalid generations directory: %w", err)
	}
	if err := validateManagedDir(genDir); err != nil {
		return nil, fmt.Errorf("invalid committed generation: %w", err)
	}
	certPath, keyPath := filepath.Join(genDir, certName), filepath.Join(genDir, keyName)
	cert, err := readRegularFile(certPath, 1<<20)
	if err != nil {
		return nil, fmt.Errorf("reading committed certificate: %w", err)
	}
	key, err := readRegularFile(keyPath, 1<<20)
	if err != nil {
		return nil, fmt.Errorf("reading committed private key: %w", err)
	}
	return &pairData{cert: cert, key: key, generation: generation, certPath: certPath, keyPath: keyPath}, nil
}

func newGenerationName() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", err
	}
	return "gen-" + hex.EncodeToString(id[:]), nil
}

// commitVersionedPair writes an immutable generation, validates it, and only
// then atomically replaces current. A crash before the pointer rename leaves
// the prior committed pair selected.
func commitVersionedPair(base, certName, keyName string, cert, key []byte, certPerm, keyPerm os.FileMode, validate func(*pairData) error, hook failureHook) (*pairData, error) {
	if err := ensureSecureDir(filepath.Join(base, generationsDir), 0700); err != nil {
		return nil, err
	}
	if err := validateManagedDir(base); err != nil {
		return nil, err
	}
	if info, err := os.Lstat(filepath.Join(base, currentFile)); err == nil && (info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular()) {
		return nil, fmt.Errorf("current generation pointer is not a regular file")
	} else if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	generation, err := newGenerationName()
	if err != nil {
		return nil, fmt.Errorf("creating generation identity: %w", err)
	}
	genDir := filepath.Join(base, generationsDir, generation)
	if err := os.Mkdir(genDir, 0700); err != nil {
		return nil, fmt.Errorf("creating generation: %w", err)
	}
	certPath, keyPath := filepath.Join(genDir, certName), filepath.Join(genDir, keyName)
	if err := writeImmutable(certPath, cert, certPerm); err != nil {
		return nil, err
	}
	if hook != nil {
		if err := hook("after-certificate"); err != nil {
			return nil, err
		}
	}
	if err := writeImmutable(keyPath, key, keyPerm); err != nil {
		return nil, err
	}
	if hook != nil {
		if err := hook("after-key"); err != nil {
			return nil, err
		}
	}
	pair := &pairData{cert: cert, key: key, generation: generation, certPath: certPath, keyPath: keyPath}
	if err := validate(pair); err != nil {
		return nil, fmt.Errorf("validating new generation: %w", err)
	}
	if err := syncDir(filepath.Join(base, generationsDir)); err != nil {
		return nil, fmt.Errorf("syncing generations directory: %w", err)
	}
	if hook != nil {
		if err := hook("before-pointer"); err != nil {
			return nil, err
		}
	}
	if err := atomicWrite(filepath.Join(base, currentFile), []byte(generation+"\n"), 0600); err != nil {
		return nil, fmt.Errorf("committing current generation: %w", err)
	}
	return pair, nil
}

func writeImmutable(path string, data []byte, perm os.FileMode) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, perm)
	if err != nil {
		return fmt.Errorf("creating immutable file %s: %w", path, err)
	}
	ok := false
	defer func() {
		file.Close()
		if !ok {
			os.Remove(path)
		}
	}()
	if _, err := file.Write(data); err != nil {
		return err
	}
	if err := file.Sync(); err != nil {
		return err
	}
	if err := file.Chmod(perm); err != nil {
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	ok = true
	return syncDir(filepath.Dir(path))
}

func syncDir(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// atomicWrite writes a single non-pair file through fsync and rename. Pair
// state must use commitVersionedPair instead.
func atomicWrite(path string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(path)
	if err := ensureSecureDir(dir, 0700); err != nil {
		return err
	}
	if info, err := os.Lstat(path); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return fmt.Errorf("refusing to replace symlink %s", path)
	} else if err != nil && !os.IsNotExist(err) {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".tmp."+filepath.Base(path)+".")
	if err != nil {
		return err
	}
	tmpPath := tmp.Name()
	cleanup := true
	defer func() {
		if cleanup {
			tmp.Close()
			os.Remove(tmpPath)
		}
	}()
	if _, err := tmp.Write(data); err != nil {
		return err
	}
	if err := tmp.Sync(); err != nil {
		return err
	}
	if err := tmp.Chmod(perm); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return err
	}
	cleanup = false
	return syncDir(dir)
}
