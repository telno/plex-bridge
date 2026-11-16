package main

import (
	"net"
	"testing"
)

func TestSourcesFor(t *testing.T) {
	self := net.ParseIP("192.168.148.2").To4()
	own := []net.IP{net.ParseIP("192.168.148.2"), net.ParseIP("127.0.0.1")}
	cases := []struct {
		claim string
		want  []string
	}{
		{"192.168.0.55", []string{"192.168.0.55", "192.168.148.2"}},
		{"203.0.113.7", []string{"203.0.113.7", "192.168.148.2"}},
		{"::ffff:192.168.0.55", []string{"192.168.0.55", "192.168.148.2"}},
		{"2001:db8::5", []string{"2001:db8::5", "192.168.148.2"}},
		// never impersonated: these must reach Plex from the container's own address only
		{"127.0.0.1", []string{"192.168.148.2"}},
		{"127.9.9.9", []string{"192.168.148.2"}},
		{"::1", []string{"192.168.148.2"}},
		{"::ffff:127.0.0.1", []string{"192.168.148.2"}},
		{"0.0.0.0", []string{"192.168.148.2"}},
		{"192.168.148.2", []string{"192.168.148.2"}},
		{"192.168.148.1", []string{"192.168.148.1", "192.168.148.2"}},
		{"fe80::1", []string{"192.168.148.2"}},
		{"224.0.0.1", []string{"192.168.148.2"}},
		{"255.255.255.255", []string{"192.168.148.2"}},
		{"local", []string{"192.168.148.2"}},
		{"", []string{"192.168.148.2"}},
	}
	for _, c := range cases {
		got := sourcesFor(c.claim, self, own)
		if len(got) != len(c.want) {
			t.Errorf("%q: got %v, want %v", c.claim, got, c.want)
			continue
		}
		for i := range got {
			if got[i].String() != c.want[i] {
				t.Errorf("%q: got %v, want %v", c.claim, got, c.want)
			}
			if got[i].IsLoopback() {
				t.Errorf("%q: loopback source %v", c.claim, got[i])
			}
		}
	}
}

func TestSelfAddr(t *testing.T) {
	if got := selfAddr([]net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1"), net.ParseIP("192.168.148.2")}); got.String() != "192.168.148.2" {
		t.Errorf("got %v", got)
	}
	if got := selfAddr([]net.IP{net.ParseIP("127.0.0.1"), net.ParseIP("::1")}); got != nil {
		t.Errorf("loopback-only container must have no self address, got %v", got)
	}
}

func TestFrontClaim(t *testing.T) {
	own := []net.IP{net.ParseIP("192.168.1.10")}
	cases := map[string]string{
		"192.168.0.55":        "192.168.0.55",
		"::ffff:192.168.0.55": "192.168.0.55",
		"203.0.113.7":         "203.0.113.7",
		"127.0.0.1":           localMark,
		"::1":                 localMark,
		"192.168.1.10":        localMark,
	}
	for in, want := range cases {
		if got := frontClaim(&net.TCPAddr{IP: net.ParseIP(in), Port: 1234}, own); got != want {
			t.Errorf("%s: got %s, want %s", in, got, want)
		}
	}
}

func TestParsePreface(t *testing.T) {
	tok := "0123456789abcdef0123"
	if c, ok := parsePreface("PLEXIP1 "+tok+" 192.168.0.55", tok); !ok || c != "192.168.0.55" {
		t.Errorf("valid preface rejected: %q %v", c, ok)
	}
	for _, bad := range []string{"", "PLEXIP1 wrong 192.168.0.55", "PLEXIP1 " + tok, "GET / HTTP/1.1", "PLEXIP2 " + tok + " 1.2.3.4", "PLEXIP1 " + tok + " 1.2.3.4 extra"} {
		if _, ok := parsePreface(bad, tok); ok {
			t.Errorf("accepted bad preface %q", bad)
		}
	}
}
