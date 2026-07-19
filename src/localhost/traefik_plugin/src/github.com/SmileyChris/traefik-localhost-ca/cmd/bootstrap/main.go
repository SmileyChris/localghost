package main

import (
	"flag"
	"fmt"
	"os"

	localhostca "github.com/SmileyChris/traefik-localhost-ca"
)

func main() {
	rootPath := flag.String("root-path", "/var/lib/localhost-root", "root-only volume path")
	signerPath := flag.String("signer-path", "/var/lib/localhost-ca", "online signer volume path")
	printRoot := flag.Bool("print-root", false, "write only the public root certificate to stdout")
	flag.Parse()

	ca, err := localhostca.BootstrapCA(*rootPath, *signerPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "bootstrap failed: %v\n", err)
		os.Exit(1)
	}
	if *printRoot {
		if _, err := os.Stdout.Write(ca.PublicCertificatePEM()); err != nil {
			fmt.Fprintf(os.Stderr, "writing public root: %v\n", err)
			os.Exit(1)
		}
		return
	}
	fmt.Fprintf(os.Stderr, "bootstrap complete (root %s, intermediate %s)\n", ca.Fingerprint(), ca.IntermediateFingerprint())
}
