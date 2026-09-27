// Package uar is the Go client for the Universal AI Runtime (HTTP/JSON + SSE).
//
//	client := uar.NewClient("http://localhost:9000")                  // key from UAR_API_KEY
//	resp, err := client.Inference(ctx, "local:default", "Explain quantum computing", "research_agent")
//	fmt.Println(resp.GetContent())
//
// Responses are the generated contract types (package uarv1). Only GET requests and requests with an
// idempotency key are retried (429, 503, network errors); inference and tool calls never are.
package uar

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/structpb"

	"github.com/washim1982/universal-ai-runtime-v1/sdks/go/uarv1"
)

const Version = "0.8.0"

// Error is a structured runtime error ({code, message, request_id, retryable, details}).
type Error struct {
	Status    int
	Code      string
	Message   string
	RequestID string
	Retryable bool
	Details   map[string]any
}

func (e *Error) Error() string {
	return fmt.Sprintf("%s: %s (status %d, request %s)", e.Code, e.Message, e.Status, e.RequestID)
}

// Status-class helpers: errors.As(err, &e) then e.IsNotFound() etc.
func (e *Error) IsAuthentication() bool   { return e.Status == 401 }
func (e *Error) IsPermissionDenied() bool { return e.Status == 403 }
func (e *Error) IsNotFound() bool         { return e.Status == 404 }
func (e *Error) IsRateLimited() bool      { return e.Status == 429 }

// Client talks to one runtime.
type Client struct {
	BaseURL    string
	APIKey     string
	Token      string
	MaxRetries int
	HTTP       *http.Client
}

// NewClient uses UAR_API_KEY from the environment unless APIKey is set afterwards.
func NewClient(baseURL string) *Client {
	return &Client{BaseURL: strings.TrimRight(baseURL, "/"), APIKey: os.Getenv("UAR_API_KEY"), MaxRetries: 2,
		HTTP: &http.Client{Timeout: 0}}
}

var marshal = protojson.MarshalOptions{UseProtoNames: true}
var unmarshal = protojson.UnmarshalOptions{DiscardUnknown: true}

func (c *Client) headers(req *http.Request, idem string, accept string) {
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", accept)
	req.Header.Set("User-Agent", "uar-go/"+Version)
	if c.APIKey != "" {
		req.Header.Set("X-API-Key", c.APIKey)
	} else if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	if idem != "" {
		req.Header.Set("Idempotency-Key", idem)
	}
}

func decodeError(status int, body []byte) error {
	var env struct {
		Error struct {
			Code      string         `json:"code"`
			Message   string         `json:"message"`
			RequestID string         `json:"request_id"`
			Retryable bool           `json:"retryable"`
			Details   map[string]any `json:"details"`
		} `json:"error"`
	}
	if json.Unmarshal(body, &env) != nil || env.Error.Code == "" {
		return &Error{Status: status, Code: "http_error", Message: strings.TrimSpace(string(body))}
	}
	e := env.Error
	return &Error{Status: status, Code: e.Code, Message: e.Message, RequestID: e.RequestID, Retryable: e.Retryable,
		Details: e.Details}
}

// do sends body (a proto message, a map, or nil) and decodes into out (a proto message or nil).
func (c *Client) do(ctx context.Context, method, path string, body any, idem string, out proto.Message) error {
	var payload []byte
	switch b := body.(type) {
	case nil:
	case proto.Message:
		var err error
		if payload, err = marshal.Marshal(b); err != nil {
			return err
		}
	default:
		var err error
		if payload, err = json.Marshal(b); err != nil {
			return err
		}
	}
	retryable := method == http.MethodGet || idem != ""
	for attempt := 0; ; attempt++ {
		req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bytes.NewReader(payload))
		if err != nil {
			return err
		}
		c.headers(req, idem, "application/json")
		resp, err := c.HTTP.Do(req)
		var status int
		var data []byte
		if err == nil {
			status = resp.StatusCode
			data, err = io.ReadAll(resp.Body)
			resp.Body.Close()
		}
		if err == nil && status < 400 {
			if out == nil {
				return nil
			}
			return unmarshal.Unmarshal(data, out)
		}
		again := retryable && attempt < c.MaxRetries && (err != nil || status == 429 || status == 503)
		if !again {
			if err != nil {
				return err
			}
			return decodeError(status, data)
		}
		wait := time.Duration(250*(1<<attempt)) * time.Millisecond
		if resp != nil {
			if s, e := strconv.Atoi(resp.Header.Get("Retry-After")); e == nil && s > 0 {
				wait = time.Duration(min(s, 30)) * time.Second
			}
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(time.Duration(float64(wait) * (0.5 + rand.Float64()))):
		}
	}
}

// ---------------------------------------------------------------------------- inference

// InferenceOptions are optional inference settings.
type InferenceOptions struct {
	Messages       []*uarv1.ChatMessage
	Agent          string
	Tools          []string
	ToolMode       string
	Temperature    *float64
	MaxTokens      *int32
	ResponseSchema map[string]any
	DataClass      string
	Extensions     map[string]any
}

func buildInference(model, prompt string, o *InferenceOptions, stream bool) (*uarv1.InferenceRequest, error) {
	r := &uarv1.InferenceRequest{Model: model, Input: prompt, Stream: stream}
	if o != nil {
		r.Messages, r.Agent, r.Tools, r.ToolMode, r.DataClass = o.Messages, o.Agent, o.Tools, o.ToolMode, o.DataClass
		if len(o.Messages) > 0 {
			r.Input = ""
		}
		if o.Temperature != nil || o.MaxTokens != nil || o.ResponseSchema != nil {
			r.Params = &uarv1.GenerationParams{Temperature: o.Temperature, MaxTokens: o.MaxTokens}
			if o.ResponseSchema != nil {
				s, err := structpb.NewStruct(o.ResponseSchema)
				if err != nil {
					return nil, err
				}
				r.Params.ResponseSchema = s
			}
		}
		if o.Extensions != nil {
			s, err := structpb.NewStruct(o.Extensions)
			if err != nil {
				return nil, err
			}
			r.Extensions = s
		}
	}
	return r, nil
}

// Inference runs a synchronous inference. With a non-empty agent it runs that agent on the prompt.
func (c *Client) Inference(ctx context.Context, model, prompt, agent string) (*uarv1.InferenceResponse, error) {
	return c.InferenceWith(ctx, model, prompt, &InferenceOptions{Agent: agent})
}

// InferenceWith is Inference with all options.
func (c *Client) InferenceWith(ctx context.Context, model, prompt string, o *InferenceOptions) (*uarv1.InferenceResponse, error) {
	req, err := buildInference(model, prompt, o, false)
	if err != nil {
		return nil, err
	}
	out := &uarv1.InferenceResponse{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/inference", req, "", out)
}

// Event is one stream event with its JSON "type" (the populated body: token, completed, ...).
type Event struct {
	Type string
	*uarv1.Event
}

func (c *Client) sse(ctx context.Context, method, path string, body proto.Message, fn func(Event) error) error {
	var payload []byte
	if body != nil {
		var err error
		if payload, err = marshal.Marshal(body); err != nil {
			return err
		}
	}
	req, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, bytes.NewReader(payload))
	if err != nil {
		return err
	}
	c.headers(req, "", "text/event-stream")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close() // closing the stream stops generation on the server
	if resp.StatusCode >= 400 {
		data, _ := io.ReadAll(resp.Body)
		return decodeError(resp.StatusCode, data)
	}
	sc := bufio.NewScanner(resp.Body)
	sc.Buffer(make([]byte, 64*1024), 8*1024*1024)
	var data []string
	emit := func() error {
		if len(data) == 0 {
			return nil
		}
		raw := strings.Join(data, "\n")
		data = nil
		var head struct {
			Type string `json:"type"`
		}
		_ = json.Unmarshal([]byte(raw), &head)
		ev := &uarv1.Event{}
		if err := unmarshal.Unmarshal([]byte(raw), ev); err != nil {
			return err
		}
		return fn(Event{Type: head.Type, Event: ev})
	}
	for sc.Scan() {
		line := sc.Text()
		switch {
		case line == "":
			if err := emit(); err != nil {
				return err
			}
		case strings.HasPrefix(line, "data:"):
			data = append(data, strings.TrimLeft(line[5:], " "))
		}
	}
	if err := sc.Err(); err != nil {
		return err
	}
	return emit()
}

// ErrStop can be returned from a stream callback to stop reading early.
var ErrStop = errors.New("stop")

// Stream runs a streaming inference and calls fn for every event (started, token..., usage, completed|error).
func (c *Client) Stream(ctx context.Context, model, prompt string, o *InferenceOptions, fn func(Event) error) error {
	req, err := buildInference(model, prompt, o, true)
	if err != nil {
		return err
	}
	if err := c.sse(ctx, http.MethodPost, "/api/v1/inference", req, fn); err != nil && !errors.Is(err, ErrStop) {
		return err
	}
	return nil
}

// ---------------------------------------------------------------------------- tools & catalog

// ExecuteTool runs a governed tool. Pass an idempotency key to make retries safe.
func (c *Client) ExecuteTool(ctx context.Context, tool string, args map[string]any, idempotencyKey string) (*uarv1.ToolResult, error) {
	a, err := structpb.NewStruct(args)
	if err != nil {
		return nil, err
	}
	out := &uarv1.ToolResult{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/tool/execute", &uarv1.ToolRequest{Tool: tool, Args: a}, idempotencyKey, out)
}

func (c *Client) ListModels(ctx context.Context) ([]*uarv1.ModelInfo, error) {
	out := &uarv1.ListModelsResponse{}
	return out.GetModels(), c.do(ctx, http.MethodGet, "/api/v1/models", nil, "", out)
}

func (c *Client) ListTools(ctx context.Context) ([]*uarv1.ToolInfo, error) {
	out := &uarv1.ListToolsResponse{}
	return out.GetTools(), c.do(ctx, http.MethodGet, "/api/v1/tools", nil, "", out)
}

// ---------------------------------------------------------------------------- agents & runs

func (c *Client) RegisterAgent(ctx context.Context, definition map[string]any) (*uarv1.AgentVersion, error) {
	out := &uarv1.AgentVersion{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/agents", map[string]any{"definition": definition}, "", out)
}

// RunAgent starts a run (202). Pass an idempotency key to make it safe to retry.
func (c *Client) RunAgent(ctx context.Context, agentID string, input map[string]any, idempotencyKey string) (*uarv1.Run, error) {
	in, err := structpb.NewStruct(input)
	if err != nil {
		return nil, err
	}
	out := &uarv1.Run{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/agent/run", &uarv1.RunRequest{AgentId: agentID, Input: in},
		idempotencyKey, out)
}

func (c *Client) GetRun(ctx context.Context, runID string) (*uarv1.Run, error) {
	out := &uarv1.Run{}
	return out, c.do(ctx, http.MethodGet, "/api/v1/runs/"+url.PathEscape(runID), nil, "", out)
}

// WaitRun polls until the run is terminal or needs attention.
func (c *Client) WaitRun(ctx context.Context, runID string) (*uarv1.Run, error) {
	for {
		r, err := c.GetRun(ctx, runID)
		if err != nil {
			return nil, err
		}
		switch r.GetStatus() {
		case "succeeded", "failed", "cancelled", "needs_attention":
			return r, nil
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(250 * time.Millisecond):
		}
	}
}

// WatchRun streams durable run events after afterSeq until the terminal event.
func (c *Client) WatchRun(ctx context.Context, runID string, afterSeq int, fn func(Event) error) error {
	err := c.sse(ctx, http.MethodGet, fmt.Sprintf("/api/v1/runs/%s/events?after_seq=%d", url.PathEscape(runID), afterSeq), nil, fn)
	if errors.Is(err, ErrStop) {
		return nil
	}
	return err
}

func (c *Client) CancelRun(ctx context.Context, runID, reason string) (*uarv1.Run, error) {
	out := &uarv1.Run{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/runs/"+url.PathEscape(runID)+"/cancel",
		&uarv1.CancelRunRequest{Reason: reason}, "", out)
}

func (c *Client) ResolveRun(ctx context.Context, runID, action, note string) (*uarv1.Run, error) {
	out := &uarv1.Run{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/runs/"+url.PathEscape(runID)+"/resolve",
		&uarv1.ResolveRunRequest{Action: action, Note: note}, "", out)
}

// DryRun previews an agent (mode "static" or "simulate"); nothing is executed.
func (c *Client) DryRun(ctx context.Context, req *uarv1.DryRunRequest) (*uarv1.DryRunReport, error) {
	out := &uarv1.DryRunReport{}
	return out, c.do(ctx, http.MethodPost, "/api/v1/dry-run", req, "", out)
}
