// UAR inference sample (Go).
//
//	go run . "Explain quantum computing in two sentences"
//
// Sign-in, first match wins:
//
//	UAR_CLIENT_ID + UAR_CLIENT_SECRET   a registered application: exchanged for an access token
//	UAR_API_KEY                         an API key
//
// Optional: UAR_URL (default http://127.0.0.1:9000), UAR_MODEL (default local:default).
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"net/url"
	"os"
	"strings"

	uar "github.com/washim1982/universal-ai-runtime-v1/sdks/go"
)

func env(name, def string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return def
}

// accessToken exchanges an application's client credentials at the token service (OAuth 2.0).
func accessToken(baseURL, clientID, secret string) (string, error) {
	resp, err := http.PostForm(baseURL+"/api/v1/oauth/token", url.Values{
		"grant_type": {"client_credentials"}, "client_id": {clientID}, "client_secret": {secret}})
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	var t struct {
		AccessToken string `json:"access_token"`
		Error       string `json:"error"`
		Description string `json:"error_description"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&t); err != nil || t.AccessToken == "" {
		return "", fmt.Errorf("token request failed (%d): %s %s", resp.StatusCode, t.Error, t.Description)
	}
	return t.AccessToken, nil
}

func main() {
	base, model := env("UAR_URL", "http://127.0.0.1:9000"), env("UAR_MODEL", "local:default")
	prompt := strings.Join(os.Args[1:], " ")
	if prompt == "" {
		prompt = "Explain quantum computing in two sentences."
	}
	ctx := context.Background()
	if os.Getenv("UAR_CLIENT_ID") == "" && os.Getenv("UAR_API_KEY") == "" {
		log.Fatal("No credentials: set UAR_CLIENT_ID and UAR_CLIENT_SECRET (a registered application) or UAR_API_KEY. See samples/README.md.")
	}

	client := uar.NewClient(base) // picks up UAR_API_KEY
	who := "API key"
	if id := os.Getenv("UAR_CLIENT_ID"); id != "" {
		tok, err := accessToken(base, id, os.Getenv("UAR_CLIENT_SECRET"))
		if err != nil {
			log.Fatal(err)
		}
		client.APIKey, client.Token, who = "", tok, "application "+id
	}
	fmt.Printf("UAR %s | model %s | signed in with %s\n\n", base, model, who)

	// 1. One request, one complete answer.
	maxTokens := int32(300)
	resp, err := client.InferenceWith(ctx, model, prompt, &uar.InferenceOptions{MaxTokens: &maxTokens})
	if err != nil {
		log.Fatal(err) // *uar.Error: e.g. 401 wrong credentials, 403 missing permission, 404 unknown model
	}
	fmt.Printf("[%s/%s] %s\n", resp.GetProvider(), resp.GetModel(), resp.GetContent())
	fmt.Printf("tokens: %d in, %d out\n\n", resp.GetUsage().GetInputTokens(), resp.GetUsage().GetOutputTokens())

	// 2. The same question, streamed token by token.
	fmt.Print("streaming: ")
	err = client.Stream(ctx, model, prompt, &uar.InferenceOptions{MaxTokens: &maxTokens}, func(ev uar.Event) error {
		switch ev.Type {
		case "token":
			fmt.Print(ev.GetToken().GetText())
		case "error":
			return fmt.Errorf("stream error: %s", ev.GetError().GetMessage())
		}
		return nil
	})
	fmt.Println()
	if err != nil {
		log.Fatal(err)
	}
}
