// The UAR Go example: go run ./examples/go (from a module that requires the SDK).
package main

import (
	"context"
	"fmt"
	"log"

	uar "github.com/washim1982/universal-ai-runtime-v1/sdks/go"
)

func main() {
	client := uar.NewClient("http://localhost:9000") // key from UAR_API_KEY
	resp, err := client.Inference(context.Background(), "local:default", "Explain quantum computing", "in_app_assistant")
	if err != nil {
		log.Fatal(err)
	}
	fmt.Println(resp.GetContent())
}
