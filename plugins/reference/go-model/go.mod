module github.com/washim1982/universal-ai-runtime-v1/plugins/reference/go-model

go 1.27.0

replace github.com/washim1982/universal-ai-runtime-v1/plugin-sdks/go => ../../../plugin-sdks/go

require github.com/washim1982/universal-ai-runtime-v1/plugin-sdks/go v0.0.0-00010101000000-000000000000

require (
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.40.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260706201446-f0a921348800 // indirect
	google.golang.org/grpc v1.84.0 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)
