package uar

// Conformance: the shared fixtures in contracts/fixtures through this SDK, against uar-mock (default)
// or a live runtime (UAR_LIVE_URL + UAR_LIVE_KEY; the runtime must use the test configuration).

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/washim1982/universal-ai-runtime-v1/sdks/go/uarv1"
)

var (
	root     = filepath.Join("..", "..")
	target   string
	key      string
	fixtures = map[string]map[string]any{}
)

func TestMain(m *testing.M) {
	files, _ := filepath.Glob(filepath.Join(root, "contracts", "fixtures", "*.json"))
	for _, f := range files {
		raw, _ := os.ReadFile(f)
		var fx map[string]any
		if err := json.Unmarshal(raw, &fx); err != nil {
			panic(err)
		}
		fixtures[fx["name"].(string)] = fx
	}
	var mock *exec.Cmd
	if u := os.Getenv("UAR_LIVE_URL"); u != "" {
		target, key = u, os.Getenv("UAR_LIVE_KEY")
	} else {
		l, _ := net.Listen("tcp", "127.0.0.1:0")
		port := l.Addr().(*net.TCPAddr).Port
		l.Close()
		py := os.Getenv("UAR_PYTHON")
		if py == "" {
			py = filepath.Join(root, ".venv", "bin", "python")
			if runtime.GOOS == "windows" {
				py = filepath.Join(root, ".venv", "Scripts", "python.exe")
			}
			if _, err := os.Stat(py); err != nil {
				py = "python"
			}
		}
		mock = exec.Command(py, filepath.Join(root, "mock", "uar_mock.py"), "--port", fmt.Sprint(port))
		if err := mock.Start(); err != nil {
			panic(err)
		}
		target, key = fmt.Sprintf("http://127.0.0.1:%d", port), "uar_mock0000_notasecretjustamockkey"
		for i := 0; i < 100; i++ {
			if r, err := http.Get(target + "/"); err == nil {
				r.Body.Close()
				break
			}
			time.Sleep(100 * time.Millisecond)
		}
	}
	code := m.Run()
	if mock != nil {
		mock.Process.Kill()
	}
	os.Exit(code)
}

func client() *Client { c := NewClient(target); c.APIKey = key; return c }

// normalize turns any JSON value into generic form, dropping nulls and ignored dotted paths.
func normalize(v any, ignore []string) any {
	raw, _ := json.Marshal(v)
	var out any
	_ = json.Unmarshal(raw, &out)
	out = dropNulls(out)
	for _, p := range ignore {
		parts := strings.Split(p, ".")
		cur, ok := out.(map[string]any)
		for i := 0; ok && i < len(parts)-1; i++ {
			cur, ok = cur[parts[i]].(map[string]any)
		}
		if ok {
			delete(cur, parts[len(parts)-1])
		}
	}
	return out
}

func dropNulls(v any) any {
	switch t := v.(type) {
	case map[string]any:
		m := map[string]any{}
		for k, x := range t {
			if x != nil {
				m[k] = dropNulls(x)
			}
		}
		return m
	case []any:
		for i := range t {
			t[i] = dropNulls(t[i])
		}
	}
	return v
}

// project keeps, at every level, only the keys the fixture mentions: typed messages always carry
// default values (empty strings, zero counts) that a fixture leaves out.
func project(have, want any) any {
	wm, ok1 := want.(map[string]any)
	hm, ok2 := have.(map[string]any)
	if ok1 && ok2 {
		out := map[string]any{}
		for k, wv := range wm {
			if hv, in := hm[k]; in {
				out[k] = project(hv, wv)
			}
		}
		return out
	}
	wl, ok1 := want.([]any)
	hl, ok2 := have.([]any)
	if ok1 && ok2 && len(wl) == len(hl) {
		out := make([]any, len(hl))
		for i := range hl {
			out[i] = project(hl[i], wl[i])
		}
		return out
	}
	return have
}

func protoJSON(t *testing.T, m proto.Message) any {
	raw, err := protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	var v any
	_ = json.Unmarshal(raw, &v)
	return v
}

func ignoreList(fx map[string]any) []string {
	var out []string
	for _, x := range fx["response"].(map[string]any)["ignore"].([]any) {
		out = append(out, x.(string))
	}
	return out
}

func expectBody(t *testing.T, name string, got proto.Message) {
	fx := fixtures[name]
	ig := ignoreList(fx)
	want := normalize(fx["response"].(map[string]any)["body"], ig)
	have := normalize(protoJSON(t, got), ig)
	have = project(have, want)
	if !reflect.DeepEqual(want, have) {
		w, _ := json.Marshal(want)
		h, _ := json.Marshal(have)
		t.Fatalf("%s mismatch\nwant %s\nhave %s", name, w, h)
	}
}

func apiError(t *testing.T, err error, status int, code string) {
	var e *Error
	if !errors.As(err, &e) || e.Status != status || e.Code != code {
		t.Fatalf("want %d %s, got %v", status, code, err)
	}
}

var tests = map[string]func(t *testing.T){
	"inference_basic": func(t *testing.T) {
		r, err := client().InferenceWith(context.Background(), "local:default", "hello", nil)
		if err != nil {
			t.Fatal(err)
		}
		expectBody(t, "inference_basic", r)
		if r.GetContent() != "echo: hello" || r.GetRoute().GetProvider() != "fake" {
			t.Fatal(r)
		}
	},
	"inference_stream": func(t *testing.T) {
		var got []any
		var text strings.Builder
		err := client().Stream(context.Background(), "local:default", "stream", nil, func(ev Event) error {
			got = append(got, normalize(protoJSON(t, ev.Event), nil))
			if ev.Type == "token" {
				text.WriteString(ev.GetToken().GetText())
			}
			return nil
		})
		if err != nil {
			t.Fatal(err)
		}
		fx := fixtures["inference_stream"]["response"].(map[string]any)
		ig := ignoreList(fixtures["inference_stream"])
		events := fx["events"].([]any)
		if len(got) != len(events) || text.String() != "echo: stream" {
			t.Fatalf("events %d/%d text %q", len(got), len(events), text.String())
		}
		for i := range events {
			want := normalize(events[i], ig)
			have := project(normalize(got[i], ig), want)
			if !reflect.DeepEqual(want, have) {
				t.Fatalf("event %d: want %v have %v", i, want, have)
			}
		}
	},
	"error_unauthenticated": func(t *testing.T) {
		c := client()
		c.APIKey = ""
		_, err := c.Inference(context.Background(), "local:default", "hi", "")
		apiError(t, err, 401, "unauthenticated")
	},
	"tool_execute_read": func(t *testing.T) {
		r, err := client().ExecuteTool(context.Background(), "fs.read_text", map[string]any{"path": "docs/faq.md"}, "")
		if err != nil {
			t.Fatal(err)
		}
		expectBody(t, "tool_execute_read", r)
	},
	"tool_policy_denied": func(t *testing.T) {
		_, err := client().ExecuteTool(context.Background(), "fs.write_text",
			map[string]any{"path": "docs/x.md", "content": "x"}, "")
		apiError(t, err, 403, "policy_denied")
	},
	"run_not_found": func(t *testing.T) {
		_, err := client().GetRun(context.Background(), "run_does_not_exist")
		apiError(t, err, 404, "not_found")
	},
	"run_start": func(t *testing.T) {
		r, err := client().RunAgent(context.Background(), "in_app_assistant",
			map[string]any{"prompt": "What is the return window?"}, "")
		if err != nil {
			t.Fatal(err)
		}
		expectBody(t, "run_start", r)
	},
	"dry_run_static": func(t *testing.T) {
		r, err := client().DryRun(context.Background(), &uarv1.DryRunRequest{
			Target: &uarv1.DryRunRequest_AgentId{AgentId: "in_app_assistant"}, Mode: "static"})
		if err != nil {
			t.Fatal(err)
		}
		expectBody(t, "dry_run_static", r)
	},
}

func TestConformance(t *testing.T) {
	for name := range fixtures {
		fn, ok := tests[name]
		if !ok {
			t.Errorf("no SDK test for fixture %s", name)
			continue
		}
		t.Run(name, fn)
	}
}

func TestNoRetryWithoutIdempotencyKey(t *testing.T) {
	calls := 0
	srv := &http.Server{Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		w.WriteHeader(503)
		_, _ = w.Write([]byte(`{"error":{"code":"unavailable","message":"x","retryable":true}}`))
	})}
	l, _ := net.Listen("tcp", "127.0.0.1:0")
	go srv.Serve(l)
	defer srv.Close()
	c := NewClient("http://" + l.Addr().String())
	c.MaxRetries = 3
	_, err := c.Inference(context.Background(), "m", "p", "")
	apiError(t, err, 503, "unavailable")
	if calls != 1 {
		t.Fatalf("inference retried %d times", calls)
	}
	calls = 0
	_, _ = c.ExecuteTool(context.Background(), "t", map[string]any{}, "k1")
	if calls != 4 {
		t.Fatalf("idempotent call attempts = %d, want 4", calls)
	}
}
