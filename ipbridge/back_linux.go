package main

import (
	"bufio"
	"fmt"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"syscall"
	"time"
)

const ipv6Transparent = 75 // IPV6_TRANSPARENT

func back() {
	token := os.Getenv("IPBRIDGE_TOKEN")
	if len(token) < 16 {
		log.Fatal("IPBRIDGE_TOKEN is missing or too short")
	}
	ln := listenRetry(":" + backPort)
	log.Printf("back: listening on :%s, local/unknown clients appear as %v", backPort, selfAddr(ownAddrs()))
	acceptLoop(ln, func(c *net.TCPConn) { serve(c, token) })
}

// check is the container healthcheck: a real request through the back half to Plex, sourced from the gateway address
// so that it exercises the impersonation path (policy route included). Exit 0 only if Plex answers.
func check() {
	claim := localMark
	if gw := defaultGateway(); gw != nil {
		claim = gw.String()
	}
	c, err := net.DialTimeout("tcp", "127.0.0.1:"+backPort, 3*time.Second)
	if err != nil {
		fmt.Println("ipbridge back half not reachable:", err)
		os.Exit(1)
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(8 * time.Second))
	fmt.Fprintf(c, "%s %s %s\nGET /identity HTTP/1.0\r\n\r\n", magic, os.Getenv("IPBRIDGE_TOKEN"), claim)
	status, err := bufio.NewReader(c).ReadString('\n')
	if err != nil || !strings.Contains(status, " 200") {
		fmt.Printf("no answer from Plex through ipbridge: %q %v\n", strings.TrimSpace(status), err)
		os.Exit(1)
	}
}

func serve(c *net.TCPConn, token string) {
	// Read the preface one byte at a time so nothing of the client's own stream is consumed.
	c.SetReadDeadline(time.Now().Add(5 * time.Second))
	var line []byte
	one := make([]byte, 1)
	for len(line) < 256 {
		if _, err := c.Read(one); err != nil {
			return
		}
		if one[0] == '\n' {
			break
		}
		line = append(line, one[0])
	}
	claim, ok := parsePreface(string(line), token)
	if !ok {
		logEvery("preface", "rejected connection from %s: bad preface or token", c.RemoteAddr())
		return
	}
	c.SetReadDeadline(time.Time{})
	own := ownAddrs()
	self := selfAddr(own)
	if self == nil { // no usable address (network being re-attached?): refuse rather than risk a loopback source
		logEvery("self", "no non-loopback IPv4 address in the container; dropping connection")
		return
	}
	for _, src := range sourcesFor(claim, self, own) {
		p, err := dialAs(src, self)
		if err != nil {
			logEvery("dial "+src.String(), "connect to Plex as %s: %v", src, err)
			continue
		}
		defer p.Close()
		pipe(c, p)
		return
	}
	// No plain (unspoofed) fallback on purpose: it would reach Plex from 127.0.0.1, which Plex trusts without a token.
}

// dialAs connects to Plex with src as the source address: on loopback with an impersonated source, or, for the
// container's own address (self), an ordinary connection from and to that address.
func dialAs(src, self net.IP) (*net.TCPConn, error) {
	if src.IsLoopback() {
		return nil, fmt.Errorf("refusing loopback source %s", src)
	}
	if src.Equal(self) {
		d := net.Dialer{Timeout: 5 * time.Second, LocalAddr: &net.TCPAddr{IP: self}}
		c, err := d.Dial("tcp4", net.JoinHostPort(self.String(), plexPort))
		if err != nil {
			return nil, err
		}
		return c.(*net.TCPConn), nil
	}
	network, target, level, opt := "tcp4", "127.0.0.1:"+plexPort, syscall.SOL_IP, syscall.IP_TRANSPARENT
	if src.To4() == nil {
		network, target, level, opt = "tcp6", "[::1]:"+plexPort, syscall.SOL_IPV6, ipv6Transparent
	}
	d := net.Dialer{
		Timeout:   5 * time.Second,
		LocalAddr: &net.TCPAddr{IP: src},
		Control: func(_, _ string, rc syscall.RawConn) error {
			var serr error
			if err := rc.Control(func(fd uintptr) { serr = syscall.SetsockoptInt(int(fd), level, opt, 1) }); err != nil {
				return err
			}
			return serr
		},
	}
	c, err := d.Dial(network, target)
	if err != nil {
		return nil, err
	}
	return c.(*net.TCPConn), nil
}

// defaultGateway reads the IPv4 default route from /proc/net/route (gateway is little-endian hex).
func defaultGateway() net.IP {
	f, err := os.Open("/proc/net/route")
	if err != nil {
		return nil
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		p := strings.Fields(sc.Text())
		if len(p) < 3 || p[1] != "00000000" {
			continue
		}
		v, err := strconv.ParseUint(p[2], 16, 32)
		if err != nil || v == 0 {
			continue
		}
		return net.IPv4(byte(v), byte(v>>8), byte(v>>16), byte(v>>24)).To4()
	}
	return nil
}
