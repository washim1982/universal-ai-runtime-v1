module uar-sample

go 1.27.0

require github.com/washim1982/universal-ai-runtime-v1/sdks/go v0.0.0

require (
	golang.org/x/net v0.57.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.40.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260706201446-f0a921348800 // indirect
	google.golang.org/grpc v1.84.0 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
)

// Use the SDK from this repository.
replace github.com/washim1982/universal-ai-runtime-v1/sdks/go => ../../sdks/go
