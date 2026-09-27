// Reference model plugin (Go): a tiny lexicon-based sentiment classifier served as
// model "sentiment" of provider "acme" (enterprise class).
//
//	go build -o sentiment.exe .
//	then register plugins/reference/go-model/plugin.yaml and activate it.
package main

import (
	"context"
	"log"
	"regexp"
	"strings"

	uarplugin "github.com/washim1982/universal-ai-runtime-v1/plugin-sdks/go"
)

var (
	positive = map[string]bool{"love": true, "great": true, "good": true, "excellent": true, "happy": true, "fast": true}
	negative = map[string]bool{"hate": true, "bad": true, "terrible": true, "slow": true, "broken": true, "awful": true}
	word     = regexp.MustCompile(`[a-z']+`)
)

func classify(text string) string {
	score := 0
	for _, w := range word.FindAllString(strings.ToLower(text), -1) {
		if positive[w] {
			score++
		}
		if negative[w] {
			score--
		}
	}
	switch {
	case score > 0:
		return "positive"
	case score < 0:
		return "negative"
	}
	return "neutral"
}

func main() {
	p := &uarplugin.Plugin{
		ID: "acme.sentiment", Version: "1.0.0", Kind: "model", Models: []string{"sentiment"},
		Model: func(ctx context.Context, call uarplugin.ModelCall) (uarplugin.Reply, error) {
			last := ""
			for i := len(call.Messages) - 1; i >= 0; i-- {
				if call.Messages[i]["role"] == "user" {
					last, _ = call.Messages[i]["content"].(string)
					break
				}
			}
			label := classify(last)
			return uarplugin.Reply{Text: label, InputTokens: int32(len(strings.Fields(last))), OutputTokens: 1}, nil
		},
	}
	log.Fatal(uarplugin.Serve(p))
}
