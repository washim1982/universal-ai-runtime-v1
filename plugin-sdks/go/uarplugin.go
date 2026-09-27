// Package uarplugin writes UAR plugins in Go.
//
//	p := &uarplugin.Plugin{ID: "acme.sentiment", Version: "1.0.0", Kind: "model", Models: []string{"sentiment"},
//		Model: func(ctx context.Context, call uarplugin.ModelCall) (uarplugin.Reply, error) { ... }}
//	log.Fatal(uarplugin.Serve(p))
//
// The runtime starts the plugin with UAR_PLUGIN_ADDR set and speaks uar.plugin.v1 over gRPC.
package uarplugin

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/structpb"

	pb "github.com/washim1982/universal-ai-runtime-v1/plugin-sdks/go/pluginv1"
)

// ModelCall is a model request with messages as plain maps ({role, content, ...}).
type ModelCall struct {
	Model    string
	Messages []map[string]any
	Params   map[string]any
	Tools    []map[string]any
}

// Reply is what a handler returns. Tokens, if set, are streamed before the final reply.
type Reply struct {
	Text         string
	Tokens       []string
	Output       map[string]any
	FinishReason string
	InputTokens  int32
	OutputTokens int32
}

// Tool describes one tool of a tool plugin.
type Tool struct {
	Name        string
	Description string
	InputSchema map[string]any
	SideEffect  string // advisory; the administrator's manifest decides
	Run         func(ctx context.Context, args map[string]any) (Reply, error)
}

// Error is returned by handlers for a domain error the caller should see.
type Error struct{ Code, Message string }

func (e *Error) Error() string { return e.Code + ": " + e.Message }

// Plugin holds the descriptor and handlers.
type Plugin struct {
	ID, Version, Kind string // Kind: model | tool | agent
	Models            []string
	Capabilities      []string
	Tools             []Tool
	Init              func(config map[string]any, secrets map[string]string) error
	Model             func(ctx context.Context, call ModelCall) (Reply, error)
	Agent             func(ctx context.Context, agent string, input map[string]any) (Reply, error)
}

type server struct {
	pb.UnimplementedPluginServer
	p    *Plugin
	stop chan struct{}
}

func toMap(s *structpb.Struct) map[string]any {
	if s == nil {
		return map[string]any{}
	}
	return s.AsMap()
}

func toStruct(m map[string]any) *structpb.Struct {
	if m == nil {
		return nil
	}
	s, err := structpb.NewStruct(m)
	if err != nil {
		return nil
	}
	return s
}

func (s *server) Describe(ctx context.Context, _ *pb.DescribeRequest) (*pb.PluginDescriptor, error) {
	d := &pb.PluginDescriptor{Id: s.p.ID, Version: s.p.Version, Kind: s.p.Kind, Api: "uar.plugin.v1",
		Capabilities: s.p.Capabilities, Models: s.p.Models}
	for _, t := range s.p.Tools {
		d.Tools = append(d.Tools, &pb.ToolDescriptor{Name: t.Name, Description: t.Description,
			InputSchema: toStruct(t.InputSchema), SideEffect: t.SideEffect})
	}
	return d, nil
}

func (s *server) Init(ctx context.Context, r *pb.PluginInitRequest) (*pb.PluginInitResponse, error) {
	if s.p.Init != nil {
		if err := s.p.Init(toMap(r.Config), r.Secrets); err != nil {
			return &pb.PluginInitResponse{Ok: false, Message: err.Error()}, nil
		}
	}
	return &pb.PluginInitResponse{Ok: true}, nil
}

func (s *server) dispatch(ctx context.Context, r *pb.PluginExecuteRequest) (Reply, error) {
	switch c := r.Call.(type) {
	case *pb.PluginExecuteRequest_Model:
		if s.p.Model == nil {
			return Reply{}, &Error{"unimplemented", "this plugin does not serve models"}
		}
		call := ModelCall{Model: c.Model.Model, Params: toMap(c.Model.Params)}
		for _, m := range c.Model.Messages {
			call.Messages = append(call.Messages, toMap(m))
		}
		for _, t := range c.Model.Tools {
			call.Tools = append(call.Tools, toMap(t))
		}
		return s.p.Model(ctx, call)
	case *pb.PluginExecuteRequest_Tool:
		for _, t := range s.p.Tools {
			if t.Name == c.Tool.Tool {
				return t.Run(ctx, toMap(c.Tool.Args))
			}
		}
		return Reply{}, &Error{"not_found", "unknown tool " + c.Tool.Tool}
	case *pb.PluginExecuteRequest_Agent:
		if s.p.Agent == nil {
			return Reply{}, &Error{"unimplemented", "this plugin does not run agents"}
		}
		return s.p.Agent(ctx, c.Agent.Agent, toMap(c.Agent.Input))
	}
	return Reply{}, &Error{"invalid_argument", "empty request"}
}

func final(r Reply, err error) *pb.PluginExecuteResponse {
	if err != nil {
		var e *Error
		if errors.As(err, &e) {
			return &pb.PluginExecuteResponse{IsError: true, ErrorCode: e.Code, ErrorMessage: e.Message}
		}
		return &pb.PluginExecuteResponse{IsError: true, ErrorCode: "internal", ErrorMessage: err.Error()}
	}
	text := r.Text
	if text == "" && len(r.Tokens) > 0 {
		text = strings.Join(r.Tokens, "")
	}
	finish := r.FinishReason
	if finish == "" {
		finish = "stop"
	}
	return &pb.PluginExecuteResponse{Text: text, Output: toStruct(r.Output), FinishReason: finish,
		InputTokens: r.InputTokens, OutputTokens: r.OutputTokens}
}

func (s *server) Execute(ctx context.Context, r *pb.PluginExecuteRequest) (*pb.PluginExecuteResponse, error) {
	return final(s.dispatch(ctx, r)), nil
}

func (s *server) ExecuteStream(r *pb.PluginExecuteRequest, stream pb.Plugin_ExecuteStreamServer) error {
	reply, err := s.dispatch(stream.Context(), r)
	if err == nil {
		tokens := reply.Tokens
		if len(tokens) == 0 && reply.Text != "" {
			tokens = []string{reply.Text}
		}
		for _, t := range tokens {
			if e := stream.Send(&pb.PluginEvent{Body: &pb.PluginEvent_Token{Token: t}}); e != nil {
				return e
			}
		}
	}
	return stream.Send(&pb.PluginEvent{Body: &pb.PluginEvent_Final{Final: final(reply, err)}})
}

func (s *server) Health(context.Context, *pb.HealthRequest) (*pb.HealthResponse, error) {
	return &pb.HealthResponse{Serving: true, Message: "ok"}, nil
}

func (s *server) Shutdown(context.Context, *pb.ShutdownRequest) (*pb.ShutdownResponse, error) {
	go func() { s.stop <- struct{}{} }()
	return &pb.ShutdownResponse{}, nil
}

// Serve listens on UAR_PLUGIN_ADDR (default 127.0.0.1:50061) until Shutdown or a signal.
func Serve(p *Plugin) error {
	addr := os.Getenv("UAR_PLUGIN_ADDR")
	if addr == "" {
		addr = "127.0.0.1:50061"
	}
	lis, err := net.Listen("tcp", addr)
	if err != nil {
		return fmt.Errorf("listen %s: %w", addr, err)
	}
	g := grpc.NewServer()
	srv := &server{p: p, stop: make(chan struct{}, 1)}
	pb.RegisterPluginServer(g, srv)
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt, syscall.SIGTERM)
	go func() {
		select {
		case <-srv.stop:
		case <-sig:
		}
		g.GracefulStop()
	}()
	fmt.Fprintf(os.Stderr, "%s@%s listening on %s\n", p.ID, p.Version, addr)
	return g.Serve(lis)
}
