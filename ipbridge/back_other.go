//go:build !linux

package main

import "log"

func back()  { log.Fatal("back mode runs only inside the (Linux) Plex container") }
func check() { log.Fatal("check mode runs only inside the (Linux) Plex container") }
