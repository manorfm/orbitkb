package main

import "net/http"

func main() {
	http.HandleFunc("/catalog", catalog)
	http.HandleFunc("/health", health)
	_ = http.ListenAndServe(":8080", nil)
}

func catalog(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("catalog"))
}

func health(w http.ResponseWriter, _ *http.Request) {
	_, _ = w.Write([]byte("ok"))
}
