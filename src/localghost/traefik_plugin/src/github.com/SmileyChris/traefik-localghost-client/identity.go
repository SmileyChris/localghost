package traefik_localghost_client

import (
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"sync"
	"time"
)

// The headers Tailscale Serve sets on requests it forwards, so an application
// written against Serve reads a tailnet user's identity here unchanged.
const (
	headerLogin      = "Tailscale-User-Login"
	headerName       = "Tailscale-User-Name"
	headerProfilePic = "Tailscale-User-Profile-Pic"
)

// identityTTL bounds how long a removed or renamed device keeps its old
// identity on connections that outlive the change. New connections are
// already refused by Tailscale itself once the device or its access is gone.
const identityTTL = 30 * time.Second

// identity is the gateway's answer for one tailnet address; a nil answer is
// a cached miss.
type identity struct {
	Login      string `json:"login"`
	Name       string `json:"name"`
	ProfilePic string `json:"profilePic"`
}

type cachedIdentity struct {
	answer  *identity
	expires time.Time
}

// identities asks the tailnet gateway who owns an address and remembers the
// answer for a while. The gateway looks it up in the node it shares the
// tunnel with, so the answer is authoritative in a way a header from the
// client never is.
type identities struct {
	whoisURL string
	// gatewayHost is the gateway's name in the whois URL. The Docker network
	// resolves it to the container Traefik receives plain-HTTP tailnet
	// requests from, so it tells the gateway's word from another container's.
	gatewayHost string
	client      *http.Client
	now         func() time.Time
	mu          sync.Mutex
	cache       map[string]cachedIdentity
	gateway     []net.IP
	gatewayTil  time.Time
}

func newIdentities(whoisURL string) *identities {
	i := &identities{
		whoisURL: whoisURL,
		client:   &http.Client{Timeout: 2 * time.Second},
		now:      time.Now,
		cache:    map[string]cachedIdentity{},
	}
	if parsed, err := url.Parse(whoisURL); err == nil {
		i.gatewayHost = parsed.Hostname()
	}
	return i
}

// isGateway reports whether ip is the tailnet gateway container. The name is
// resolved through Docker's DNS and remembered briefly, since the container's
// address changes only when it is recreated.
func (i *identities) isGateway(ip net.IP) bool {
	if ip == nil || i.gatewayHost == "" {
		return false
	}
	now := i.now()
	i.mu.Lock()
	addresses := i.gateway
	stale := !now.Before(i.gatewayTil)
	i.mu.Unlock()
	if stale {
		addresses, _ = net.LookupIP(i.gatewayHost)
		i.mu.Lock()
		i.gateway, i.gatewayTil = addresses, now.Add(identityTTL)
		i.mu.Unlock()
	}
	for _, address := range addresses {
		if address.Equal(ip) {
			return true
		}
	}
	return false
}

// lookup returns the identity behind ip, or nil when the gateway does not
// know it or cannot be asked. Failures are cached like misses so a gateway
// that is down is not asked on every request.
func (i *identities) lookup(ip string) *identity {
	now := i.now()
	i.mu.Lock()
	if entry, ok := i.cache[ip]; ok && now.Before(entry.expires) {
		i.mu.Unlock()
		return entry.answer
	}
	i.mu.Unlock()
	answer, err := i.ask(ip)
	if err != nil {
		fmt.Printf("localghost client: leaving %s anonymous: %v\n", ip, err)
	}
	i.mu.Lock()
	i.cache[ip] = cachedIdentity{answer: answer, expires: now.Add(identityTTL)}
	i.mu.Unlock()
	return answer
}

func (i *identities) ask(ip string) (*identity, error) {
	response, err := i.client.Get(i.whoisURL + "?ip=" + url.QueryEscape(ip))
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	switch response.StatusCode {
	case http.StatusOK:
	case http.StatusNotFound:
		return nil, nil
	default:
		return nil, fmt.Errorf("gateway answered %d", response.StatusCode)
	}
	var answer identity
	if err := json.NewDecoder(response.Body).Decode(&answer); err != nil {
		return nil, fmt.Errorf("gateway answer unreadable: %w", err)
	}
	if answer.Login == "" {
		return nil, nil
	}
	return &answer, nil
}

// setIdentity replaces whatever the request claimed about its user with what
// the gateway knows, or with nothing.
func setIdentity(header http.Header, answer *identity) {
	header.Del(headerLogin)
	header.Del(headerName)
	header.Del(headerProfilePic)
	if answer == nil {
		return
	}
	header.Set(headerLogin, answer.Login)
	header.Set(headerName, answer.Name)
	if answer.ProfilePic != "" {
		header.Set(headerProfilePic, answer.ProfilePic)
	}
}
