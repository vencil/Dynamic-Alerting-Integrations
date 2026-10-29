package main

import "testing"

// prometheus/common moves `bearer_token` / `bearer_token_file` to
// `authorization` and clears them (moves.go). The reading prints where the
// value is — `authorization` with the type the loader gives it and the
// credentials as their value — at the written key's place, and never the
// cleared key.
func TestAlertmanagerBearerTokenIsPrintedWhereTheLoaderPutsIt(t *testing.T) {
	t.Parallel()
	in := "global:\n  http_config: {bearer_token: s3cret}\n" +
		"route: {receiver: r}\n" +
		"receivers:\n- name: r\n  webhook_configs:\n" +
		"  - url: 'https://example.com/'\n    http_config: {bearer_token_file: /etc/am/token}\n"
	want := `{"global":{"http_config":{"authorization":{"type":"Bearer","credentials":"s3cret"}}},` +
		`"route":{"receiver":"r"},"receivers":[{"name":"r","webhook_configs":[{"url":"https://example.com/",` +
		`"http_config":{"authorization":{"type":"Bearer","credentials_file":"/etc/am/token"}}}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
	// Written as authorization, nothing moves: the keys are printed as written.
	in = "route: {receiver: r}\nreceivers:\n- name: r\n  webhook_configs:\n" +
		"  - url: 'https://example.com/'\n    http_config: {authorization: {credentials: c}}\n"
	want = `{"route":{"receiver":"r"},"receivers":[{"name":"r","webhook_configs":[{"url":"https://example.com/",` +
		`"http_config":{"authorization":{"credentials":"c"}}}]}]}`
	if got := document(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}

func TestPrometheusBearerTokenIsPrintedWhereTheLoaderPutsIt(t *testing.T) {
	t.Parallel()
	in := "scrape_configs:\n- job_name: a\n  bearer_token: s3cret\n  static_configs: [{targets: [h]}]\n" +
		"remote_write:\n- url: http://rw/w\n  bearer_token_file: /etc/p/token\n"
	want := `{"scrape_configs":[{"job_name":"a","authorization":{"type":"Bearer","credentials":"s3cret"},` +
		`"static_configs":[{"targets":["h"]}]}],"remote_write":[{"url":"http://rw/w",` +
		`"authorization":{"type":"Bearer","credentials_file":"/etc/p/token"}}]}`
	if got := promDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
	// The blind review's shapes: a source written empty next to a moved one
	// is printed as written, and `authorization` once. Also inside a service
	// discovery that inlines HTTPClientConfig.
	in = "scrape_configs:\n- job_name: a\n  bearer_token: x\n  bearer_token_file: ''\n" +
		"  digitalocean_sd_configs:\n  - {bearer_token: '', bearer_token_file: /f}\n"
	want = `{"scrape_configs":[{"job_name":"a","authorization":{"type":"Bearer","credentials":"x"},` +
		`"bearer_token_file":"","digitalocean_sd_configs":[{"authorization":{"type":"Bearer",` +
		`"credentials_file":"/f"},"bearer_token":""}]}]}`
	if got := promDocument(t, in); got != want {
		t.Fatalf("document %s\nwant     %s", got, want)
	}
}
