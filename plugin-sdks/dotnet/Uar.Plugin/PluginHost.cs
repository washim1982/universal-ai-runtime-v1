// Write UAR plugins in .NET.
//
//   await PluginHost.ServeAsync(new PluginDefinition
//   {
//       Id = "acme.mathutil", Version = "1.0.0", Kind = "tool",
//       Tools = { new PluginTool("add", "Add numbers", schema, (args, ct) => Task.FromResult(PluginReply.Of(new { sum = 3 }))) },
//   });
//
// The runtime starts the plugin with UAR_PLUGIN_ADDR set and speaks uar.plugin.v1 over gRPC (HTTP/2 cleartext).
using System.Text.Json;
using System.Text.Json.Nodes;
using Google.Protobuf;
using Google.Protobuf.WellKnownTypes;
using Grpc.Core;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Server.Kestrel.Core;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using Uar.Plugin.V1;

namespace Uar.Plugin;

/// <summary>What a handler returns: text and/or structured output, optionally streamed tokens.</summary>
public sealed class PluginReply
{
    public string Text { get; init; } = "";
    public JsonObject? Output { get; init; }
    public IReadOnlyList<string>? Tokens { get; init; }
    public string FinishReason { get; init; } = "stop";
    public int InputTokens { get; init; }
    public int OutputTokens { get; init; }

    public static PluginReply Of(object output) =>
        new() { Output = JsonSerializer.SerializeToNode(output)!.AsObject() };
}

/// <summary>A domain error the caller should see (tool errors, invalid input).</summary>
public sealed class PluginException(string message, string code = "plugin_error") : Exception(message)
{
    public string Code { get; } = code;
}

public sealed record ModelCall(string Model, IReadOnlyList<JsonObject> Messages, JsonObject Params, IReadOnlyList<JsonObject> Tools);

public sealed record PluginTool(string Name, string Description, JsonObject InputSchema,
                                Func<JsonObject, CancellationToken, Task<PluginReply>> Run, string SideEffect = "read");

public sealed class PluginDefinition
{
    public required string Id { get; init; }
    public required string Version { get; init; }
    /// <summary>model | tool | agent</summary>
    public required string Kind { get; init; }
    public List<string> Models { get; init; } = new();
    public List<string> Capabilities { get; init; } = new();
    public List<PluginTool> Tools { get; init; } = new();
    public Func<JsonObject, IReadOnlyDictionary<string, string>, Task>? Init { get; init; }
    public Func<ModelCall, CancellationToken, Task<PluginReply>>? Model { get; init; }
    public Func<string, JsonObject, CancellationToken, Task<PluginReply>>? Agent { get; init; }
}

public static class PluginHost
{
    public static async Task ServeAsync(PluginDefinition def, string[]? args = null)
    {
        var addr = Environment.GetEnvironmentVariable("UAR_PLUGIN_ADDR") ?? "127.0.0.1:50061";
        var (host, port) = (addr[..addr.LastIndexOf(':')], int.Parse(addr[(addr.LastIndexOf(':') + 1)..]));
        var builder = WebApplication.CreateSlimBuilder(args ?? Array.Empty<string>());
        builder.Logging.SetMinimumLevel(LogLevel.Warning);
        builder.WebHost.ConfigureKestrel(k => k.Listen(System.Net.IPAddress.Parse(host == "localhost" ? "127.0.0.1" : host),
                                                        port, o => o.Protocols = HttpProtocols.Http2));
        builder.Services.AddGrpc();
        builder.Services.AddSingleton(def);
        var app = builder.Build();
        app.MapGrpcService<PluginService>();
        Console.Error.WriteLine($"{def.Id}@{def.Version} listening on {addr}");
        await app.RunAsync();
    }

    internal static JsonObject ToJson(Struct? s) =>
        s is null ? new JsonObject() : JsonNode.Parse(JsonFormatter.Default.Format(s))!.AsObject();

    internal static Struct? ToStruct(JsonObject? o) => o is null ? null : JsonParser.Default.Parse<Struct>(o.ToJsonString());
}

internal sealed class PluginService(PluginDefinition def, IHostApplicationLifetime lifetime) : V1.Plugin.PluginBase
{
    public override Task<PluginDescriptor> Describe(DescribeRequest request, ServerCallContext context)
    {
        var d = new PluginDescriptor { Id = def.Id, Version = def.Version, Kind = def.Kind, Api = "uar.plugin.v1" };
        d.Models.AddRange(def.Models);
        d.Capabilities.AddRange(def.Capabilities);
        foreach (var t in def.Tools)
            d.Tools.Add(new ToolDescriptor { Name = t.Name, Description = t.Description, SideEffect = t.SideEffect,
                                             InputSchema = PluginHost.ToStruct(t.InputSchema) });
        return Task.FromResult(d);
    }

    public override async Task<PluginInitResponse> Init(PluginInitRequest request, ServerCallContext context)
    {
        try
        {
            if (def.Init is not null) await def.Init(PluginHost.ToJson(request.Config), request.Secrets);
            return new PluginInitResponse { Ok = true };
        }
        catch (Exception e) { return new PluginInitResponse { Ok = false, Message = e.Message }; }
    }

    private Task<PluginReply> Dispatch(PluginExecuteRequest r, CancellationToken ct) => r.CallCase switch
    {
        PluginExecuteRequest.CallOneofCase.Model when def.Model is not null => def.Model(new ModelCall(
            r.Model.Model, r.Model.Messages.Select(PluginHost.ToJson).ToList(), PluginHost.ToJson(r.Model.Params),
            r.Model.Tools.Select(PluginHost.ToJson).ToList()), ct),
        PluginExecuteRequest.CallOneofCase.Tool => (def.Tools.FirstOrDefault(t => t.Name == r.Tool.Tool)
            ?? throw new PluginException($"unknown tool {r.Tool.Tool}", "not_found")).Run(PluginHost.ToJson(r.Tool.Args), ct),
        PluginExecuteRequest.CallOneofCase.Agent when def.Agent is not null =>
            def.Agent(r.Agent.Agent, PluginHost.ToJson(r.Agent.Input), ct),
        _ => throw new PluginException("this plugin does not handle that call", "unimplemented"),
    };

    private static PluginExecuteResponse Final(PluginReply reply) => new()
    {
        Text = reply.Text.Length > 0 ? reply.Text : string.Concat(reply.Tokens ?? Array.Empty<string>()),
        Output = PluginHost.ToStruct(reply.Output), FinishReason = reply.FinishReason,
        InputTokens = reply.InputTokens, OutputTokens = reply.OutputTokens,
    };

    private static PluginExecuteResponse Error(Exception e) => e is PluginException p
        ? new PluginExecuteResponse { IsError = true, ErrorCode = p.Code, ErrorMessage = p.Message }
        : new PluginExecuteResponse { IsError = true, ErrorCode = "internal", ErrorMessage = $"{e.GetType().Name}: {e.Message}" };

    public override async Task<PluginExecuteResponse> Execute(PluginExecuteRequest request, ServerCallContext context)
    {
        try { return Final(await Dispatch(request, context.CancellationToken)); }
        catch (Exception e) { return Error(e); }
    }

    public override async Task ExecuteStream(PluginExecuteRequest request, IServerStreamWriter<PluginEvent> stream,
                                             ServerCallContext context)
    {
        try
        {
            var reply = await Dispatch(request, context.CancellationToken);
            foreach (var t in reply.Tokens ?? (reply.Text.Length > 0 ? new[] { reply.Text } : Array.Empty<string>()))
                await stream.WriteAsync(new PluginEvent { Token = t });
            await stream.WriteAsync(new PluginEvent { Final = Final(reply) });
        }
        catch (Exception e) { await stream.WriteAsync(new PluginEvent { Final = Error(e) }); }
    }

    public override Task<HealthResponse> Health(HealthRequest request, ServerCallContext context) =>
        Task.FromResult(new HealthResponse { Serving = true, Message = "ok" });

    public override Task<ShutdownResponse> Shutdown(ShutdownRequest request, ServerCallContext context)
    {
        _ = Task.Delay(200).ContinueWith(_ => lifetime.StopApplication());
        return Task.FromResult(new ShutdownResponse());
    }
}
