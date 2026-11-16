// plex-ipbridge: lets Plex in a container on macOS see real client addresses.
//
// Docker engines on macOS (OrbStack, Docker Desktop) forward published ports through a proxy that rewrites the source
// of every connection to a gateway address, so Plex cannot tell clients apart (or LAN from WAN). This program replaces
// the published port with two halves:
//
//	front  (macOS host, LaunchAgent)  listens on :32400, where the real peer address is still visible, and forwards
//	                                  each connection to the back half with a one-line preface naming that address.
//	check  (inside the Plex container) end-to-end probe used as the Docker healthcheck: back half -> Plex /identity.
//	back   (inside the Plex container) opens the connection to Plex *from that address* (IP_TRANSPARENT + a policy
//	                                  route that brings Plex's replies back to this process; see s6-run).
//
// Preface: "PLEXIP1 <token> <client-ip|local>\n". The token (IPBRIDGE_TOKEN) stops other local processes from
// claiming an arbitrary address. Plex trusts loopback peers without authentication, so the back half never opens a
// loopback-sourced connection: anything it cannot or must not impersonate arrives from the container's own eth0
// address, which is on Plex's own subnet but not trusted: authentication is required.
//
// Failure handling: both halves are supervised (launchd KeepAlive on the host, s6 in the container) and are restarted
// if they exit. Neither keeps state, so a restart of either half, of Plex, of the container or of the engine only drops
// the connections open at that moment; clients reconnect. A panic in one connection is contained to that connection,
// every dial and the preface exchange have timeouts, and dead peers are reaped by TCP keepalive.
package main

import (
	"bufio"
	"crypto/subtle"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

const (
	magic     = "PLEXIP1"
	plexPort  = "32400"
	backPort  = "32390" // container side; published to 127.0.0.1:$PLEX_IPBRIDGE_PORT on the host
	localMark = "local"
)

func main() {
	log.SetFlags(log.LstdFlags)
	mode := ""
	if len(os.Args) > 1 {
		mode = os.Args[1]
	}
	switch mode {
	case "front":
		envFile := ""
		if len(os.Args) > 2 {
			envFile = os.Args[2]
		}
		front(envFile)
	case "back":
		back()
	case "check":
		check()
	default:
		fmt.Fprintln(os.Stderr, "usage: plex-ipbridge front [path/to/.env] | back | check")
		os.Exit(2)
	}
}

// ---------------------------------------------------------------- front (host)

func front(envFile string) {
	env := loadEnv(envFile)
	token := env("IPBRIDGE_TOKEN", "")
	if len(token) < 16 {
		log.Fatal("IPBRIDGE_TOKEN is missing or too short (run ./setup-host.sh)")
	}
	backend := "127.0.0.1:" + env("PLEX_IPBRIDGE_PORT", backPort)
	listen := env("PLEX_IPBRIDGE_LISTEN", ":"+plexPort) // override only for testing; clients expect 32400
	ln := listenRetry(listen)
	log.Printf("front: listening on %s, forwarding to %s", listen, backend)
	acceptLoop(ln, func(c *net.TCPConn) {
		b, err := net.DialTimeout("tcp", backend, 5*time.Second)
		if err != nil {
			logEvery("dial", "backend %s unreachable (container down or restarting?): %v", backend, err)
			return
		}
		defer b.Close()
		b.SetWriteDeadline(time.Now().Add(5 * time.Second))
		if _, err := fmt.Fprintf(b, "%s %s %s\n", magic, token, frontClaim(c.RemoteAddr(), ownAddrs())); err != nil {
			return
		}
		b.SetWriteDeadline(time.Time{})
		pipe(c, b.(*net.TCPConn))
	})
}

// frontClaim is the address the front reports for a peer. Connections from this machine itself (loopback or one of
// its own addresses) are reported as "local" so they stay authenticated, instead of becoming a LAN address that
// Plex's "allowed without auth" list may trust.
func frontClaim(peer net.Addr, own []net.IP) string {
	ta, ok := peer.(*net.TCPAddr)
	if !ok || ta.IP == nil {
		return localMark
	}
	ip := ta.IP
	if v4 := ip.To4(); v4 != nil {
		ip = v4
	}
	if ip.IsLoopback() || ip.IsUnspecified() || containsIP(own, ip) {
		return localMark
	}
	return ip.String()
}

// ---------------------------------------------------------------- back (container): address selection

// sourcesFor returns the source addresses to try, in order, for a claimed client address. The last one is always
// self, the container's own (non-loopback) address, which needs no impersonation. It never returns a loopback address.
func sourcesFor(claim string, self net.IP, own []net.IP) []net.IP {
	ip := net.ParseIP(claim)
	if ip == nil {
		return []net.IP{self}
	}
	if v4 := ip.To4(); v4 != nil {
		ip = v4
	}
	if ip.IsLoopback() || ip.IsUnspecified() || ip.IsMulticast() || ip.IsLinkLocalUnicast() ||
		ip.Equal(net.IPv4bcast) || containsIP(own, ip) {
		return []net.IP{self}
	}
	return []net.IP{ip, self}
}

// selfAddr picks the container's own address for non-impersonated connections: the first non-loopback IPv4 address.
func selfAddr(own []net.IP) net.IP {
	for _, ip := range own {
		if v4 := ip.To4(); v4 != nil && v4.IsGlobalUnicast() {
			return v4
		}
	}
	return nil
}

// parsePreface validates "PLEXIP1 <token> <claim>" and returns the claim.
func parsePreface(line, token string) (string, bool) {
	f := strings.Fields(line)
	if len(f) != 3 || f[0] != magic || subtle.ConstantTimeCompare([]byte(f[1]), []byte(token)) != 1 {
		return "", false
	}
	return f[2], true
}

// ---------------------------------------------------------------- shared helpers

// listenRetry keeps trying instead of exiting, so the bridge comes up by itself once the port is free (for example
// while an older container still publishes 32400, or the previous instance is still shutting down).
func listenRetry(addr string) net.Listener {
	for {
		ln, err := net.Listen("tcp", addr)
		if err == nil {
			return ln
		}
		logEvery("listen", "listen %s: %v; retrying every 2 s", addr, err)
		time.Sleep(2 * time.Second)
	}
}

// acceptLoop never returns. Accept errors (for example a temporary shortage of file descriptors) are retried, and a
// panic while handling one connection ends only that connection.
func acceptLoop(ln net.Listener, handle func(*net.TCPConn)) {
	for {
		c, err := ln.Accept()
		if err != nil {
			logEvery("accept", "accept: %v", err)
			time.Sleep(100 * time.Millisecond)
			continue
		}
		go func(c *net.TCPConn) {
			defer c.Close()
			defer func() {
				if r := recover(); r != nil {
					logEvery("panic", "recovered from panic in connection handler: %v", r)
				}
			}()
			handle(c)
		}(c.(*net.TCPConn))
	}
}

// pipe copies both ways until both directions are finished. A clean end of one direction is passed on as a half-close
// (the other direction may still be delivering a response); an error tears down both sides at once. Dead peers are
// detected by TCP keepalive, which Go enables on every connection, so a pipe cannot hang forever.
func pipe(a, b *net.TCPConn) {
	done := make(chan struct{}, 2)
	cp := func(dst, src *net.TCPConn) {
		defer func() { done <- struct{}{} }()
		if _, err := io.Copy(dst, src); err != nil {
			a.Close()
			b.Close()
			return
		}
		dst.CloseWrite()
	}
	go cp(a, b)
	go cp(b, a)
	<-done
	<-done
}

func containsIP(list []net.IP, ip net.IP) bool {
	for _, o := range list {
		if o.Equal(ip) {
			return true
		}
	}
	return false
}

func ownAddrs() []net.IP {
	var out []net.IP
	addrs, _ := net.InterfaceAddrs()
	for _, a := range addrs {
		if n, ok := a.(*net.IPNet); ok {
			out = append(out, n.IP)
		}
	}
	return out
}

// loadEnv reads KEY=VALUE lines (same rules as runner/envconf.py); real environment variables win.
func loadEnv(path string) func(key, def string) string {
	file := map[string]string{}
	if f, err := os.Open(path); err == nil {
		defer f.Close()
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			line := strings.TrimSpace(sc.Text())
			k, v, ok := strings.Cut(line, "=")
			if !ok || strings.HasPrefix(line, "#") {
				continue
			}
			k, v = strings.TrimSpace(k), strings.TrimSpace(v)
			if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
				v = v[1 : len(v)-1]
			} else if i := strings.Index(v, " #"); i >= 0 {
				v = strings.TrimSpace(v[:i])
			}
			file[k] = v
		}
	}
	return func(key, def string) string {
		if v := os.Getenv(key); v != "" {
			return v
		}
		if v := file[key]; v != "" {
			return v
		}
		return def
	}
}

var (
	logMu   sync.Mutex
	logLast = map[string]time.Time{}
)

// logEvery logs at most one message per key every 5 minutes: a dead backend would otherwise log once per connection
// and the log files are not rotated.
func logEvery(key, format string, args ...any) {
	logMu.Lock()
	defer logMu.Unlock()
	if time.Since(logLast[key]) < 5*time.Minute {
		return
	}
	logLast[key] = time.Now()
	log.Printf(format, args...)
}
